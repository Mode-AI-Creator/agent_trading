"""OKX trading execution — place/cancel orders and query account state for perpetual swaps."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
from datetime import datetime, timezone
from typing import Optional

import httpx

from backend.config import get_settings
from backend.utils.logger import get_logger

logger = get_logger("backend.services.okx_trading")

_TIMEOUT = httpx.Timeout(15.0)

# Contract specs: ctVal = USD value of 1 contract at unit price
# BTC-USDT-SWAP: 1 contract = 0.01 BTC
# ETH-USDT-SWAP: 1 contract = 0.1 ETH
_CT_VAL = {
    "BTC-USDT-SWAP": 0.01,
    "ETH-USDT-SWAP": 0.1,
}


class OKXTradingClient:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._base = self._settings.okx_base_url
        self._client = httpx.AsyncClient(timeout=_TIMEOUT)

    # ── Auth helpers ──────────────────────────────────────────────────────────

    def _sign(self, ts: str, method: str, path: str, body: str = "") -> str:
        msg = f"{ts}{method}{path}{body}"
        sig = hmac.new(
            self._settings.okx_secret_key.encode(),
            msg.encode(),
            hashlib.sha256,
        ).digest()
        return base64.b64encode(sig).decode()

    def _headers(self, method: str, path: str, body: str = "") -> dict:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        return {
            "OK-ACCESS-KEY": self._settings.okx_api_key,
            "OK-ACCESS-SIGN": self._sign(ts, method, path, body),
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self._settings.okx_passphrase,
            "Content-Type": "application/json",
        }

    async def _get(self, path: str, params: dict | None = None) -> dict:
        qs = ""
        if params:
            qs = "?" + "&".join(f"{k}={v}" for k, v in params.items())
        full_path = path + qs
        headers = self._headers("GET", full_path)
        r = await self._client.get(self._base + path, params=params, headers=headers)
        r.raise_for_status()
        return r.json()

    async def _post(self, path: str, payload: dict) -> dict:
        body = json.dumps(payload)
        headers = self._headers("POST", path, body)
        r = await self._client.post(self._base + path, content=body, headers=headers)
        r.raise_for_status()
        return r.json()

    def _check(self, data: dict, context: str) -> dict:
        if data.get("code") != "0":
            raise RuntimeError(f"{context}: OKX error {data.get('code')} — {data.get('msg')}")
        return data

    # ── Account ────────────────────────────────────────────────────────────────

    async def get_balance(self) -> float:
        """Return available USDT balance for position sizing."""
        data = self._check(await self._get("/api/v5/account/balance"), "get_balance")
        for detail in data["data"][0].get("details", []):
            if detail.get("ccy") == "USDT":
                return float(detail.get("availBal", 0))
        return 0.0

    async def get_total_equity(self) -> float:
        """Return total account equity in USDT (includes unrealized PnL)."""
        data = self._check(await self._get("/api/v5/account/balance"), "get_total_equity")
        total_eq = data["data"][0].get("totalEq")
        if total_eq:
            return float(total_eq)
        for detail in data["data"][0].get("details", []):
            if detail.get("ccy") == "USDT":
                return float(detail.get("eq", 0))
        return 0.0

    async def get_open_positions(self) -> list[dict]:
        """Return all currently open perpetual swap positions."""
        data = self._check(
            await self._get("/api/v5/account/positions", {"instType": "SWAP"}),
            "get_open_positions",
        )
        def _sf(val, default=0.0):
            try:
                return float(val) if val not in (None, "", "NaN") else default
            except (TypeError, ValueError):
                return default

        positions = []
        for p in data.get("data", []):
            if _sf(p.get("pos", 0)) == 0:
                continue
            pos_val = _sf(p.get("pos", 0))
            positions.append({
                "symbol": p["instId"],
                "direction": "long" if p.get("posSide") in ("long", "net") and pos_val > 0 else "short",
                "contracts": abs(pos_val),
                "entry_price": _sf(p.get("avgPx")),
                "unrealized_pnl": _sf(p.get("upl")),
                "leverage": _sf(p.get("lever"), 1.0),
                "margin": _sf(p.get("margin")),
            })
        return positions

    async def get_instrument_info(self, swap_symbol: str) -> dict:
        """Return instrument details (ctVal, minSz, tickSz, lotSz)."""
        data = self._check(
            await self._get("/api/v5/public/instruments", {"instType": "SWAP", "instId": swap_symbol}),
            "get_instrument_info",
        )
        inst = data["data"][0]
        return {
            "ctVal": float(inst["ctVal"]),      # underlying per contract (e.g. 0.01 BTC)
            "minSz": float(inst["minSz"]),       # minimum order size in contracts
            "lotSz": float(inst["lotSz"]),       # order size increment
            "tickSz": float(inst["tickSz"]),     # price tick size
        }

    # ── Order placement ────────────────────────────────────────────────────────

    def _calc_contracts(
        self,
        swap_symbol: str,
        balance_usdt: float,
        size_pct: float,
        leverage: int,
        entry_price: float,
        ct_val: float,
        lot_sz: float,
    ) -> float:
        """Calculate how many contracts to trade given account parameters."""
        margin = balance_usdt * size_pct / 100.0
        position_value = margin * leverage
        raw_contracts = position_value / (entry_price * ct_val)
        # Round down to nearest lot_sz
        contracts = math.floor(raw_contracts / lot_sz) * lot_sz
        return max(contracts, lot_sz)  # at least 1 lot

    async def place_trade(
        self,
        swap_symbol: str,
        direction: str,
        entry_price: float,
        stop_loss: float,
        take_profit_1: float,
        take_profit_2: Optional[float],
        size_pct: float,
        leverage: int,
    ) -> dict:
        """
        Place a limit entry order with attached SL/TP algo orders.

        Returns dict with:
            order_id, algo_id, contracts, margin_used, status
        """
        # 1. Fetch instrument info
        inst = await self.get_instrument_info(swap_symbol)
        ct_val = inst["ctVal"]
        lot_sz = inst["lotSz"]
        tick_sz = inst["tickSz"]

        # 2. Get available balance
        balance = await self.get_balance()
        if balance < 10:
            raise RuntimeError(f"Insufficient balance: {balance:.2f} USDT")

        # 3. Calculate position size
        contracts = self._calc_contracts(
            swap_symbol, balance, size_pct, leverage, entry_price, ct_val, lot_sz
        )
        margin_used = contracts * ct_val * entry_price / leverage

        # 4. Set leverage first
        await self._post("/api/v5/account/set-leverage", {
            "instId": swap_symbol,
            "lever": str(leverage),
            "mgnMode": "cross",
        })

        # 5. Round prices to tick size
        def round_price(px: float) -> str:
            decimals = len(str(tick_sz).rstrip("0").split(".")[-1]) if "." in str(tick_sz) else 0
            return str(round(px, decimals))

        side = "buy" if direction == "long" else "sell"
        pos_side = direction  # long | short

        # 6. Build attach algo for SL/TP — always use TP1 as the exit target
        tp_price = take_profit_1

        attach_algo = [{
            "tpTriggerPx": round_price(tp_price),
            "tpOrdPx": "-1",       # market order at TP
            "slTriggerPx": round_price(stop_loss),
            "slOrdPx": "-1",       # market order at SL
            "tpTriggerPxType": "last",
            "slTriggerPxType": "last",
        }]

        # 7. Place market order with attached SL/TP
        payload = {
            "instId": swap_symbol,
            "tdMode": "cross",
            "side": side,
            "posSide": pos_side,
            "ordType": "market",
            "sz": "",  # overwritten below with correct lot_sz precision
            "attachAlgoOrds": attach_algo,
        }

        # Format sz with correct decimal precision from lot_sz (e.g. lot_sz=0.01 → 2dp)
        if lot_sz >= 1:
            sz_str = str(int(contracts))
        else:
            n_dec = len(str(lot_sz).rstrip("0").split(".")[-1])
            sz_str = f"{contracts:.{n_dec}f}"
        payload["sz"] = sz_str

        logger.info(
            "Placing %s %s: MARKET sl=%s tp=%s contracts=%s margin=%.2f USDT",
            direction, swap_symbol,
            round_price(stop_loss), round_price(tp_price),
            sz_str, margin_used,
        )

        data = self._check(await self._post("/api/v5/trade/order", payload), "place_trade")
        result = data["data"][0]

        if result.get("sCode") != "0":
            raise RuntimeError(f"Order rejected: {result.get('sMsg')}")

        order_id = result.get("ordId", "")
        algo_id = result.get("attachAlgoOrds", [{}])[0].get("algoId", "") if result.get("attachAlgoOrds") else ""

        return {
            "order_id": order_id,
            "algo_id": algo_id,
            "contracts": contracts,
            "margin_used": round(margin_used, 2),
            "status": "pending_entry",
        }

    async def get_recent_fills(self, swap_symbol: str, limit: int = 20) -> list[dict]:
        """Return recent fills for a symbol (used to determine live close price)."""
        try:
            data = self._check(
                await self._get("/api/v5/trade/fills", {"instId": swap_symbol, "limit": str(limit)}),
                "get_recent_fills",
            )
            return [
                {
                    "side": f.get("side"),        # buy | sell
                    "pos_side": f.get("posSide"), # long | short | net
                    "price": float(f.get("fillPx", 0)),
                    "sz": float(f.get("fillSz", 0)),
                    "ts": int(f.get("ts", 0)),
                }
                for f in data.get("data", [])
            ]
        except Exception as e:
            logger.error("get_recent_fills %s: %s", swap_symbol, e)
            return []

    async def get_order_status(self, swap_symbol: str, order_id: str) -> dict:
        try:
            data = self._check(
                await self._get("/api/v5/trade/order", {"instId": swap_symbol, "ordId": order_id}),
                "get_order_status",
            )
            o = data["data"][0]
            return {
                "order_id": order_id,
                "status": o.get("state"),  # live | partially_filled | filled | canceled
                "filled_sz": float(o.get("fillSz", 0)),
                "avg_fill_px": float(o.get("avgPx", 0)) if o.get("avgPx") else None,
            }
        except Exception as e:
            logger.error("get_order_status: %s", e)
            return {"order_id": order_id, "status": "unknown"}

    async def close_position(self, swap_symbol: str, direction: str) -> bool:
        """Market-close an open position (used for agent early-close on live trades)."""
        try:
            data = await self._post("/api/v5/trade/close-position", {
                "instId": swap_symbol,
                "mgnMode": "cross",
                "posSide": direction,  # "long" | "short"
            })
            ok = data.get("code") == "0"
            if not ok:
                logger.error("close_position %s: OKX error %s — %s",
                             swap_symbol, data.get("code"), data.get("msg"))
            return ok
        except Exception as e:
            logger.error("close_position %s: %s", swap_symbol, e)
            return False

    async def close(self) -> None:
        await self._client.aclose()


# ── Module singleton ──────────────────────────────────────────────────────────

_trading_client: OKXTradingClient | None = None


def get_trading_client() -> OKXTradingClient:
    global _trading_client
    if _trading_client is None:
        _trading_client = OKXTradingClient()
    return _trading_client

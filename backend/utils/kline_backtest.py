"""
K线回测工具模块

使用规则（必须严格遵守）：

【拉取 K 线】
- 端点：OKX /api/v5/market/history-candles
- after 参数语义：返回比指定时间戳"更早"的数据（往旧方向翻页）
- 正确分页方式：从 end_ts 出发，每批取最旧时间戳，用 after=oldest_ts 继续往前
  直到 oldest_ts <= start_ts 为止

【判断入场】
- 做多（long）：第一根 low <= entry_price 的 K 线 → 以 entry_price 成交
- 做空（short）：第一根 high >= entry_price 的 K 线 → 以 entry_price 成交
- 若遍历完所有 K 线都未触及入场价 → outcome="no_entry"

【判断出场】（从入场那根 K 线开始逐根检查）
- 做多：先查止损（low <= sl），再查 TP2（high >= tp2），再查 TP1（high >= tp1）
- 做空：先查止损（high >= sl），再查 TP2（low <= tp2），再查 TP1（low <= tp1）
- 止损优先于止盈，避免在同一根 K 线内高估收益
- 若遍历完仍未触发 → outcome="pending"

【出场价计算】
- 止损/止盈价即信号中设定的目标价（sl / tp1 / tp2），不是 K 线的 close
- 收益率 = (exit_price - entry_price) / entry_price × 100（做多）
           (entry_price - exit_price) / entry_price × 100（做空）
"""

import time
from datetime import datetime, timezone

import pandas as pd
import requests


# ---------------------------------------------------------------------------
# K 线拉取
# ---------------------------------------------------------------------------

def fetch_klines(
    symbol: str,
    start_time: datetime,
    end_time: datetime,
    bar: str = "1m",
    verbose: bool = True,
) -> pd.DataFrame:
    """
    从 OKX 拉取指定时间范围内的 K 线数据。

    返回 DataFrame，列：timestamp(tz-aware UTC)、timestamp_ms、open、high、low、close、volume
    按时间升序排列，已去重。

    参数
    ----
    symbol     : 交易对，如 "BTC-USDT"
    start_time : 起始时间（UTC，带或不带 tzinfo 均可）
    end_time   : 结束时间（UTC）
    bar        : K 线周期，如 "1m" "5m" "1H"
    verbose    : 是否打印分页进度
    """
    if start_time.tzinfo is None:
        start_time = start_time.replace(tzinfo=timezone.utc)
    if end_time.tzinfo is None:
        end_time = end_time.replace(tzinfo=timezone.utc)

    start_ts = int(start_time.timestamp() * 1000)
    end_ts = int(end_time.timestamp() * 1000)

    if verbose:
        print(f"   拉取 {bar} K线: {start_time.strftime('%Y-%m-%d %H:%M')} UTC → {end_time.strftime('%Y-%m-%d %H:%M')} UTC")

    all_data: list[pd.DataFrame] = []
    after_ts = end_ts
    batch_num = 0

    while True:
        batch_num += 1
        df_batch = _fetch_one_batch(symbol, bar, after_ts)

        if df_batch.empty:
            if verbose:
                print(f"   批次 {batch_num}: 无数据，停止")
            break

        df_in_range = df_batch[
            (df_batch["timestamp_ms"] >= start_ts) &
            (df_batch["timestamp_ms"] <= end_ts)
        ]
        if not df_in_range.empty:
            all_data.append(df_in_range)

        oldest_ts = int(df_batch["timestamp_ms"].min())
        newest_ts = int(df_batch["timestamp_ms"].max())

        if verbose and (batch_num <= 3 or batch_num % 20 == 0):
            total = sum(len(d) for d in all_data)
            print(
                f"   批次 {batch_num}: "
                f"[{pd.to_datetime(oldest_ts, unit='ms', utc=True).strftime('%m-%d %H:%M')}"
                f" ~ {pd.to_datetime(newest_ts, unit='ms', utc=True).strftime('%m-%d %H:%M')}]"
                f"  范围内 {len(df_in_range)} 条  累计 {total} 条"
            )

        if oldest_ts <= start_ts:
            break

        after_ts = oldest_ts
        time.sleep(0.05)

    if not all_data:
        return pd.DataFrame()

    df = pd.concat(all_data, ignore_index=True)
    df = df.drop_duplicates(subset=["timestamp_ms"]).sort_values("timestamp").reset_index(drop=True)
    return df


def _fetch_one_batch(symbol: str, bar: str, after_ts: int) -> pd.DataFrame:
    """单次 API 请求，返回最多 300 根 K 线（比 after_ts 更早的数据）。"""
    try:
        resp = requests.get(
            "https://www.okx.com/api/v5/market/history-candles",
            params={"instId": symbol, "bar": bar, "limit": "300", "after": str(after_ts)},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != "0":
            raise ValueError(f"API 错误 {data.get('code')}: {data.get('msg')}")
        candles = data.get("data", [])
        if not candles:
            return pd.DataFrame()

        rows = []
        for c in candles:
            ts_ms = int(c[0])
            rows.append({
                "timestamp": pd.to_datetime(ts_ms, unit="ms", utc=True),
                "timestamp_ms": ts_ms,
                "open":   float(c[1]),
                "high":   float(c[2]),
                "low":    float(c[3]),
                "close":  float(c[4]),
                "volume": float(c[5]),
            })
        df = pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)
        return df
    except Exception as e:
        print(f"   K线请求失败: {e}")
        return pd.DataFrame()


# ---------------------------------------------------------------------------
# 回测核心
# ---------------------------------------------------------------------------

def backtest(
    df: pd.DataFrame,
    direction: str,
    entry_price: float,
    stop_loss: float,
    take_profit_1: float,
    take_profit_2: float | None = None,
    fee_pct: float = 0.12,
) -> dict:
    """
    对已拉取好的 K 线 DataFrame 执行一次信号回测。

    返回 dict，字段：
        outcome       : "no_entry" | "sl_hit" | "tp1_hit" | "tp2_hit" | "pending"
        entry_time    : 实际入场时间（pandas Timestamp, UTC）或 None
        exit_price    : 出场价格 或 None
        exit_time     : 出场时间（pandas Timestamp, UTC）或 None
        pnl_pct       : 扣费后收益率（%），负数为亏损
        pnl_pct_gross : 扣费前毛收益率（%）
        fee_pct_applied: 实际扣除的费率（%）
        duration_hours: 持仓时长（小时），从入场到出场

    参数
    ----
    df            : fetch_klines() 返回的 DataFrame
    direction     : "long" 或 "short"
    entry_price   : 入场价
    stop_loss     : 止损价
    take_profit_1 : 第一止盈价
    take_profit_2 : 第二止盈价（可为 None）
    fee_pct       : 单笔往返手续费+滑点占名义价值的百分比（默认 0.12%）
                    = 入场 Maker 0.02% + 出场 Taker 0.05% + 滑点 0.05%
    """
    if df.empty:
        return _result("no_entry")

    # --- Phase 1: 找入场 ---
    entry_time = None
    entry_pos = None

    for pos, (_, row) in enumerate(df.iterrows()):
        if direction == "long" and row["low"] <= entry_price:
            entry_time, entry_pos = row["timestamp"], pos
            break
        if direction == "short" and row["high"] >= entry_price:
            entry_time, entry_pos = row["timestamp"], pos
            break

    if entry_time is None:
        return _result("no_entry")

    # --- Phase 2: 找出场（从入场那根 K 线开始） ---
    for _, row in df.iloc[entry_pos:].iterrows():
        high, low = row["high"], row["low"]
        candle_time = row["timestamp"]

        if direction == "long":
            if stop_loss and low <= stop_loss:
                return _result("sl_hit", entry_price, entry_time, stop_loss, candle_time, direction, fee_pct)
            if take_profit_2 and high >= take_profit_2:
                return _result("tp2_hit", entry_price, entry_time, take_profit_2, candle_time, direction, fee_pct)
            if take_profit_1 and high >= take_profit_1:
                return _result("tp1_hit", entry_price, entry_time, take_profit_1, candle_time, direction, fee_pct)

        elif direction == "short":
            if stop_loss and high >= stop_loss:
                return _result("sl_hit", entry_price, entry_time, stop_loss, candle_time, direction, fee_pct)
            if take_profit_2 and low <= take_profit_2:
                return _result("tp2_hit", entry_price, entry_time, take_profit_2, candle_time, direction, fee_pct)
            if take_profit_1 and low <= take_profit_1:
                return _result("tp1_hit", entry_price, entry_time, take_profit_1, candle_time, direction, fee_pct)

    return _result("pending", entry_price, entry_time)


def run_signal_backtest(
    symbol: str,
    direction: str,
    entry_price: float,
    stop_loss: float,
    take_profit_1: float,
    take_profit_2: float | None,
    signal_time: datetime,
    end_time: datetime | None = None,
    bar: str = "1m",
    fee_pct: float = 0.12,
    verbose: bool = True,
) -> dict:
    """
    一站式：拉 K 线 + 回测。

    signal_time 作为 K 线拉取起点；end_time 默认为当前 UTC 时间。
    fee_pct: 单笔往返手续费+滑点（默认 0.12%，= Maker 0.02% + Taker 0.05% + 滑点 0.05%）
    """
    if end_time is None:
        end_time = datetime.now(timezone.utc)

    df = fetch_klines(symbol, signal_time, end_time, bar=bar, verbose=verbose)

    if df.empty:
        print("   ❌ 未获取到 K 线数据")
        return _result("no_entry")

    if verbose:
        print(f"   ✓ 共 {len(df)} 根 K 线  [{df['timestamp'].iloc[0].strftime('%m-%d %H:%M')} ~ {df['timestamp'].iloc[-1].strftime('%m-%d %H:%M')} UTC]")

    result = backtest(df, direction, entry_price, stop_loss, take_profit_1, take_profit_2, fee_pct=fee_pct)

    if verbose:
        _print_result(result, entry_price, stop_loss, take_profit_1, take_profit_2)

    return result


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------

def _result(
    outcome: str,
    entry_price: float = 0,
    entry_time=None,
    exit_price: float | None = None,
    exit_time=None,
    direction: str = "long",
    fee_pct: float = 0.12,
) -> dict:
    if exit_price is not None and entry_price and entry_time is not None and exit_time is not None:
        if direction == "long":
            gross_pct = (exit_price - entry_price) / entry_price * 100
        else:
            gross_pct = (entry_price - exit_price) / entry_price * 100
        net_pct = gross_pct - fee_pct   # 扣除往返手续费+滑点
        entry_ts = entry_time
        exit_ts = exit_time
        if hasattr(entry_ts, "to_pydatetime"):
            entry_ts = entry_ts.to_pydatetime()
        if hasattr(exit_ts, "to_pydatetime"):
            exit_ts = exit_ts.to_pydatetime()
        duration = (exit_ts - entry_ts).total_seconds() / 3600
    else:
        gross_pct = 0.0
        net_pct = 0.0
        fee_pct = 0.0
        duration = 0.0

    return {
        "outcome": outcome,
        "entry_time": entry_time,
        "exit_price": exit_price,
        "exit_time": exit_time,
        "pnl_pct": round(net_pct, 4),          # 扣费后净收益率
        "pnl_pct_gross": round(gross_pct, 4),   # 扣费前毛收益率
        "fee_pct_applied": fee_pct,
        "duration_hours": duration,
    }


def _print_result(result: dict, entry_price, sl, tp1, tp2):
    outcome = result["outcome"]

    def cst(t):
        if t is None:
            return "N/A"
        if hasattr(t, "to_pydatetime"):
            t = t.to_pydatetime()
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t.astimezone().strftime("%Y-%m-%d %H:%M")

    if outcome == "no_entry":
        print(f"   ⚠️  价格从未触及入场价 ${entry_price:,.2f}，未入场")
        return

    entry_cst = cst(result["entry_time"])
    print(f"   入场触发: {entry_cst} (CST)  @ ${entry_price:,.2f}")

    if outcome == "pending":
        print(f"   ⏳ 持仓中，止损/止盈尚未触发")
        return

    emoji = "✅" if "tp" in outcome else "❌"
    exit_cst = cst(result["exit_time"])
    print(f"   {emoji} {outcome.upper()}  出场: {exit_cst} (CST)  @ ${result['exit_price']:,.2f}")
    gross = result.get("pnl_pct_gross", result["pnl_pct"])
    fee   = result.get("fee_pct_applied", 0.0)
    print(f"   毛收益: {gross:+.2f}%  手续费+滑点: -{fee:.2f}%  净收益: {result['pnl_pct']:+.2f}%  持仓: {result['duration_hours']:.1f}h")

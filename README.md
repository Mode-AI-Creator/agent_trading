# Crypto Trading Agent · 加密自动交易 Agent

![Python](https://img.shields.io/badge/Python-3.10-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white)
![React](https://img.shields.io/badge/React-19-61DAFB?logo=react&logoColor=black)
![DeepSeek](https://img.shields.io/badge/LLM-DeepSeek-4D6BFE)
![OKX](https://img.shields.io/badge/Exchange-OKX-black)
![License](https://img.shields.io/badge/License-MIT-green)

一个由大模型(DeepSeek)驱动的**加密永续合约自动交易 Agent**:FastAPI 后端 + 定时任务调度 + React 仪表盘。Agent 通过工具调用分析实时行情,自主决定**开仓或观望**,并以**模拟盘**记录或在 **OKX** 真实下单。

An autonomous crypto **perpetual-futures trading agent** powered by an LLM (DeepSeek), with a FastAPI backend, a scheduled job runner, and a React dashboard. The agent analyzes live market data via tool-calling, decides **trade or hold**, and either records a **paper trade** or places a **real order on OKX**.

---

> ## ⚠️ 风险与免责声明 / Disclaimer
>
> **中文** — 本项目为**实验性**软件,仅供研究与学习。加密货币**杠杆永续合约交易风险极高,可能导致全部本金损失**。请在充分理解代码行为前始终使用**模拟模式**(`TRADING_PAPER_MODE=true`)。是否实盘、使用何种杠杆与仓位由你自行决定并自负全部后果。你需自行遵守交易所(OKX)的服务条款及所在地区的法律法规。本项目**不构成任何投资建议**,作者不对任何盈亏或损失承担责任。软件按"原样"提供,不附带任何担保。
>
> **EN** — This is **experimental** software for research and educational use only. Trading **leveraged crypto perpetual futures carries extreme risk and can lead to total loss of capital**. Always run in **paper mode** (`TRADING_PAPER_MODE=true`) until you fully understand the behavior. Going live, and any leverage/sizing choice, is your decision and your sole responsibility. You are responsible for complying with the exchange's (OKX) terms of service and the laws of your jurisdiction. **Nothing here is financial advice**; the authors accept no liability for any gains or losses. The software is provided "AS IS", without warranty of any kind.

---

## 工作原理 / How it works

每 15 分钟调度器运行一次交易 Agent。对每个标的(BTC、ETH),Agent:

1. 接收系统提示词,内含风险规则(最低 1:2 盈亏比、强制止损、杠杆/仓位预算)。
2. 调用**行情数据工具**收集原始数据 —— K 线、行情、资金费率、持仓量、多空比、爆仓、订单簿、布林带、MACD、恐惧贪婪指数、当前持仓。
3. 调用一次 `submit_decision` 给出最终 `trade` / `hold`,含入场价、止损、止盈、仓位 %、杠杆与理由。
4. 后端**校验**决策(止损/止盈方向正确、仓位/杠杆夹取)并施加**风控闸门**(单日亏损上限、最大持仓数)后才执行。
5. **模拟模式**记录交易并模拟结果;**实盘模式**在 OKX 下市价单并附带止损/止盈。

Every 15 minutes the scheduler runs the agent. For each symbol (BTC, ETH) it receives risk rules, calls **market-data tools** (klines, ticker, funding rate, open interest, long/short ratio, liquidations, order book, Bollinger Bands, MACD, Fear & Greed, open positions), then calls `submit_decision` once. The backend **validates** the decision and applies **risk gates** (daily loss limit, max open positions) before acting — recording a paper trade or placing a real OKX market order with attached SL/TP. A separate monitor job resolves open positions (TP/SL, trailing, partial close); balance snapshots feed the dashboard equity curve.

---

## 架构 / Architecture

```
┌──────────────┐      HTTP/REST       ┌─────────────────────────────────────────┐
│  React SPA   │ ───────────────────▶ │  FastAPI backend  (backend/main.py)      │
│  (frontend/) │   /api/trades/...    │                                          │
│  Vite + TS   │ ◀─────────────────── │  • router_trades.py  (trades API)        │
│ lightweight- │                      │  • APScheduler        (scheduler_service)│
│   charts     │                      │  • trading_agent.py   (LLM tool-calling) │
└──────────────┘                      │  • okx_client / okx_trading             │
                                      └───────────┬─────────────────┬───────────┘
                                                  │                 │
                              ┌───────────────────▼──┐   ┌──────────▼──────────┐
                              │  DeepSeek API         │   │  OKX REST API       │
                              │ (OpenAI-compatible    │   │  market data +      │
                              │  tool calling)        │   │  order execution    │
                              └───────────────────────┘   └─────────────────────┘
                                                  │
                                         ┌────────▼────────┐
                                         │ SQLite (data/)  │
                                         │ trades, logs,   │
                                         │ balance, jobs   │
                                         └─────────────────┘
```

### 后端 / Backend (`backend/`)

| 路径 / Path | 职责 / Responsibility |
|------|----------------|
| `main.py` | FastAPI 应用工厂、生命周期(建表 + 启停调度器)、`/health`。 |
| `config.py` | Pydantic `Settings`,从 `.env` 加载(所有环境变量的唯一来源)。 |
| `database.py` | SQLAlchemy 引擎、会话辅助、建表。 |
| `api/router_trades.py` | REST 端点:交易列表/统计、资产曲线、行情快照、手动触发、Agent 日志。 |
| `services/trading_agent.py` | 核心 LLM Agent 循环 —— 工具定义、工具执行、决策校验、模拟/实盘执行。 |
| `services/scheduler_service.py` | APScheduler 定时任务(交易 Agent、监控、余额快照)。 |
| `services/okx_client.py` / `okx_trading.py` | OKX 行情数据与带鉴权的下单。 |
| `tasks/` | 定时任务入口(交易 Agent、模拟监控、实盘监控、余额快照、持仓评审)。 |
| `models/` | SQLAlchemy 模型:`AutoTrade`、`AgentRunLog`、`BalanceSnapshot`。 |
| `utils/` | 日志、限速、K 线回测辅助。 |

### 前端 / Frontend (`frontend/`)

React 19 + Vite + TypeScript 单页仪表盘。`src/api/client.ts` 封装后端 REST(经 `/api` 代理);`src/pages/TradingPage.tsx` 用 `lightweight-charts` 渲染交易表、统计与资产曲线。

A React 19 + Vite + TypeScript SPA. `client.ts` wraps the backend REST API; `TradingPage.tsx` renders the trade table, summary stats, and an equity curve.

### 定时任务 / Scheduled jobs

| Job | 频率 / Schedule | 用途 / Purpose |
|-----|----------|---------|
| `trading_agent` | 每 15 分钟 | 对每个标的运行 LLM Agent → 开仓或观望。 |
| `paper_monitor` | 每 5 分钟 | 模拟模拟盘成交结果(入场、止盈止损、移动止损)。 |
| `live_monitor` | 每 5 分钟 | 跟踪并结算真实 OKX 持仓。 |
| `balance_snapshot` | 每分钟 | 记录权益,用于仪表盘曲线。 |

---

## 技术栈 / Tech stack

- **后端 / Backend**: Python 3.10, FastAPI, Uvicorn, SQLAlchemy 2, Pydantic Settings, APScheduler, httpx, pandas
- **大模型 / LLM**: DeepSeek(OpenAI 兼容 SDK,工具调用 + 思考模式)
- **交易所 / Exchange**: OKX v5 REST API
- **前端 / Frontend**: React 19, Vite 6, TypeScript, axios, lightweight-charts
- **存储 / Storage**: SQLite(默认,可经 `DATABASE_URL` 配置)
- **部署 / Deploy**: Docker + docker-compose

---

## 快速开始 / Getting started

### 1. 配置环境 / Configure

```bash
cp .env.example .env
# 填入 DEEPSEEK_API_KEY(Agent 必需)。
# 实盘还需 OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSPHRASE 且 TRADING_PAPER_MODE=false。
```

保持 `TRADING_PAPER_MODE=true` 与 `TRADING_ENABLED=true` 即可从模拟盘开始。

### 2. 后端 / Backend

```bash
pip install -r backend/requirements.txt
uvicorn backend.main:app --host 0.0.0.0 --port 8000   # API 文档: /docs
```

### 3. 前端 / Frontend

```bash
cd frontend && npm install && npm run dev   # 代理 /api → backend:8000
```

### 4. Docker

```bash
docker compose up --build   # backend → http://localhost:8000
```

---

## 主要 API / Key endpoints

| Method | Path | 说明 / Description |
|--------|------|-------------|
| `GET` | `/health` | 存活检查 / Liveness. |
| `GET` | `/api/trades/` | 交易列表 / List trades (`?paper=true&status=open`). |
| `GET` | `/api/trades/summary` | 胜率、盈亏汇总 / Win rate, PnL totals. |
| `GET` | `/api/trades/balance-history` | 资产曲线 / Equity curve. |
| `GET` | `/api/trades/market-snapshot` | 实时资金费率、多空比、恐惧贪婪 / Live snapshot. |
| `GET` | `/api/trades/agent-log` | 近期 Agent 决策 + 运行时长 / Recent decisions + uptime. |
| `POST` | `/api/trades/run-agent-now` | 手动触发一次 Agent / Manually trigger one run. |

---

## 安全 / Security

- 所有密钥放在 `.env`(已被 git 忽略)。`.env.example` 仅含占位符,**切勿提交真实密钥**。
- Agent 内置总开关(`TRADING_ENABLED`)、模拟/实盘开关(`TRADING_PAPER_MODE`)、单日亏损上限与最大持仓数。
- 当前 CORS 为开放状态(`allow_origins=["*"]`),公开部署前请收紧。

## 许可证 / License

MIT — 见 `LICENSE` / see `LICENSE`.

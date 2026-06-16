import { useEffect, useState, useCallback, useRef } from 'react'
import { createChart, ColorType, LineStyle, CrosshairMode } from 'lightweight-charts'
import api from '../api/client'

// ── Design tokens ─────────────────────────────────────────────────────────────
const N    = '#00ffaa'
const N20  = 'rgba(0,255,170,0.20)'
const N05  = 'rgba(0,255,170,0.05)'
const RED  = '#ef5350'
const YELLOW = '#fbbf24'
const BLUE   = '#60a5fa'
const DIM    = '#6e7681'
const MONO   = "'Courier New', monospace"

const neonBorder = (color = N20) => ({ border: `1px solid ${color}`, boxShadow: `0 0 8px rgba(0,255,170,0.04)` })
const glowText   = (color = N)   => ({ color, textShadow: `0 0 8px ${color}66`, fontFamily: MONO })

// ── Types ─────────────────────────────────────────────────────────────────────
interface Trade {
  id: number; symbol: string; direction: 'long' | 'short'
  entry_price: number; stop_loss: number; take_profit_1: number; take_profit_2: number | null
  size_pct: number; leverage: number; contracts: number | null; margin_used: number | null
  status: string; close_reason: string | null; pnl_pct: number | null; pnl_usdt: number | null
  is_paper: boolean; agent_reasoning: string | null
  created_at: string; opened_at: string | null; closed_at: string | null
}
interface Summary {
  total: number; pending: number; open: number; closed: number
  wins: number; losses: number
  win_rate_pct: number | null; avg_pnl_pct: number | null
  total_pnl_pct: number | null; total_pnl_usdt: number | null; is_paper: boolean
}
interface Prices { btc: number; btcChange: number; eth: number; ethChange: number }
interface AgentLogEntry { ts: string; symbol: string; action: string; reasoning: string | null }
interface AgentLog { uptime_seconds: number; server_start: string; log: AgentLogEntry[] }
interface MarketSnapshot {
  btc: { funding_rate_pct: number | null; long_short_ratio: number | null }
  eth: { funding_rate_pct: number | null; long_short_ratio: number | null }
  fear_greed: number | null; fear_greed_label: string | null; ts: string
}
interface BalanceHistory {
  initial_balance: number
  points: { ts: number; value: number }[]
}

// ── OKX WebSocket price hook ──────────────────────────────────────────────────
function useOKXPrices(): Prices | null {
  const [prices, setPrices] = useState<Prices | null>(null)
  const latestRef    = useRef<Record<string, { last: number; open24h: number }>>({})
  const wsRef        = useRef<WebSocket | null>(null)
  const reconnectRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const tickRef      = useRef<ReturnType<typeof setInterval> | null>(null)

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return
    const ws = new WebSocket('wss://ws.okx.com:8443/ws/v5/public')
    wsRef.current = ws
    ws.onopen = () => {
      ws.send(JSON.stringify({ op: 'subscribe', args: [
        { channel: 'tickers', instId: 'BTC-USDT' },
        { channel: 'tickers', instId: 'ETH-USDT' },
      ]}))
    }
    ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data)
        const d = msg.data?.[0]
        if (!d?.instId) return
        latestRef.current[d.instId] = { last: parseFloat(d.last), open24h: parseFloat(d.open24h) }
      } catch { /* ignore */ }
    }
    ws.onclose = () => { reconnectRef.current = setTimeout(connect, 2000) }
    ws.onerror = () => ws.close()
  }, [])

  useEffect(() => {
    connect()
    tickRef.current = setInterval(() => {
      const btc = latestRef.current['BTC-USDT']
      const eth = latestRef.current['ETH-USDT']
      if (!btc || !eth) return
      setPrices({
        btc: btc.last, btcChange: (btc.last - btc.open24h) / btc.open24h * 100,
        eth: eth.last, ethChange: (eth.last - eth.open24h) / eth.open24h * 100,
      })
    }, 500)
    return () => {
      if (tickRef.current)      clearInterval(tickRef.current)
      if (reconnectRef.current) clearTimeout(reconnectRef.current)
      wsRef.current?.close()
    }
  }, [connect])

  return prices
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function cst(iso: string | null) {
  if (!iso) return '—'
  return new Date(iso.endsWith('Z') ? iso : iso + 'Z').toLocaleString('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
  })
}
function timeAgo(iso: string): string {
  const secs = Math.floor((Date.now() - new Date(iso.endsWith('Z') ? iso : iso + 'Z').getTime()) / 1000)
  if (secs < 60)   return `${secs}s ago`
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`
  return `${Math.floor(secs / 3600)}h ${Math.floor((secs % 3600) / 60)}m ago`
}
function fmtUptime(secs: number): string {
  const h = Math.floor(secs / 3600), m = Math.floor((secs % 3600) / 60), s = secs % 60
  if (h > 0) return `${h}h ${m}m`
  if (m > 0) return `${m}m ${s}s`
  return `${s}s`
}
function pnlColor(v: number | null) { return v === null ? DIM : v >= 0 ? N : RED }
function fmt(n: number, d = 2)     { return n.toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d }) }

function outcomeLabel(t: Trade) {
  if (t.status === 'pending_entry') return { label: '等待入场', color: YELLOW }
  if (t.status === 'open')          return { label: '持仓中',   color: BLUE }
  if (t.close_reason === 'sl_hit')  return { label: 'SL 止损',  color: RED }
  if (t.close_reason === 'tp1_hit') return { label: 'TP1 止盈', color: N }
  if (t.close_reason === 'tp2_hit') return { label: 'TP2 止盈', color: N }
  if (t.status === 'failed')        return { label: '下单失败', color: RED }
  return { label: t.status, color: DIM }
}

function fngColor(v: number | null): string {
  if (v === null) return DIM
  if (v <= 25) return RED; if (v <= 45) return YELLOW
  if (v <= 55) return '#e6edf3'; if (v <= 75) return N
  return '#f97316'
}
function lsColor(v: number | null): string {
  if (v === null) return DIM
  if (v > 1.3) return '#f97316'; if (v < 0.75) return BLUE
  return '#e6edf3'
}

// ── Sub-components ────────────────────────────────────────────────────────────
function Tag({ children, color }: { children: React.ReactNode; color: string }) {
  return (
    <span style={{ fontFamily: MONO, fontSize: 11, color, border: `1px solid ${color}44`, padding: '1px 6px', borderRadius: 2, letterSpacing: 1 }}>
      {children}
    </span>
  )
}

function StatCard({ label, value, sub, accent }: { label: string; value: string; sub?: string; accent?: string }) {
  const c = accent ?? '#e6edf3'
  return (
    <div style={{ background: '#0f0f0f', flex: 1, minWidth: 130, padding: '14px 18px', ...neonBorder() }}>
      <div style={{ fontFamily: MONO, fontSize: 11, color: DIM, letterSpacing: 2, marginBottom: 6 }}>{label}</div>
      <div style={{ fontFamily: MONO, fontSize: 22, fontWeight: 700, color: c, lineHeight: 1 }}>{value}</div>
      {sub && <div style={{ fontFamily: MONO, fontSize: 11, color: DIM, marginTop: 4 }}>{sub}</div>}
    </div>
  )
}

function PriceChip({ symbol, price, change }: { symbol: string; price: number; change: number }) {
  const up = change >= 0
  return (
    <span style={{ fontFamily: MONO, fontSize: 13, display: 'inline-flex', gap: 6, alignItems: 'center' }}>
      <span style={{ color: DIM }}>{symbol}</span>
      <span style={{ color: '#e6edf3' }}>${fmt(price)}</span>
      <span style={{ color: up ? N : RED, fontSize: 11 }}>{up ? '+' : ''}{change.toFixed(2)}%</span>
    </span>
  )
}

function Btn({ onClick, loading, children, variant = 'primary' }: {
  onClick: () => void; loading?: boolean; children: React.ReactNode; variant?: 'primary' | 'dim'
}) {
  const base: React.CSSProperties = {
    fontFamily: MONO, fontSize: 12, letterSpacing: 1, cursor: loading ? 'wait' : 'pointer',
    padding: '7px 16px', borderRadius: 2, transition: 'all 0.15s', outline: 'none', opacity: loading ? 0.6 : 1,
  }
  if (variant === 'dim') {
    return (
      <button onClick={onClick} disabled={loading} style={{ ...base, background: 'transparent', color: DIM, border: '1px solid #30363d' }}
        onMouseEnter={e => { const b = e.target as HTMLButtonElement; b.style.color = '#e6edf3'; b.style.borderColor = DIM }}
        onMouseLeave={e => { const b = e.target as HTMLButtonElement; b.style.color = DIM; b.style.borderColor = '#30363d' }}
      >{loading ? '...' : children}</button>
    )
  }
  return (
    <button onClick={onClick} disabled={loading} style={{ ...base, background: 'transparent', color: N, border: `1px solid ${N}` }}
      onMouseEnter={e => { const b = e.target as HTMLButtonElement; b.style.background = N; b.style.color = '#000' }}
      onMouseLeave={e => { const b = e.target as HTMLButtonElement; b.style.background = 'transparent'; b.style.color = N }}
    >{loading ? '执行中...' : children}</button>
  )
}

function TradeRow({ t, expanded, onToggle, livePrice }: {
  t: Trade; expanded: boolean; onToggle: () => void; livePrice?: number
}) {
  const outcome = outcomeLabel(t)
  const isOpen  = t.status === 'pending_entry' || t.status === 'open'
  const d       = t.entry_price > 1000 ? 1 : 4

  let displayPnl: number | null = t.pnl_pct
  let isLive = false
  if (t.status === 'open' && livePrice != null) {
    const gross = t.direction === 'long'
      ? (livePrice - t.entry_price) / t.entry_price * 100
      : (t.entry_price - livePrice) / t.entry_price * 100
    displayPnl = Math.round((gross - 0.12) * t.leverage * 10000) / 10000
    isLive = true
  }

  let distToEntry: number | null = null
  if (t.status === 'pending_entry' && livePrice != null)
    distToEntry = (livePrice - t.entry_price) / t.entry_price * 100

  let progressPct: number | null = null
  if (t.status === 'open' && livePrice != null) {
    const lo = t.direction === 'long' ? t.stop_loss    : t.take_profit_1
    const hi = t.direction === 'long' ? t.take_profit_1 : t.stop_loss
    progressPct = Math.max(0, Math.min(100, (livePrice - lo) / (hi - lo) * 100))
  }

  return (
    <>
      <tr onClick={onToggle} style={{ cursor: 'pointer', background: expanded ? N05 : 'transparent' }}
        onMouseEnter={e => !expanded && ((e.currentTarget as HTMLTableRowElement).style.background = '#ffffff08')}
        onMouseLeave={e => !expanded && ((e.currentTarget as HTMLTableRowElement).style.background = 'transparent')}
      >
        <td style={{ color: DIM, fontSize: 12, padding: '9px 12px' }}>#{t.id}</td>
        <td style={{ fontFamily: MONO, fontSize: 12, padding: '9px 12px' }}>
          {t.symbol.replace('-SWAP', '')}
          {t.is_paper && <span style={{ color: DIM, fontSize: 10, marginLeft: 4 }}>[P]</span>}
        </td>
        <td style={{ padding: '9px 12px' }}><Tag color={t.direction === 'long' ? N : RED}>{t.direction === 'long' ? 'LONG' : 'SHORT'}</Tag></td>
        <td style={{ fontFamily: MONO, fontSize: 12, padding: '9px 12px' }}>
          {fmt(t.entry_price, d)}
          {livePrice != null && (t.status === 'pending_entry' || t.status === 'open') && (
            <div style={{ fontSize: 10, color: DIM, marginTop: 1 }}>现价 <span style={{ color: '#e6edf3' }}>{fmt(livePrice, d)}</span></div>
          )}
        </td>
        <td style={{ fontFamily: MONO, fontSize: 12, padding: '9px 12px', color: RED }}>{fmt(t.stop_loss, d)}</td>
        <td style={{ fontFamily: MONO, fontSize: 12, padding: '9px 12px', color: N }}>
          {fmt(t.take_profit_1, d)}
          {t.take_profit_2 && <span style={{ color: DIM, fontSize: 10, marginLeft: 4 }}>/ {fmt(t.take_profit_2, d)}</span>}
        </td>
        <td style={{ padding: '9px 12px' }}><Tag color={outcome.color}>{outcome.label}</Tag></td>
        <td style={{ fontFamily: MONO, fontSize: 12, padding: '9px 12px', fontWeight: displayPnl !== null ? 700 : 400 }}>
          {t.status === 'pending_entry' && distToEntry !== null ? (
            <span style={{ color: DIM, fontSize: 11 }}>
              距入场 <span style={{ color: Math.abs(distToEntry) < 0.3 ? YELLOW : DIM }}>
                {distToEntry >= 0 ? '+' : ''}{distToEntry.toFixed(2)}%
              </span>
            </span>
          ) : displayPnl !== null ? (
            <span style={{ color: pnlColor(displayPnl) }}>
              {displayPnl >= 0 ? '+' : ''}{displayPnl.toFixed(2)}%
              {isLive && <span style={{ fontSize: 8, color: N, marginLeft: 3, verticalAlign: 'middle' }}>●</span>}
            </span>
          ) : '—'}
        </td>
        <td style={{ fontFamily: MONO, fontSize: 11, padding: '9px 12px', color: DIM }}>{isOpen ? cst(t.created_at) : cst(t.closed_at)}</td>
        <td style={{ padding: '9px 12px', color: DIM, fontSize: 12 }}>{expanded ? '▲' : '▼'}</td>
      </tr>
      {expanded && (
        <tr style={{ background: N05 }}>
          <td colSpan={10} style={{ padding: '8px 12px 14px 44px' }}>
            {progressPct !== null && (
              <div style={{ marginBottom: 10 }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', fontFamily: MONO, fontSize: 10, color: DIM, marginBottom: 3 }}>
                  <span style={{ color: RED }}>SL {fmt(t.stop_loss, d)}</span>
                  {livePrice != null && <span style={{ color: '#e6edf3' }}>现价 {fmt(livePrice, d)}</span>}
                  <span style={{ color: N }}>TP1 {fmt(t.take_profit_1, d)}</span>
                </div>
                <div style={{ height: 4, background: '#1a1a1a', borderRadius: 2, overflow: 'hidden', position: 'relative' }}>
                  <div style={{
                    position: 'absolute', left: 0, top: 0, height: '100%', width: `${progressPct}%`,
                    background: t.direction === 'long' ? `linear-gradient(to right, ${RED}88, ${N})` : `linear-gradient(to right, ${N}, ${RED}88)`,
                    transition: 'width 0.5s',
                  }} />
                  <div style={{ position: 'absolute', left: `${progressPct}%`, top: -2, width: 2, height: 8, background: '#fff', transform: 'translateX(-50%)' }} />
                </div>
              </div>
            )}
            <div style={{ fontFamily: MONO, fontSize: 11, color: DIM, marginBottom: 4 }}>
              SL {fmt(t.stop_loss, d)} | TP1 {fmt(t.take_profit_1, d)}
              {t.take_profit_2 ? ` | TP2 ${fmt(t.take_profit_2, d)}` : ''} | 杠杆 {t.leverage}x | 仓位 {t.size_pct}%
              {t.margin_used ? ` | 保证金 ${fmt(t.margin_used)} USDT` : ''}
            </div>
            {t.opened_at && (
              <div style={{ fontFamily: MONO, fontSize: 11, color: DIM, marginBottom: 4 }}>
                入场 {cst(t.opened_at)}{t.closed_at ? ` → 出场 ${cst(t.closed_at)}` : ''}
                {t.pnl_usdt !== null ? ` | ${t.pnl_usdt >= 0 ? '+' : ''}${fmt(t.pnl_usdt)} USDT` : ''}
              </div>
            )}
            {t.agent_reasoning && (
              <div style={{ fontFamily: MONO, fontSize: 11, color: '#9ca3af', borderLeft: `2px solid ${N20}`, paddingLeft: 10, marginTop: 6, lineHeight: 1.6 }}>
                <span style={{ color: DIM }}>AI: </span>{t.agent_reasoning}
              </div>
            )}
          </td>
        </tr>
      )}
    </>
  )
}

function TH({ children }: { children?: React.ReactNode }) {
  return (
    <th style={{ fontFamily: MONO, fontSize: 10, color: DIM, letterSpacing: 2, textAlign: 'left', padding: '8px 12px', borderBottom: `1px solid ${N20}`, fontWeight: 400 }}>
      {children}
    </th>
  )
}

// ── Equity Curve ──────────────────────────────────────────────────────────────
function EquityChart({ history, isPaper }: { history: BalanceHistory | null; isPaper: boolean }) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef     = useRef<ReturnType<typeof createChart> | null>(null)
  const seriesRef    = useRef<any>(null)
  const baselineRef  = useRef<any>(null)

  useEffect(() => {
    if (!containerRef.current) return
    const chart = createChart(containerRef.current, {
      layout: { background: { type: ColorType.Solid, color: '#0a0a0a' }, textColor: '#6e7681', fontFamily: "'Courier New', monospace", fontSize: 11 },
      grid: { vertLines: { color: '#161b22', style: LineStyle.Dotted }, horzLines: { color: '#161b22', style: LineStyle.Dotted } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: '#30363d' },
      timeScale: { borderColor: '#30363d', timeVisible: true, secondsVisible: false },
      width: containerRef.current.clientWidth, height: 200,
    })
    seriesRef.current   = chart.addLineSeries({ color: N, lineWidth: 2, priceFormat: { type: 'price', precision: 2, minMove: 0.01 }, lastValueVisible: true, priceLineVisible: false })
    baselineRef.current = chart.addLineSeries({ color: '#30363d', lineWidth: 1, lineStyle: LineStyle.Dashed, lastValueVisible: false, priceLineVisible: false, priceFormat: { type: 'price', precision: 2, minMove: 0.01 } })
    chartRef.current = chart
    const ro = new ResizeObserver(() => { if (containerRef.current) chart.applyOptions({ width: containerRef.current.clientWidth }) })
    ro.observe(containerRef.current)
    return () => { ro.disconnect(); chart.remove() }
  }, [])

  useEffect(() => {
    if (!seriesRef.current || !baselineRef.current || !history || history.points.length === 0) return
    const data = history.points.map(p => ({ time: p.ts as any, value: p.value }))
    seriesRef.current.setData(data)
    const first = history.points[0].ts, last = history.points[history.points.length - 1].ts
    baselineRef.current.setData([{ time: first as any, value: history.initial_balance }, { time: last as any, value: history.initial_balance }])
    seriesRef.current.applyOptions({ color: data[data.length - 1].value < history.initial_balance ? RED : N })
    chartRef.current?.timeScale().fitContent()
  }, [history])

  const pct = history && history.points.length > 0 ? ((history.points[history.points.length - 1].value - history.initial_balance) / history.initial_balance * 100) : null
  const cur = history && history.points.length > 0 ? history.points[history.points.length - 1].value : null

  return (
    <div style={{ background: '#0a0a0a', ...neonBorder(), padding: '16px 18px' }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 16, marginBottom: 12 }}>
        <div style={{ fontFamily: MONO, fontSize: 10, color: DIM, letterSpacing: 3 }}>EQUITY CURVE {isPaper ? '· PAPER' : '· LIVE'}</div>
        {cur !== null && <div style={{ fontFamily: MONO, fontSize: 20, fontWeight: 700, color: pnlColor(pct) }}>${fmt(cur)}</div>}
        {pct !== null && <div style={{ fontFamily: MONO, fontSize: 13, color: pnlColor(pct) }}>{pct >= 0 ? '+' : ''}{pct.toFixed(2)}%</div>}
        {history && <div style={{ fontFamily: MONO, fontSize: 11, color: DIM, marginLeft: 'auto' }}>起始 ${fmt(history.initial_balance)}</div>}
      </div>
      {(!history || history.points.length === 0)
        ? <div style={{ height: 200, display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#3a3a3a', fontFamily: MONO, fontSize: 12 }}>等待首次快照…（每分钟记录一次）</div>
        : <div ref={containerRef} style={{ height: 200 }} />
      }
    </div>
  )
}

// ── Market Signals ─────────────────────────────────────────────────────────────
function MarketSignals({ snapshot, prices }: { snapshot: MarketSnapshot | null; prices: Prices | null }) {
  const row = (label: string, value: React.ReactNode) => (
    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '5px 0', borderBottom: '1px solid #1a1a1a' }}>
      <span style={{ fontFamily: MONO, fontSize: 11, color: DIM }}>{label}</span>
      <span style={{ fontFamily: MONO, fontSize: 12 }}>{value}</span>
    </div>
  )
  const frColor = (v: number | null) => v === null ? DIM : v > 0.02 ? '#f97316' : v < -0.01 ? BLUE : '#e6edf3'
  const coinBlock = (coin: 'btc' | 'eth', label: string) => {
    const d = snapshot?.[coin]
    const price  = coin === 'btc' ? prices?.btc      : prices?.eth
    const change = coin === 'btc' ? prices?.btcChange : prices?.ethChange
    return (
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontFamily: MONO, fontSize: 10, color: N, letterSpacing: 3, marginBottom: 8 }}>{label}</div>
        {row('价格', price
          ? <span><span style={{ color: '#e6edf3' }}>${fmt(price)}</span>{change !== undefined && <span style={{ color: change >= 0 ? N : RED, fontSize: 10, marginLeft: 6 }}>{change >= 0 ? '+' : ''}{change.toFixed(2)}%</span>}</span>
          : <span style={{ color: DIM }}>—</span>)}
        {row('资金费率', d?.funding_rate_pct != null
          ? <span style={{ color: frColor(d.funding_rate_pct) }}>{d.funding_rate_pct >= 0 ? '+' : ''}{d.funding_rate_pct.toFixed(4)}%</span>
          : <span style={{ color: DIM }}>—</span>)}
        {row('多空比 (1H)', d?.long_short_ratio != null
          ? <span style={{ color: lsColor(d.long_short_ratio) }}>{d.long_short_ratio.toFixed(3)}<span style={{ color: DIM, fontSize: 10, marginLeft: 5 }}>{d.long_short_ratio > 1.2 ? '多头主导' : d.long_short_ratio < 0.85 ? '空头主导' : '均衡'}</span></span>
          : <span style={{ color: DIM }}>—</span>)}
      </div>
    )
  }
  return (
    <div style={{ background: '#0a0a0a', ...neonBorder(), padding: '16px 18px', height: '100%', boxSizing: 'border-box' }}>
      <div style={{ fontFamily: MONO, fontSize: 10, color: DIM, letterSpacing: 3, marginBottom: 12 }}>MARKET SIGNALS</div>
      <div style={{ display: 'flex', gap: 20 }}>
        {coinBlock('btc', 'BTC')}
        <div style={{ width: 1, background: '#1a1a1a' }} />
        {coinBlock('eth', 'ETH')}
      </div>
      <div style={{ marginTop: 12, paddingTop: 10, borderTop: '1px solid #1a1a1a', display: 'flex', alignItems: 'center', gap: 12 }}>
        <span style={{ fontFamily: MONO, fontSize: 11, color: DIM }}>恐惧贪婪</span>
        {snapshot?.fear_greed != null ? (
          <>
            <span style={{ fontFamily: MONO, fontSize: 20, fontWeight: 700, color: fngColor(snapshot.fear_greed) }}>{snapshot.fear_greed}</span>
            <span style={{ fontFamily: MONO, fontSize: 11, color: fngColor(snapshot.fear_greed) }}>{snapshot.fear_greed_label}</span>
            <div style={{ flex: 1, height: 4, background: '#1a1a1a', borderRadius: 2, overflow: 'hidden' }}>
              <div style={{ height: '100%', width: `${snapshot.fear_greed}%`, background: fngColor(snapshot.fear_greed), transition: 'width 0.5s' }} />
            </div>
          </>
        ) : <span style={{ fontFamily: MONO, fontSize: 11, color: DIM }}>加载中…</span>}
      </div>
      {snapshot?.ts && <div style={{ fontFamily: MONO, fontSize: 10, color: '#3a3a3a', marginTop: 8, textAlign: 'right' }}>{timeAgo(snapshot.ts)}</div>}
    </div>
  )
}

// ── Agent Activity Log ─────────────────────────────────────────────────────────
function AgentActivityLog({ agentLog, onRunAgent, running }: { agentLog: AgentLog | null; onRunAgent: () => void; running: boolean }) {
  const actionStyle = (action: string): React.CSSProperties => {
    if (action === 'trade')   return { color: N,   border: `1px solid ${N}44` }
    if (action === 'hold')    return { color: DIM, border: '1px solid #30363d' }
    if (action === 'skipped') return { color: '#3a3a3a', border: '1px solid #2a2a2a' }
    return { color: RED, border: `1px solid ${RED}44` }
  }
  return (
    <div style={{ background: '#0a0a0a', ...neonBorder(), padding: '16px 18px', height: '100%', boxSizing: 'border-box', display: 'flex', flexDirection: 'column' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
        <div style={{ fontFamily: MONO, fontSize: 10, color: DIM, letterSpacing: 3 }}>AGENT ACTIVITY</div>
        <Btn onClick={onRunAgent} loading={running} variant="dim">▶ RUN NOW</Btn>
      </div>
      {agentLog && (
        <div style={{ display: 'flex', gap: 16, marginBottom: 10, paddingBottom: 10, borderBottom: '1px solid #1a1a1a' }}>
          <div><div style={{ fontFamily: MONO, fontSize: 10, color: DIM, marginBottom: 2 }}>UPTIME</div><div style={{ fontFamily: MONO, fontSize: 13, color: N }}>{fmtUptime(agentLog.uptime_seconds)}</div></div>
          <div><div style={{ fontFamily: MONO, fontSize: 10, color: DIM, marginBottom: 2 }}>LAST RUN</div><div style={{ fontFamily: MONO, fontSize: 13, color: '#e6edf3' }}>{agentLog.log.length > 0 ? timeAgo(agentLog.log[0].ts) : '—'}</div></div>
          <div><div style={{ fontFamily: MONO, fontSize: 10, color: DIM, marginBottom: 2 }}>DECISIONS</div><div style={{ fontFamily: MONO, fontSize: 13, color: '#e6edf3' }}>{agentLog.log.length}</div></div>
        </div>
      )}
      <div style={{ height: 420, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 6 }}>
        {!agentLog || agentLog.log.length === 0 ? (
          <div style={{ fontFamily: MONO, fontSize: 12, color: '#3a3a3a', textAlign: 'center', paddingTop: 20 }}>等待首次运行…</div>
        ) : agentLog.log.map((entry, i) => (
          <div key={i} style={{ padding: '7px 10px', background: '#0f0f0f', borderLeft: `2px solid ${entry.action === 'trade' ? N : entry.action === 'hold' ? '#30363d' : '#2a2a2a'}` }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: entry.reasoning ? 4 : 0 }}>
              <span style={{ fontFamily: MONO, fontSize: 10, ...actionStyle(entry.action), padding: '1px 5px', borderRadius: 2, letterSpacing: 1 }}>{entry.action.toUpperCase()}</span>
              <span style={{ fontFamily: MONO, fontSize: 11, color: '#9ca3af' }}>{entry.symbol.replace('-USDT', '')}</span>
              <span style={{ fontFamily: MONO, fontSize: 10, color: '#3a3a3a', marginLeft: 'auto' }}>{timeAgo(entry.ts)}</span>
            </div>
            {entry.reasoning && (
              <div style={{ fontFamily: MONO, fontSize: 10, color: '#4a5568', lineHeight: 1.5, overflow: 'hidden', display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical' }}>
                {entry.reasoning}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}

// ── Main page ──────────────────────────────────────────────────────────────────
export default function TradingPage() {
  const prices = useOKXPrices()
  const [summary, setSummary]               = useState<Summary | null>(null)
  const [trades, setTrades]                 = useState<Trade[]>([])
  const [agentLog, setAgentLog]             = useState<AgentLog | null>(null)
  const [snapshot, setSnapshot]             = useState<MarketSnapshot | null>(null)
  const [balanceHistory, setBalanceHistory] = useState<BalanceHistory | null>(null)
  const [isPaper, setIsPaper]               = useState(false)
  const [loading, setLoading]               = useState(true)
  const [runningAgent, setRunningAgent]     = useState(false)
  const [runningMonitor, setRunningMonitor] = useState(false)
  const [lastUpdated, setLastUpdated]       = useState<Date | null>(null)
  const [toast, setToast]                   = useState<{ msg: string; ok: boolean } | null>(null)
  const [expanded, setExpanded]             = useState<Set<number>>(new Set())
  const refreshRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const showToast = (msg: string, ok = true) => { setToast({ msg, ok }); setTimeout(() => setToast(null), 3500) }

  const fetchAgentLog = useCallback(async () => {
    try { const res = await api.get('/trades/agent-log?limit=50'); setAgentLog(res.data) } catch { /* ignore */ }
  }, [])

  const fetchSnapshot = useCallback(async () => {
    try { const res = await api.get('/trades/market-snapshot'); setSnapshot(res.data) } catch { /* ignore */ }
  }, [])

  const fetchBalanceHistory = useCallback(async () => {
    try { const res = await api.get(`/trades/balance-history?paper=false&hours=24`); setBalanceHistory(res.data) } catch { /* ignore */ }
  }, [])

  const fetchData = useCallback(async () => {
    try {
      const [sumRes, tradeRes] = await Promise.all([
        api.get(`/trades/summary?paper=${isPaper}`),
        api.get(`/trades/?paper=${isPaper}&limit=100`),
      ])
      setSummary(sumRes.data); setTrades(tradeRes.data); setLastUpdated(new Date())
    } catch (e) { console.error(e) } finally { setLoading(false) }
  }, [isPaper])

  const refreshAll = useCallback(() => {
    fetchData(); fetchAgentLog(); fetchSnapshot(); fetchBalanceHistory()
  }, [fetchData, fetchAgentLog, fetchSnapshot, fetchBalanceHistory])

  useEffect(() => {
    setLoading(true); refreshAll()
    refreshRef.current = setInterval(refreshAll, 30_000)
    return () => { if (refreshRef.current) clearInterval(refreshRef.current) }
  }, [refreshAll])

  const runAgent = async () => {
    setRunningAgent(true)
    try { await api.post('/trades/run-agent-now'); showToast('Agent 已触发，正在后台运行…'); setTimeout(refreshAll, 10_000) }
    catch { showToast('触发失败，检查服务端日志', false) }
    finally { setRunningAgent(false) }
  }

  const runMonitor = async () => {
    setRunningMonitor(true)
    try { await api.post('/trades/run-paper-monitor-now'); showToast('Paper monitor 已触发，正在更新持仓…'); setTimeout(fetchData, 5000) }
    catch { showToast('触发失败', false) }
    finally { setRunningMonitor(false) }
  }

  const toggleRow = (id: number) => setExpanded(prev => { const n = new Set(prev); n.has(id) ? n.delete(id) : n.add(id); return n })
  const openTrades   = trades.filter(t => t.status === 'pending_entry' || t.status === 'open')
  const closedTrades = trades.filter(t => t.status === 'closed' || t.status === 'failed')

  return (
    <div style={{ minHeight: '100vh', background: '#0d1117', color: '#e6edf3', fontFamily: MONO, position: 'relative' }}>

      {toast && (
        <div style={{ position: 'fixed', top: 20, right: 20, zIndex: 9999, background: '#0f0f0f', border: `1px solid ${toast.ok ? N : RED}`, boxShadow: `0 0 20px ${toast.ok ? N : RED}44`, padding: '10px 18px', fontFamily: MONO, fontSize: 12, color: toast.ok ? N : RED, borderRadius: 2, maxWidth: 340 }}>
          {toast.ok ? '✓ ' : '✗ '}{toast.msg}
        </div>
      )}

      <div style={{ background: '#0a0a0a', borderBottom: `1px solid ${N20}`, padding: '12px 24px', display: 'flex', alignItems: 'center', gap: 24, flexWrap: 'wrap' }}>
        <div style={{ ...glowText(), fontSize: 15, fontWeight: 700, letterSpacing: 3 }}>TRADING AGENT</div>
        <Tag color={isPaper ? YELLOW : RED}>{isPaper ? 'PAPER' : 'LIVE'}</Tag>
        <div style={{ display: 'flex', gap: 20, marginLeft: 8 }}>
          {prices
            ? <><PriceChip symbol="BTC" price={prices.btc} change={prices.btcChange} /><PriceChip symbol="ETH" price={prices.eth} change={prices.ethChange} /></>
            : <span style={{ color: DIM, fontSize: 12 }}>价格加载中…</span>}
        </div>
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 8, alignItems: 'center' }}>
          <button onClick={() => setIsPaper(!isPaper)} style={{ fontFamily: MONO, fontSize: 11, cursor: 'pointer', letterSpacing: 1, padding: '4px 12px', background: 'transparent', borderRadius: 2, color: DIM, border: '1px solid #30363d' }}>
            切换 {isPaper ? '→ LIVE' : '→ PAPER'}
          </button>
          {lastUpdated && <span style={{ color: DIM, fontSize: 10 }}>更新 {lastUpdated.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })}</span>}
        </div>
      </div>

      <div style={{ padding: '20px 24px', maxWidth: 1400, margin: '0 auto' }}>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, marginBottom: 16, minHeight: 220 }}>
          <MarketSignals snapshot={snapshot} prices={prices} />
          <AgentActivityLog agentLog={agentLog} onRunAgent={runAgent} running={runningAgent} />
        </div>

        <div style={{ marginBottom: 20 }}><EquityChart history={balanceHistory} isPaper={isPaper} /></div>

        {summary && (
          <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', marginBottom: 20 }}>
            <StatCard label="总交易" value={String(summary.total)} sub={`持仓 ${summary.open + summary.pending} | 已平 ${summary.closed}`} />
            <StatCard label="胜率" value={summary.win_rate_pct !== null ? `${summary.win_rate_pct.toFixed(1)}%` : '—'} sub={summary.closed > 0 ? `${summary.wins}胜 ${summary.losses}负` : '暂无记录'} accent={summary.win_rate_pct !== null ? (summary.win_rate_pct >= 50 ? N : RED) : undefined} />
            <StatCard label="平均盈亏" value={summary.avg_pnl_pct !== null ? `${summary.avg_pnl_pct >= 0 ? '+' : ''}${summary.avg_pnl_pct.toFixed(2)}%` : '—'} accent={summary.avg_pnl_pct !== null ? pnlColor(summary.avg_pnl_pct) : undefined} />
            <StatCard label="累计盈亏" value={summary.total_pnl_pct !== null ? `${summary.total_pnl_pct >= 0 ? '+' : ''}${summary.total_pnl_pct.toFixed(2)}%` : '—'} sub={summary.total_pnl_usdt !== null ? `${summary.total_pnl_usdt >= 0 ? '+' : ''}${fmt(summary.total_pnl_usdt)} USDT` : undefined} accent={summary.total_pnl_pct !== null ? pnlColor(summary.total_pnl_pct) : undefined} />
            <StatCard label="持仓中" value={String(summary.open + summary.pending)} sub={`等待入场 ${summary.pending} | 已入场 ${summary.open}`} accent={summary.open + summary.pending > 0 ? BLUE : DIM} />
          </div>
        )}

        <div style={{ display: 'flex', gap: 10, alignItems: 'center', marginBottom: 24, flexWrap: 'wrap' }}>
          <Btn onClick={runMonitor} loading={runningMonitor} variant="dim">⟳ CHECK POSITIONS</Btn>
          <Btn onClick={refreshAll} variant="dim">↻ REFRESH</Btn>
          <span style={{ color: DIM, fontSize: 11, marginLeft: 8 }}>自动刷新 30s</span>
        </div>

        {loading ? (
          <div style={{ color: DIM, fontFamily: MONO, fontSize: 13, textAlign: 'center', padding: 60 }}>加载中…</div>
        ) : (
          <>
            <section style={{ marginBottom: 32 }}>
              <div style={{ fontFamily: MONO, fontSize: 11, color: N, letterSpacing: 3, marginBottom: 10, display: 'flex', alignItems: 'center', gap: 10 }}>
                OPEN POSITIONS
                <span style={{ background: N20, color: N, fontSize: 10, padding: '1px 7px', borderRadius: 10 }}>{openTrades.length}</span>
              </div>
              {openTrades.length === 0
                ? <div style={{ ...neonBorder(), background: '#0a0a0a', padding: '28px 20px', color: DIM, fontFamily: MONO, fontSize: 12, textAlign: 'center' }}>暂无持仓 — 等待 Agent 发出交易信号</div>
                : <div style={{ ...neonBorder(), background: '#0a0a0a', overflowX: 'auto' }}>
                    <table style={{ width: '100%', borderCollapse: 'collapse', minWidth: 700 }}>
                      <thead><tr><TH>ID</TH><TH>交易对</TH><TH>方向</TH><TH>入场价</TH><TH>止损</TH><TH>止盈</TH><TH>状态</TH><TH>盈亏</TH><TH>时间</TH><TH></TH></tr></thead>
                      <tbody>{openTrades.map(t => {
                        const lp = prices ? (t.symbol.includes('BTC') ? prices.btc : prices.eth) : undefined
                        return <TradeRow key={t.id} t={t} expanded={expanded.has(t.id)} onToggle={() => toggleRow(t.id)} livePrice={lp} />
                      })}</tbody>
                    </table>
                  </div>
              }
            </section>

            <section>
              <div style={{ fontFamily: MONO, fontSize: 11, color: DIM, letterSpacing: 3, marginBottom: 10, display: 'flex', alignItems: 'center', gap: 10 }}>
                TRADE HISTORY
                <span style={{ background: '#ffffff10', color: DIM, fontSize: 10, padding: '1px 7px', borderRadius: 10 }}>{closedTrades.length}</span>
              </div>
              {closedTrades.length === 0
                ? <div style={{ border: '1px solid #21262d', background: '#0a0a0a', padding: '28px 20px', color: DIM, fontFamily: MONO, fontSize: 12, textAlign: 'center' }}>暂无历史记录</div>
                : <div style={{ border: '1px solid #21262d', background: '#0a0a0a', overflowX: 'auto' }}>
                    <table style={{ width: '100%', borderCollapse: 'collapse', minWidth: 700 }}>
                      <thead><tr><TH>ID</TH><TH>交易对</TH><TH>方向</TH><TH>入场价</TH><TH>止损</TH><TH>止盈</TH><TH>结果</TH><TH>盈亏</TH><TH>平仓时间</TH><TH></TH></tr></thead>
                      <tbody>{closedTrades.map(t => <TradeRow key={t.id} t={t} expanded={expanded.has(t.id)} onToggle={() => toggleRow(t.id)} />)}</tbody>
                    </table>
                  </div>
              }
            </section>
          </>
        )}
      </div>
    </div>
  )
}

import axios from 'axios'

const api = axios.create({
  baseURL: '/api',
  timeout: 30000,
})

export const getTrades = (params?: { paper?: boolean; status?: string; limit?: number }) =>
  api.get('/trades/', { params })

export const getTradeSummary = (paper = true) =>
  api.get('/trades/summary', { params: { paper } })

export const getTrade = (id: number) => api.get(`/trades/${id}`)

export const runAgentNow = () => api.post('/trades/run-agent-now')

export const runPaperMonitor = () => api.post('/trades/run-paper-monitor-now')

export default api

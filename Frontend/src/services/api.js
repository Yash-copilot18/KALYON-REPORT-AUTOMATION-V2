// src/services/api.js
import axios from 'axios'
import { withAggregation } from '../utils/intervals'

const api = axios.create({
  baseURL: import.meta.env.VITE_API_URL || 'http://localhost:8000',
  timeout: 30000,
  headers: { 'Content-Type': 'application/json' },
})

api.interceptors.request.use(cfg => {
  const token = localStorage.getItem('ge_token')
  if (token) cfg.headers.Authorization = `Bearer ${token}`
  return cfg
})

api.interceptors.response.use(
  res => res.data,
  async err => {
    let detail = err?.response?.data?.detail
    // Blob responses (e.g. Excel/CSV export) carry their error body as a Blob —
    // read + parse it so the real backend message reaches the user, not a generic one.
    if (!detail && err?.response?.data instanceof Blob) {
      try {
        const text = await err.response.data.text()
        detail = JSON.parse(text)?.detail
      } catch { /* body wasn't JSON */ }
    }
    const msg = detail || err.message || 'API error'
    console.error(`[API Error] ${err?.config?.url} →`, msg)
    return Promise.reject(new Error(msg))
  }
)

export function safeArray(data, keys = ['items','data','rows','alarms','inverters']) {
  if (!data) return []
  if (Array.isArray(data)) return data
  for (const key of keys) {
    if (Array.isArray(data[key])) return data[key]
  }
  return []
}

// ── Health ─────────────────────────────────────────────────────────────────
export const fetchHealth  = () => api.get('/health')
export const fetchDBTest  = () => api.get('/db-test')

// ── Dashboard ──────────────────────────────────────────────────────────────
export const fetchKPIs              = () => api.get('/api/v1/dashboard/kpis')
export const fetchPowerTrend        = (date)     => api.get('/api/v1/dashboard/power-trend',     { params: date ? { date } : {} })
export const fetchDailyEnergy       = (days = 7) => api.get('/api/v1/dashboard/daily-energy',    { params: { days } })
export const fetchMonthlyEnergy     = (year)     => api.get('/api/v1/dashboard/monthly-energy',  { params: year ? { year } : {} })
export const fetchIrradiancePower   = (date)     => api.get('/api/v1/dashboard/irradiance-power',{ params: date ? { date } : {} })
export const fetchDashboardAlarms   = (limit=6)  => api.get('/api/v1/dashboard/alarms',          { params: { limit } })
export const fetchInverterTemps        = ()            => api.get('/api/v1/dashboard/inverter-temps')
export const fetchInverterTempHistory  = (inverter, range = '24h') =>
  api.get('/api/v1/dashboard/inverter-temp-history', { params: { inverter, range } })
export const fetchEquipmentSummary  = ()         => api.get('/api/v1/dashboard/equipment-summary')

// ── Equipment ──────────────────────────────────────────────────────────────
export const fetchEquipment         = ()         => api.get('/api/v1/equipment-live/')
export const fetchEquipmentById     = (id)       => api.get(`/api/v1/equipment-live/inverter/${id}`)
export const fetchEquipmentTypes    = ()         => api.get('/api/v1/equipment/', { params: { page:1, page_size:100 } })
export const createEquipment        = (data)     => api.post('/api/v1/equipment/', data)
export const updateEquipment        = (id, data) => api.put(`/api/v1/equipment/${id}`, data)
export const deleteEquipment        = (id)       => api.delete(`/api/v1/equipment/${id}`)

// ── Analytics ──────────────────────────────────────────────────────────────
// Consolidated analytics dashboard: KPIs + trends + per-inverter ranking for a
// date range, all from real telemetry. Omitted dates default to the latest 30 days.
export const fetchAnalyticsOverview = ({ from, to, equipment } = {}) =>
  api.get('/api/v1/analytics/overview', {
    params: {
      ...(from ? { from_date: from } : {}),
      ...(to ? { to_date: to } : {}),
      ...(equipment && equipment !== 'all' ? { equipment } : {}),
    },
  })

export const fetchAnalyticsPerformance = (days=7) => api.get('/api/v1/analytics/performance',          { params: { days } })
export const fetchInverterComparison   = (date)   => api.get('/api/v1/analytics/inverter-comparison',  { params: date ? { date } : {} })
export const fetchPRIrradiance         = (date)   => api.get('/api/v1/analytics/pr-irradiance',        { params: date ? { date } : {} })
export const fetchWeeklyEnergy         = (weeks=1)=> api.get('/api/v1/analytics/weekly-energy',        { params: { weeks } })
export const fetchTemperature          = (date)   => api.get('/api/v1/analytics/temperature',          { params: date ? { date } : {} })
export const fetchWMSTrend             = (date)   => api.get('/api/v1/analytics/wms-trend',            { params: date ? { date } : {} })

// ── Reports v2 — Real SQL Server ───────────────────────────────────────────
export const fetchReportEquipmentTypes = () =>
  api.get('/api/v1/reports-v2/equipment-types')

export const fetchReportEquipmentList = (type) =>
  api.get('/api/v1/reports-v2/equipment-list', { params: { type } })

export const fetchReportTags = (equipmentType, equipmentId) =>
  api.get('/api/v1/reports-v2/tags', {
    params: { equipment_type: equipmentType, equipment_id: equipmentId }
  })

// Tag availability across a set of equipment (cache-backed) — each tag carries
// available_in/total/available so the UI can flag partially-available tags.
export const fetchTagAvailability = (equipmentType, equipmentIds) =>
  api.post('/api/v1/reports-v2/tags/availability', {
    equipment_type: equipmentType, equipment_ids: equipmentIds,
  })

export const fetchReportDataV2 = (payload) =>
  api.post('/api/v1/reports-v2/data', payload)

// PPC Trend: server-side aggregated time-series. Reuses the reports data endpoint
// (interval + aggregation done in SQL); longer timeout for wide ranges.
export const fetchTrendData = (payload) =>
  api.post('/api/v1/reports-v2/data', payload, { timeout: 120000 })

// Multi-equipment load: bounded-parallel background job + SSE progress.
export const createBatchDataJob = (payload) =>
  api.post('/api/v1/reports-v2/data/batch/async', payload)

export const fetchBatchDataResult = (jobId) =>
  api.get(`/api/v1/reports-v2/data/batch/result/${jobId}`)

// Server-side paginated merged view (infinite scroll). The first page runs as a
// background job (SSE progress via exportProgressUrl → result via fetchBatchDataResult);
// subsequent pages are fetched synchronously as the user scrolls.
export const createMergedFirstJob = (payload) =>
  api.post('/api/v1/reports-v2/data/merged/first', payload)

export const fetchMergedPage = (payload) =>
  api.post('/api/v1/reports-v2/data/merged/page', payload)

// Exports can be large (multi-inverter workbooks) — allow well beyond the 30s
// default. 10 minutes for very large solar-plant exports (normal loads stay fast).
const EXPORT_TIMEOUT = 600000

export const exportReportCSVV2 = (payload) =>
  api.post('/api/v1/reports-v2/export/csv', payload, { responseType: 'blob', timeout: EXPORT_TIMEOUT })

export const exportReportExcelV2 = (payload) =>
  api.post('/api/v1/reports-v2/export/excel', payload, { responseType: 'blob', timeout: EXPORT_TIMEOUT })

// ── Async batch export (job + SSE progress + download) ──────────────────────
export const createExcelExportJob = (payload) =>
  api.post('/api/v1/reports-v2/export/excel/async', payload)

export const downloadExcelExportJob = (jobId) =>
  api.get(`/api/v1/reports-v2/export/download/${jobId}`, { responseType: 'blob', timeout: EXPORT_TIMEOUT })

// Full URL for an EventSource (SSE) progress stream.
export const exportProgressUrl = (jobId) =>
  `${api.defaults.baseURL}/api/v1/reports-v2/export/progress/${jobId}`

// ── Single-request streaming export (T1 Isolation) ──────────────────────────
// Prepare stashes the request server-side and returns a job id; the browser then
// downloads the streaming GET URL natively (straight to disk — never buffered in
// JS memory) while the same job feeds the SSE progress stream above.
export const prepareExcelStreamJob = (payload) =>
  api.post('/api/v1/reports-v2/export/excel/stream/prepare', payload)

export const exportStreamUrl = (jobId) =>
  `${api.defaults.baseURL}/api/v1/reports-v2/export/excel/stream/${jobId}`

// Direct-to-Downloads export (T1/T2 Isolation): the backend builds each device
// report in parallel and saves each .xlsx into the Downloads folder as it finishes
// (no ZIP, no browser download). Returns a job id; progress via exportProgressUrl.
export const startExcelToDownloads = (payload) =>
  api.post('/api/v1/reports-v2/export/excel/to-downloads', payload)

// Folder exports (NEW, additive): one Excel file per unit, organised into folders on
// the server's disk. Progress is reported over the same SSE endpoint (exportProgressUrl).
//   Tracker → <TRACKER_EXPORT_DIR>\Tracker{n}\Tags_{a}_{b}.xlsx
//   SMB     → <SMB_EXPORT_DIR>\INV{n}\SCB{k}.xlsx
export const startTrackerFolderExport = (payload) =>
  api.post('/api/v1/reports-v2/export/trackers/to-folders', payload)

export const startSmbFolderExport = (payload) =>
  api.post('/api/v1/reports-v2/export/smb/to-folders', payload)

export const fetchReportSummary = (payload) =>
  api.post('/api/v1/reports-v2/summary', payload)

// ── Tracker module (dbo.T1_IS1) ─────────────────────────────────────────────
export const fetchTrackerIds  = () => api.get('/api/v1/tracker/ids')

export const fetchTrackerData = (payload) =>
  api.post('/api/v1/tracker/data', payload)

// Export reuses the shared job progress (SSE) + download endpoints.
export const createTrackerExportJob = (payload) =>
  api.post('/api/v1/tracker/export/excel/async', payload)

// ── Live Tracker Status (dbo.T1_IS*) ────────────────────────────────────────
export const fetchTrackerStatus = (table) =>
  api.get(`/api/v1/isolator/${table}/status`)

export const fetchTrackerKpis = (table) =>
  api.get(`/api/v1/isolator/${table}/kpis`)

export const fetchTrackerTrend = (table, params = {}) =>
  api.get(`/api/v1/isolator/${table}/trend`, { params })

// WebSocket URL for live snapshot push (http→ws).
export const trackerStatusSocketUrl = (table) =>
  `${api.defaults.baseURL.replace(/^http/, 'ws')}/api/v1/isolator/${table}/ws`

// ── Reports (legacy aliases) ───────────────────────────────────────────────
export const fetchAvailableTags = () =>
  api.get('/api/v1/reports-v2/tags?equipment_type=Inverter&equipment_id=INVERTER_01')

export const fetchReportData = (payload) =>
  api.post('/api/v1/reports-v2/data', payload || withAggregation({
    equipment_type: 'PPC',
    equipment_id:   'PPC',
    tags:           ['GRID_ACTIVE_POWER_MEASURED', 'INVERTER_TOTAL_ACTIVE_POWER'],
    from_datetime:  '2024-02-08T00:00:00',
    to_datetime:    '2024-02-08T23:59:59',
    page:           1,
    page_size:      50,
  }, 'hourly'))

export const exportReportCSV   = (payload) => exportReportCSVV2(payload)
export const exportReportExcel = (payload) => exportReportExcelV2(payload)
export const generateReport    = (payload) => fetchReportDataV2(payload)

export const fetchInverterSummary = (inverterNo, fromDt, toDt) =>
  api.get(`/api/v1/reports/inverter/${inverterNo}/summary`, {
    params: { from_dt: fromDt, to_dt: toDt }
  })

// ── WMS / PPC ──────────────────────────────────────────────────────────────
export const fetchWMSLatest = () => api.get('/api/v1/reports/wms/latest')
export const fetchPPCLatest = () => api.get('/api/v1/reports/ppc/latest')

// ── DGR ───────────────────────────────────────────────────────────────────
// Daily Generation Report — real plant-meter telemetry from the PPC table,
// aggregated server-side into the requested interval buckets.
//   PLANT_DAILY_PRODUCTION : cumulative MWh counter, resets at midnight, so the
//                            bucket MAX is the meter reading at the bucket's end
//                            and consecutive deltas give the energy per interval.
//   GRID_ACTIVE_POWER_MEASURED : MW; the bucket MAX is that interval's peak power.
export const DGR_TAGS = {
  energy: 'PLANT_DAILY_PRODUCTION',
  power:  'GRID_ACTIVE_POWER_MEASURED',
}
// On a 1-minute (instant) interval withAggregation() omits `agg_function` and the
// backend returns raw records. On coarser intervals the counter must be read with
// MAX — the bucket's closing meter value. AVG of a cumulative counter is physically
// meaningless, so this aggregation is fixed by the data model, not user-selectable
// (the DGR page has no Aggregation dropdown).
export const fetchDGRSeries = (date, interval) =>
  api.post('/api/v1/reports-v2/data', withAggregation({
    equipment_type: 'PPC',
    equipment_id:   'PPC',
    tags:           [DGR_TAGS.energy, DGR_TAGS.power],
    from_datetime:  `${date}T00:00:00`,
    to_datetime:    `${date}T23:59:59`,
    page:           1,
    page_size:      2000,   // 1-minute buckets over a full day = 1440 rows
  }, interval, 'max'))

// Inverter-wise generation for the DGR chart, over the SAME date + interval the
// DGR filters apply. INVERTER_xx_GEN are daily-resetting cumulative kWh counters,
// so — exactly like the plant meter above — the bucket MAX is the closing reading
// and consecutive deltas give the kWh generated inside each interval. `tags` is the
// column list discovered from the database (never a hardcoded inverter list).
export const fetchDGRInverterSeries = (date, interval, tags) =>
  api.post('/api/v1/reports-v2/data', withAggregation({
    equipment_type: 'Daily Generation',
    equipment_id:   'INVERTER_DAILY_GEN',
    tags,
    from_datetime:  `${date}T00:00:00`,
    to_datetime:    `${date}T23:59:59`,
    page:           1,
    page_size:      2000,   // 1-minute buckets over a full day = 1440 rows
  }, interval, 'max'))

// Per-inverter active power (kW) from [dbo].[POWER_GRAPH], over the same date +
// interval. Read with MAX so each bucket carries that interval's peak; the day's
// peak per inverter is the maximum across buckets.
export const fetchDGRInverterPower = (date, interval, tags) =>
  api.post('/api/v1/reports-v2/data', withAggregation({
    equipment_type: 'Power Graph',
    equipment_id:   'POWER_GRAPH',
    tags,
    from_datetime:  `${date}T00:00:00`,
    to_datetime:    `${date}T23:59:59`,
    page:           1,
    page_size:      2000,
  }, interval, 'max'))

// ── Multi-equipment report (Preconfigured Reports) ─────────────────────────
// One timestamp-aligned dataset spanning several equipment types. `sources` is
// [{ equipment_type, equipment_id, tags[] }]; column order follows the user's
// selection order. Excel is the only export offered for these reports.
export const fetchMultiReportData = (payload) =>
  api.post('/api/v1/reports-v2/multi/data', payload)

export const exportMultiReportExcel = (payload) =>
  api.post('/api/v1/reports-v2/multi/export/excel', payload, {
    responseType: 'blob', timeout: 300000,
  })

// ── MGR ───────────────────────────────────────────────────────────────────
// Monthly Generation Report — aggregated in SQL from [dbo].[INVERTER_DAILY_GEN].
// One request returns both views the page needs:
//   • inverters : [{ inverter: 'INVERTER_01', generation }]  — kWh for the month
//   • daily     : [{ date, day, generation }]                — plant kWh per day
export const fetchMGRMonthlyGeneration = (month, year) =>
  api.get('/api/v1/reports-v2/mgr/monthly-generation', { params: { month, year } })

// Year/month combinations that actually carry generation data — drives the MGR
// month dropdown so it only ever offers periods that exist in the database.
export const fetchMGRPeriods = () =>
  api.get('/api/v1/reports-v2/mgr/periods')

// ── YGR ───────────────────────────────────────────────────────────────────
// Yearly Generation Report — aggregated in SQL from [dbo].[PPC] (energy + peak)
// and [dbo].[WMS] (insolation, for PR). The year list is the set of years that
// actually carry data, so the dropdown can never offer an empty year.
export const fetchYGRYears = () =>
  api.get('/api/v1/reports-v2/ygr/years')

export const fetchYGRYearlyGeneration = (year) =>
  api.get('/api/v1/reports-v2/ygr/yearly-generation', { params: { year } })

export const fetchDGRData = () =>
  api.post('/api/v1/reports-v2/data', {
    equipment_type: 'Daily Generation',
    equipment_id:   'INVERTER_DAILY_GEN',
    tags: [
      'INVERTER_01_GEN','INVERTER_02_GEN','INVERTER_03_GEN',
      'INVERTER_04_GEN','INVERTER_05_GEN','INVERTER_06_GEN',
    ],
    from_datetime: '2024-02-08T00:00:00',
    to_datetime:   '2024-05-21T23:59:59',
    interval:      'daily',
    agg_function:  'sum',
    page:          1,
    page_size:     30,
  })

export const fetchDailyGeneration = (fromDt, toDt) =>
  api.post('/api/v1/reports-v2/data', {
    equipment_type: 'Daily Generation',
    equipment_id:   'INVERTER_DAILY_GEN',
    tags: [
      'INVERTER_01_GEN','INVERTER_02_GEN','INVERTER_03_GEN',
      'INVERTER_04_GEN','INVERTER_05_GEN','INVERTER_06_GEN',
    ],
    from_datetime: fromDt || '2024-02-08T00:00:00',
    to_datetime:   toDt   || '2024-05-21T23:59:59',
    interval:      'daily',
    agg_function:  'sum',
    page:          1,
    page_size:     100,
  })

export const fetchMonthlyGeneration = (fromDt, toDt) =>
  api.post('/api/v1/reports-v2/data', {
    equipment_type: 'Monthly Generation',
    equipment_id:   'INVERTER_MONTHLY_GEN',
    tags: [
      'INVERTER_01_GEN','INVERTER_02_GEN','INVERTER_03_GEN',
      'INVERTER_04_GEN','INVERTER_05_GEN','INVERTER_06_GEN',
    ],
    from_datetime: fromDt || '2024-02-08T00:00:00',
    to_datetime:   toDt   || '2024-05-21T23:59:59',
    interval:      'monthly',
    agg_function:  'sum',
    page:          1,
    page_size:     12,
  })

// ── Alarms ─────────────────────────────────────────────────────────────────
export const fetchAlarms = () =>
  api.get('/api/v1/dashboard/alarms', { params: { limit: 50 } })
    .then(res => res?.alarms || [])

// ── Scheduled Reports ──────────────────────────────────────────────────────
export const fetchScheduledReports = () =>
  Promise.resolve([
    { id:1, name:'Daily Generation Report',   freq:'Daily 06:00', next:'2024-02-09', status:'Active', email:'ops@ge.com'        },
    { id:2, name:'Weekly Performance Report', freq:'Mon 07:00',   next:'2024-02-12', status:'Active', email:'management@ge.com' },
    { id:3, name:'Monthly KPI Report',        freq:'1st 08:00',   next:'2024-03-01', status:'Active', email:'ceo@ge.com'        },
    { id:4, name:'Alarm Summary Report',      freq:'Daily 20:00', next:'2024-02-08', status:'Paused', email:'ops@ge.com'        },
    { id:5, name:'Equipment Health Report',   freq:'Weekly Mon',  next:'2024-02-12', status:'Active', email:'eng@ge.com'        },
  ])

export const saveScheduledReport   = (data) => Promise.resolve({ ...data, id: data.id || Date.now() })
export const deleteScheduledReport = (id)   => Promise.resolve({ id })
export const runReport             = (id)   => Promise.resolve({ id, status: 'running' })

// Scheduled-report e-mail (testing phase) — fixed recipient configured in backend .env
export const fetchScheduledEmailConfig = () =>
  api.get('/api/v1/scheduled/config')

export const sendScheduledEmail = (payload) =>
  api.post('/api/v1/scheduled/send', payload)

export const sendTestEmail = (payload = {}) =>
  api.post('/api/v1/scheduled/test-email', payload)

// ── Scheduled reports — database-backed CRUD + actions ──────────────────────
export const fetchSchedules = () =>
  api.get('/api/v1/scheduled/schedules')

export const createSchedule = (payload) =>
  api.post('/api/v1/scheduled/schedules', payload)

export const updateSchedule = (id, payload) =>
  api.put(`/api/v1/scheduled/schedules/${id}`, payload)

export const deleteSchedule = (id) =>
  api.delete(`/api/v1/scheduled/schedules/${id}`)

export const runSchedule = (id) =>
  api.post(`/api/v1/scheduled/schedules/${id}/run`)

export const pauseSchedule = (id) =>
  api.post(`/api/v1/scheduled/schedules/${id}/pause`)

export const resumeSchedule = (id) =>
  api.post(`/api/v1/scheduled/schedules/${id}/resume`)

export const fetchScheduleRuns = (id) =>
  api.get(`/api/v1/scheduled/schedules/${id}/runs`)

// ── Users ──────────────────────────────────────────────────────────────────
export const fetchUsers = () =>
  Promise.resolve([
    { id:1, name:'Ahmad Khalid',     email:'a.khalid@ge.com',      role:'Admin',    dept:'Operations', last:'2 min ago', active:true  },
    { id:2, name:'Sara Al-Rashid',   email:'s.rashid@ge.com',      role:'Engineer', dept:'Maintenance',last:'1 hr ago',  active:true  },
    { id:3, name:'Mohammed Hassan',  email:'m.hassan@ge.com',      role:'Analyst',  dept:'Analytics',  last:'3 hrs ago', active:false },
    { id:4, name:'Emma Clarke',      email:'e.clarke@ge.com',      role:'Manager',  dept:'Management', last:'1 day ago', active:false },
    { id:5, name:'Ravi Subramaniam', email:'r.subramaniam@ge.com', role:'Engineer', dept:'Monitoring', last:'5 hrs ago', active:true  },
  ])

export const createUser = (data)      => Promise.resolve({ ...data, id: Date.now() })
export const updateUser = (id, data)  => Promise.resolve({ id, ...data })
export const deleteUser = (id)        => Promise.resolve({ id })

export const fetchDBHealth  = () => api.get('/db-test')
export const fetchPlantInfo = () => api.get('/health')

export default api
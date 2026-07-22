import React, { useState } from 'react'
import { KpiCard, PageHeader, AsyncButton, SearchInput, Skeleton } from '../../components/Common'
import { useFetch } from '../../hooks/useFetch'
import { fetchAlarms } from '../../services/api'
import { severityPillClass } from '../../utils/helpers'
import { useApp } from '../../utils/AppContext'

const FALLBACK_ALARMS = [
  { id:'ALM-2847', eq:'INV-003', type:'Temperature', sev:'Warning',  msg:'Module temperature exceeded 40°C threshold',          time:'13:24:15', ack:false },
  { id:'ALM-2846', eq:'SCB-014', type:'Fault',       sev:'Critical', msg:'String current below minimum — shading or failure',   time:'12:58:02', ack:false },
  { id:'ALM-2845', eq:'WS-001',  type:'Info',        sev:'Info',     msg:'Irradiance sensor calibration drift detected',         time:'11:30:47', ack:true  },
  { id:'ALM-2844', eq:'INV-001', type:'Warning',     sev:'Warning',  msg:'AC power output variance >5% from expected',          time:'10:15:33', ack:true  },
  { id:'ALM-2843', eq:'TRF-001', type:'Info',        sev:'Info',     msg:'Scheduled maintenance window reminder',                time:'08:00:00', ack:true  },
  { id:'ALM-2842', eq:'INV-006', type:'Warning',     sev:'Warning',  msg:'DC voltage drop detected — string inspection needed', time:'07:42:11', ack:true  },
]

function normalizeAlarms(raw) {
  if (!raw) return FALLBACK_ALARMS
  // API returns array directly (fetchAlarms does .then(res => res.rows || []))
  if (Array.isArray(raw)) {
    if (raw.length === 0) return FALLBACK_ALARMS
    // Map API alarm shape to UI shape
    return raw.map((a, i) => ({
      id:   a.uni_id ? `ALM-${a.uni_id}` : `ALM-${i}`,
      eq:   a.eq || 'Unknown',
      type: a.event || a.type || 'Info',
      sev:  a.sev || (a.event === 'Alarm' ? 'Warning' : 'Info'),
      msg:  a.description || a.msg || a.ev_description || '—',
      time: a.timestamp ? new Date(a.timestamp).toLocaleTimeString() : '—',
      ack:  a.ack || false,
    }))
  }
  // If rows shape
  if (raw.rows && Array.isArray(raw.rows)) {
    if (raw.rows.length === 0) return FALLBACK_ALARMS
    return raw.rows.map((a, i) => ({
      id:   `ALM-${i + 1}`,
      eq:   'Plant',
      type: a.event || 'Info',
      sev:  a.event === 'Alarm' ? 'Warning' : 'Info',
      msg:  a.description || a.ev_description || '—',
      time: a.timestamp ? new Date(a.timestamp).toLocaleTimeString() : '—',
      ack:  false,
    }))
  }
  return FALLBACK_ALARMS
}

export default function Alarms() {
  const { showToast } = useApp()
  const { data: rawData, loading } = useFetch(fetchAlarms)
  const [search,  setSearch]  = useState('')
  const [filter,  setFilter]  = useState('All')
  const [acked,   setAcked]   = useState(new Set())

  const alarmData = normalizeAlarms(rawData)

  const rows = alarmData.filter(a => {
    const matchSearch = !search || [a.id, a.eq, a.msg, a.type].some(
      v => String(v).toLowerCase().includes(search.toLowerCase())
    )
    const matchFilter = filter === 'All' ||
      (filter === 'Unacknowledged' && !acked.has(a.id) && !a.ack) ||
      (filter === 'Acknowledged'   && (acked.has(a.id) || a.ack)) ||
      a.sev === filter
    return matchSearch && matchFilter
  })

  const handleAck = id => {
    setAcked(prev => new Set([...prev, id]))
    showToast(`Alarm ${id} acknowledged`)
  }

  const handleAckAll = () => {
    setAcked(new Set(alarmData.map(a => a.id)))
    showToast('All alarms acknowledged')
  }

  const counts = alarmData.reduce((acc, a) => {
    acc[a.sev] = (acc[a.sev] || 0) + 1
    return acc
  }, {})

  return (
    <div>
      <PageHeader title="Alarm Management" subtitle="Active and historical plant alarms">
        <AsyncButton className="btn btn-outline btn-sm" successMsg="Alarm report exported!">
          ⬇ Export Alarms
        </AsyncButton>
        <button className="btn btn-outline btn-sm" onClick={handleAckAll}>
          ✓ Acknowledge All
        </button>
      </PageHeader>

      {/* KPI Counts */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3 mb-4">
        <KpiCard label="Critical"     value={counts.Critical || 0} color="red"   />
        <KpiCard label="Warning"      value={counts.Warning  || 0} color="amber" />
        <KpiCard label="Info"         value={counts.Info     || 0} color="blue"  />
        <KpiCard label="Acknowledged" value={alarmData.filter(a => acked.has(a.id) || a.ack).length} color="green" />
      </div>

      {/* Alarm Table */}
      <div className="card">
        <div className="card-title justify-between">
          <span>Active &amp; Recent Alarms</span>
          <div className="flex items-center gap-2 flex-wrap">
            {['All','Critical','Warning','Info','Unacknowledged','Acknowledged'].map(f => (
              <button key={f} onClick={() => setFilter(f)}
                className={`btn btn-sm ${filter === f ? 'btn-primary' : 'btn-outline'}`}>
                {f}
              </button>
            ))}
            <SearchInput value={search} onChange={setSearch} placeholder="Search..." className="w-40" />
          </div>
        </div>

        {loading ? (
          <div className="space-y-1">{[...Array(5)].map((_, i) => <Skeleton key={i} h="h-10" />)}</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Alarm ID</th><th>Equipment</th><th>Type</th><th>Severity</th>
                  <th>Message</th><th>Time</th><th>Status</th><th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {rows.length === 0
                  ? <tr><td colSpan={8} className="text-center text-ge-text3 py-8">No alarms match filter</td></tr>
                  : rows.map(a => {
                    const isAcked = acked.has(a.id) || a.ack
                    return (
                      <tr key={a.id}>
                        <td className="font-mono text-[11px]">{a.id}</td>
                        <td className="font-medium text-ge-text1">{a.eq}</td>
                        <td>{a.type}</td>
                        <td><span className={severityPillClass(a.sev)}>{a.sev}</span></td>
                        <td className="max-w-xs">
                          <span className="block truncate text-ge-text2">{a.msg}</span>
                        </td>
                        <td className="font-mono text-[11px]">{a.time}</td>
                        <td>
                          <span className={`status-pill ${isAcked ? 'pill-green' : 'pill-amber'}`}>
                            {isAcked ? 'Acknowledged' : 'Pending'}
                          </span>
                        </td>
                        <td>
                          {!isAcked
                            ? <button className="btn btn-outline btn-sm" onClick={() => handleAck(a.id)}>✓ Ack</button>
                            : <span className="text-[11px] text-ge-text3">Done</span>
                          }
                        </td>
                      </tr>
                    )
                  })
                }
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
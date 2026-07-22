import React, { useState, useMemo } from 'react'
import {
  KpiCard, PageHeader, DataTable, SearchInput, AsyncButton, Skeleton
} from '../../components/Common'
import { useFetch } from '../../hooks/useFetch'
import { fetchEquipment } from '../../services/api'
import { statusPillClass } from '../../utils/helpers'

const COLS = [
  { key: 'id',     label: 'Equipment ID', className: 'font-medium text-ge-text1' },
  { key: 'type',   label: 'Type' },
  { key: 'power',  label: 'Active Power',
    render: v => <span className="font-mono text-ge-accent">{v}</span> },
  { key: 'temp',   label: 'Temperature',
    render: v => <span className="font-mono">{v}</span> },
  { key: 'eff',    label: 'Efficiency',
    render: v => <span className="font-mono">{v}</span> },
  { key: 'status', label: 'Status',
    render: v => <span className={statusPillClass(v)}>{v}</span> },
  { key: 'id',     label: 'Actions',
    render: (v, row) => (
      <AsyncButton
        className="btn btn-outline btn-sm"
        successMsg={`Opening ${row.id} details`}
      >
        View
      </AsyncButton>
    )
  },
]

export default function Equipment() {
  const { data: rawData, loading, error } = useFetch(fetchEquipment)
  const [search, setSearch] = useState('')
  const [filter, setFilter] = useState('All')

  const allData = useMemo(() => {
    if (!rawData) return []
    // API returns { total, online, warning, fault, items: [...] }
    const items = Array.isArray(rawData.items) ? rawData.items
      : Array.isArray(rawData) ? rawData
      : []
    return items
  }, [rawData])

  const counts = useMemo(() => ({
    total:   rawData?.total   || allData.length,
    online:  rawData?.online  || allData.filter(r => r.status === 'Online').length,
    warning: rawData?.warning || allData.filter(r => r.status === 'Warning').length,
    fault:   rawData?.fault   || allData.filter(r => r.status === 'Fault').length,
  }), [rawData, allData])

  const rows = useMemo(() =>
    allData.filter(r => {
      const matchSearch = !search ||
        Object.values(r).some(v =>
          String(v).toLowerCase().includes(search.toLowerCase())
        )
      const matchFilter = filter === 'All' || r.status === filter
      return matchSearch && matchFilter
    }),
    [allData, search, filter]
  )

  return (
    <div>
      <PageHeader title="Equipment Monitoring" subtitle="Real-time status of all plant equipment">
        <AsyncButton className="btn btn-outline btn-sm" successMsg="Report exported!">
          ⬇ Export Report
        </AsyncButton>
      </PageHeader>

      {/* KPIs */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3 mb-4">
        <KpiCard label="Online"         value={loading ? '…' : `${counts.online}/${counts.total}`} color="green"  />
        <KpiCard label="Warnings"       value={loading ? '…' : counts.warning} color="amber"  />
        <KpiCard label="Faults"         value={loading ? '…' : counts.fault}   color="red"    />
        <KpiCard label="Total Capacity" value="2,000" unit="MW" color="blue" />
      </div>

      {/* Error state */}
      {error && (
        <div className="card mb-3 border-ge-danger/40 bg-red-950/20">
          <p className="text-[12px] text-ge-danger">⚠ Failed to load equipment: {error}</p>
        </div>
      )}

      {/* Table */}
      <div className="card">
        <div className="card-title justify-between">
          <span>Equipment Status Board</span>
          <div className="flex items-center gap-2 flex-wrap">
            {['All','Online','Warning','Fault','Offline'].map(f => (
              <button key={f} onClick={() => setFilter(f)}
                className={`btn btn-sm ${filter === f ? 'btn-primary' : 'btn-outline'}`}>
                {f}
              </button>
            ))}
            <SearchInput value={search} onChange={setSearch} placeholder="Search..." className="w-40" />
          </div>
        </div>

        {loading ? (
          <div className="space-y-1">
            {[...Array(10)].map((_, i) => <Skeleton key={i} h="h-8" />)}
          </div>
        ) : (
          <DataTable
            columns={COLS}
            rows={rows}
            emptyMsg="No equipment matches filter"
          />
        )}
      </div>
    </div>
  )
}
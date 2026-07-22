import React from 'react'
import { KpiCard, PageHeader, Skeleton } from '../../components/Common'
import { PowerTrendChart, DailyEnergyChart, IrradiancePowerChart } from '../../components/Charts'
import InverterTempPanel from './InverterTempPanel'
import { useDashboardData, SHOW_INVERTER_TEMPS } from './useDashboardData'

// ── KPI grid layout — every value is sourced live from the backend ───────────
const KPI_LAYOUT = [
  { key: 'today_energy',      color: 'green'  },
  { key: 'current_power',     color: 'blue'   },
  { key: 'performance_ratio', color: 'amber'  },
  { key: 'availability',      color: 'purple' },
  { key: 'monthly_energy',    color: 'blue'   },
  { key: 'inverters_running', color: 'green'  },
  { key: 'grid_frequency',    color: 'amber'  },
  { key: 'module_temp',       color: 'purple' },
]

// Unavailable values render as "--" (never a fake 0).
const fmt = (v) =>
  v === null || v === undefined || v === ''
    ? '--'
    : typeof v === 'number'
      ? v.toLocaleString(undefined, { maximumFractionDigits: 3 })
      : String(v)

// Compact chart plot height (~208px) — trimmed from h-56 and paired with tighter
// card padding/title margin below so charts fit above the fold. `!` overrides the
// shared .card padding for the Dashboard's chart cards only (KPI cards untouched).
const CHART_H = 'h-52'

// ── Reusable chart card: consistent loading / empty / error handling ─────────
const ChartCard = React.memo(function ChartCard({
  title, loading, error, isEmpty, height = CHART_H, className = '', action = null, children,
}) {
  return (
    <div className={`card !p-3 ${className}`}>
      <div className="card-title !mb-2 justify-between">
        <span>{title}</span>
        {action}
      </div>
      <div className={height}>
        {loading ? (
          <Skeleton h="h-full" />
        ) : error ? (
          <div className="h-full flex items-center justify-center text-[12px] text-red-400">
            Failed to load data
          </div>
        ) : isEmpty ? (
          <div className="h-full flex items-center justify-center text-[12px] text-ge-text3">
            No data available
          </div>
        ) : (
          children
        )}
      </div>
    </div>
  )
})

// ── KPI grid ──────────────────────────────────────────────────────────────
const KpiGrid = React.memo(function KpiGrid({ kpis, loading }) {
  return (
    <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3 mb-4">
      {KPI_LAYOUT.map(({ key, color }) => {
        const kpi = kpis[key] || {}
        return loading ? (
          <Skeleton key={key} h="h-24" className="rounded-lg" />
        ) : (
          <KpiCard
            key={key}
            label={kpi.label || key}
            value={fmt(kpi.value)}
            unit={kpi.unit || ''}
            change={kpi.change || ''}
            up={kpi.up ?? true}
            color={color}
          />
        )
      })}
    </div>
  )
})

export default function Dashboard() {
  // The live-status badge and "Updated HH:MM:SS" stamp are intentionally not
  // rendered. Polling is unchanged — useDashboardData still refreshes KPIs and
  // charts on their intervals; only the status indicators are hidden.
  const {
    kpis, kpiState, power, daily, irradiance, invTemps,
  } = useDashboardData()

  return (
    <div>
      <PageHeader
        title="Operations Dashboard"
        subtitle="Real-time plant monitoring"
      />

      {/* KPIs — single responsive grid, all values live from backend */}
      <KpiGrid kpis={kpis} loading={kpiState.loading} />

      {/* Charts row 1 */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-3 mb-3">
        <ChartCard
          className="lg:col-span-2"
          title="⚡ Power Generation Trend (MW)"
          loading={power.loading}
          error={power.error}
          isEmpty={!power.data.length}
        >
          <PowerTrendChart data={power.data} />
        </ChartCard>

        <ChartCard
          title="☀ Irradiance vs Power"
          loading={irradiance.loading}
          error={irradiance.error}
          isEmpty={!irradiance.data.length}
        >
          <IrradiancePowerChart data={irradiance.data} />
        </ChartCard>
      </div>

      {/* Charts row 2 */}
      <div className="grid grid-cols-1 gap-3">
        <ChartCard
          title="📊 Daily Energy (MWh) — Last 7 Days"
          loading={daily.loading}
          error={daily.error}
          isEmpty={!daily.data.length}
        >
          <DailyEnergyChart data={daily.data} />
        </ChartCard>
      </div>

      {/* Inverter temperatures — hidden (toggle SHOW_INVERTER_TEMPS to restore) */}
      {SHOW_INVERTER_TEMPS && (
        <div className="mt-3">
          <InverterTempPanel invTemps={invTemps} />
        </div>
      )}
    </div>
  )
}

// src/pages/Dashboard/useDashboardData.js
//
// Business-logic layer for the Operations Dashboard.
// Composes the independent dashboard API calls (fetched in parallel), applies
// silent auto-refresh, and exposes normalized, memoized view-models to the UI.
// Keeps the page component purely presentational.

import { useMemo, useCallback } from 'react'
import { useFetch } from '../../hooks/useFetch'
import {
  fetchKPIs, fetchPowerTrend, fetchDailyEnergy, fetchIrradiancePower, fetchInverterTemps,
} from '../../services/api'

// Refresh cadences (ms). KPIs are near-real-time (5–10 s); charts change slowly;
// inverter temps drift slowly so they poll less often to avoid unnecessary calls.
// `useFetch` pauses all polling while the tab is hidden.
const REFRESH = { KPIS: 8_000, CHARTS: 60_000, TEMPS: 30_000 }

// Inverter Temperature panel is hidden from the Dashboard. The component and its
// data wiring are intentionally preserved — flip this to true to restore it.
// While false the temps endpoint is not polled at all (no wasted API calls).
export const SHOW_INVERTER_TEMPS = false

export function useDashboardData() {
  // Independent sources → fetched concurrently, each self-refreshing.
  const kpi   = useFetch(fetchKPIs,                [], { refetchInterval: REFRESH.KPIS })
  const power = useFetch(fetchPowerTrend,          [], { refetchInterval: REFRESH.CHARTS })
  const daily = useFetch(() => fetchDailyEnergy(7),[], { refetchInterval: REFRESH.CHARTS })
  const irr   = useFetch(fetchIrradiancePower,     [], { refetchInterval: REFRESH.CHARTS })
  const temps = useFetch(fetchInverterTemps,       [], {
    refetchInterval: REFRESH.TEMPS, enabled: SHOW_INVERTER_TEMPS,
  })

  const kpis = kpi.data || {}

  // ── Normalized chart series (no mock fallbacks — real data or empty) ──────
  const powerSeries = useMemo(
    () => (power.data?.data ?? []).map(r => ({ hour: r.hour, power: r.power })),
    [power.data],
  )
  const dailySeries = useMemo(
    () => (daily.data?.data ?? []).map(r => ({ label: r.label, value: r.energy })),
    [daily.data],
  )
  const irrSeries = useMemo(
    () => (irr.data?.data ?? []).filter(p => p && (p.irr || p.pwr)),
    [irr.data],
  )

  const refetchAll = useCallback(() => {
    kpi.refetch(); power.refetch(); daily.refetch(); irr.refetch(); temps.refetch()
  }, [kpi, power, daily, irr, temps])

  const refreshing =
    kpi.refreshing || power.refreshing || daily.refreshing || irr.refreshing || temps.refreshing

  return {
    kpis,
    kpiState:   { loading: kpi.loading, error: kpi.error },
    power:      { data: powerSeries, loading: power.loading, error: power.error },
    daily:      { data: dailySeries, loading: daily.loading, error: daily.error },
    irradiance: { data: irrSeries,   loading: irr.loading,   error: irr.error   },
    invTemps:   {
      data: temps.data, loading: temps.loading, error: temps.error,
      lastUpdated: temps.lastUpdated, refetch: temps.refetch,
    },
    lastUpdated: kpi.lastUpdated,
    refreshing,
    refetchAll,
  }
}

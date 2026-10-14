import React, { Suspense, lazy } from 'react'
import { Routes, Route, Navigate } from 'react-router-dom'
import { Spinner } from '../components/Common'

// Every page is code-split. Previously all fifteen were imported eagerly, so opening
// the Dashboard also downloaded Analytics' chart library, the Excel writer and the
// PDF stack — one 1.29 MB bundle before anything could render. Each page now arrives
// only when its route is first visited; the heavy dependencies (Recharts, jsPDF,
// html2canvas) travel with the pages that actually use them.
//
// Dashboard is the landing route, so it is NOT lazy: splitting it would only add a
// round trip before the first paint.
import Dashboard from '../pages/Dashboard/Dashboard'

const Reports       = lazy(() => import('../pages/Reports/Reports'))
const Tracker       = lazy(() => import('../pages/Tracker/Tracker'))
const TrackerStatus = lazy(() => import('../pages/TrackerStatus/TrackerStatus'))
const DGRReports    = lazy(() => import('../pages/DGRReports/DGRReports'))
const Analytics     = lazy(() => import('../pages/Analytics/Analytics'))
const Preconfigured = lazy(() => import('../pages/Preconfigured/Preconfigured'))
const AIReport      = lazy(() => import('../pages/AIReport/AIReport'))
const PPCTrend      = lazy(() => import('../pages/PPCTrend/PPCTrend'))
const PowerGraph    = lazy(() => import('../pages/PowerGraph/PowerGraph'))
const Equipment     = lazy(() => import('../pages/Equipment/Equipment'))
const Alarms        = lazy(() => import('../pages/Alarms/Alarms'))
const Scheduled     = lazy(() => import('../pages/Scheduled/Scheduled'))
const Users         = lazy(() => import('../pages/Users/Users'))
const Settings      = lazy(() => import('../pages/Settings/Settings'))

/** Shown for the moment a page's chunk is in flight. */
function PageLoading() {
  return (
    <div className="flex items-center justify-center gap-2 py-20 text-ge-text3 text-[12px]">
      <Spinner size={14} /> Loading…
    </div>
  )
}

export default function AppRoutes() {
  return (
    <Suspense fallback={<PageLoading />}>
      <Routes>
        <Route path="/"           element={<Navigate to="/dashboard" replace />} />
        <Route path="/dashboard"  element={<Dashboard />} />
        <Route path="/reports"    element={<Reports />} />
        <Route path="/tracker"    element={<Tracker />} />
        <Route path="/tracker-status" element={<TrackerStatus />} />
        <Route path="/dgr"        element={<DGRReports />} />
        <Route path="/analytics"  element={<Analytics />} />
        <Route path="/preconfigured" element={<Preconfigured />} />
        <Route path="/ai-report"  element={<AIReport />} />
        <Route path="/ppc-trend"  element={<PPCTrend />} />
        <Route path="/power-graph" element={<PowerGraph />} />
        <Route path="/equipment"  element={<Equipment />} />
        <Route path="/alarms"     element={<Alarms />} />
        <Route path="/scheduled"  element={<Scheduled />} />
        <Route path="/users"      element={<Users />} />
        <Route path="/settings"   element={<Settings />} />
        <Route path="*"           element={<Navigate to="/dashboard" replace />} />
      </Routes>
    </Suspense>
  )
}

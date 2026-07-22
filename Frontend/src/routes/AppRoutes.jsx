import React from 'react'
import { Routes, Route, Navigate } from 'react-router-dom'
import Dashboard  from '../pages/Dashboard/Dashboard'
import Reports    from '../pages/Reports/Reports'
import Tracker    from '../pages/Tracker/Tracker'
import TrackerStatus from '../pages/TrackerStatus/TrackerStatus'
import DGRReports from '../pages/DGRReports/DGRReports'
import Analytics  from '../pages/Analytics/Analytics'
import Preconfigured from '../pages/Preconfigured/Preconfigured'
import PPCTrend   from '../pages/PPCTrend/PPCTrend'
import PowerGraph from '../pages/PowerGraph/PowerGraph'
import Equipment  from '../pages/Equipment/Equipment'
import Alarms     from '../pages/Alarms/Alarms'
import Scheduled  from '../pages/Scheduled/Scheduled'
import Users      from '../pages/Users/Users'
import Settings   from '../pages/Settings/Settings'

export default function AppRoutes() {
  return (
    <Routes>
      <Route path="/"           element={<Navigate to="/dashboard" replace />} />
      <Route path="/dashboard"  element={<Dashboard />} />
      <Route path="/reports"    element={<Reports />} />
      <Route path="/tracker"    element={<Tracker />} />
      <Route path="/tracker-status" element={<TrackerStatus />} />
      <Route path="/dgr"        element={<DGRReports />} />
      <Route path="/analytics"  element={<Analytics />} />
      <Route path="/preconfigured" element={<Preconfigured />} />
      <Route path="/ppc-trend"  element={<PPCTrend />} />
      <Route path="/power-graph" element={<PowerGraph />} />
      <Route path="/equipment"  element={<Equipment />} />
      <Route path="/alarms"     element={<Alarms />} />
      <Route path="/scheduled"  element={<Scheduled />} />
      <Route path="/users"      element={<Users />} />
      <Route path="/settings"   element={<Settings />} />
      <Route path="*"           element={<Navigate to="/dashboard" replace />} />
    </Routes>
  )
}
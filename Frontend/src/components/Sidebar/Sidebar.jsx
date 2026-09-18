// src/components/Sidebar/Sidebar.jsx
import React from 'react'
import { NavLink } from 'react-router-dom'
import {
  MdDashboard, MdBarChart, MdAssignment, MdAnalytics,
  MdBuild, MdNotifications, MdSchedule, MdPeople, MdSettings,
  MdChevronLeft, MdChevronRight, MdSatelliteAlt, MdShowChart, MdBolt, MdTune,
} from 'react-icons/md'
import { useApp } from '../../utils/AppContext'

// `hidden: true` removes an item from the sidebar without deleting its route or
// component — the page is still reachable by navigating directly to its URL.
const NAV_ITEMS = [
  { label: 'Dashboard',         path: '/dashboard', icon: MdDashboard,     group: 'Operations'  },
  { label: 'Reports',           path: '/reports',   icon: MdBarChart,      group: 'Operations'  },
  { label: 'Tracker Reports',   path: '/tracker',   icon: MdSatelliteAlt,  group: 'Operations', hidden: true },
  { label: 'Tracker Status',    path: '/tracker-status', icon: MdSatelliteAlt, group: 'Operations', hidden: true },
  { label: 'DGR Reports',       path: '/dgr',       icon: MdAssignment,    group: 'Operations'  },
  { label: 'Analytics',         path: '/analytics', icon: MdAnalytics,     group: 'Operations'  },
  { label: 'Preconfigured Reports', path: '/preconfigured', icon: MdTune,   group: 'Operations'  },
  { label: 'PPC Trend',         path: '/ppc-trend', icon: MdShowChart,     group: 'Operations', hidden: true },
  { label: 'Power Graph',       path: '/power-graph', icon: MdBolt,        group: 'Operations', hidden: true },
  { label: 'Equipment',         path: '/equipment', icon: MdBuild,         group: 'Monitoring', hidden: true },
  { label: 'Alarms',            path: '/alarms',    icon: MdNotifications, group: 'Monitoring', hidden: true },
  { label: 'Scheduled Reports', path: '/scheduled', icon: MdSchedule,      group: 'Monitoring'  },
  { label: 'User Management',   path: '/users',     icon: MdPeople,        group: 'Admin'       },
  { label: 'Settings',          path: '/settings',  icon: MdSettings,      group: 'Admin', hidden: true },
]

const GROUPS = ['Operations', 'Monitoring', 'Admin']

export default function Sidebar() {
  const { sidebarCollapsed, setSidebarCollapsed } = useApp()

  return (
    <aside
      className={`flex flex-col bg-ge-dark border-r border-ge-border flex-shrink-0 z-10 transition-all duration-250 ${
        sidebarCollapsed ? 'w-14' : 'w-[220px]'
      }`}
    >
      {/* Logo — the exact Trinity Touch image (public/trinity-touch-logo.png), replacing the
          former KALYON NIGDE 130 MW / TURKEY block and the old GE chip. The PNG already carries
          its OWN opaque white background, so no extra chip/background/padding is added around it
          (that was the source of the empty gap) — it sits flush in the 52px header area, sized to
          fill it neatly. object-contain preserves the original proportions (no stretch/crop).
          Rendered from the shared Sidebar, so the spacing is identical on every page. */}
      <div className={`flex items-center border-b border-ge-border min-h-[52px] ${sidebarCollapsed ? 'justify-center px-2' : 'px-4'}`}>
        <img
          src="/trinity-touch-logo.png"
          alt="Trinity Touch"
          className={`object-contain ${sidebarCollapsed ? 'h-8 w-10' : 'h-10 max-w-full'}`}
        />
      </div>

      {/* Nav */}
      <nav className="flex-1 overflow-y-auto py-2">
        {GROUPS.map(group => {
          const items = NAV_ITEMS.filter(i => i.group === group && !i.hidden)
          if (items.length === 0) return null
          return (
          <div key={group} className="py-1">
            {!sidebarCollapsed && (
              <div className="px-4 py-1.5 text-[9px] font-semibold text-ge-text3 uppercase tracking-widest">
                {group}
              </div>
            )}
            {items.map(item => (
              <NavLink
                key={item.path}
                to={item.path}
                className={({ isActive }) =>
                  'nav-item' + (isActive ? ' active' : '') +
                  (sidebarCollapsed ? ' justify-center px-0' : '')
                }
                title={sidebarCollapsed ? item.label : undefined}
              >
                <item.icon className="text-[17px] flex-shrink-0" />
                {!sidebarCollapsed && (
                  <span className="text-[13px] truncate">{item.label}</span>
                )}
              </NavLink>
            ))}
          </div>
          )
        })}
      </nav>

      {/* Collapse button */}
      <div className="p-3 border-t border-ge-border">
        <button
          onClick={() => setSidebarCollapsed(s => !s)}
          className="w-full flex items-center justify-center gap-1.5 py-1.5 bg-ge-surface
                     border border-ge-border rounded-md text-ge-text2 text-xs
                     hover:bg-ge-elevated hover:text-ge-text1 transition-all"
        >
          {sidebarCollapsed
            ? <MdChevronRight className="text-base" />
            : <><MdChevronLeft className="text-base" /><span>Collapse</span></>
          }
        </button>
      </div>
    </aside>
  )
}

# KALYON NIGDE 130 MW — Report Automation Platform

Enterprise-grade React dashboard for utility-scale solar power plant monitoring.

---

## Tech Stack

| Layer         | Technology                          |
|---------------|-------------------------------------|
| Framework     | React 18 + Vite 5                   |
| Styling       | Tailwind CSS 3 (dark industrial theme) |
| Routing       | React Router v6                     |
| HTTP Client   | Axios                               |
| Charts        | Recharts                            |
| Icons         | React Icons (Material Design)       |
| State         | React Context + useState/useEffect  |
| Data          | Mock API (swap `src/services/api.js` for real endpoints) |

---

## Quick Start

```bash
# 1. Install dependencies
npm install

# 2. Start dev server
npm run dev

# 3. Open in browser
# http://localhost:5173
```

## Build for Production

```bash
npm run build
npm run preview   # test the build locally
```

---

## Project Structure

```
src/
├── App.jsx                        # Root layout (Sidebar + Header + Routes)
├── main.jsx                       # Entry point
├── index.css                      # Tailwind + global component styles
│
├── routes/
│   └── AppRoutes.jsx              # React Router config
│
├── components/
│   ├── Sidebar/Sidebar.jsx        # Collapsible nav sidebar
│   ├── Header/Header.jsx          # Top header bar
│   ├── Charts/index.jsx           # Recharts wrappers (6 chart types)
│   ├── Filters/TagSelector.jsx    # Dual-panel tag picker
│   └── Common/index.jsx           # KpiCard, DataTable, Modal, Toast, etc.
│
├── pages/
│   ├── Dashboard/Dashboard.jsx    # Live KPIs + charts + alarm feed
│   ├── Reports/Reports.jsx        # Full Report Automation screen
│   ├── DGRReports/DGRReports.jsx  # Daily Generation Reports
│   ├── Analytics/Analytics.jsx    # PR, inverter comparison, trends
│   ├── Equipment/Equipment.jsx    # Equipment status board
│   ├── Alarms/Alarms.jsx          # Alarm management + acknowledge
│   ├── Scheduled/Scheduled.jsx    # Schedule CRUD with modal form
│   ├── Users/Users.jsx            # User management
│   └── Settings/Settings.jsx      # Platform settings with toggles
│
├── services/
│   ├── api.js                     # Axios client + mock async functions
│   └── mockData.js                # All mock datasets
│
├── hooks/
│   └── useFetch.js                # useFetch + useAsync hooks
│
└── utils/
    ├── AppContext.jsx              # Global state (sidebar, toasts, modal)
    └── helpers.js                 # Formatters, pill classes, chart colors
```

---

## Connecting to a Real API

1. Set `VITE_API_URL` in a `.env` file:
   ```
   VITE_API_URL=https://your-api.ge-solar.com/api
   ```

2. Replace the mock delay functions in `src/services/api.js` with real `axios.get/post` calls.

3. The rest of the app (hooks, pages, components) stays exactly the same.

---

## Key Features

- **Collapsible dark sidebar** with active route highlighting
- **Report Automation** — equipment + tag selection, date/time filters, export
- **Tag Selector** — dual-panel with search, sort, add-all/remove-all
- **6 Recharts charts** — area, bar, scatter, line dual-axis, horizontal bar
- **Alarm management** — acknowledge individually or all at once
- **Scheduled Reports** — CRUD modal, run-now, delete
- **Settings** — toggle switches, dropdowns, persisted in local state
- **Toast notifications** on every action
- **Loading skeletons** while data fetches
- **Async buttons** with spinner states

---

## Customisation

- **Colors** — edit `tailwind.config.js` → `theme.extend.colors.ge`
- **Mock data** — edit `src/services/mockData.js`
- **Plant name/site** — update `PLANT_INFO` in `mockData.js` and `Header.jsx`
- **Nav items** — add entries to `NAV_ITEMS` in `Sidebar.jsx` + add a route in `AppRoutes.jsx`

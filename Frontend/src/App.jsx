import React from 'react'
import Sidebar from './components/Sidebar/Sidebar'
import Header from './components/Header/Header'
import AppRoutes from './routes/AppRoutes'
import { ToastContainer, Modal } from './components/Common'
import { AppProvider } from './utils/AppContext'

export default function App() {
  return (
    <AppProvider>
      <div className="flex h-screen overflow-hidden bg-ge-navy">
        <Sidebar />
        <div className="flex flex-col flex-1 overflow-hidden">
          <Header />
          <main className="flex-1 overflow-y-auto bg-ge-navy p-4">
            <AppRoutes />
          </main>
        </div>
      </div>
      <ToastContainer />
      <Modal />
    </AppProvider>
  )
}
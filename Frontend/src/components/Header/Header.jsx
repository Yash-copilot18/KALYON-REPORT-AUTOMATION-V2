// src/components/Header/Header.jsx
import React from 'react'
import { MdRefresh, MdLightMode, MdDarkMode } from 'react-icons/md'
import { useApp } from '../../utils/AppContext'
import { Spinner } from '../Common'

export default function Header() {
  const { showToast, theme, toggleTheme, userProfile, refreshing, triggerRefresh } = useApp()

  const handleRefresh = async () => {
    await triggerRefresh()
    showToast('Data refreshed')
  }

  return (
    <header className="h-[52px] bg-ge-dark border-b border-ge-border flex items-center px-4 gap-3 flex-shrink-0">
      {/* Title */}
      <div className="flex-1 min-w-0">
        <h1 className="text-[13px] font-semibold text-ge-text1 leading-tight">
          KALYON NIGDE 130 MW
        </h1>
      </div>

      {/* Actions */}
      <div className="flex items-center gap-2">
        {/* Refresh */}
        <button
          onClick={handleRefresh}
          disabled={refreshing}
          className="w-8 h-8 bg-ge-surface border border-ge-border rounded-md flex items-center
                     justify-center text-ge-text2 hover:bg-ge-elevated hover:text-ge-text1
                     transition-all disabled:opacity-60"
          title="Refresh data"
        >
          {refreshing
            ? <Spinner size={13} />
            : <MdRefresh className="text-base" />
          }
        </button>

        {/* Theme toggle */}
        <button
          onClick={toggleTheme}
          className="w-8 h-8 bg-ge-surface border border-ge-border rounded-md flex items-center
                     justify-center text-ge-text2 hover:bg-ge-elevated hover:text-ge-text1 transition-all"
          title={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
        >
          {theme === 'dark'
            ? <MdLightMode className="text-base" />
            : <MdDarkMode className="text-base" />
          }
        </button>

        {/* User profile */}
        <div className="flex items-center gap-2 bg-ge-surface border border-ge-border rounded-md px-2.5 py-1
                        cursor-pointer hover:bg-ge-elevated transition-all"
          title={userProfile.email}
        >
          <div
            className="w-6 h-6 rounded-full flex items-center justify-center text-[10px] font-semibold text-white flex-shrink-0"
            style={{ background: 'linear-gradient(135deg,#0099ff,#7c3aed)' }}
          >
            {userProfile.initials}
          </div>
          <span className="text-[12px] text-ge-text2 hidden sm:block max-w-[100px] truncate">
            {userProfile.name.split(' ')[0]}
          </span>
          <span className="w-1.5 h-1.5 rounded-full bg-ge-success animate-pulse-slow flex-shrink-0" />
        </div>
      </div>
    </header>
  )
}

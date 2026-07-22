// src/utils/AppContext.jsx
import React, { createContext, useContext, useState, useCallback, useRef, useEffect } from 'react'

const AppContext = createContext(null)

function getInitials(name) {
  return name
    .split(' ')
    .map(w => w[0])
    .join('')
    .toUpperCase()
    .slice(0, 2)
}

function loadUserProfile() {
  try {
    const raw = localStorage.getItem('ge_user_profile')
    if (raw) return JSON.parse(raw)
  } catch {}
  return { name: 'Kalyon Admin', email: 'admin@trinitytouch.com', initials: 'KA' }
}

export function AppProvider({ children }) {
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false)
  const [toasts, setToasts] = useState([])
  const [modalState, setModalState] = useState({ open: false, title: '', content: null })

  // ── Theme ──────────────────────────────────────────────────────────────────
  const [theme, setTheme] = useState(
    () => localStorage.getItem('ge_theme') || 'dark'
  )

  useEffect(() => {
    localStorage.setItem('ge_theme', theme)
    if (theme === 'dark') {
      document.documentElement.classList.add('dark')
    } else {
      document.documentElement.classList.remove('dark')
    }
  }, [theme])

  const toggleTheme = useCallback(() => {
    setTheme(t => (t === 'dark' ? 'light' : 'dark'))
  }, [])

  // ── User Profile ───────────────────────────────────────────────────────────
  const [userProfile, setUserProfileState] = useState(loadUserProfile)

  const updateUserProfile = useCallback((partial) => {
    setUserProfileState(prev => {
      const next = { ...prev, ...partial }
      if (!next.initials) next.initials = getInitials(next.name || 'KA')
      localStorage.setItem('ge_user_profile', JSON.stringify(next))
      return next
    })
  }, [])

  // ── Page Refresh Registry ──────────────────────────────────────────────────
  // Pages register an async refresh fn; Header calls triggerRefresh()
  const refreshFnRef = useRef(null)
  const [refreshing, setRefreshing] = useState(false)

  const registerRefresh = useCallback((fn) => {
    refreshFnRef.current = fn
  }, [])

  const triggerRefresh = useCallback(async () => {
    if (!refreshFnRef.current) return
    setRefreshing(true)
    try {
      await refreshFnRef.current()
    } finally {
      setRefreshing(false)
    }
  }, [])

  // ── Toast ──────────────────────────────────────────────────────────────────
  const toastSeq = useRef(0)
  const showToast = useCallback((msg, type = 'success') => {
    // Monotonic unique id — Date.now() alone collides for toasts fired in the
    // same millisecond, causing React "two children with the same key" warnings.
    const id = `${Date.now()}-${toastSeq.current++}`
    setToasts(prev => [...prev, { id, msg, type }])
    setTimeout(() => setToasts(prev => prev.filter(t => t.id !== id)), 3000)
  }, [])

  // ── Modal ──────────────────────────────────────────────────────────────────
  const openModal = useCallback((title, content) => {
    setModalState({ open: true, title, content })
  }, [])

  const closeModal = useCallback(() => {
    setModalState(s => ({ ...s, open: false }))
  }, [])

  return (
    <AppContext.Provider value={{
      sidebarCollapsed, setSidebarCollapsed,
      toasts, showToast,
      modalState, openModal, closeModal,
      theme, toggleTheme,
      userProfile, updateUserProfile,
      refreshing, registerRefresh, triggerRefresh,
    }}>
      {children}
    </AppContext.Provider>
  )
}

export const useApp = () => useContext(AppContext)

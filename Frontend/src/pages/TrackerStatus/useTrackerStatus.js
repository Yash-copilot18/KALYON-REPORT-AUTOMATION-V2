// src/pages/TrackerStatus/useTrackerStatus.js
import { useState, useEffect, useRef, useCallback } from 'react'
import { fetchTrackerStatus, trackerStatusSocketUrl } from '../../services/api'

/**
 * Live tracker-status transport: WebSocket-first with automatic reconnection,
 * and REST polling as a fallback whenever the socket is down. The dashboard
 * stays populated across reconnects (last snapshot is retained).
 *
 * Returns: { snapshot, transport, connected, error, lastUpdated }
 *  transport: 'connecting' | 'ws' | 'poll'
 */
export function useTrackerStatus(table, { pollMs = 5000 } = {}) {
  const [snapshot,    setSnapshot]    = useState(null)   // { timestamp, kpis, trackers }
  const [transport,   setTransport]   = useState('connecting')
  const [connected,   setConnected]   = useState(false)
  const [error,       setError]       = useState(null)
  const [lastUpdated, setLastUpdated] = useState(null)

  const wsRef    = useRef(null)
  const pollRef  = useRef(null)
  const retryRef = useRef(0)
  const aliveRef = useRef(true)

  const stopPolling = useCallback(() => {
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null }
  }, [])

  const startPolling = useCallback(() => {
    if (pollRef.current) return
    setTransport('poll')
    const tick = async () => {
      try {
        const d = await fetchTrackerStatus(table)
        if (!aliveRef.current) return
        setSnapshot({ timestamp: d.timestamp, kpis: d.kpis, trackers: d.trackers })
        setLastUpdated(Date.now()); setError(null)
      } catch (e) {
        if (aliveRef.current) setError(e.message || 'Failed to fetch status')
      }
    }
    tick()
    pollRef.current = setInterval(tick, pollMs)
  }, [table, pollMs])

  const connectWs = useCallback(() => {
    let ws
    try { ws = new WebSocket(trackerStatusSocketUrl(table)) }
    catch { startPolling(); return }
    wsRef.current = ws

    ws.onopen = () => {
      retryRef.current = 0
      setConnected(true); setTransport('ws'); setError(null)
      stopPolling()                         // WS is authoritative once connected
    }
    ws.onmessage = (evt) => {
      let d; try { d = JSON.parse(evt.data) } catch { return }
      if (d.type === 'snapshot') {
        setSnapshot({ timestamp: d.timestamp, kpis: d.kpis, trackers: d.trackers })
        setLastUpdated(Date.now()); setError(null)
      } else if (d.type === 'heartbeat') {
        setLastUpdated(Date.now())
      } else if (d.type === 'error') {
        setError(d.message || 'Stream error')
      }
    }
    ws.onclose = () => {
      setConnected(false)
      if (!aliveRef.current) return
      startPolling()                        // keep data flowing while reconnecting
      const delay = Math.min(30000, 1000 * 2 ** retryRef.current)
      retryRef.current += 1
      setTimeout(() => { if (aliveRef.current) connectWs() }, delay)
    }
    ws.onerror = () => { try { ws.close() } catch { /* onclose handles retry */ } }
  }, [table, startPolling, stopPolling])

  useEffect(() => {
    aliveRef.current = true
    retryRef.current = 0
    connectWs()
    return () => {
      aliveRef.current = false
      stopPolling()
      if (wsRef.current) { try { wsRef.current.close() } catch { /* noop */ } }
    }
  }, [connectWs, stopPolling])

  return { snapshot, transport, connected, error, lastUpdated }
}

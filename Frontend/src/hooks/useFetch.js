// src/hooks/useFetch.js
import { useState, useEffect, useCallback, useRef } from 'react'

const sleep = (ms) => new Promise(r => setTimeout(r, ms))

/**    
 * Production data-fetching hook.
 *
 * @param {Function} fetchFn          async data source
 * @param {Array}    deps             re-fetch when these change
 * @param {Object}   options
 *   @param {number}  refetchInterval  ms between silent background refreshes (0 = off)
 *   @param {number}  retries          retry attempts on failure (default 2, exp. backoff)
 *   @param {number}  retryDelay       base backoff in ms (default 800)
 *   @param {boolean} enabled          gate the fetch (default true)
 *
 * `loading` is only true on the very first load; background refreshes surface
 * via `refreshing` so the UI never flashes skeletons while auto-updating.
 */
export function useFetch(fetchFn, deps = [], options = {}) {
  const { refetchInterval = 0, retries = 2, retryDelay = 800, enabled = true } = options

  const [data, setData]             = useState(null)
  const [loading, setLoading]       = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError]           = useState(null)
  const [lastUpdated, setLastUpdated] = useState(null)

  const mounted     = useRef(true)
  const hasLoaded   = useRef(false)

  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])

  const load = useCallback(async ({ silent = false } = {}) => {
    if (!enabled) return
    if (silent) setRefreshing(true)
    else setLoading(true)

    let attempt = 0
    while (true) {
      try {
        const result = await fetchFn()
        if (!mounted.current) return
        setData(result)
        setError(null)
        setLastUpdated(new Date())
        hasLoaded.current = true
        break
      } catch (e) {
        if (attempt < retries) {
          attempt += 1
          await sleep(retryDelay * attempt)   // linear backoff
          continue
        }
        if (!mounted.current) return
        // Keep the last good data on a failed silent refresh; only surface error.
        setError(e.message || 'Failed to load')
        break
      }
    }
    if (!mounted.current) return
    if (silent) setRefreshing(false)
    else setLoading(false)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, ...deps])

  // Initial + dependency-driven load.
  useEffect(() => { load() }, [load])

  // Auto-refresh: silent polling on an interval; pauses when tab is hidden.
  useEffect(() => {
    if (!refetchInterval || !enabled) return
    let id
    const tick = () => {
      if (!document.hidden) load({ silent: true })
    }
    id = setInterval(tick, refetchInterval)
    return () => clearInterval(id)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refetchInterval, enabled, load])

  return {
    data, loading, refreshing, error, lastUpdated,
    refetch: () => load({ silent: hasLoaded.current }),
  }
}

// src/hooks/useAsync.js
export function useAsync() {
  const [loading, setLoading] = useState(false)
  const [error, setError]     = useState(null)

  const run = useCallback(async (asyncFn, onSuccess, onError) => {
    setLoading(true)
    setError(null)
    try {
      const result = await asyncFn()
      if (onSuccess) onSuccess(result)
    } catch (e) {    
      const msg = e.message || 'Operation failed'
      setError(msg)
      if (onError) onError(msg)
    } finally {
      setLoading(false)
    }
  }, [])

  return { loading, error, run }
}

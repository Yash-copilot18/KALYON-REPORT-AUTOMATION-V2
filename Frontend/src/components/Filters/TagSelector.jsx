import React, { useState, useMemo } from 'react'
import { SearchInput } from '../Common'

const ALL_TAGS = [
  { name: 'AC Power',          unit: 'kW'   },
  { name: 'DC Voltage',        unit: 'V'    },
  { name: 'DC Current',        unit: 'A'    },
  { name: 'Energy Today',      unit: 'MWh'  },
  { name: 'Irradiance',        unit: 'W/m²' },
  { name: 'Temperature',       unit: '°C'   },
  { name: 'Frequency',         unit: 'Hz'   },
  { name: 'Grid Voltage',      unit: 'kV'   },
  { name: 'Performance Ratio', unit: '%'    },
  { name: 'Availability',      unit: '%'    },
  { name: 'DC Power',          unit: 'kW'   },
  { name: 'Reactive Power',    unit: 'kVAR' },
  { name: 'Active Power',      unit: 'MW'   },
  { name: 'Power Factor',      unit: ''     },
  { name: 'Module Temp',       unit: '°C'   },
]

export default function TagSelector({ selected, setSelected }) {
  const [query,  setQuery]  = useState('')
  const [sorted, setSorted] = useState(false)

  const displayed = useMemo(() => {
    const list = sorted
      ? [...ALL_TAGS].sort((a, b) => a.name.localeCompare(b.name))
      : ALL_TAGS
    return list.filter(t =>
      t.name.toLowerCase().includes(query.toLowerCase())
    )
  }, [query, sorted])

  const toggle = name => {
    setSelected(prev => {
      const s = new Set(prev)
      s.has(name) ? s.delete(name) : s.add(name)
      return s
    })
  }

  const addAll    = () => setSelected(new Set(ALL_TAGS.map(t => t.name)))
  const removeAll = () => setSelected(new Set())

  return (
    <div>
      {/* Header */}
      <div className="flex items-center justify-between mb-2">
        <span className="text-[10px] font-semibold text-ge-text3 uppercase tracking-widest">
          Tag Selection
        </span>
        <span className="text-[10px] font-mono text-ge-accent">
          {selected.size} selected
        </span>
      </div>

      {/* Search */}
      <SearchInput
        value={query}
        onChange={setQuery}
        placeholder="Search tags..."
        className="mb-2.5"
      />

      {/* Action Buttons */}
      <div className="flex items-center gap-2 mb-2.5 flex-wrap">
        <button className="btn btn-outline btn-sm" onClick={addAll}>
          + Add All ({ALL_TAGS.length})
        </button>
        <button className="btn btn-outline btn-sm" onClick={removeAll}>
          ✕ Remove All
        </button>
        <button className="btn btn-outline btn-sm" onClick={() => setSorted(s => !s)}>
          {sorted ? 'Default Order' : 'A→Z Sort'}
        </button>
        <span className="ml-auto text-[11px] text-ge-text3 font-mono">
          Available: {displayed.length}
        </span>
      </div>

      {/* Dual Panel */}
      <div className="grid grid-cols-2 gap-2.5">

        {/* Available Tags */}
        <div>
          <div className="flex items-center justify-between px-2.5 py-1.5
                          bg-ge-elevated border border-ge-border rounded-t-md border-b-0">
            <span className="text-[10px] font-semibold text-ge-text3 uppercase">
              Available
            </span>
            <span className="text-[10px] font-mono text-ge-text3">
              {displayed.length}
            </span>
          </div>
          <div className="bg-ge-elevated border border-ge-border rounded-b-md
                          overflow-y-auto max-h-48">
            {displayed.length === 0 ? (
              <div className="px-3 py-6 text-[12px] text-ge-text3 text-center">
                No tags found
              </div>
            ) : (
              displayed.map(tag => (
                <div
                  key={tag.name}
                  onClick={() => toggle(tag.name)}
                  className={`flex items-center gap-2 px-2.5 py-1.5 cursor-pointer
                             border-b border-ge-border last:border-b-0
                             transition-colors hover:bg-ge-surface
                             ${selected.has(tag.name) ? 'bg-blue-950/40' : ''}`}
                >
                  <div className={`w-3.5 h-3.5 rounded flex items-center justify-center
                                  text-[9px] flex-shrink-0 border
                                  ${selected.has(tag.name)
                                    ? 'bg-ge-blue border-ge-blue text-white'
                                    : 'border-ge-border2'}`}>
                    {selected.has(tag.name) && '✓'}
                  </div>
                  <span className="text-[12px] text-ge-text1 flex-1">{tag.name}</span>
                  <span className="text-[10px] font-mono text-ge-text3">{tag.unit}</span>
                </div>
              ))
            )}
          </div>
        </div>

        {/* Selected Tags */}
        <div>
          <div className="flex items-center justify-between px-2.5 py-1.5
                          bg-ge-elevated border border-ge-border rounded-t-md border-b-0">
            <span className="text-[10px] font-semibold text-ge-text3 uppercase">
              Selected
            </span>
            <span className="text-[10px] font-mono text-ge-accent">
              {selected.size} tags
            </span>
          </div>
          <div className="bg-ge-elevated border border-ge-border rounded-b-md
                          overflow-y-auto max-h-48">
            {selected.size === 0 ? (
              <div className="px-3 py-6 text-[12px] text-ge-text3 text-center">
                No tags selected
              </div>
            ) : (
              [...selected].map(name => {
                const tag = ALL_TAGS.find(t => t.name === name) || { name, unit: '' }
                return (
                  <div
                    key={name}
                    onClick={() => toggle(name)}
                    className="flex items-center gap-2 px-2.5 py-1.5 cursor-pointer
                               border-b border-ge-border last:border-b-0
                               transition-colors hover:bg-ge-surface bg-blue-950/30"
                  >
                    <div className="w-3.5 h-3.5 rounded flex items-center justify-center
                                    text-[9px] flex-shrink-0 bg-ge-blue border
                                    border-ge-blue text-white">
                      ✓
                    </div>
                    <span className="text-[12px] text-ge-text1 flex-1">{tag.name}</span>
                    <span className="text-[10px] font-mono text-ge-text3">{tag.unit}</span>
                  </div>
                )
              })
            )}
          </div>
        </div>

      </div>
    </div>
  )
}
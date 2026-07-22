// src/utils/chartCapture.js
//
// Convert a live Recharts SVG (already in the DOM) into a high-resolution PNG data
// URL for embedding in a PDF. Recharts colours/fonts are inline attributes, so a
// serialized clone renders faithfully without external CSS.

const PNG_SCALE = 2.5   // 2.5× device pixels → crisp at print DPI

/**
 * Rasterise the first <svg> inside `container` to a PNG data URL.
 * Returns { dataUrl, width, height } (CSS px) or null if there's no chart yet.
 */
export function captureChartPng(container, { background = '#141928', scale = PNG_SCALE } = {}) {
  const svg = container?.querySelector('svg')
  if (!svg) return null

  const rect = svg.getBoundingClientRect()
  const w = Math.max(1, Math.round(rect.width))
  const h = Math.max(1, Math.round(rect.height))

  // Clone so we can pin explicit dimensions without disturbing the live chart.
  const clone = svg.cloneNode(true)
  clone.setAttribute('width', w)
  clone.setAttribute('height', h)
  clone.setAttribute('xmlns', 'http://www.w3.org/2000/svg')
  // A solid backing rect keeps the dark theme instead of transparent PDF gaps.
  const bg = document.createElementNS('http://www.w3.org/2000/svg', 'rect')
  bg.setAttribute('width', w)
  bg.setAttribute('height', h)
  bg.setAttribute('fill', background)
  clone.insertBefore(bg, clone.firstChild)

  const svgStr = new XMLSerializer().serializeToString(clone)
  const svgUrl = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svgStr)

  return new Promise((resolve) => {
    const img = new Image()
    img.onload = () => {
      const canvas = document.createElement('canvas')
      canvas.width = Math.round(w * scale)
      canvas.height = Math.round(h * scale)
      const ctx = canvas.getContext('2d')
      ctx.fillStyle = background
      ctx.fillRect(0, 0, canvas.width, canvas.height)
      ctx.drawImage(img, 0, 0, canvas.width, canvas.height)
      try {
        resolve({ dataUrl: canvas.toDataURL('image/png'), width: w, height: h })
      } catch {
        resolve(null)   // tainted canvas — skip the chart rather than fail the export
      }
    }
    img.onerror = () => resolve(null)
    img.src = svgUrl
  })
}

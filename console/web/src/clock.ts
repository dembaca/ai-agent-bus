// Time as the console shows it: ages short, seconds only where the reports
// ask for them (counts and seconds, no timestamps, ADR-15 point 8).

export function since(atMs: number, nowMs: number = Date.now()): string {
  const s = Math.max(0, Math.round((nowMs - atMs) / 1000))
  if (s < 60) return `${s} s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m} min`
  const h = Math.floor(m / 60)
  if (h < 48) return `${h} h`
  return `${Math.floor(h / 24)} d`
}

export function seconds(count: number | null, unit: "count" | "s" | "%"): string {
  if (count === null) return "n/a"
  if (unit === "s") return count >= 3600
    ? `${Math.floor(count / 3600)} h ${Math.round((count % 3600) / 60)} min`
    : `${count} s`
  if (unit === "%") return `${count} %`
  return `${count}`
}

export function clock(atMs: number): string {
  return new Date(atMs).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
}

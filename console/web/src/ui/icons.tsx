import logoUrl from "../assets/choirbus-logo.svg"

// The console's marks: the Choirbus logo — the website's own file
// (site/public/choirbus-logo.svg, copied to src/assets/), never redrawn —
// and the crown, sun and moon as drawn paths, no emoji. Each is decorative
// (aria-hidden or an empty alt) unless it carries its own label.

/** The logo beside the wordmark. Its navy tile melts into the dark page
 * (1.07:1): TopBar.css gives it a cream hairline there, as the site does. */
export function Logo({ size = 22 }: { size?: number }) {
  return <img className="logo" src={logoUrl} alt="" width={size} height={size} />
}

/** The coordinator's mark (the crown of its channel). */
export function CrownIcon({ size = 13, label }: { size?: number; label?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor"
      role={label ? "img" : undefined} aria-label={label} aria-hidden={label ? undefined : true}>
      {label && <title>{label}</title>}
      <path d="M3 7l4.5 4L12 4l4.5 7L21 7l-2 12H5z" />
    </svg>
  )
}

export function SunIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2" strokeLinecap="round" aria-hidden="true">
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
    </svg>
  )
}

export function MoonIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M20.5 14.5A8.5 8.5 0 1 1 9.5 3.5a7 7 0 0 0 11 11z" />
    </svg>
  )
}

export function SearchIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2" aria-hidden="true">
      <circle cx="11" cy="11" r="7" />
      <path d="m20 20-3.5-3.5" />
    </svg>
  )
}

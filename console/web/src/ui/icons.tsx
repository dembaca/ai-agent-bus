// The console's own drawn marks: the Choirbus logo (site/public/choirbus-logo.svg,
// its main shapes at top-bar size), the crown, sun and moon. Drawn paths, no
// emoji; each is decorative (aria-hidden) unless it carries its own label.

export function Logo({ size = 22 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="40 40 1176 1176" aria-hidden="true" focusable="false">
      <rect x="63" y="62.5" width="1129" height="1129" rx="225" fill="#140b47" />
      <circle cx="620" cy="626" r="456" fill="#fdf6ea" />
      <path fill="#140b47" d="M205 808 C225 700 250 595 310 520 C380 440 470 410 548 415 C625 420 685 455 722 487 L745 750 C640 790 535 900 520 1076 L520 1100 L200 1100 L190 808 Z" />
      <path fill="#ec4a3a" d="M722 487 L852 442 C880 433 890 450 872 466 L700 640 L855 720 C876 732 868 750 850 750 L745 752 C680 742 630 702 628 642 C628 572 670 515 722 487 Z" />
      <path fill="#ec4a3a" d="M889.7 582.3 L1001.7 529.3 L986.3 496.7 L874.3 549.7 Z M885.1 653.3 L985.1 681.3 L994.9 646.7 L894.9 618.7 Z" />
    </svg>
  )
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

export function AskIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2.2" aria-hidden="true">
      <path d="M9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3M12 17h.01" />
    </svg>
  )
}

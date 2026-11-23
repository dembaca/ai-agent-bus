import { StrictMode, useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import type { Theme } from './kit'
import Console from './illustrations/Console'

// The target picture (target.html) is plain HTML too and reads fully without
// JS: the diagram and the timeline are page markup. This file only mounts the
// console preview into its slot (the page hides the slot without JS).

const dark = window.matchMedia('(prefers-color-scheme: dark)')

function Themed() {
  const [theme, setTheme] = useState<Theme>(dark.matches ? 'dark' : 'light')
  useEffect(() => {
    const on = () => setTheme(dark.matches ? 'dark' : 'light')
    dark.addEventListener('change', on)
    return () => dark.removeEventListener('change', on)
  }, [])
  return <Console theme={theme} />
}

const slot = document.querySelector<HTMLElement>('[data-picture="console"]')
if (slot) {
  createRoot(slot).render(
    <StrictMode>
      <Themed />
    </StrictMode>,
  )
}

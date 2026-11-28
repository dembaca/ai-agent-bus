import { useLayoutEffect, useRef, useState } from 'react'
import { Avatar, Badge, Bar, Stage, States, Text, Window, type Act, type Theme } from '../kit'
import './console.css'

// The operator console as planned (ADR-015, ADR-016), drawn as a preview,
// not a screenshot of the pilot. The page says so above the
// picture and the window carries a "Preview" badge of its own.
//
// One window in the console's agreed design (2026-11-25): a top bar with the
// views (Agents, the start page, then Asks and Stats), the open ask
// on top of the agents list, then one compact row per agent with its state.
// One caused motion: the ask's "Waits on you" settles to "Answered in the
// chat", since the console reads first and the answer still comes from a
// chat or a terminal. Poster (reduced motion): the settled list.
//
// Names are examples, as on the start page.

type S = { answered: boolean }
const initial: S = { answered: false }
const poster: S = { answered: true }

async function script(a: Act<S>) {
  await a.wait(1400)
  await a.set({ answered: true })
  await a.wait(2000)
}

type Row = { name: string; who: string; task: number; state: 'working' | 'listening' | 'idle' }
const ROWS: Row[] = [
  { name: 'agent-laptop-api-3f2a1c', who: 'Laptop', task: 120, state: 'working' },
  { name: 'agent-build-docs-7e05b2', who: 'Build', task: 96, state: 'working' },
  { name: 'agent-desk-web-9c41d0', who: 'Desk', task: 108, state: 'listening' },
  { name: 'agent-ci-tests-52be7d', who: 'Runner', task: 84, state: 'idle' },
]

const TONE = { working: 'accent', listening: 'success', idle: 'neutral' } as const

const VIEWS = ['Agents', 'Asks', 'Stats']

function useWidth(fallback: number) {
  const ref = useRef<HTMLDivElement>(null)
  const [width, setWidth] = useState(fallback)
  useLayoutEffect(() => {
    const node = ref.current
    if (!node) return
    const measure = () => setWidth(Math.max(1, Math.round(node.getBoundingClientRect().width)))
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(node)
    return () => observer.disconnect()
  }, [])
  return [ref, width] as const
}

function Views() {
  return (
    <span className="cn-views">
      {VIEWS.map((v) => (
        <span key={v} className="cn-view" data-on={v === 'Agents' ? '' : undefined}>
          {v}
        </span>
      ))}
    </span>
  )
}

export default function Console({ theme }: { theme: Theme }) {
  const [ref, width] = useWidth(700)
  const compact = width < 560
  const rows = compact ? ROWS.slice(0, 3) : ROWS
  return (
    <div ref={ref} className="cn-fill">
      <Stage<S> width={width} height={compact ? 300 : 320} initial={initial} poster={poster} script={script} play="once" theme={theme} className="cn-stage">
        {(s) => (
          <Window
            title="Choirbus"
            lights="grey"
            className="cn-win"
            lead={<Views />}
            toolbar={
              <span className="cn-preview">
                <Badge tone="neutral">Read only</Badge>
                <Badge tone="warning">Preview</Badge>
              </span>
            }
          >
            <div className="cn-page">
              <div className="cn-ask">
                <Text className="cn-ask-text">Merge the docs branch?</Text>
                <States
                  value={s.answered ? 'answered' : 'waiting'}
                  align="end"
                  states={{
                    waiting: <Badge tone="warning">Waits on you</Badge>,
                    answered: <Badge tone="success">Answered in the chat</Badge>,
                  }}
                />
              </div>
              <div className="cn-table">
                {rows.map((r) => (
                  <div key={r.name} className="cn-row">
                    <span className="cn-row-who">
                      <Avatar name={r.who} size={18} />
                      <Text className="cn-mono">{r.name}</Text>
                    </span>
                    <Badge tone={TONE[r.state]} dot>{r.state}</Badge>
                    {!compact && <Bar w={r.task} h={6} r={1} tone={2} />}
                  </div>
                ))}
              </div>
            </div>
          </Window>
        )}
      </Stage>
    </div>
  )
}

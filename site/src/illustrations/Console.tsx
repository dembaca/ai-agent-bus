import { useLayoutEffect, useRef, useState } from 'react'
import { Avatar, Badge, Bar, Stage, States, Text, Window, type Act, type Theme } from '../kit'
import './console.css'

// The operator console as planned (ADR-015, ADR-016), drawn as a preview,
// not a screenshot: nothing of it is built yet. The page says so above the
// picture and the window carries a "Preview" badge of its own.
//
// One window, after the `window-row` gold's chrome: a sidebar with the three
// views (Kanban, Graph, Reports) and the read-only mark, then the Kanban
// view, one card per agent in the column of its state. One caused motion:
// a card's "Waits on you" settles to "Answered in the chat", since the
// console reads first and the answer still comes from a chat or a terminal.
// Poster (reduced motion): the settled board.
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

type Card = { name: string; who: string; task: number; wait?: boolean }
const COLUMNS: { title: string; cards: Card[] }[] = [
  {
    title: 'Working',
    cards: [
      { name: 'agent-laptop-api-3f2a1c', who: 'Laptop', task: 120 },
      { name: 'agent-build-docs-7e05b2', who: 'Build', task: 96 },
    ],
  },
  {
    title: 'Listening',
    cards: [{ name: 'agent-desk-web-9c41d0', who: 'Desk', task: 108, wait: true }],
  },
  {
    title: 'Idle',
    cards: [{ name: 'agent-ci-tests-52be7d', who: 'Runner', task: 84 }],
  },
]

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

function Sidebar() {
  return (
    <div className="cn-side">
      <Text className="cn-side-head">my-team</Text>
      <span className="cn-view" data-on>
        Kanban
      </span>
      <span className="cn-view">Graph</span>
      <span className="cn-view">Reports</span>
      <span className="cn-side-foot">
        <Badge tone="neutral">Read only</Badge>
      </span>
    </div>
  )
}

export default function Console({ theme }: { theme: Theme }) {
  const [ref, width] = useWidth(700)
  const compact = width < 560
  const columns = compact ? COLUMNS.slice(0, 2) : COLUMNS
  return (
    <div ref={ref} className="cn-fill">
      <Stage<S> width={width} height={compact ? 300 : 320} initial={initial} poster={poster} script={script} play="once" theme={theme} className="cn-stage">
        {(s) => (
          <Window
            title="Operator console"
            lights="grey"
            className="cn-win"
            sidebar={compact ? undefined : <Sidebar />}
            sidebarWidth={140}
            toolbar={
              <span className="cn-preview">
                <Badge tone="warning">Preview</Badge>
              </span>
            }
          >
            <div className="cn-board" style={{ gridTemplateColumns: `repeat(${columns.length}, 1fr)` }}>
              {columns.map((col) => (
                <div key={col.title} className="cn-col">
                  <Text className="cn-col-head">{col.title}</Text>
                  {col.cards.map((c) => (
                    <div key={c.name} className="cn-card">
                      <div className="cn-card-head">
                        <Avatar name={c.who} size={18} />
                        <Text className="cn-mono">{c.name}</Text>
                      </div>
                      <Bar w={c.task} h={6} r={3} tone={2} />
                      {c.wait && (
                        <States
                          value={s.answered ? 'answered' : 'waiting'}
                          align="start"
                          states={{
                            waiting: <Badge tone="warning">Waits on you</Badge>,
                            answered: <Badge tone="success">Answered in the chat</Badge>,
                          }}
                        />
                      )}
                    </div>
                  ))}
                </div>
              ))}
            </div>
          </Window>
        )}
      </Stage>
    </div>
  )
}

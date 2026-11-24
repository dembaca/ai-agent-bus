import { useEffect, useState } from "react"
import type { ConsoleApi, GraphData } from "../api/types"
import "./GraphView.css"

type Fail = (what: string, error: unknown) => void

/** The graph (ADR-015 point 7): nodes people, channels (homes), agents;
 * the edges shown at start are trusts and coordinates, followed further
 * only to a depth of two, with a mark where it is cut. Hand-rolled SVG:
 * the libraries stay an open ADR point, and the pilot graph is small. */
export function GraphView({ api, onFail }: { api: ConsoleApi; onFail: Fail }) {
  const [data, setData] = useState<GraphData | null>(null)
  useEffect(() => {
    let gone = false
    api.graph()
      .then((g) => { if (!gone) setData(g) })
      .catch((error: unknown) => { if (!gone) onFail("graph", error) })
    return () => { gone = true }
  }, [api, onFail])
  if (!data) return <div className="graph"><p className="hint">loading…</p></div>
  return (
    <div className="graph">
      <p className="hint">
        trusts and coordinates, to a depth of two; a dashed edge is cut there.
      </p>
      <Graph data={data} />
    </div>
  )
}

/** Layered left-to-right: people, then channels, then agents — a plain,
 * readable shape while the console is a pilot. A crown-holding node is
 * marked by a small crown drawn beside it (point 9: no emoji literal). */
function Graph({ data }: { data: GraphData }) {
  const width = 900
  const height = 460
  const by = (kind: GraphData["nodes"][number]["kind"]) =>
    data.nodes.filter((n) => n.kind === kind)
  const people = by("person")
  const channels = by("channel")
  const agents = by("agent")

  const column = (list: GraphData["nodes"], x: number) =>
    Object.fromEntries(list.map((node, i) => [node.id, {
      x, y: (height / (list.length + 1)) * (i + 1),
    }]))

  const pos: Record<string, { x: number; y: number }> = {
    ...column(people, 120),
    ...column(channels, 440),
    ...column(agents, 760),
  }

  return (
    <svg className="graph__svg" viewBox={`0 0 ${width} ${height}`} role="img"
      aria-label="the bus graph: people, channels and agents">
      {data.edges.map((edge) => {
        const a = pos[edge.from]
        const b = pos[edge.to]
        if (!a || !b) return null
        return <line key={edge.id}
          x1={a.x} y1={a.y} x2={b.x} y2={b.y}
          className={`graph__edge graph__edge--${edge.kind}${edge.cut ? " graph__edge--cut" : ""}`}
          aria-label={`${edge.kind}: ${edge.from} to ${edge.to}`} />
      })}
      {data.nodes.map((node) => {
        const p = pos[node.id]
        if (!p) return null
        return (
          <g key={node.id} className={`graph__node graph__node--${node.kind}`}>
            {node.kind === "person" && <circle cx={p.x} cy={p.y} r="18" />}
            {node.kind === "channel" && <rect x={p.x - 22} y={p.y - 14} width="44" height="28" rx="6" />}
            {node.kind === "agent" && <rect x={p.x - 26} y={p.y - 16} width="52" height="32" rx="16" />}
            <text x={p.x} y={p.y - 24}>{node.label}</text>
            {node.crown && <path className="graph__crown" transform={`translate(${p.x - 13} ${p.y - 46})`}
              d="M0 8 L2 2 L6 6 L10 0 L14 6 L18 2 L20 8 Z" />}
            {node.state && <text className="graph__state" x={p.x} y={p.y + 34}>{node.state}</text>}
          </g>
        )
      })}
      {data.cutDepth === 2 &&
        <text className="graph__hint" x={width - 12} y={height - 12} textAnchor="end">
          depth cut at 2
        </text>}
    </svg>
  )
}

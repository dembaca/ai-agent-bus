import { useCallback, useEffect, useRef, useState } from "react"
import {
  Background, Controls, ReactFlow, ReactFlowProvider,
  useEdgesState, useNodesInitialized, useNodesState, useReactFlow,
} from "@xyflow/react"
import type { Edge, Node } from "@xyflow/react"
import "@xyflow/react/dist/style.css"
import type { AgentCard, Ask, ConsoleApi, GraphData } from "../api/types"
import { reconcile, toFlow } from "./graph/model"
import { nodeTypes } from "./graph/nodes"
import type { Theme } from "../theme"
import "./GraphView.css"

type Fail = (what: string, error: unknown) => void

/** The graph endpoint is read again this long after the set of agents
 * changed, so their trusts and guest channels show; a burst costs one read. */
const GRAPH_AGAIN_MS = 500

type Props = {
  api: ConsoleApi
  cards: AgentCard[]
  asks: Ask[]
  fleetSeen: boolean
  /** The theme in use: React Flow draws its controls in it. */
  theme: Theme
  onOpenAgent: (card: AgentCard) => void
  onOpenAsk: (ask: Ask) => void
  onFail: Fail
}

/** The graph (ADR-015 point 7, issue #145) on React Flow: you, the open
 * asks, the agents, their home and guest channels, left to right. The
 * fleet's cards and the open asks come from the app, which keeps them live:
 * a status recolors an agent in place, an agent or ask that comes or goes
 * adds or drops its node. The graph endpoint is read on start and again
 * when the set of agents changes. A new layout runs only when a node or an
 * edge comes or goes, and the nodes already shown keep their place. A click
 * on an agent or an ask opens the same detail as elsewhere (point 7). */
export function GraphView({ api, cards, asks, fleetSeen, theme, onOpenAgent, onOpenAsk, onFail }: Props) {
  const [graph, setGraph] = useState<GraphData | null>(null)
  const agents = cards.map((c) => c.name).sort().join(",")
  const graphRead = useRef(0)
  const loaded = useRef(false)
  useEffect(() => {
    // Each read is numbered: only the newest answer is shown.
    const mine = ++graphRead.current
    const read = () => {
      api.graph()
        .then((g) => {
          if (mine !== graphRead.current) return
          loaded.current = true
          setGraph(g)
        })
        .catch((error: unknown) => { if (mine === graphRead.current) onFail("graph", error) })
    }
    // The first read at once, a read again after a short wait.
    const timer = setTimeout(read, loaded.current ? GRAPH_AGAIN_MS : 0)
    return () => { clearTimeout(timer); graphRead.current += 1 }
  }, [api, onFail, agents])

  const onNodeClick = useCallback((_: unknown, node: Node) => {
    const [kind, ...rest] = node.id.split(":")
    const key = rest.join(":")
    if (kind === "agent") {
      const card = cards.find((c) => c.name === key)
      if (card) onOpenAgent(card)
    } else if (kind === "ask") {
      const row = asks.find((a) => a.id === key)
      if (row) onOpenAsk(row)
    }
  }, [cards, asks, onOpenAgent, onOpenAsk])

  return (
    <ReactFlowProvider>
      <LiveGraph graph={graph} cards={cards} asks={asks} fleetSeen={fleetSeen} theme={theme} onNodeClick={onNodeClick} />
    </ReactFlowProvider>
  )
}

function LiveGraph({ graph, cards, asks, fleetSeen, theme, onNodeClick }: {
  graph: GraphData | null
  cards: AgentCard[]
  asks: Ask[]
  fleetSeen: boolean
  theme: Theme
  onNodeClick: (event: unknown, node: Node) => void
}) {
  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([])
  const [edges, setEdges] = useEdgesState<Edge>([])
  const shown = useRef<{ nodes: Node[]; edges: Edge[] }>({ nodes: [], edges: [] })
  shown.current = { nodes, edges }

  useEffect(() => {
    if (!graph) return
    const next = toFlow({ graph, cards, asks, fleetSeen })
    const r = reconcile(shown.current.nodes, shown.current.edges, next.nodes, next.edges)
    setNodes(r.nodes)
    setEdges(r.edges)
  }, [graph, cards, asks, fleetSeen, setNodes, setEdges])

  // The viewport is fitted once, on the first layout; updates never move it.
  const { fitView } = useReactFlow()
  const ready = useNodesInitialized()
  const fitted = useRef(false)
  useEffect(() => {
    if (ready && !fitted.current && nodes.length > 0) {
      fitted.current = true
      void fitView({ padding: 0.12 })
    }
  }, [ready, nodes.length, fitView])

  if (!graph) return <div className="graph"><p className="hint">loading…</p></div>
  return (
    <div className="graph">
      <div className="page-head">
        <div className="page-head__title"><h1>Graph</h1></div>
      </div>
      <p className="hint">
        you, open asks, agents, their homes and guest channels; trusts and
        coordinates to a depth of two, a dashed edge is cut there.
        {graph.cutDepth === 2 && <span className="graph__cut"> Depth cut at 2.</span>}
      </p>
      <div className="graph__canvas" role="group" aria-label="the bus graph: people, asks, agents and channels">
        <ReactFlow
          nodes={nodes}
          edges={edges}
          onNodesChange={onNodesChange}
          onNodeClick={onNodeClick}
          nodeTypes={nodeTypes}
          nodesConnectable={false}
          edgesFocusable={false}
          elementsSelectable={false}
          colorMode={theme}
          minZoom={0.2}
        >
          <Background gap={24} size={1} />
          <Controls showInteractive={false} />
        </ReactFlow>
      </div>
    </div>
  )
}

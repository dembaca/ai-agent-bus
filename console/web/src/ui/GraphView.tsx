import { useEffect, useRef, useState } from "react"
import {
  Background, Controls, ReactFlow, ReactFlowProvider,
  useEdgesState, useNodesInitialized, useNodesState, useReactFlow,
} from "@xyflow/react"
import type { Edge, Node } from "@xyflow/react"
import "@xyflow/react/dist/style.css"
import type { AgentCard, Ask, ConsoleApi, GraphData } from "../api/types"
import { reconcile, toFlow } from "./graph/model"
import { nodeTypes } from "./graph/nodes"
import "./GraphView.css"

type Fail = (what: string, error: unknown) => void

/** The graph (ADR-015 point 7, issue #145) on React Flow: you, the open
 * asks, the agents, their home and guest channels, left to right. The graph
 * endpoint is read once; the fleet's cards and the open asks come from the
 * app, which keeps them live: a status recolors an agent in place, an agent
 * or ask that comes or goes adds or drops its node. A new layout runs only
 * when a node or an edge comes or goes, and the nodes already shown keep
 * their place. */
export function GraphView({ api, cards, asks, onFail }: {
  api: ConsoleApi; cards: AgentCard[]; asks: Ask[]; onFail: Fail
}) {
  const [graph, setGraph] = useState<GraphData | null>(null)
  useEffect(() => {
    let gone = false
    api.graph()
      .then((g) => { if (!gone) setGraph(g) })
      .catch((error: unknown) => { if (!gone) onFail("graph", error) })
    return () => { gone = true }
  }, [api, onFail])
  return (
    <ReactFlowProvider>
      <LiveGraph graph={graph} cards={cards} asks={asks} />
    </ReactFlowProvider>
  )
}

function LiveGraph({ graph, cards, asks }: { graph: GraphData | null; cards: AgentCard[]; asks: Ask[] }) {
  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([])
  const [edges, setEdges] = useEdgesState<Edge>([])
  const shown = useRef<{ nodes: Node[]; edges: Edge[] }>({ nodes: [], edges: [] })
  shown.current = { nodes, edges }

  useEffect(() => {
    if (!graph) return
    const next = toFlow({ graph, cards, asks })
    const r = reconcile(shown.current.nodes, shown.current.edges, next.nodes, next.edges)
    setNodes(r.nodes)
    setEdges(r.edges)
  }, [graph, cards, asks, setNodes, setEdges])

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
      <p className="hint">
        you, open asks, agents, their homes and guest channels; trusts and
        coordinates to a depth of two, a dashed edge is cut there.
        {graph.cutDepth === 2 && <span className="graph__cut"> Depth cut at 2.</span>}
      </p>
      <div className="graph__canvas" role="img" aria-label="the bus graph: people, asks, agents and channels">
        <ReactFlow
          nodes={nodes}
          edges={edges}
          onNodesChange={onNodesChange}
          nodeTypes={nodeTypes}
          nodesConnectable={false}
          edgesFocusable={false}
          elementsSelectable={false}
          colorMode="system"
          minZoom={0.2}
        >
          <Background gap={24} size={1} />
          <Controls showInteractive={false} />
        </ReactFlow>
      </div>
    </div>
  )
}

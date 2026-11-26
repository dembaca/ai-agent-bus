import { Handle, Position } from "@xyflow/react"
import type { NodeProps, NodeTypes } from "@xyflow/react"
import { HARNESS_LABEL } from "../../api/types"
import type { FlowData } from "./model"

/** The two fixed ends of a node: edges come in on the left, go out on the
 * right. Read-only: nothing connects by hand. */
function Ends() {
  return (
    <>
      <Handle type="target" position={Position.Left} isConnectable={false} />
      <Handle type="source" position={Position.Right} isConnectable={false} />
    </>
  )
}

/** The crown on the coordinator's node: a drawn path, no emoji literal. */
function Crown() {
  return (
    <svg className="gnode__crown" viewBox="0 0 20 8" width="20" height="8" role="img" aria-label="coordinator">
      <path d="M0 8 L2 2 L6 6 L10 0 L14 6 L18 2 L20 8 Z" />
    </svg>
  )
}

const dataOf = (props: NodeProps) => props.data as FlowData

function PersonNode(props: NodeProps) {
  const d = dataOf(props)
  return (
    <div className="gnode gnode--person" title={d.label}>
      <Ends />
      <span className="gnode__label">{d.label}</span>
    </div>
  )
}

function AgentNode(props: NodeProps) {
  const d = dataOf(props)
  return (
    <div className="gnode gnode--agent" data-state={d.state} title={`${d.label}: ${d.state ?? "n/a"}`}>
      <Ends />
      <span className="gnode__label">
        {d.crown && <Crown />}
        {d.label}
      </span>
      <span className="gnode__meta">
        <span className="gnode__state">{d.state ?? "n/a"}</span>
        {d.harness && <span className="gnode__harness">{HARNESS_LABEL[d.harness]}</span>}
      </span>
    </div>
  )
}

function ChannelNode(props: NodeProps) {
  const d = dataOf(props)
  return (
    <div className={`gnode gnode--channel gnode--${d.role ?? "home"}`} title={`${d.label} (${d.role ?? "home"})`}>
      <Ends />
      <span className="gnode__label">{d.label}</span>
      {d.role === "guest" && <span className="gnode__meta">guest</span>}
    </div>
  )
}

function AskNode(props: NodeProps) {
  const d = dataOf(props)
  return (
    <div className={`gnode gnode--ask${d.forMe ? " gnode--for-me" : ""}`} title={d.label}>
      <Ends />
      <span className="gnode__meta">{d.askKind} by {d.by}</span>
      <span className="gnode__label gnode__label--clip">{d.label}</span>
    </div>
  )
}

export const nodeTypes: NodeTypes = {
  person: PersonNode,
  agent: AgentNode,
  channel: ChannelNode,
  ask: AskNode,
}

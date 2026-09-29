import { useMemo } from "react";
import { Background, Controls, MarkerType, ReactFlow, type Edge, type Node } from "@xyflow/react";

export type NodeStatus = { state?: string; runs?: number; seconds?: number; route?: string; error?: string };

const POS: Record<string, [number, number]> = {
  intake: [0, 0], load_context: [0, 80], quality_investigator: [-170, 170], lineage_investigator: [170, 170],
  single_investigation: [0, 170], root_cause: [0, 260], repair_planner: [0, 350], policy_check: [0, 440],
  shadow_validate: [0, 530], verification: [0, 620], request_approval: [0, 710], execute: [0, 800],
  resolve: [0, 890], close_no_action: [340, 350], escalate: [-340, 620], manual_intervention: [-340, 890],
};
const COLORS: Record<string, string> = {
  completed: "#047857", running: "#4338ca", failed: "#be123c", waiting: "#b45309",
};

export function ExecutionGraph({ nodes, edges, status, onSelect, selected }: {
  nodes: string[]; edges: { source: string; target: string }[]; status: Record<string, NodeStatus>;
  onSelect: (n: string) => void; selected: string | null;
}) {
  const rfNodes: Node[] = useMemo(() => nodes.map((n) => {
    const st = status[n];
    const color = st?.state ? COLORS[st.state] ?? "#475569" : "#94a3b8";
    return {
      id: n, position: { x: POS[n]?.[0] ?? 0, y: POS[n]?.[1] ?? 0 },
      data: { label: `${n.replace(/_/g, " ")}${st?.runs && st.runs > 1 ? ` ×${st.runs}` : ""}` },
      style: { border: `2px solid ${color}`, background: st?.state ? "#fff" : "#f8fafc", color: "#0f172a",
        fontSize: 12, borderRadius: 8, width: 170, boxShadow: selected === n ? `0 0 0 3px ${color}55` : undefined,
        opacity: st?.state ? 1 : 0.6 },
      ariaLabel: `${n} ${st?.state ?? "not reached"}`,
    };
  }), [nodes, status, selected]);
  const rfEdges: Edge[] = useMemo(() => edges.map((e, i) => {
    const active = status[e.source]?.state === "completed" && status[e.target]?.state;
    return { id: `${i}`, source: e.source, target: e.target, animated: status[e.target]?.state === "running",
      markerEnd: { type: MarkerType.ArrowClosed }, style: { stroke: active ? "#0f766e" : "#cbd5e1", strokeWidth: active ? 2 : 1 } };
  }), [edges, status]);
  return (
    <div className="h-[560px] rounded-md border border-slate-200 bg-slate-50" data-testid="execution-graph">
      <ReactFlow nodes={rfNodes} edges={rfEdges} fitView nodesDraggable={false} nodesConnectable={false}
        onNodeClick={(_, n) => onSelect(n.id)} proOptions={{ hideAttribution: true }}>
        <Background gap={16} />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}

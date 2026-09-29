import { useMemo } from "react";
import { useNavigate } from "react-router-dom";
import { Background, Controls, MarkerType, ReactFlow, type Edge, type Node } from "@xyflow/react";

const COL: Record<string, number> = { raw: 0, staging: 1, dimension: 2, fact: 2, mart: 3 };

export function LineageGraph({ nodes, edges, highlight, unhealthy }: {
  nodes: { id: string; kind: string }[]; edges: { upstream_asset_id: string; downstream_asset_id: string }[];
  highlight?: string; unhealthy?: Set<string>;
}) {
  const nav = useNavigate();
  const rfNodes: Node[] = useMemo(() => {
    const rows: Record<number, number> = {};
    return nodes.map((n) => {
      const c = COL[n.kind] ?? 0;
      const r = (rows[c] = (rows[c] ?? -1) + 1);
      const bad = unhealthy?.has(n.id);
      return { id: n.id, position: { x: c * 240, y: r * 80 }, data: { label: n.id },
        style: { fontSize: 12, width: 190, borderRadius: 8, fontFamily: "ui-monospace, monospace",
          border: `2px solid ${n.id === highlight ? "#0f766e" : bad ? "#e11d48" : "#cbd5e1"}`,
          background: bad ? "#fff1f2" : "#fff" } };
    });
  }, [nodes, highlight, unhealthy]);
  const rfEdges: Edge[] = useMemo(() => edges.map((e, i) => ({ id: `${i}`, source: e.upstream_asset_id,
    target: e.downstream_asset_id, markerEnd: { type: MarkerType.ArrowClosed } })), [edges]);
  return (
    <div className="h-[420px] rounded-md border border-slate-200 bg-slate-50" data-testid="lineage-graph">
      <ReactFlow nodes={rfNodes} edges={rfEdges} fitView nodesDraggable={false} onNodeClick={(_, n) => nav(`/assets/${n.id}`)}
        proOptions={{ hideAttribution: true }}>
        <Background gap={16} /><Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}

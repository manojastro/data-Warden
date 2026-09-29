import { Link, useParams } from "react-router-dom";
import { LineageGraph } from "../components/LineageGraph";
import { Badge, Card, ErrorBox, Loading, Mono, Table } from "../components/ui";
import { ago } from "../format";
import { useFetch } from "../hooks";

type Detail = { id: string; kind: string; owner: string; schema_name: string; description: string; business_critical: boolean;
  columns: { name: string; type: string }[]; upstream: string[]; downstream: string[];
  checks: { id: string; check_type: string; severity: string; description: string }[];
  live: { row_count?: number; min_business_date?: string; max_business_date?: string; model_sql?: string | null; error?: string } };
type CheckRow = { id: string; latest: { status: string; message: string; evaluated_at: string } | null };

export default function AssetDetail() {
  const { id } = useParams();
  const { data, error, loading } = useFetch<Detail>(`/assets/${id}`, [id]);
  const lineage = useFetch<{ nodes: { id: string; kind: string }[]; edges: { upstream_asset_id: string; downstream_asset_id: string }[] }>("/lineage");
  const checks = useFetch<{ items: CheckRow[] }>("/checks");
  if (loading) return <Loading />;
  if (error || !data) return <ErrorBox error={error ?? "not found"} />;
  const latest = new Map((checks.data?.items ?? []).map((c) => [c.id, c.latest]));
  return (
    <div className="space-y-5">
      <div>
        <div className="text-xs text-slate-500"><Link to="/assets" className="hover:underline">Assets</Link> / {data.id}</div>
        <h1 className="font-mono text-lg font-semibold">{data.schema_name}.{data.id}</h1>
        <p className="text-sm text-slate-600">{data.description} · owner {data.owner} · {data.kind}{data.business_critical ? " · business critical" : ""}</p>
      </div>
      <div className="grid gap-4 lg:grid-cols-3">
        <Card title="Freshness & size">
          {data.live.error ? <ErrorBox error={data.live.error} /> : (
            <dl className="grid grid-cols-2 gap-y-1 text-sm">
              <dt className="text-slate-500">Rows</dt><dd className="tabular-nums">{data.live.row_count?.toLocaleString()}</dd>
              <dt className="text-slate-500">Earliest date</dt><dd>{data.live.min_business_date}</dd>
              <dt className="text-slate-500">Latest date</dt><dd>{data.live.max_business_date}</dd>
            </dl>
          )}
        </Card>
        <Card title="Upstream / downstream" className="lg:col-span-2">
          <div className="text-xs text-slate-500">Upstream</div>
          <div className="mb-2 flex flex-wrap gap-1">{data.upstream.map((u) => <Link key={u} to={`/assets/${u}`}><Mono>{u}</Mono></Link>)}</div>
          <div className="text-xs text-slate-500">Downstream</div>
          <div className="flex flex-wrap gap-1">{data.downstream.map((u) => <Link key={u} to={`/assets/${u}`}><Mono>{u}</Mono></Link>)}</div>
        </Card>
      </div>
      {lineage.data && <Card title="Lineage"><LineageGraph nodes={lineage.data.nodes} edges={lineage.data.edges} highlight={data.id} /></Card>}
      <div className="grid gap-4 lg:grid-cols-2">
        <Card title={`Schema (${data.columns.length} columns; personal fields hidden)`}>
          <Table head={["Column", "Type"]}>
            {data.columns.map((c) => <tr key={c.name}><td className="px-2 py-1 font-mono text-xs">{c.name}</td><td className="px-2 py-1 text-xs text-slate-500">{c.type}</td></tr>)}
          </Table>
        </Card>
        <Card title="Protected checks">
          <Table head={["Check", "Severity", "Latest", "When"]}>
            {data.checks.map((c) => { const l = latest.get(c.id); return (
              <tr key={c.id}><td className="px-2 py-1 font-mono text-xs" title={c.description}>{c.id}</td><td className="px-2 py-1"><Badge value={c.severity} /></td>
                <td className="px-2 py-1">{l ? <Badge value={l.status} /> : "—"}</td><td className="px-2 py-1 text-xs text-slate-500">{ago(l?.evaluated_at)}</td></tr>
            ); })}
          </Table>
        </Card>
      </div>
      {data.live.model_sql && <Card title="Model SQL (current canonical commit)"><pre className="max-h-96 overflow-auto rounded bg-slate-900 p-3 text-xs text-slate-100">{data.live.model_sql}</pre></Card>}
    </div>
  );
}

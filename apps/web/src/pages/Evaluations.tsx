import { Link } from "react-router-dom";
import { Badge, Card, Empty, Loading, Table } from "../components/ui";
import { short, when } from "../format";
import { useFetch } from "../hooks";

type EvalRun = { id: string; status: string; config: any; results: any; report_path: string | null; started_at: string; finished_at: string | null };
type Usage = { incident_id: string; mode: string; input_tokens: number; output_tokens: number; estimated_cost_usd: number | null; calls: number; model_latency_ms: number };

export default function Evaluations() {
  const evals = useFetch<{ items: EvalRun[] }>("/evaluations");
  const usage = useFetch<{ items: Usage[] }>("/usage");
  const latest = evals.data?.items.find((e) => e.status === "completed");
  const metrics = latest?.results?.metrics as Record<string, Record<string, any>> | undefined;
  return (
    <div className="space-y-5">
      <h1 className="text-xl font-semibold">Evaluation</h1>
      <div className="rounded-md border border-violet-200 bg-violet-50 p-3 text-sm text-violet-900">
        Results produced in <b>fixture model mode</b> measure the workflow, tools, validation and safety controls on
        seeded scenarios. They are not evidence of LLM reasoning quality. Run with a live provider for that.
      </div>
      <Card title="Latest benchmark">
        {evals.loading ? <Loading /> : metrics ? (
          <>
            <p className="mb-2 text-xs text-slate-500">{latest!.results.runs} runs · seeds {JSON.stringify(latest!.config.seeds)} · {when(latest!.finished_at)} · report <code>{latest!.report_path}</code></p>
            <Table head={["Metric", ...Object.keys(metrics)]}>
              {Object.keys(Object.values(metrics)[0] ?? {}).map((m) => (
                <tr key={m}><td className="px-2 py-1 text-xs font-medium">{m.replace(/_/g, " ")}</td>
                  {Object.keys(metrics).map((mode) => <td key={mode} className="px-2 py-1 text-xs tabular-nums">{fmt(metrics[mode][m])}</td>)}</tr>
              ))}
            </Table>
          </>
        ) : <Empty>No completed evaluation yet. Run <code>make eval</code>.</Empty>}
      </Card>
      <Card title="Evaluation runs">
        {evals.data?.items.length ? (
          <Table head={["Run", "Status", "Started", "Finished"]}>
            {evals.data.items.map((e) => <tr key={e.id}><td className="px-2 py-1 font-mono text-xs">{e.id}</td><td className="px-2 py-1"><Badge value={e.status} /></td>
              <td className="px-2 py-1 text-xs">{when(e.started_at)}</td><td className="px-2 py-1 text-xs">{when(e.finished_at)}</td></tr>)}
          </Table>
        ) : <Empty>None.</Empty>}
      </Card>
      <Card title="Per-incident tokens, cost and model latency">
        {usage.loading ? <Loading /> : usage.data?.items.length ? (
          <Table head={["Incident", "Mode", "Model calls", "Input tokens", "Output tokens", "Model latency", "Est. cost"]}>
            {usage.data.items.map((u) => (
              <tr key={`${u.incident_id}-${u.mode}`}>
                <td className="px-2 py-1 text-xs"><Link className="text-teal-800 hover:underline" to={`/incidents/${u.incident_id}`}>{short(u.incident_id, 14)}</Link></td>
                <td className="px-2 py-1"><Badge value={u.mode} /></td><td className="px-2 py-1 text-xs tabular-nums">{u.calls}</td>
                <td className="px-2 py-1 text-xs tabular-nums">{u.input_tokens}{u.mode === "fixture" ? " (est.)" : ""}</td>
                <td className="px-2 py-1 text-xs tabular-nums">{u.output_tokens}{u.mode === "fixture" ? " (est.)" : ""}</td>
                <td className="px-2 py-1 text-xs tabular-nums">{u.model_latency_ms} ms</td>
                <td className="px-2 py-1 text-xs">{u.estimated_cost_usd === null ? "unavailable" : `$${u.estimated_cost_usd.toFixed(4)}`}</td>
              </tr>
            ))}
          </Table>
        ) : <Empty>No model usage recorded.</Empty>}
      </Card>
    </div>
  );
}

function fmt(v: any): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : v.toFixed(3);
  return String(v);
}

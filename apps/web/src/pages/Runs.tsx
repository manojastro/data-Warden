import { Link } from "react-router-dom";
import { Badge, Card, Empty, ErrorBox, Loading, Table } from "../components/ui";
import { ago, short } from "../format";
import { useFetch, useStream } from "../hooks";

type Run = { id: string; trigger: string; status: string; code_commit: string | null; source_watermark: number | null;
  summary: { checks?: Record<string, number>; tasks?: Record<string, string> }; reported_at: string };

export default function Runs() {
  const { data, error, loading, reload } = useFetch<{ items: Run[]; total: number }>("/pipeline-runs?limit=50");
  useStream(null, (e) => e.event_type === "incident.opened" && reload());
  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Pipeline runs</h1>
      <p className="text-sm text-slate-500">Runs reported through the signed event endpoint by the pipeline runner or Airflow.</p>
      <Card>
        <ErrorBox error={error} />
        {loading ? <Loading /> : data?.items.length ? (
          <Table head={["Run", "Trigger", "Status", "Checks", "Code", "Watermark", "Reported"]}>
            {data.items.map((r) => (
              <tr key={r.id}>
                <td className="px-2 py-2"><Link to={`/runs/${r.id}`} className="font-mono text-xs text-teal-800 hover:underline">{r.id}</Link></td>
                <td className="px-2 py-2 text-xs">{r.trigger}</td><td className="px-2 py-2"><Badge value={r.status} /></td>
                <td className="px-2 py-2 text-xs">{Object.entries(r.summary.checks ?? {}).map(([k, v]) => `${v} ${k}`).join(", ")}</td>
                <td className="px-2 py-2 font-mono text-xs">{short(r.code_commit)}</td>
                <td className="px-2 py-2 text-xs">{r.source_watermark}</td><td className="px-2 py-2 text-xs text-slate-500">{ago(r.reported_at)}</td>
              </tr>
            ))}
          </Table>
        ) : <Empty>No runs reported yet.</Empty>}
      </Card>
    </div>
  );
}

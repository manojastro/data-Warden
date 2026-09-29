import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { Badge, Card, ErrorBox, Loading, Table } from "../components/ui";
import { when } from "../format";
import { useFetch } from "../hooks";

type RunDetail = { run: Record<string, any>; tasks: { task: string; status: string; started_at: string; finished_at: string | null; detail: any }[];
  logs: { ts: string; task: string; level: string; message: string }[] };

export default function RunDetailPage() {
  const { id } = useParams();
  const [task, setTask] = useState<string | null>(null);
  const q = `/pipeline-runs/${id}${task ? `?task=${task}` : ""}`;
  const { data, error, loading } = useFetch<RunDetail>(q, [q]);
  if (loading && !data) return <Loading />;
  if (error || !data) return <ErrorBox error={error ?? "not found"} />;
  return (
    <div className="space-y-5">
      <div>
        <div className="text-xs text-slate-500"><Link to="/runs" className="hover:underline">Pipeline runs</Link> / {id}</div>
        <h1 className="flex items-center gap-2 font-mono text-lg font-semibold">{id} <Badge value={data.run.status} /></h1>
        <p className="text-sm text-slate-500">trigger {data.run.trigger} · started {when(data.run.started_at)} · commit {String(data.run.code_commit).slice(0, 12)}</p>
        {data.run.error && <ErrorBox error={data.run.error} />}
      </div>
      <Card title="Tasks">
        <Table head={["Task", "Status", "Started", "Finished", "Detail"]}>
          {data.tasks.map((t, i) => (
            <tr key={i} className="cursor-pointer hover:bg-slate-50" onClick={() => setTask(task === t.task ? null : t.task)}>
              <td className="px-2 py-1.5 font-mono text-xs">{t.task}</td><td className="px-2 py-1.5"><Badge value={t.status} /></td>
              <td className="px-2 py-1.5 text-xs">{when(t.started_at)}</td><td className="px-2 py-1.5 text-xs">{when(t.finished_at)}</td>
              <td className="max-w-md truncate px-2 py-1.5 font-mono text-[11px] text-slate-500" title={JSON.stringify(t.detail)}>{JSON.stringify(t.detail)}</td>
            </tr>
          ))}
        </Table>
      </Card>
      <Card title={`Logs${task ? ` · ${task}` : ""} (untrusted text)`}>
        <pre className="max-h-[480px] overflow-auto rounded bg-slate-900 p-3 text-[11px] leading-5 text-slate-100">
          {data.logs.map((l, i) => <div key={i} className={l.level === "error" ? "text-rose-300" : l.level === "warning" ? "text-amber-200" : ""}>{new Date(l.ts).toLocaleTimeString()} [{l.task}] {l.message}</div>)}
        </pre>
      </Card>
    </div>
  );
}

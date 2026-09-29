import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth";
import { Badge, Button, Card, ErrorBox, Loading, Table } from "../components/ui";
import { when } from "../format";
import { useFetch, useInterval } from "../hooks";

type Scenarios = { synthetic: boolean; demo_mode: boolean; active: string[]; items: { id: string; description: string }[] };
type Job = { id: string; kind: string; status: string; attempts: number; last_error: string | null; created_at: string; result: any };
type Integration = { name: string; status: string; detail: string };

export default function Demo() {
  const { can } = useAuth();
  const sc = useFetch<Scenarios>("/demo/scenarios");
  const jobs = useFetch<{ items: Job[] }>("/jobs?limit=12");
  const integ = useFetch<{ items: Integration[] }>("/integrations");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useInterval(() => { jobs.reload(); sc.reload(); }, 3000);
  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setErr(null);
    try { await fn(); jobs.reload(); } catch (e) { setErr(e instanceof Error ? e.message : String(e)); } finally { setBusy(false); }
  };
  const allowed = can("demo.control");
  return (
    <div className="space-y-5">
      <div className="rounded-md border-2 border-dashed border-amber-400 bg-amber-50 p-3 text-sm text-amber-900" role="note">
        <b>Synthetic environment.</b> These controls only touch generated demo data, the local runtime workspace, and rows the
        faults themselves delivered. They are disabled outside demo mode and never reach external systems.
      </div>
      <h1 className="text-xl font-semibold">Demo control panel</h1>
      <ErrorBox error={err} />
      {!allowed && <p className="text-sm text-slate-500">Your role can view but not use demo controls.</p>}
      <div className="grid gap-5 lg:grid-cols-2">
        <Card title="Fault scenarios" actions={<Button disabled={!allowed || busy} onClick={() => run(() => api.post("/demo/reset"))}>Reset synthetic data</Button>}>
          {sc.loading ? <Loading /> : (
            <ul className="divide-y divide-slate-100">
              {sc.data?.items.map((s) => (
                <li key={s.id} className="flex items-center justify-between gap-3 py-2">
                  <div className="min-w-0"><div className="font-mono text-xs font-medium">{s.id}</div><div className="text-xs text-slate-500">{s.description}</div></div>
                  {sc.data?.active.includes(s.id) ? <Badge value="running" label="active" /> : (
                    <Button disabled={!allowed || busy || (sc.data?.active.length ?? 0) > 0} onClick={() => run(() => api.post("/demo/faults", { scenario: s.id }))}
                      title="Inject and run the pipeline">Inject</Button>
                  )}
                </li>
              ))}
            </ul>
          )}
          <p className="mt-2 text-xs text-slate-500">Inject runs the pipeline; failing checks create an incident through the signed ingestion endpoint.</p>
        </Card>
        <Card title="Background jobs">
          <Table head={["Kind", "Status", "Tries", "Created"]}>
            {(jobs.data?.items ?? []).map((j) => (
              <tr key={j.id}><td className="px-2 py-1 font-mono text-xs">{j.kind}</td><td className="px-2 py-1"><Badge value={j.status} /></td>
                <td className="px-2 py-1 text-xs">{j.attempts}</td><td className="px-2 py-1 text-xs text-slate-500">{when(j.created_at)}
                  {j.result?.emitted?.incidents?.[0] && <> · <Link className="text-teal-800 hover:underline" to={`/incidents/${j.result.emitted.incidents[0]}`}>open incident</Link></>}</td></tr>
            ))}
          </Table>
        </Card>
        <Card title="Integration status" className="lg:col-span-2">
          <Table head={["Integration", "Status", "Detail"]}>
            {(integ.data?.items ?? []).map((i) => <tr key={i.name}><td className="px-2 py-1 text-sm">{i.name}</td><td className="px-2 py-1"><Badge value={i.status} /></td><td className="px-2 py-1 text-xs text-slate-600">{i.detail}</td></tr>)}
          </Table>
        </Card>
      </div>
    </div>
  );
}

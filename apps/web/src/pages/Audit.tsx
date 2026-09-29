import { useState } from "react";
import { Link } from "react-router-dom";
import type { RecoveryOp } from "../api";
import { Badge, Button, Card, Empty, ErrorBox, Loading, Mono, Table, Tabs } from "../components/ui";
import { short, when } from "../format";
import { useFetch } from "../hooks";

type AuditRow = { id: number; ts: string; actor: string; action: string; target_type: string; target_id: string | null;
  incident_id: string | null; detail: any };

export default function Audit() {
  const [tab, setTab] = useState("audit");
  const [offset, setOffset] = useState(0);
  const [action, setAction] = useState("");
  const q = `/audit?limit=50&offset=${offset}${action ? `&action=${encodeURIComponent(action)}` : ""}`;
  const audit = useFetch<{ items: AuditRow[]; total: number }>(q, [q]);
  const rec = useFetch<{ items: RecoveryOp[] }>("/recovery-operations?limit=50");
  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Audit history & recovery journal</h1>
      <Tabs active={tab} onChange={setTab} tabs={[{ id: "audit", label: "Audit events" }, { id: "recovery", label: "Recovery journal" }]} />
      {tab === "audit" && (
        <Card actions={
          <select aria-label="Filter action" value={action} onChange={(e) => { setAction(e.target.value); setOffset(0); }}
            className="rounded-md border border-slate-300 px-2 py-1 text-xs">
            {["", "approval", "tool", "incident", "recovery", "proposal", "auth", "demo"].map((a) => <option key={a} value={a}>{a || "all actions"}</option>)}
          </select>}>
          <p className="mb-2 text-xs text-slate-500">Append-only through application permissions (a database administrator can still alter rows).</p>
          <ErrorBox error={audit.error} />
          {audit.loading ? <Loading /> : audit.data?.items.length ? (
            <>
              <Table head={["When", "Actor", "Action", "Target", "Incident", "Detail"]}>
                {audit.data.items.map((a) => (
                  <tr key={a.id}>
                    <td className="whitespace-nowrap px-2 py-1 text-xs text-slate-500">{when(a.ts)}</td>
                    <td className="px-2 py-1 text-xs">{a.actor}</td><td className="px-2 py-1"><Mono>{a.action}</Mono></td>
                    <td className="px-2 py-1 text-xs">{a.target_type}:{short(a.target_id, 18)}</td>
                    <td className="px-2 py-1 text-xs">{a.incident_id ? <Link className="text-teal-800 hover:underline" to={`/incidents/${a.incident_id}`}>{short(a.incident_id, 12)}</Link> : "—"}</td>
                    <td className="max-w-sm truncate px-2 py-1 font-mono text-[11px] text-slate-500" title={JSON.stringify(a.detail)}>{JSON.stringify(a.detail)}</td>
                  </tr>
                ))}
              </Table>
              <div className="mt-3 flex justify-end gap-2">
                <Button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 50))}>Newer</Button>
                <Button disabled={offset + 50 >= audit.data.total} onClick={() => setOffset(offset + 50)}>Older</Button>
              </div>
            </>
          ) : <Empty>No audit events.</Empty>}
        </Card>
      )}
      {tab === "recovery" && (
        <Card>
          {rec.loading ? <Loading /> : rec.data?.items.length ? (
            <Table head={["Operation", "Status", "Incident", "Steps", "Snapshot", "Commits", "Started"]}>
              {rec.data.items.map((op) => (
                <tr key={op.id}>
                  <td className="px-2 py-1.5"><Mono>{op.id}</Mono></td><td className="px-2 py-1.5"><Badge value={op.status} /></td>
                  <td className="px-2 py-1.5 text-xs"><Link className="text-teal-800 hover:underline" to={`/incidents/${op.incident_id}`}>{short(op.incident_id, 12)}</Link></td>
                  <td className="px-2 py-1.5 text-xs">{op.journal.map((j) => `${j.step}${j.status === "done" ? "" : `(${j.status})`}`).join(" → ")}</td>
                  <td className="px-2 py-1.5 font-mono text-[11px]">{short(op.snapshot_checksum, 10)}</td>
                  <td className="px-2 py-1.5 font-mono text-[11px]">{short(op.pre_commit, 8)} → {short(op.post_commit, 8)}</td>
                  <td className="px-2 py-1.5 text-xs text-slate-500">{when(op.started_at)}</td>
                </tr>
              ))}
            </Table>
          ) : <Empty>No recovery operations yet.</Empty>}
        </Card>
      )}
    </div>
  );
}

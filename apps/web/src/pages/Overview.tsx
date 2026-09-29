import { Link } from "react-router-dom";
import type { Incident, Page, RecoveryOp } from "../api";
import { Badge, Card, Empty, ErrorBox, Loading, Stat, Table } from "../components/ui";
import { ago } from "../format";
import { useFetch, useStream } from "../hooks";

type Asset = { id: string; kind: string; owner: string; business_critical: boolean; recent_check_failures: { check_id: string; status: string }[] };
type Check = { id: string; check_type: string; severity: string; latest: { status: string; message: string; evaluated_at: string } | null };

export default function Overview() {
  const assets = useFetch<{ items: Asset[] }>("/assets");
  const active = useFetch<Page<Incident>>("/incidents?status=active&limit=20");
  const recent = useFetch<Page<Incident>>("/incidents?limit=8");
  const checks = useFetch<{ items: Check[] }>("/checks");
  const recovery = useFetch<{ items: RecoveryOp[] }>("/recovery-operations?limit=6");
  const live = useStream(null, () => {
    active.reload();
    recent.reload();
    recovery.reload();
  });
  const failing = (checks.data?.items ?? []).filter((c) => c.latest && c.latest.status !== "pass");
  const unhealthy = (assets.data?.items ?? []).filter((a) => a.recent_check_failures.length);
  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h1 className="text-xl font-semibold">Overview</h1>
          <p className="text-sm text-slate-500">Retail revenue pipeline · synthetic data · INR · Asia/Kolkata business days</p>
        </div>
        <span className="text-xs text-slate-500" aria-live="polite">{live ? "● live" : "○ reconnecting"}</span>
      </div>
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label="Active incidents" value={active.data?.total ?? "…"} />
        <Stat label="Failing checks" value={checks.data ? failing.length : "…"} hint={`${checks.data?.items.length ?? 0} protected checks`} />
        <Stat label="Assets with issues" value={assets.data ? unhealthy.length : "…"} hint={`${assets.data?.items.length ?? 0} assets`} />
        <Stat label="Recent recoveries" value={recovery.data?.items.length ?? "…"}
          hint={`${(recovery.data?.items ?? []).filter((r) => r.status === "succeeded").length} succeeded`} />
      </div>
      <div className="grid gap-6 lg:grid-cols-2">
        <Card title="Active incidents">
          <ErrorBox error={active.error} />
          {active.loading ? <Loading /> : active.data?.items.length ? (
            <ul className="divide-y divide-slate-100">
              {active.data.items.map((i) => (
                <li key={i.id} className="flex items-center justify-between gap-3 py-2">
                  <Link to={`/incidents/${i.id}`} className="min-w-0 truncate text-sm text-teal-800 hover:underline">
                    INC-{i.number} · {i.title}
                  </Link>
                  <Badge value={i.status} />
                </li>
              ))}
            </ul>
          ) : <Empty>No active incidents.</Empty>}
        </Card>
        <Card title="Failing checks (latest result)">
          {checks.loading ? <Loading /> : failing.length ? (
            <Table head={["Check", "Severity", "Result", "When"]}>
              {failing.map((c) => (
                <tr key={c.id}>
                  <td className="px-2 py-1.5 font-mono text-xs">{c.id}</td>
                  <td className="px-2 py-1.5"><Badge value={c.severity} /></td>
                  <td className="max-w-xs truncate px-2 py-1.5 text-xs text-slate-600" title={c.latest?.message}>{c.latest?.message}</td>
                  <td className="px-2 py-1.5 text-xs text-slate-500">{ago(c.latest?.evaluated_at)}</td>
                </tr>
              ))}
            </Table>
          ) : <Empty>All reported checks pass.</Empty>}
        </Card>
        <Card title="Asset health">
          {assets.loading ? <Loading /> : (
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
              {(assets.data?.items ?? []).map((a) => (
                <Link key={a.id} to={`/assets/${a.id}`} className={`rounded-md border px-3 py-2 text-xs hover:shadow ${a.recent_check_failures.length ? "border-rose-200 bg-rose-50" : "border-emerald-200 bg-emerald-50"}`}>
                  <div className="truncate font-mono font-medium">{a.id}</div>
                  <div className="text-slate-500">{a.kind}{a.business_critical ? " · critical" : ""}</div>
                </Link>
              ))}
            </div>
          )}
        </Card>
        <Card title="Recent outcomes">
          {recent.loading ? <Loading /> : recent.data?.items.length ? (
            <Table head={["Incident", "Status", "Mode", "Opened"]}>
              {recent.data.items.map((i) => (
                <tr key={i.id}>
                  <td className="px-2 py-1.5"><Link className="text-teal-800 hover:underline" to={`/incidents/${i.id}`}>INC-{i.number}</Link></td>
                  <td className="px-2 py-1.5"><Badge value={i.status} /></td>
                  <td className="px-2 py-1.5"><Badge value={i.model_mode ?? "—"} /></td>
                  <td className="px-2 py-1.5 text-xs text-slate-500">{ago(i.created_at)}</td>
                </tr>
              ))}
            </Table>
          ) : <Empty>No incidents yet. Use Demo controls to inject a synthetic fault.</Empty>}
        </Card>
      </div>
    </div>
  );
}

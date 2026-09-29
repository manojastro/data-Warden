import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, idempotencyKey, type Evidence, type IncidentDetail, type Proposal, type StreamItem } from "../api";
import { useAuth } from "../auth";
import { Diff } from "../components/Diff";
import { ExecutionGraph, type NodeStatus } from "../components/ExecutionGraph";
import { Badge, Button, Card, Drawer, Empty, ErrorBox, Loading, Mono, Stat, Table, Tabs } from "../components/ui";
import { ago, paise, short, when } from "../format";
import { useFetch, useStream } from "../hooks";

type GraphDef = { nodes: string[]; edges: { source: string; target: string }[]; version: string };
type GraphStatus = { incident_status: string; variant: string; nodes: Record<string, NodeStatus> };

const NODE_AGENT: Record<string, string> = {
  quality_investigator: "quality_investigator", lineage_investigator: "lineage_investigator",
  root_cause: "root_cause_investigator", repair_planner: "repair_planner", verification: "verification_analyst",
  single_investigation: "single_agent",
};

export default function IncidentWorkspace() {
  const { id } = useParams();
  const detail = useFetch<IncidentDetail>(`/incidents/${id}`, [id]);
  const gstatus = useFetch<GraphStatus>(`/incidents/${id}/graph-status`, [id]);
  const variant = gstatus.data?.variant ?? "multi";
  const gdef = useFetch<GraphDef>(`/graph/definition?variant=${variant}`, [variant]);
  const timeline = useFetch<{ items: StreamItem[] }>(`/incidents/${id}/timeline?limit=500`, [id]);
  const [events, setEvents] = useState<StreamItem[]>([]);
  const [tab, setTab] = useState("overview");
  const [evidence, setEvidence] = useState<string | null>(null);
  const [node, setNode] = useState<string | null>(null);
  const timer = useRef<number | null>(null);

  useEffect(() => setEvents(timeline.data?.items ?? []), [timeline.data]);
  const refresh = useCallback(() => {
    if (timer.current) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => { detail.reload(); gstatus.reload(); }, 400);
  }, [detail, gstatus]);
  const live = useStream(id, (e) => {
    setEvents((prev) => (prev.some((p) => p.id === e.id) ? prev : [...prev, e]));
    refresh();
  });

  if (detail.loading && !detail.data) return <Loading />;
  if (detail.error) return <ErrorBox error={detail.error} />;
  const d = detail.data!;
  const inc = d.incident;
  const pending = d.proposals.find((p) => p.approval?.status === "pending");
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-xs text-slate-500"><Link to="/incidents" className="hover:underline">Incidents</Link> / INC-{inc.number}</div>
          <h1 className="text-lg font-semibold" data-testid="incident-title">{inc.title}</h1>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-slate-500">
            <Badge value={inc.status} /> <Badge value={inc.severity} />
            <Badge value={inc.model_mode ?? "fixture"} label={inc.model_mode === "live" ? "live model" : "fixture model (not an LLM)"} />
            <span>opened {ago(inc.created_at)}</span> · <span>{inc.graph_version}</span>
            <span aria-live="polite">{live ? "● live" : "○ reconnecting"}</span>
          </div>
          {inc.terminal_reason && <p className="mt-2 max-w-3xl text-sm text-slate-700" data-testid="terminal-reason">{inc.terminal_reason}</p>}
        </div>
        {pending && <Button variant="primary" onClick={() => setTab("repair")}>Review pending approval</Button>}
      </div>
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
        <Stat label="Evidence" value={d.evidence_count} />
        <Stat label="Tool calls" value={inc.usage?.tool_calls ?? 0} hint="budget 25" />
        <Stat label="Agent runs" value={d.agent_runs.length} />
        <Stat label="Tokens" value={(d.model_usage.input_tokens + d.model_usage.output_tokens).toLocaleString()}
          hint={inc.model_mode === "live" ? "measured" : "estimated (fixture)"} />
        <Stat label="Model cost" value={d.model_usage.estimated_cost_usd === null ? "unavailable" : `$${d.model_usage.estimated_cost_usd.toFixed(4)}`}
          hint={d.model_usage.estimated_cost_usd === null ? "no pricing configured" : "estimate"} />
      </div>
      <Tabs active={tab} onChange={setTab} tabs={[
        { id: "overview", label: "Overview" }, { id: "graph", label: "Execution graph" },
        { id: "agents", label: `Specialists (${d.agent_runs.length})` },
        { id: "hypotheses", label: `Hypotheses & evidence (${d.hypotheses.length})` },
        { id: "repair", label: `Repair review (${d.proposals.length})` }, { id: "timeline", label: `Timeline (${events.length})` },
      ]} />
      {tab === "overview" && <OverviewTab d={d} onEvidence={setEvidence} />}
      {tab === "graph" && gdef.data && (
        <div className="grid gap-4 xl:grid-cols-[1fr_380px]">
          <Card title="Incident execution graph (not data lineage)">
            <ExecutionGraph nodes={gdef.data.nodes} edges={gdef.data.edges} status={gstatus.data?.nodes ?? {}}
              onSelect={setNode} selected={node} />
            <p className="mt-2 text-xs text-slate-500">Node colours come from persisted node events. Click a node for details.</p>
          </Card>
          <NodePanel node={node} d={d} status={gstatus.data?.nodes ?? {}} onEvidence={setEvidence} />
        </div>
      )}
      {tab === "agents" && <AgentsTab d={d} onEvidence={setEvidence} />}
      {tab === "hypotheses" && <HypothesesTab d={d} onEvidence={setEvidence} />}
      {tab === "repair" && <RepairTab d={d} onChanged={() => detail.reload()} />}
      {tab === "timeline" && <TimelineTab events={events} onEvidence={setEvidence} />}
      <EvidenceDrawer id={evidence} onClose={() => setEvidence(null)} />
    </div>
  );
}

function EvidenceChip({ id, onClick }: { id: string; onClick: (id: string) => void }) {
  return (
    <button onClick={() => onClick(id)} className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[11px] text-slate-700 hover:bg-teal-50 hover:text-teal-800">
      {id}
    </button>
  );
}

function OverviewTab({ d, onEvidence }: { d: IncidentDetail; onEvidence: (id: string) => void }) {
  const inc = d.incident;
  const lineage = d.agent_runs.find((a) => a.agent === "lineage_investigator")?.finding;
  const rc = inc.summary?.root_cause;
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Card title="Current state">
        <dl className="grid grid-cols-[140px_1fr] gap-y-2 text-sm">
          <dt className="text-slate-500">Status</dt><dd><Badge value={inc.status} /></dd>
          <dt className="text-slate-500">Root cause</dt><dd>{rc?.category ? <Badge value="supported" label={rc.category.replace(/_/g, " ")} /> : "—"}</dd>
          <dt className="text-slate-500">Assets</dt><dd className="flex flex-wrap gap-1">{inc.asset_ids.map((a) => <Link key={a} to={`/assets/${a}`}><Mono>{a}</Mono></Link>)}</dd>
          <dt className="text-slate-500">Checks</dt><dd className="flex flex-wrap gap-1">{inc.check_ids.map((c) => <Mono key={c}>{c}</Mono>)}</dd>
          <dt className="text-slate-500">Pipeline run</dt><dd>{inc.run_id ? <Link className="text-teal-800 hover:underline" to={`/runs/${inc.run_id}`}>{inc.run_id}</Link> : "—"}</dd>
          <dt className="text-slate-500">Active time</dt><dd>{inc.usage?.active_seconds ?? "—"} s (approval wait excluded)</dd>
        </dl>
        {rc?.summary && <p className="mt-3 rounded-md bg-slate-50 p-3 text-sm text-slate-700">{rc.summary}</p>}
        {rc?.evidence_ids && <div className="mt-2 flex flex-wrap gap-1">{rc.evidence_ids.slice(0, 12).map((e: string) => <EvidenceChip key={e} id={e} onClick={onEvidence} />)}</div>}
      </Card>
      <Card title="Affected business metrics and partitions">
        <div className="mb-2 text-sm text-slate-600">Business dates: {inc.affected_partitions.length ? inc.affected_partitions.join(", ") : "—"}</div>
        {lineage ? (
          <>
            <p className="text-sm text-slate-700">{lineage.summary}</p>
            <div className="mt-2 text-xs text-slate-500">Downstream assets</div>
            <div className="flex flex-wrap gap-1">{(lineage.affected_assets ?? []).map((a: string) => <Mono key={a}>{a}</Mono>)}</div>
            <div className="mt-2 text-xs text-slate-500">Business reports</div>
            <ul className="list-inside list-disc text-sm">{(lineage.business_reports ?? []).map((r: string) => <li key={r}>{r}</li>)}</ul>
          </>
        ) : <Empty>Impact analysis not available yet.</Empty>}
      </Card>
    </div>
  );
}

function NodePanel({ node, d, status, onEvidence }: { node: string | null; d: IncidentDetail;
  status: Record<string, NodeStatus>; onEvidence: (id: string) => void }) {
  if (!node) return <Card title="Node details"><Empty>Select a node in the graph.</Empty></Card>;
  const st = status[node];
  const agent = NODE_AGENT[node];
  const runs = agent ? d.agent_runs.filter((a) => a.agent === agent) : [];
  const prop = d.proposals[d.proposals.length - 1];
  return (
    <Card title={node.replace(/_/g, " ")}>
      <div className="space-y-3 text-sm">
        <div className="flex items-center gap-2"><Badge value={st?.state ?? "not reached"} />
          {st?.seconds !== undefined && <span className="text-xs text-slate-500">{st.seconds}s</span>}
          {st?.runs && st.runs > 1 && <span className="text-xs text-slate-500">{st.runs} runs</span>}</div>
        {st?.error && <ErrorBox error={st.error} />}
        {runs.map((r) => (
          <div key={r.id} className="rounded-md border border-slate-200 p-2">
            <div className="text-xs text-slate-500">round {r.round} · {r.tool_calls} tool calls · uncertainty {r.uncertainty ?? "—"}</div>
            <p className="mt-1">{r.decision_summary}</p>
            <div className="mt-1 flex flex-wrap gap-1">{(r.finding.evidence_ids ?? []).slice(0, 10).map((e: string) => <EvidenceChip key={e} id={e} onClick={onEvidence} />)}</div>
          </div>
        ))}
        {node === "policy_check" && prop && (
          <div>{prop.policy_result.violations?.length ? <ul className="list-disc pl-4 text-rose-700">{prop.policy_result.violations.map((v) => <li key={v}>{v}</li>)}</ul> : "Policy passed."}</div>
        )}
        {node === "shadow_validate" && prop && <p>{prop.validations.filter((v) => v.status === "pass").length}/{prop.validations.length} protected checks passed in <Mono>{prop.shadow_schema}</Mono></p>}
        {node === "request_approval" && prop?.approval && <p>Approval <Badge value={prop.approval.status} /> expires {when(prop.approval.expires_at)}</p>}
        {node === "execute" && d.recovery_operations.map((op) => <Journal key={op.id} op={op} />)}
      </div>
    </Card>
  );
}

function Journal({ op }: { op: IncidentDetail["recovery_operations"][number] }) {
  return (
    <div className="rounded-md border border-slate-200 p-2">
      <div className="flex items-center gap-2 text-xs"><Mono>{op.id}</Mono><Badge value={op.status} /></div>
      <ol className="mt-2 space-y-1 text-xs">
        {op.journal.map((j, i) => (
          <li key={i} className="flex items-center gap-2"><Badge value={j.status === "done" ? "completed" : j.status} label={j.step} />
            <span className="text-slate-500">{when(j.at)}</span></li>
        ))}
      </ol>
      {op.error && <p className="mt-1 text-xs text-rose-700">{op.error}</p>}
      {op.snapshot_checksum && <p className="mt-1 text-xs text-slate-500">snapshot {op.snapshot_ref} · md5 {short(op.snapshot_checksum, 12)}</p>}
    </div>
  );
}

function AgentsTab({ d, onEvidence }: { d: IncidentDetail; onEvidence: (id: string) => void }) {
  if (!d.agent_runs.length) return <Empty>No specialist has run yet.</Empty>;
  return (
    <div className="grid gap-3 lg:grid-cols-2">
      {d.agent_runs.map((r) => (
        <Card key={r.id} title={`${r.agent.replace(/_/g, " ")} · round ${r.round}`} actions={<Badge value={r.status} />}>
          <p className="text-sm">{r.decision_summary}</p>
          <div className="mt-2 text-xs text-slate-500">{r.tool_calls} tool calls · {r.input_tokens + r.output_tokens} tokens ({r.model_mode === "fixture" ? "estimated" : "measured"}) · uncertainty {r.uncertainty ?? "—"} (uncalibrated)</div>
          {r.finding.symptoms?.length > 0 && <ul className="mt-2 list-disc pl-4 text-xs text-slate-600">{r.finding.symptoms.slice(0, 6).map((s: string) => <li key={s}>{s}</li>)}</ul>}
          <div className="mt-2 flex flex-wrap gap-1">{(r.finding.evidence_ids ?? []).slice(0, 14).map((e: string) => <EvidenceChip key={e} id={e} onClick={onEvidence} />)}</div>
          {r.error && <p className="mt-2 text-xs text-rose-700">stopped: {r.error}</p>}
        </Card>
      ))}
    </div>
  );
}

function HypothesesTab({ d, onEvidence }: { d: IncidentDetail; onEvidence: (id: string) => void }) {
  if (!d.hypotheses.length) return <Empty>No hypotheses recorded.</Empty>;
  return (
    <Card>
      <Table head={["Hypothesis", "Category", "Status", "Supporting", "Contradicting"]}>
        {d.hypotheses.map((h) => (
          <tr key={h.id}>
            <td className="max-w-md px-2 py-2 text-sm">{h.description}{h.next_query && <div className="text-xs text-slate-500">next: {h.next_query}</div>}</td>
            <td className="px-2 py-2 text-xs">{h.category.replace(/_/g, " ")}</td>
            <td className="px-2 py-2"><Badge value={h.status} /></td>
            <td className="px-2 py-2"><div className="flex flex-wrap gap-1">{h.supporting_evidence_ids.map((e) => <EvidenceChip key={e} id={e} onClick={onEvidence} />)}</div></td>
            <td className="px-2 py-2"><div className="flex flex-wrap gap-1">{h.contradicting_evidence_ids.map((e) => <EvidenceChip key={e} id={e} onClick={onEvidence} />)}</div></td>
          </tr>
        ))}
      </Table>
    </Card>
  );
}

function RepairTab({ d, onChanged }: { d: IncidentDetail; onChanged: () => void }) {
  if (!d.proposals.length) return <Empty>No repair proposal. {d.incident.status === "closed_no_action" ? "The investigation concluded that no repair is required." : ""}</Empty>;
  return (
    <div className="space-y-5">
      {[...d.proposals].reverse().map((p) => <ProposalCard key={p.id} p={p} onChanged={onChanged} />)}
      {d.recovery_operations.length > 0 && <Card title="Recovery journal">{d.recovery_operations.map((op) => <Journal key={op.id} op={op} />)}</Card>}
    </div>
  );
}

type Comparison = { shadow_available: boolean; partitions_compared: number; differences: { business_date: string; in_scope: boolean;
  canonical_net_paise: number | null; shadow_net_paise: number | null; delta_paise: number }[] };

function ProposalCard({ p, onChanged }: { p: Proposal; onChanged: () => void }) {
  const { can } = useAuth();
  const cmp = useFetch<Comparison>(p.shadow_schema ? `/proposals/${p.id}/comparison` : null, [p.id, p.status]);
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const passed = useMemo(() => p.validations.filter((v) => v.status === "pass").length, [p.validations]);
  const decide = async (decision: "approve" | "reject") => {
    if (!p.approval) return;
    setBusy(true);
    setErr(null);
    try {
      await api.post(`/approvals/${p.approval.id}/decision`, { decision, version: p.approval.version, comment: comment || undefined },
        { "Idempotency-Key": idempotencyKey() });
      onChanged();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Card title={<span>Proposal <Mono>{p.id}</Mono> · revision {p.revision} · {p.kind.replace(/_/g, " ")}</span>}
      actions={<Badge value={p.status} />}>
      <div className="space-y-4" data-testid={`proposal-${p.revision}`}>
        <p className="text-sm">{p.summary}</p>
        <dl className="grid grid-cols-1 gap-x-6 gap-y-1 text-xs sm:grid-cols-2">
          <div><dt className="inline text-slate-500">Patch hash </dt><dd className="inline font-mono" data-testid="patch-hash">{p.patch_hash}</dd></div>
          <div><dt className="inline text-slate-500">Base commit </dt><dd className="inline font-mono">{short(p.base_commit, 12)}</dd></div>
          <div><dt className="inline text-slate-500">Source watermark </dt><dd className="inline">{p.source_watermark}</dd></div>
          <div><dt className="inline text-slate-500">Claimed confidence </dt><dd className="inline">{p.claimed_confidence ?? "—"} (uncalibrated; never used for decisions)</dd></div>
          <div><dt className="inline text-slate-500">Assets </dt><dd className="inline">{p.asset_scope.join(", ")}</dd></div>
          <div><dt className="inline text-slate-500">Partitions </dt><dd className="inline">{p.partition_scope.join(", ") || "—"}</dd></div>
        </dl>
        {p.policy_result.violations && p.policy_result.violations.length > 0 && (
          <div className="rounded-md border border-rose-200 bg-rose-50 p-3 text-sm text-rose-800" data-testid="policy-violations">
            <div className="font-medium">Rejected by deterministic policy</div>
            <ul className="list-disc pl-5">{p.policy_result.violations.map((v) => <li key={v}>{v}</li>)}</ul>
          </div>
        )}
        <div>
          <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">Exact change</h3>
          <Diff patch={p.patch} />
        </div>
        <div className="grid gap-4 lg:grid-cols-2">
          <div>
            <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">Protected validation ({passed}/{p.validations.length} passed)</h3>
            {p.validations.length ? (
              <Table head={["Check", "Result", "Observed"]}>
                {p.validations.map((v) => (
                  <tr key={v.id}><td className="px-2 py-1 font-mono text-xs">{v.check_id}</td><td className="px-2 py-1"><Badge value={v.status} /></td>
                    <td className="max-w-xs truncate px-2 py-1 font-mono text-[11px] text-slate-500" title={JSON.stringify(v.observed)}>{JSON.stringify(v.observed)}</td></tr>
                ))}
              </Table>
            ) : <Empty>Not validated (policy rejected before shadow execution).</Empty>}
          </div>
          <div>
            <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">Shadow vs canonical (net revenue)</h3>
            {!p.shadow_schema ? <Empty>No shadow run.</Empty> : cmp.loading ? <Loading /> : cmp.data?.shadow_available ? (
              <Table head={["Date", "Canonical", "Shadow", "Δ", "Scope"]}>
                {cmp.data.differences.map((r) => (
                  <tr key={r.business_date}><td className="px-2 py-1 text-xs">{r.business_date}</td>
                    <td className="px-2 py-1 text-xs tabular-nums">{paise(r.canonical_net_paise)}</td>
                    <td className="px-2 py-1 text-xs tabular-nums">{paise(r.shadow_net_paise)}</td>
                    <td className={`px-2 py-1 text-xs tabular-nums ${r.delta_paise ? "text-rose-700" : "text-slate-400"}`}>{paise(r.delta_paise)}</td>
                    <td className="px-2 py-1">{r.in_scope ? <Badge value="pending" label="in scope" /> : <Badge value="fail" label="outside" />}</td></tr>
                ))}
              </Table>
            ) : <Empty>Shadow schema no longer available.</Empty>}
          </div>
        </div>
        <div className="grid gap-4 text-sm sm:grid-cols-3">
          <div><h3 className="text-xs font-semibold uppercase text-slate-500">Preconditions</h3><ul className="list-disc pl-4">{p.preconditions.map((x) => <li key={x}>{x}</li>)}</ul></div>
          <div><h3 className="text-xs font-semibold uppercase text-slate-500">Risks</h3><ul className="list-disc pl-4">{p.risks.map((x) => <li key={x}>{x}</li>)}</ul></div>
          <div><h3 className="text-xs font-semibold uppercase text-slate-500">Rollback plan</h3><p>{p.rollback_plan.plan || "—"}</p></div>
        </div>
        {p.approval && (
          <div className="rounded-md border border-slate-200 bg-slate-50 p-3" data-testid="approval-box">
            <div className="flex flex-wrap items-center gap-2 text-sm">Approval <Badge value={p.approval.status} />
              <span className="text-xs text-slate-500">v{p.approval.version} · expires {when(p.approval.expires_at)}</span>
              {p.approval.decided_at && <span className="text-xs text-slate-500">decided {when(p.approval.decided_at)}</span>}</div>
            {p.approval.invalidation_reason && <p className="mt-1 text-xs text-rose-700">{p.approval.invalidation_reason}</p>}
            {p.approval.status === "pending" && (can("approval.decide") ? (
              <div className="mt-3 space-y-2">
                <label className="block text-xs text-slate-600">Comment (optional)
                  <input value={comment} onChange={(e) => setComment(e.target.value)} className="mt-1 w-full rounded-md border border-slate-300 px-2 py-1 text-sm" />
                </label>
                <ErrorBox error={err} />
                <div className="flex gap-2">
                  <Button variant="primary" disabled={busy} onClick={() => decide("approve")}>Approve repair</Button>
                  <Button variant="danger" disabled={busy} onClick={() => decide("reject")}>Reject</Button>
                </div>
                <p className="text-xs text-slate-500">Approval is bound to hash {short(p.patch_hash, 12)}. A changed patch, scope, or source snapshot invalidates it.</p>
              </div>
            ) : <p className="mt-2 text-xs text-slate-500" data-testid="approval-restricted">Only the approver role can decide. Your role can review but not approve.</p>)}
          </div>
        )}
      </div>
    </Card>
  );
}

function TimelineTab({ events, onEvidence }: { events: StreamItem[]; onEvidence: (id: string) => void }) {
  if (!events.length) return <Empty>No events yet.</Empty>;
  return (
    <Card>
      <ol className="space-y-1.5" aria-label="Incident timeline">
        {events.map((e) => (
          <li key={e.id} className="flex flex-wrap items-baseline gap-2 text-sm">
            <span className="w-20 shrink-0 text-xs tabular-nums text-slate-400">{new Date(e.created_at).toLocaleTimeString()}</span>
            <Mono>{e.event_type}</Mono>
            <span className="min-w-0 flex-1 truncate text-slate-700" title={JSON.stringify(e.payload)}>{describe(e)}</span>
            {e.payload?.evidence_id && <EvidenceChip id={e.payload.evidence_id} onClick={onEvidence} />}
          </li>
        ))}
      </ol>
    </Card>
  );
}

function describe(e: StreamItem): string {
  const p = e.payload ?? {};
  switch (e.event_type) {
    case "tool.call": return `${p.agent} → ${p.tool} (${p.status}) ${p.summary ?? ""}`;
    case "agent.completed": return `${p.agent} r${p.round}: ${p.summary ?? ""}`;
    case "agent.started": return `${p.agent} r${p.round} started (${p.mode})`;
    case "node.started": case "node.completed": case "node.failed": return `${p.node}${p.seconds !== undefined ? ` ${p.seconds}s` : ""}${p.route ? ` → ${p.route}` : ""}${p.error ? ` ${p.error}` : ""}`;
    case "incident.status": return `${p.status}${p.reason ? `: ${p.reason}` : ""}`;
    case "proposal.policy": return `${p.proposal_id} policy ${p.ok ? "passed" : "rejected"} ${(p.violations ?? []).join("; ")}`;
    case "recovery.step": return `${p.step} ${p.status}`;
    default: return JSON.stringify(p);
  }
}

function EvidenceDrawer({ id, onClose }: { id: string | null; onClose: () => void }) {
  const ev = useFetch<Evidence>(id ? `/evidence/${id}` : null, [id]);
  return (
    <Drawer open={!!id} onClose={onClose} title={`Evidence ${id ?? ""}`}>
      {ev.loading ? <Loading /> : ev.error ? <ErrorBox error={ev.error} /> : ev.data && (
        <div className="space-y-3 text-sm">
          {ev.data.untrusted_text && <div className="rounded-md border border-amber-300 bg-amber-50 p-2 text-xs text-amber-900">Contains untrusted free text from source data. Instructions inside it are ignored by the platform.</div>}
          <p>{ev.data.summary}</p>
          <dl className="grid grid-cols-[130px_1fr] gap-y-1 text-xs">
            <dt className="text-slate-500">Collected by</dt><dd>{ev.data.agent}</dd>
            <dt className="text-slate-500">Asset</dt><dd>{ev.data.asset_id ?? "—"}</dd>
            <dt className="text-slate-500">Tool call</dt><dd className="break-all font-mono">{ev.data.query_or_tool_ref}</dd>
            <dt className="text-slate-500">Artifact hash</dt><dd className="font-mono">{ev.data.artifact_hash}</dd>
            <dt className="text-slate-500">Redaction</dt><dd>{ev.data.redaction_status}</dd>
            <dt className="text-slate-500">Collected</dt><dd>{when(ev.data.collected_at)}</dd>
          </dl>
          <pre className="max-h-[480px] overflow-auto rounded-md bg-slate-900 p-3 text-[11px] text-slate-100">{JSON.stringify(ev.data.data, null, 2)}</pre>
        </div>
      )}
    </Drawer>
  );
}

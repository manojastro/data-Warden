import { useState } from "react";
import { Link } from "react-router-dom";
import type { Incident, Page } from "../api";
import { Badge, Button, Card, Empty, ErrorBox, Loading, Table } from "../components/ui";
import { ago } from "../format";
import { useFetch, useStream } from "../hooks";

const FILTERS = ["active", "awaiting_approval", "resolved", "closed_no_action", "escalated", "manual_intervention", ""];

export default function Incidents() {
  const [status, setStatus] = useState("");
  const [offset, setOffset] = useState(0);
  const q = `/incidents?limit=25&offset=${offset}${status ? `&status=${status}` : ""}`;
  const { data, error, loading, reload } = useFetch<Page<Incident>>(q, [q]);
  useStream(null, (e) => e.event_type.startsWith("incident.") && reload());
  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Incidents</h1>
      <div className="flex flex-wrap gap-2" role="group" aria-label="Filter by status">
        {FILTERS.map((f) => (
          <button key={f || "all"} onClick={() => { setStatus(f); setOffset(0); }} aria-pressed={status === f}
            className={`rounded-full px-3 py-1 text-xs ring-1 ${status === f ? "bg-teal-700 text-white ring-teal-700" : "bg-white text-slate-600 ring-slate-300"}`}>
            {f ? f.replace(/_/g, " ") : "all"}
          </button>
        ))}
      </div>
      <Card>
        <ErrorBox error={error} />
        {loading ? <Loading /> : data?.items.length ? (
          <>
            <Table head={["#", "Title", "Severity", "Status", "Mode", "Partitions", "Opened"]}>
              {data.items.map((i) => (
                <tr key={i.id} className="hover:bg-slate-50">
                  <td className="px-2 py-2 text-xs text-slate-500">INC-{i.number}</td>
                  <td className="max-w-md px-2 py-2"><Link to={`/incidents/${i.id}`} className="line-clamp-2 text-teal-800 hover:underline">{i.title}</Link></td>
                  <td className="px-2 py-2"><Badge value={i.severity} /></td>
                  <td className="px-2 py-2"><Badge value={i.status} /></td>
                  <td className="px-2 py-2"><Badge value={i.model_mode ?? "—"} /></td>
                  <td className="px-2 py-2 text-xs text-slate-600">{i.affected_partitions.length}</td>
                  <td className="px-2 py-2 text-xs text-slate-500">{ago(i.created_at)}</td>
                </tr>
              ))}
            </Table>
            <div className="mt-3 flex items-center justify-between text-xs text-slate-500">
              <span>{data.total} incident(s)</span>
              <div className="flex gap-2">
                <Button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 25))}>Previous</Button>
                <Button disabled={offset + 25 >= (data.total ?? 0)} onClick={() => setOffset(offset + 25)}>Next</Button>
              </div>
            </div>
          </>
        ) : <Empty>No incidents match.</Empty>}
      </Card>
    </div>
  );
}

import { useCallback, useEffect, useRef, useState } from "react";
import { api, type StreamItem } from "./api";

export function useFetch<T>(path: string | null, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const reload = useCallback(async () => {
    if (!path) return;
    try {
      const d = await api.get<T>(path);
      setData(d);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, ...deps]);
  useEffect(() => {
    setLoading(true);
    reload();
  }, [reload]);
  return { data, error, loading, reload };
}

/** Live server-sent events with persisted ids. EventSource resends Last-Event-ID on reconnect. */
export function useStream(incidentId: string | null | undefined, onEvent: (e: StreamItem) => void) {
  const handler = useRef(onEvent);
  handler.current = onEvent;
  const [connected, setConnected] = useState(false);
  useEffect(() => {
    const url = `/api/v1/stream${incidentId ? `?incident_id=${encodeURIComponent(incidentId)}` : ""}`;
    const es = new EventSource(url, { withCredentials: true });
    const types = ["incident.opened", "incident.status", "incident.event_attached", "node.started", "node.completed",
      "node.failed", "agent.started", "agent.completed", "tool.call", "proposal.policy", "approval.requested",
      "approval.decided", "approval.expired", "recovery.step"];
    const listener = (ev: MessageEvent) => {
      const d = JSON.parse(ev.data);
      handler.current({ id: Number(ev.lastEventId), event_type: ev.type, payload: d.payload, created_at: d.created_at,
        incident_id: d.incident_id });
    };
    types.forEach((t) => es.addEventListener(t, listener));
    es.onopen = () => setConnected(true);
    es.onerror = () => setConnected(false);
    return () => es.close();
  }, [incidentId]);
  return connected;
}

export function useInterval(fn: () => void, ms: number) {
  const saved = useRef(fn);
  saved.current = fn;
  useEffect(() => {
    const id = setInterval(() => saved.current(), ms);
    return () => clearInterval(id);
  }, [ms]);
}

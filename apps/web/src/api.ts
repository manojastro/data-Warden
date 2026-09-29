// Thin API client: same-origin cookies, CSRF header on mutations, typed JSON, useful errors.
let csrfToken = "";

export function setCsrf(token: string) {
  csrfToken = token;
}

export class ApiError extends Error {
  constructor(public status: number, message: string, public requestId?: string) {
    super(message);
  }
}

async function request<T>(method: string, path: string, body?: unknown, headers: Record<string, string> = {}): Promise<T> {
  const res = await fetch(`/api/v1${path}`, {
    method,
    credentials: "same-origin",
    headers: {
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      ...(method !== "GET" ? { "X-CSRF-Token": csrfToken } : {}),
      ...headers,
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  const data = text ? JSON.parse(text) : {};
  if (!res.ok) {
    const msg = typeof data.error === "string" ? data.error : JSON.stringify(data.error ?? data);
    throw new ApiError(res.status, msg, data.request_id);
  }
  return data as T;
}

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body: unknown = {}, headers?: Record<string, string>) => request<T>("POST", path, body, headers),
};

export function idempotencyKey(): string {
  return crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

// ---- types (subset of the API) ------------------------------------------------------------
export type Incident = {
  id: string; number: number; title: string; status: string; severity: string; asset_ids: string[];
  check_ids: string[]; affected_partitions: string[]; run_id: string | null; terminal_reason: string | null;
  summary: Record<string, any>; usage: Record<string, any>; created_at: string; updated_at: string;
  closed_at: string | null; model_mode: string | null; graph_version: string | null; version: number;
};
export type AgentRun = {
  id: string; agent: string; round: number; status: string; model_mode: string; decision_summary: string;
  finding: Record<string, any>; uncertainty: string | null; tool_calls: number; input_tokens: number;
  output_tokens: number; error: string | null; started_at: string; finished_at: string | null;
};
export type Hypothesis = {
  id: string; category: string; description: string; status: string; supporting_evidence_ids: string[];
  contradicting_evidence_ids: string[]; next_query: string | null; agent_run_id: string | null;
};
export type Validation = { id: number; check_id: string; status: string; expected: any; observed: any; proposal_hash: string };
export type Approval = {
  id: string; status: string; version: number; expires_at: string; decided_by: string | null; decided_at: string | null;
  comment: string | null; invalidation_reason: string | null; proposal_hash: string;
};
export type Proposal = {
  id: string; revision: number; kind: string; summary: string; patch: string; patch_hash: string; base_commit: string;
  source_watermark: number; asset_scope: string[]; partition_scope: string[]; preconditions: string[]; risks: string[];
  rollback_plan: { plan: string; files: string[] }; claimed_confidence: number | null; status: string;
  policy_result: { ok?: boolean; violations?: string[]; files?: Record<string, string> }; shadow_schema: string | null;
  validations: Validation[]; approval: Approval | null; created_at: string;
};
export type RecoveryOp = {
  id: string; status: string; journal: { step: string; status: string; at: string; [k: string]: any }[];
  snapshot_ref: string | null; snapshot_checksum: string | null; pre_commit: string | null; post_commit: string | null;
  error: string | null; started_at: string; finished_at: string | null; incident_id: string; lock_keys: string[];
};
export type IncidentDetail = {
  incident: Incident; events: any[]; agent_runs: AgentRun[]; hypotheses: Hypothesis[]; evidence_count: number;
  proposals: Proposal[]; recovery_operations: RecoveryOp[];
  model_usage: { input_tokens: number; output_tokens: number; estimated_cost_usd: number | null; calls: number };
};
export type Evidence = {
  id: string; agent: string | null; asset_id: string | null; query_or_tool_ref: string; artifact_hash: string;
  summary: string; redaction_status: string; untrusted_text: boolean; data: any; collected_at: string;
};
export type StreamItem = { id: number; event_type: string; payload: any; created_at: string; incident_id?: string | null };
export type Page<T> = { items: T[]; total?: number; limit?: number; offset?: number };

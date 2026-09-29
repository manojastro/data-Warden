import { useId, type ReactNode, type KeyboardEvent } from "react";

export function Card({ title, actions, children, className = "" }: { title?: ReactNode; actions?: ReactNode;
  children: ReactNode; className?: string }) {
  return (
    <section className={`rounded-lg border border-slate-200 bg-white shadow-sm ${className}`}>
      {(title || actions) && (
        <header className="flex items-center justify-between gap-3 border-b border-slate-100 px-4 py-2.5">
          <h2 className="text-sm font-semibold text-slate-700">{title}</h2>
          <div className="flex items-center gap-2">{actions}</div>
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}

const TONES: Record<string, string> = {
  pass: "bg-emerald-50 text-emerald-700 ring-emerald-200", resolved: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  succeeded: "bg-emerald-50 text-emerald-700 ring-emerald-200", approved: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  applied: "bg-emerald-50 text-emerald-700 ring-emerald-200", success: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  validated: "bg-emerald-50 text-emerald-700 ring-emerald-200", completed: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  connected: "bg-emerald-50 text-emerald-700 ring-emerald-200", ok: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  closed_no_action: "bg-sky-50 text-sky-700 ring-sky-200", supported: "bg-sky-50 text-sky-700 ring-sky-200",
  investigating: "bg-indigo-50 text-indigo-700 ring-indigo-200", running: "bg-indigo-50 text-indigo-700 ring-indigo-200",
  open: "bg-indigo-50 text-indigo-700 ring-indigo-200", recovering: "bg-indigo-50 text-indigo-700 ring-indigo-200",
  validating: "bg-indigo-50 text-indigo-700 ring-indigo-200", proposed: "bg-slate-100 text-slate-700 ring-slate-200",
  awaiting_approval: "bg-amber-50 text-amber-800 ring-amber-200", pending: "bg-amber-50 text-amber-800 ring-amber-200",
  waiting: "bg-amber-50 text-amber-800 ring-amber-200", warn: "bg-amber-50 text-amber-800 ring-amber-200",
  warning: "bg-amber-50 text-amber-800 ring-amber-200", inconclusive: "bg-amber-50 text-amber-800 ring-amber-200",
  high: "bg-orange-50 text-orange-700 ring-orange-200", escalated: "bg-orange-50 text-orange-700 ring-orange-200",
  rolled_back: "bg-orange-50 text-orange-700 ring-orange-200", disabled: "bg-slate-100 text-slate-600 ring-slate-200",
  fail: "bg-rose-50 text-rose-700 ring-rose-200", failed: "bg-rose-50 text-rose-700 ring-rose-200",
  error: "bg-rose-50 text-rose-700 ring-rose-200", critical: "bg-rose-50 text-rose-700 ring-rose-200",
  rejected: "bg-rose-50 text-rose-700 ring-rose-200", policy_rejected: "bg-rose-50 text-rose-700 ring-rose-200",
  validation_failed: "bg-rose-50 text-rose-700 ring-rose-200", invalidated: "bg-rose-50 text-rose-700 ring-rose-200",
  manual_intervention: "bg-rose-100 text-rose-800 ring-rose-300", refuted: "bg-slate-100 text-slate-500 ring-slate-200",
  unavailable: "bg-rose-50 text-rose-700 ring-rose-200", conflict: "bg-rose-50 text-rose-700 ring-rose-200",
  denied: "bg-rose-50 text-rose-700 ring-rose-200", cancelled: "bg-slate-100 text-slate-500 ring-slate-200",
  expired: "bg-slate-100 text-slate-500 ring-slate-200", fixture: "bg-violet-50 text-violet-700 ring-violet-200",
  live: "bg-teal-50 text-teal-700 ring-teal-200",
};

export function Badge({ value, label }: { value: string | null | undefined; label?: string }) {
  const v = value ?? "unknown";
  return (
    <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${TONES[v] ?? "bg-slate-100 text-slate-700 ring-slate-200"}`}>
      {label ?? v.replace(/_/g, " ")}
    </span>
  );
}

export function Stat({ label, value, hint }: { label: string; value: ReactNode; hint?: ReactNode }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white px-4 py-3 shadow-sm">
      <div className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</div>
      <div className="mt-1 text-2xl font-semibold tabular-nums">{value}</div>
      {hint && <div className="mt-0.5 text-xs text-slate-500">{hint}</div>}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="py-6 text-center text-sm text-slate-500">{children}</p>;
}

export function ErrorBox({ error }: { error: string | null }) {
  if (!error) return null;
  return <div role="alert" className="rounded-md border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-800">{error}</div>;
}

export function Loading() {
  return <div className="animate-pulse py-8 text-center text-sm text-slate-400" aria-busy="true">Loading…</div>;
}

export function Button({ children, onClick, variant = "secondary", disabled, type = "button", title: t }: {
  children: ReactNode; onClick?: () => void; variant?: "primary" | "secondary" | "danger"; disabled?: boolean;
  type?: "button" | "submit"; title?: string }) {
  const styles = {
    primary: "bg-teal-700 text-white hover:bg-teal-800 disabled:bg-teal-300",
    secondary: "bg-white text-slate-700 ring-1 ring-inset ring-slate-300 hover:bg-slate-50 disabled:text-slate-400",
    danger: "bg-rose-600 text-white hover:bg-rose-700 disabled:bg-rose-300",
  }[variant];
  return (
    <button type={type} onClick={onClick} disabled={disabled} title={t}
      className={`inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium shadow-sm transition disabled:cursor-not-allowed ${styles}`}>
      {children}
    </button>
  );
}

export function Tabs({ tabs, active, onChange }: { tabs: { id: string; label: ReactNode }[]; active: string;
  onChange: (id: string) => void }) {
  const base = useId();
  const onKey = (e: KeyboardEvent, i: number) => {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    const next = (i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length;
    onChange(tabs[next].id);
    document.getElementById(`${base}-${tabs[next].id}`)?.focus();
  };
  return (
    <div role="tablist" className="flex gap-1 overflow-x-auto border-b border-slate-200">
      {tabs.map((t, i) => (
        <button key={t.id} id={`${base}-${t.id}`} role="tab" aria-selected={active === t.id}
          tabIndex={active === t.id ? 0 : -1} onKeyDown={(e) => onKey(e, i)} onClick={() => onChange(t.id)}
          className={`-mb-px whitespace-nowrap border-b-2 px-3 py-2 text-sm font-medium ${active === t.id ? "border-teal-700 text-teal-800" : "border-transparent text-slate-500 hover:text-slate-800"}`}>
          {t.label}
        </button>
      ))}
    </div>
  );
}

export function Table({ head, children }: { head: ReactNode[]; children: ReactNode }) {
  return (
    <div className="overflow-x-auto">
      <table className="min-w-full text-sm">
        <thead>
          <tr className="border-b border-slate-200 text-left text-xs uppercase tracking-wide text-slate-500">
            {head.map((h, i) => <th key={i} scope="col" className="px-2 py-2 font-medium">{h}</th>)}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">{children}</tbody>
      </table>
    </div>
  );
}

export function Mono({ children }: { children: ReactNode }) {
  return <code className="rounded bg-slate-100 px-1 py-0.5 font-mono text-xs text-slate-700">{children}</code>;
}

export function Drawer({ open, onClose, title: t, children }: { open: boolean; onClose: () => void; title: ReactNode;
  children: ReactNode }) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-40 flex justify-end bg-slate-900/30" onClick={onClose}
      onKeyDown={(e) => e.key === "Escape" && onClose()} role="presentation">
      <aside role="dialog" aria-modal="true" aria-label={typeof t === "string" ? t : "details"}
        className="h-full w-full max-w-2xl overflow-y-auto bg-white shadow-xl" onClick={(e) => e.stopPropagation()}>
        <header className="sticky top-0 flex items-center justify-between border-b bg-white px-5 py-3">
          <h2 className="font-semibold">{t}</h2>
          <Button onClick={onClose}>Close</Button>
        </header>
        <div className="p-5">{children}</div>
      </aside>
    </div>
  );
}

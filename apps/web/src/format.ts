const inr = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 2 });

export const paise = (v: number | null | undefined) => (v === null || v === undefined ? "—" : inr.format(v / 100));
export const when = (iso: string | null | undefined) => (iso ? new Date(iso).toLocaleString() : "—");
export const short = (s: string | null | undefined, n = 10) => (s ? s.slice(0, n) : "—");
export const title = (s: string) => s.replace(/[_.]/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
export function ago(iso: string | null | undefined): string {
  if (!iso) return "—";
  const s = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

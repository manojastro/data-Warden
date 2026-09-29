export function Diff({ patch }: { patch: string }) {
  if (!patch) return <p className="text-sm text-slate-500">No code change (replay of existing trusted code).</p>;
  return (
    <pre className="max-h-[420px] overflow-auto rounded-md border border-slate-200 bg-slate-950 p-3 text-xs leading-5" data-testid="diff">
      {patch.split("\n").map((l, i) => (
        <div key={i} className={l.startsWith("+") && !l.startsWith("+++") ? "bg-emerald-900/50 text-emerald-200"
          : l.startsWith("-") && !l.startsWith("---") ? "bg-rose-900/50 text-rose-200"
          : l.startsWith("@@") ? "text-sky-300" : "text-slate-300"}>{l || " "}</div>
      ))}
    </pre>
  );
}

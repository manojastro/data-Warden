import { NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { useAuth } from "./auth";
import { Loading } from "./components/ui";
import Login from "./pages/Login";
import Overview from "./pages/Overview";
import Assets from "./pages/Assets";
import AssetDetail from "./pages/AssetDetail";
import Incidents from "./pages/Incidents";
import IncidentWorkspace from "./pages/IncidentWorkspace";
import Runs from "./pages/Runs";
import RunDetail from "./pages/RunDetail";
import Audit from "./pages/Audit";
import Evaluations from "./pages/Evaluations";
import Demo from "./pages/Demo";

const NAV = [
  { to: "/", label: "Overview" },
  { to: "/incidents", label: "Incidents" },
  { to: "/assets", label: "Assets & lineage" },
  { to: "/runs", label: "Pipeline runs" },
  { to: "/audit", label: "Audit & recovery" },
  { to: "/evaluations", label: "Evaluation" },
  { to: "/demo", label: "Demo controls" },
];

export default function App() {
  const { me, loading, logout } = useAuth();
  const loc = useLocation();
  if (loading) return <Loading />;
  if (!me) return loc.pathname === "/login" ? <Login /> : <Navigate to="/login" replace state={{ from: loc.pathname }} />;
  return (
    <div className="flex min-h-full flex-col md:flex-row">
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:m-2 focus:rounded focus:bg-white focus:p-2">Skip to content</a>
      <aside className="border-b border-slate-200 bg-white md:w-56 md:shrink-0 md:border-b-0 md:border-r">
        <div className="flex items-center gap-2 px-4 py-4">
          <div className="grid h-8 w-8 place-items-center rounded-md bg-teal-700 text-sm font-bold text-white" aria-hidden>DW</div>
          <div>
            <div className="text-sm font-semibold">DataWarden</div>
            <div className="text-[11px] font-medium uppercase tracking-wide text-amber-700">Synthetic demo data</div>
          </div>
        </div>
        <nav aria-label="Main" className="flex gap-1 overflow-x-auto px-2 pb-2 md:flex-col md:overflow-visible">
          {NAV.map((n) => (
            <NavLink key={n.to} to={n.to} end={n.to === "/"}
              className={({ isActive }) => `whitespace-nowrap rounded-md px-3 py-2 text-sm ${isActive ? "bg-teal-50 font-medium text-teal-800" : "text-slate-600 hover:bg-slate-100"}`}>
              {n.label}
            </NavLink>
          ))}
        </nav>
        <div className="hidden border-t border-slate-100 px-4 py-3 text-xs text-slate-500 md:block">
          Signed in as <span className="font-medium text-slate-700">{me.username}</span>
          <div>Role: <span data-testid="role" className="font-medium text-slate-700">{me.role}</span></div>
          <button onClick={logout} className="mt-2 text-teal-700 hover:underline">Sign out</button>
        </div>
      </aside>
      <main id="main" className="min-w-0 flex-1 px-4 py-5 md:px-8">
        <Routes>
          <Route path="/" element={<Overview />} />
          <Route path="/incidents" element={<Incidents />} />
          <Route path="/incidents/:id" element={<IncidentWorkspace />} />
          <Route path="/assets" element={<Assets />} />
          <Route path="/assets/:id" element={<AssetDetail />} />
          <Route path="/runs" element={<Runs />} />
          <Route path="/runs/:id" element={<RunDetail />} />
          <Route path="/audit" element={<Audit />} />
          <Route path="/evaluations" element={<Evaluations />} />
          <Route path="/demo" element={<Demo />} />
          <Route path="/login" element={<Navigate to="/" replace />} />
          <Route path="*" element={<p className="text-sm text-slate-500">Page not found.</p>} />
        </Routes>
      </main>
    </div>
  );
}

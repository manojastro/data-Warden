import { Link } from "react-router-dom";
import { LineageGraph } from "../components/LineageGraph";
import { Badge, Card, ErrorBox, Loading, Table } from "../components/ui";
import { useFetch } from "../hooks";

type Asset = { id: string; kind: string; owner: string; description: string; business_critical: boolean;
  recent_check_failures: { check_id: string; status: string }[] };

export default function Assets() {
  const assets = useFetch<{ items: Asset[] }>("/assets");
  const lineage = useFetch<{ nodes: Asset[]; edges: { upstream_asset_id: string; downstream_asset_id: string }[] }>("/lineage");
  const unhealthy = new Set((assets.data?.items ?? []).filter((a) => a.recent_check_failures.length).map((a) => a.id));
  return (
    <div className="space-y-5">
      <h1 className="text-xl font-semibold">Assets & data lineage</h1>
      <Card title="Data lineage (from the dbt manifest)">
        {lineage.data ? <LineageGraph nodes={lineage.data.nodes} edges={lineage.data.edges} unhealthy={unhealthy} /> : <Loading />}
        <p className="mt-2 text-xs text-slate-500">This is the data lineage graph. The incident execution graph is shown inside each incident.</p>
      </Card>
      <Card title="Catalog">
        <ErrorBox error={assets.error} />
        {assets.loading ? <Loading /> : (
          <Table head={["Asset", "Kind", "Owner", "Critical", "Health", "Description"]}>
            {(assets.data?.items ?? []).map((a) => (
              <tr key={a.id}>
                <td className="px-2 py-2"><Link to={`/assets/${a.id}`} className="font-mono text-xs text-teal-800 hover:underline">{a.id}</Link></td>
                <td className="px-2 py-2 text-xs">{a.kind}</td><td className="px-2 py-2 text-xs">{a.owner}</td>
                <td className="px-2 py-2 text-xs">{a.business_critical ? "yes" : "no"}</td>
                <td className="px-2 py-2">{a.recent_check_failures.length ? <Badge value="fail" label={`${a.recent_check_failures.length} failing`} /> : <Badge value="pass" label="healthy" />}</td>
                <td className="px-2 py-2 text-xs text-slate-600">{a.description}</td>
              </tr>
            ))}
          </Table>
        )}
      </Card>
    </div>
  );
}

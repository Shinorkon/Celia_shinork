import { History, ArrowRight } from "lucide-react";
import type { RecentRun } from "../api";

interface Props {
  data: RecentRun[];
}

const STATUS_BADGE: Record<string, string> = {
  completed: "badge-success",
  dispatched: "badge-warning",
  running: "badge-warning",
  failed: "badge-danger",
  blocked: "badge-danger",
};

export default function RunsTable({ data }: Props) {
  return (
    <div className="glass-card-hover p-5">
      <div className="flex items-center gap-2 mb-4">
        <History className="w-5 h-5 text-brand-400" />
        <h2 className="text-sm font-semibold tracking-wide uppercase">
          Recent Runs
        </h2>
        <span className="badge badge-neutral ml-auto">
          {data.length} entries
        </span>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-zinc-500 text-[11px] uppercase tracking-wider border-b border-zinc-800/60">
              <th className="pb-3 pr-4 font-medium">Run ID</th>
              <th className="pb-3 pr-4 font-medium">Agent</th>
              <th className="pb-3 pr-4 font-medium">Status</th>
              <th className="pb-3 pr-4 font-medium hidden sm:table-cell">
                Started
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-800/40">
            {data.length === 0 && (
              <tr>
                <td
                  colSpan={4}
                  className="py-10 text-center text-zinc-600 text-sm"
                >
                  No runs recorded yet.
                </td>
              </tr>
            )}
            {data.slice(0, 15).map((run) => (
              <tr
                key={run.run_id}
                className="group hover:bg-zinc-800/30 transition-colors"
              >
                <td className="py-2.5 pr-4 font-mono text-[11px] text-zinc-400">
                  {run.run_id.slice(0, 8)}...
                </td>
                <td className="py-2.5 pr-4">
                  <span className="text-zinc-200 font-medium">
                    {run.agent_role || "—"}
                  </span>
                </td>
                <td className="py-2.5 pr-4">
                  <span className={STATUS_BADGE[run.status] || "badge-neutral"}>
                    {run.status}
                  </span>
                </td>
                <td className="py-2.5 pr-4 text-zinc-500 text-[11px] hidden sm:table-cell">
                  {run.started_at
                    ? new Date(run.started_at).toLocaleTimeString()
                    : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

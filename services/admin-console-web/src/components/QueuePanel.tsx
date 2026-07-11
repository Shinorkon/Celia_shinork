import { BarChart3, Inbox, Send, CheckCheck } from "lucide-react";
import type { QueueDepth } from "../api";

interface Props {
  data: QueueDepth | null;
}

export default function QueuePanel({ data }: Props) {
  const ingress = data?.ingress_pending ?? 0;
  const orchestration = data?.orchestration_pending ?? 0;
  const worker = data?.worker_pending ?? 0;
  const total = ingress + orchestration + worker;

  return (
    <div className="glass-card-hover p-5 lg:col-span-2">
      <div className="flex items-center gap-2 mb-4">
        <BarChart3 className="w-5 h-5 text-brand-400" />
        <h2 className="text-sm font-semibold tracking-wide uppercase">
          Queue Depth
        </h2>
        <span className="badge badge-neutral ml-auto">{total} pending</span>
      </div>

      <div className="space-y-3">
        <QueueBar
          icon={Inbox}
          label="Ingress"
          value={ingress}
          color="bg-sky-500"
          max={Math.max(total, 1)}
        />
        <QueueBar
          icon={Send}
          label="Orchestration"
          value={orchestration}
          color="bg-amber-500"
          max={Math.max(total, 1)}
        />
        <QueueBar
          icon={CheckCheck}
          label="Completed"
          value={worker}
          color="bg-emerald-500"
          max={Math.max(total, 1)}
        />
      </div>
    </div>
  );
}

function QueueBar({
  icon: Icon,
  label,
  value,
  color,
  max,
}: {
  icon: typeof Inbox;
  label: string;
  value: number;
  color: string;
  max: number;
}) {
  const pct = max > 0 ? (value / max) * 100 : 0;
  return (
    <div>
      <div className="flex items-center justify-between text-xs mb-1">
        <span className="flex items-center gap-1.5 text-zinc-400">
          <Icon className="w-3.5 h-3.5" />
          {label}
        </span>
        <span className="font-mono font-semibold text-zinc-200">{value}</span>
      </div>
      <div className="h-2 bg-zinc-800 rounded-full overflow-hidden">
        <div
          className={`h-full rounded-full transition-all duration-700 ${color}`}
          style={{ width: `${Math.min(pct, 100)}%` }}
        />
      </div>
    </div>
  );
}

import { DollarSign, Zap, Hash } from "lucide-react";
import type { UsageSummary } from "../api";

interface Props {
  data: UsageSummary | null;
}

function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(n);
}

export default function UsageCard({ data }: Props) {
  return (
    <div className="glass-card-hover p-5">
      <div className="flex items-center gap-2 mb-4">
        <DollarSign className="w-5 h-5 text-brand-400" />
        <h2 className="text-sm font-semibold tracking-wide uppercase">
          Usage
        </h2>
        <span className="text-[11px] text-zinc-600 ml-auto">
          {data?.month || "—"}
        </span>
      </div>

      <div className="space-y-4">
        <div className="text-center">
          <div className="stat-value text-emerald-400">
            ${(data?.total_cost_usd ?? 0).toFixed(4)}
          </div>
          <div className="stat-label mt-1">Total Cost MTD</div>
        </div>

        <div className="grid grid-cols-2 gap-3">
          <div className="bg-zinc-800/50 rounded-xl p-3 text-center">
            <Zap className="w-4 h-4 text-amber-400 mx-auto mb-1" />
            <div className="text-lg font-bold font-mono">
              {fmtTokens(data?.total_prompt_tokens ?? 0)}
            </div>
            <div className="stat-label">Prompt</div>
          </div>
          <div className="bg-zinc-800/50 rounded-xl p-3 text-center">
            <Hash className="w-4 h-4 text-sky-400 mx-auto mb-1" />
            <div className="text-lg font-bold font-mono">
              {fmtTokens(data?.total_completion_tokens ?? 0)}
            </div>
            <div className="stat-label">Completion</div>
          </div>
        </div>
      </div>
    </div>
  );
}

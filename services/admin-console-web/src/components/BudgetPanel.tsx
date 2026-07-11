import { PieChartIcon } from "lucide-react";
import type { BudgetItem } from "../api";

interface Props {
  data: BudgetItem[];
}

export default function BudgetPanel({ data }: Props) {
  return (
    <div className="glass-card-hover p-5">
      <div className="flex items-center gap-2 mb-4">
        <PieChartIcon className="w-5 h-5 text-brand-400" />
        <h2 className="text-sm font-semibold tracking-wide uppercase">
          Budgets
        </h2>
      </div>

      {data.length === 0 && (
        <p className="text-zinc-600 text-sm py-6 text-center">
          No budget entries configured.
        </p>
      )}

      <div className="space-y-3">
        {data.map((b, i) => {
          const pct =
            b.monthly_limit_usd > 0
              ? (b.spent_usd / b.monthly_limit_usd) * 100
              : 0;
          const isOver = pct >= 100;
          const isWarn = pct >= 80 && !isOver;

          return (
            <div key={i} className="bg-zinc-800/40 rounded-xl p-3">
              <div className="flex items-center justify-between mb-1.5">
                <span className="text-xs font-medium text-zinc-300">
                  {b.scope_type}:{b.scope_id}
                </span>
                <span
                  className={`text-xs font-mono font-semibold ${
                    isOver
                      ? "text-red-400"
                      : isWarn
                      ? "text-amber-400"
                      : "text-zinc-400"
                  }`}
                >
                  ${b.spent_usd.toFixed(2)} / ${b.monthly_limit_usd.toFixed(2)}
                </span>
              </div>
              <div className="h-1.5 bg-zinc-700 rounded-full overflow-hidden">
                <div
                  className={`h-full rounded-full transition-all duration-500 ${
                    isOver
                      ? "bg-red-500"
                      : isWarn
                      ? "bg-amber-500"
                      : "bg-emerald-500"
                  }`}
                  style={{ width: `${Math.min(pct, 100)}%` }}
                />
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

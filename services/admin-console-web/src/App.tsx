import { useState, useEffect, useCallback, useRef } from "react";
import Header from "./components/Header";
import HealthGrid from "./components/HealthGrid";
import QueuePanel from "./components/QueuePanel";
import RunsTable from "./components/RunsTable";
import UsageCard from "./components/UsageCard";
import BudgetPanel from "./components/BudgetPanel";
import {
  api,
  Health,
  QueueDepth,
  RecentRun,
  UsageSummary,
  BudgetItem,
} from "./api";

const REFRESH_MS = 8_000;

export default function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [queue, setQueue] = useState<QueueDepth | null>(null);
  const [runs, setRuns] = useState<RecentRun[]>([]);
  const [usage, setUsage] = useState<UsageSummary | null>(null);
  const [budgets, setBudgets] = useState<BudgetItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const fetchAll = useCallback(async () => {
    try {
      const [h, q, r, u, b] = await Promise.all([
        api.health(),
        api.queueDepth(),
        api.recentRuns(),
        api.usageSummary(),
        api.budgets(),
      ]);
      setHealth(h);
      setQueue(q);
      setRuns(r);
      setUsage(u);
      setBudgets(b);
      setError(null);
    } catch (e) {
      setError(`API unreachable: ${e}`);
    }
  }, []);

  useEffect(() => {
    fetchAll();
    intervalRef.current = setInterval(fetchAll, REFRESH_MS);
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current);
    };
  }, [fetchAll]);

  return (
    <div className="min-h-screen pb-16">
      <Header
        onRefresh={fetchAll}
        healthy={health?.status === "ok"}
        error={!!error}
      />

      <main className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 mt-6 space-y-6">
        {error && (
          <div className="glass-card border-red-500/20 p-4 text-red-400 text-sm font-medium flex items-center gap-2">
            <span className="w-2 h-2 rounded-full bg-red-500 animate-pulse" />
            {error}
          </div>
        )}

        <HealthGrid services={health ? [{ name: health.service, status: health.status }] : []} />

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          <QueuePanel data={queue} />
          <UsageCard data={usage} />
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          <div className="lg:col-span-2">
            <RunsTable data={runs} />
          </div>
          <BudgetPanel data={budgets} />
        </div>
      </main>
    </div>
  );
}

// Proxied through Caddy: shnuk-admin.shinorkon.com/api/* → admin-console-api:8106
const API_BASE = "/api";

export interface Health {
  service: string;
  status: string;
  timestamp: string;
}

export interface QueueDepth {
  ingress_pending: number;
  orchestration_pending: number;
  worker_pending: number;
}

export interface RecentRun {
  run_id: string;
  status: string;
  agent_role: string;
  started_at: string;
}

export interface UsageSummary {
  month: string;
  total_cost_usd: number;
  total_prompt_tokens: number;
  total_completion_tokens: number;
}

export interface BudgetItem {
  scope_type: string;
  scope_id: string;
  monthly_limit_usd: number;
  spent_usd: number;
}

async function fetchJson<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

export const api = {
  health: () => fetchJson<Health>("/health"),
  queueDepth: () => fetchJson<QueueDepth>("/metrics/queue-depth"),
  recentRuns: () => fetchJson<RecentRun[]>("/runs/recent"),
  usageSummary: () => fetchJson<UsageSummary>("/usage/summary"),
  budgets: () => fetchJson<BudgetItem[]>("/budgets"),
};

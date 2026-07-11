import {
  Activity,
  Server,
  Shield,
  Clock,
  Layers,
  Workflow,
  BrainCircuit,
  LineChart,
} from "lucide-react";

const SERVICE_META: Record<string, { icon: typeof Activity; label: string }> = {
  "telegram-ingress": { icon: Activity, label: "Telegram Ingress" },
  orchestrator: { icon: Workflow, label: "Orchestrator" },
  "worker-runtime": { icon: BrainCircuit, label: "Worker Runtime" },
  scheduler: { icon: Clock, label: "Scheduler" },
  "policy-gateway": { icon: Shield, label: "Policy Gateway" },
  "admin-console-api": { icon: LineChart, label: "Admin API" },
  postgres: { icon: Server, label: "PostgreSQL" },
  redis: { icon: Layers, label: "Redis" },
  litellm: { icon: BrainCircuit, label: "LiteLLM" },
};

interface Props {
  services: { name: string; status: string }[];
}

export default function HealthGrid({ services }: Props) {
  const allNames = Object.keys(SERVICE_META);
  const map = new Map(services.map((s) => [s.name, s.status]));

  return (
    <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 gap-3">
      {allNames.map((name) => {
        const meta = SERVICE_META[name] || {
          icon: Activity,
          label: name,
        };
        const status = map.get(name);
        const isUp = status === "ok";
        const Icon = meta.icon;

        return (
          <div
            key={name}
            className={`glass-card-hover p-4 flex flex-col items-center gap-1.5 ${
              isUp ? "animate-pulse-glow" : "opacity-40"
            }`}
          >
            <div
              className={`w-10 h-10 rounded-xl flex items-center justify-center ${
                isUp
                  ? "bg-emerald-500/10 text-emerald-400"
                  : "bg-zinc-800 text-zinc-600"
              }`}
            >
              <Icon className="w-5 h-5" />
            </div>
            <span className="text-[11px] font-medium text-zinc-400 text-center leading-tight">
              {meta.label}
            </span>
            <span
              className={`badge ${
                isUp ? "badge-success" : "badge-neutral"
              }`}
            >
              {isUp ? "UP" : "—"}
            </span>
          </div>
        );
      })}
    </div>
  );
}

import {
  RefreshCw,
  Wifi,
  WifiOff,
  CircleDot,
  Zap,
} from "lucide-react";

interface Props {
  onRefresh: () => void;
  healthy: boolean;
  error: boolean;
}

export default function Header({ onRefresh, healthy, error }: Props) {
  return (
    <header className="sticky top-0 z-50 border-b border-zinc-800/60 bg-zinc-950/80 backdrop-blur-xl">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-16 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-xl bg-brand-600 flex items-center justify-center shadow-lg shadow-brand-500/20">
            <Zap className="w-5 h-5 text-white" fill="currentColor" />
          </div>
          <div>
            <h1 className="text-lg font-bold tracking-tight">
              Agent<span className="text-brand-400">Ops</span>
            </h1>
            <p className="text-[10px] text-zinc-500 uppercase tracking-widest">
              Orchestration Console
            </p>
          </div>
        </div>

        <div className="flex items-center gap-4">
          <div className="hidden sm:flex items-center gap-2 text-xs">
            {healthy ? (
              <>
                <Wifi className="w-3.5 h-3.5 text-emerald-400" />
                <span className="text-zinc-500">System Online</span>
              </>
            ) : error ? (
              <>
                <WifiOff className="w-3.5 h-3.5 text-red-400" />
                <span className="text-red-400">Disconnected</span>
              </>
            ) : (
              <>
                <CircleDot className="w-3.5 h-3.5 text-amber-400 animate-pulse" />
                <span className="text-zinc-500">Connecting...</span>
              </>
            )}
          </div>

          <button
            onClick={onRefresh}
            className="inline-flex items-center gap-2 px-4 py-2 rounded-xl bg-zinc-800 hover:bg-zinc-700 border border-zinc-700/50 text-sm font-medium transition-all active:scale-95"
          >
            <RefreshCw className="w-4 h-4" />
            <span className="hidden sm:inline">Refresh</span>
          </button>
        </div>
      </div>
    </header>
  );
}

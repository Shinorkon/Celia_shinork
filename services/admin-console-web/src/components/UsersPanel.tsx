import { useState } from "react";
import { api, UserItem } from "../api";

interface Props {
  data: UserItem[];
  onRefresh: () => void;
}

export default function UsersPanel({ data, onRefresh }: Props) {
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);

  const handleAuthorize = async () => {
    const id = parseInt(input.trim(), 10);
    if (!id) return;
    setLoading(true);
    try {
      await api.authorizeUser(id);
      setInput("");
      onRefresh();
    } catch (e) {
      alert(`Failed: ${e}`);
    } finally {
      setLoading(false);
    }
  };

  const handleToggle = async (user: UserItem) => {
    try {
      await api.toggleUser(user.id, !user.is_active);
      onRefresh();
    } catch (e) {
      alert(`Failed: ${e}`);
    }
  };

  const handleRemove = async (user: UserItem) => {
    if (!confirm(`Remove ${user.telegram_user_id}?`)) return;
    try {
      await api.removeUser(user.id);
      onRefresh();
    } catch (e) {
      alert(`Failed: ${e}`);
    }
  };

  const authorized = data.filter((u) => u.role === "authorized");

  return (
    <div className="glass-card p-5">
      <h2 className="text-sm font-semibold text-zinc-400 uppercase tracking-wider mb-4">
        Authorized Users
      </h2>

      {/* Add user */}
      <div className="flex gap-2 mb-4">
        <input
          type="text"
          placeholder="Telegram User ID"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && handleAuthorize()}
          className="flex-1 bg-zinc-800 border border-zinc-700 rounded-lg px-3 py-2 text-sm text-zinc-200 placeholder-zinc-500 focus:outline-none focus:border-emerald-500"
        />
        <button
          onClick={handleAuthorize}
          disabled={loading || !input.trim()}
          className="px-4 py-2 bg-emerald-600 hover:bg-emerald-500 disabled:opacity-40 text-white text-sm font-medium rounded-lg transition-colors"
        >
          {loading ? "..." : "Authorize"}
        </button>
      </div>

      {/* Stats */}
      <div className="flex gap-4 mb-4 text-xs text-zinc-500">
        <span>
          <strong className="text-emerald-400">{authorized.length}</strong>{" "}
          authorized
        </span>
        <span>
          <strong className="text-zinc-300">{data.length}</strong> total
        </span>
      </div>

      {/* User list */}
      <div className="space-y-1 max-h-64 overflow-y-auto">
        {data.length === 0 && (
          <p className="text-xs text-zinc-600 py-4 text-center">
            No users yet. Add your Telegram ID above.
          </p>
        )}
        {data.map((u) => {
          const isAuth = u.role === "authorized" && u.is_active;
          return (
            <div
              key={u.id}
              className={`flex items-center justify-between px-3 py-2 rounded-lg text-sm ${
                isAuth
                  ? "bg-emerald-500/10 border border-emerald-500/20"
                  : "bg-zinc-800/50"
              }`}
            >
              <div className="flex items-center gap-2 min-w-0">
                <span
                  className={`w-2 h-2 rounded-full flex-shrink-0 ${
                    isAuth ? "bg-emerald-400" : "bg-zinc-600"
                  }`}
                />
                <span className="text-zinc-200 truncate font-mono text-xs">
                  {u.telegram_user_id}
                </span>
                <span className="text-zinc-600 text-xs capitalize">{u.role}</span>
              </div>
              <div className="flex items-center gap-1 flex-shrink-0">
                {u.role === "authorized" && (
                  <button
                    onClick={() => handleToggle(u)}
                    className="text-xs px-2 py-1 rounded hover:bg-zinc-700 text-zinc-400 hover:text-zinc-200 transition-colors"
                  >
                    {u.is_active ? "Disable" : "Enable"}
                  </button>
                )}
                <button
                  onClick={() => handleRemove(u)}
                  className="text-xs px-2 py-1 rounded hover:bg-red-500/20 text-zinc-500 hover:text-red-400 transition-colors"
                >
                  ✕
                </button>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

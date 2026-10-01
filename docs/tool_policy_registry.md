# Tool + policy registration (Life OS)

**Rule:** a new tool is not done until all three exist:

1. **Schema** — register the OpenAI-style function schema on the roles that may call it in `services/worker-runtime/app/llm_client.py` (`TOOL_SCHEMAS` / `register_tool`).
2. **POLICY_TABLE key** — add an explicit action key in `services/telegram-ingress/app/side_effect_policy.py`. Unknown actions **default to `confirm`** (fail closed).
3. **Test** — assert the schema is wired for the role, the policy key resolves as intended, and prior finance/list/ops/memory/task/cal/note keys still pass.

## Where things live

| Concern | Module |
|---|---|
| LLM tool schemas + role → tools map | `worker-runtime/app/llm_client.py` |
| Side-effect auto / confirm / refuse | `telegram-ingress/app/side_effect_policy.py` |
| Quiet strip for chat + proactive pings | `telegram-ingress/app/quiet_mode.py` (worker applies the same strip on `life-reflect` notify) |

Do **not** scatter ad-hoc `if action == …` gates. Extend `POLICY_TABLE` and `TOOL_SCHEMAS`.

## Current worker tools (slice 5)

| Role | Tools | Notes |
|---|---|---|
| `coder` | `run_shell_command` | Policy-gateway gated |
| `memory-writer` | `save_memory_items` | DB only; no SSH |
| `ops-reflect` | `run_shell_command`, `notify_user` | Read-biased health; separate from life |
| `life-reflect` | `recall_memory`, `notify_user` | **No shell.** Proactive life ping only |

## Policy keys added for life-reflect

- `memory.recall` → `auto` (read path)
- `life.reflect` → `auto` (scheduled cycle itself)
- `life.reflect.notify` → `auto` (self-chat notify; still quiet-stripped + rate-limited)

## Phase D

### Phase D ops (Celia self-ops multi-step) — landed

| Role | Tools | Notes |
|---|---|---|
| `ops` | `ops_stack_status`, `ops_service_health`, `ops_host_resources`, `ops_container_logs`, `ops_edge_status`, `ops_restart_container` | Max 5 rounds; Celia/AOP stack on this VPS only; no raw shell; no other apps |

Policy: `ops.shell_read` → **auto** (ingress dispatches ops agent); `ops.shell_write` / `ops.deploy` / `ops.destructive` → **confirm**; other apps → **refuse**. Restart tool requires `ops_mutate_confirmed` from ingress confirm.

**Deferred:** arbitrary remote SSH fleet / multi-host ops — not in v1.

### Phase D1 (life agent) — landed

| Role | Tools | Notes |
|---|---|---|
| `life` | `create_reminder`, `list_reminders`, `cancel_reminder`, `create_task`, `list_tasks` | Max 5 rounds; Carlia short reply; no shell |

Policy keys reuse existing `reminder.*` / `task.*`. Ingress: `/start` → help; bulk clear list/memory before list-append; action-ish text → preferred `life` role.

### Phase D2 (life agent lists/cal/notes) — landed

| Role | Added tools | Policy |
|---|---|---|
| `life` | `create_list`, `show_list`, `add_list_items`, `remove_list_item`, `clear_list`, `mark_list_item_bought` | list.* auto |
| `life` | `create_calendar_event`, `list_calendar_events` | cal.create **confirm** (pending yes); cal.list auto |
| `life` | `add_note`, `list_notes` | note.* auto |

Compound multi-domain turns (list+remind, etc.) skip single-domain ingress handlers and go to the life tool loop.

### Phase D3 (memory + receipt session) — landed

| Role | Added tools | Policy |
|---|---|---|
| `life` | `memory_remember`, `memory_recall` | memory.write / memory.recall auto |
| `life` | `memory_forget`, `memory_correct` | **confirm** (Redis pending → yes) |
| `life` | `set_lower_text_amount_pref`, `recalculate_receipts` | finance.amount_pref / finance.recalculate auto |

Bulk wipe (`memory.forget_all`) stays confirm via ingress. Compound e.g. remember+remind → life agent.

### Phase D4 (finance writes on life) — landed

| Role | Added tools | Policy |
|---|---|---|
| `life` | `log_spend`, `record_expense`, `set_budget` | **finance.write** confirm (shared `finance_pending_logs`) |
| `life` | `spent_summary` | finance.read auto |

Clear single-line spends stay on the finance NL fast-path. Compound `spent … and remind me…` skips finance steal → life agent. Bare `what do you know` → memory recall. **Phase D life-agent build order complete.**

### Telegram relay (owner)

| Role | Tool | Policy |
|---|---|---|
| `life`, `frontoffice`, `comms` | `send_telegram_message` | **comms.third_party** confirm |

Owner ask ("pass a message to Raaish") routes to the life agent. Unknown names return `NEED_RECIPIENT` (ask for a chat/user id, draft the text). A known name (`TELEGRAM_KNOWN_CONTACTS`) or an explicit numeric id stages a yes/no; ingress sends on yes. Guests are refused before publish. Sending does not add the recipient to any allowlist. Guest turns do not receive this tool.

## Checklist for the next tool

```
[ ] Schema constant + TOOL_SCHEMAS[role] entry (llm_client.register_tool)
[ ] POLICY_TABLE["domain.action"] = auto|confirm|refuse
[ ] Handler path respects policy_for(...)
[ ] Unit test: policy + schema presence + unknown→confirm
[ ] Quiet: no capability brochure / ✅ on chat or life-reflect notify
```

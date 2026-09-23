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

Multi-step ops loop is **PARKED**. Do not add multi-step ops tool chains here.

## Checklist for the next tool

```
[ ] Schema constant + TOOL_SCHEMAS[role] entry (llm_client.register_tool)
[ ] POLICY_TABLE["domain.action"] = auto|confirm|refuse
[ ] Handler path respects policy_for(...)
[ ] Unit test: policy + schema presence + unknown→confirm
[ ] Quiet: no capability brochure / ✅ on chat or life-reflect notify
```

# Architecture Overview

## Layers
- Interface: Telegram ingress and outbound notifications.
- Orchestration: Supervisor-worker routing and checkpointing.
- Execution: Worker runtime and policy gateway.
- Temporal: Scheduler and reminder callbacks.
- Model Gateway: LiteLLM alias-based routing.

## Day-1 Focus
- Bootable infra and service boundaries.
- Health checks and migration baseline.
- Deterministic skeleton before advanced autonomy.

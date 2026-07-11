# Agent Orchestration Platform

Initial implementation scaffold for a Telegram-first multi-agent orchestration system.

## Included in this slice
- Docker Compose infrastructure (Postgres, Redis, LiteLLM)
- Service skeletons with health endpoints
- SQL migration baseline for core tables
- LiteLLM alias routing config

## Quick start
1. Copy `.env.example` to `.env` and edit values.
2. Run `make up`.
3. Run `make migrate`.
4. Verify services with `make health`.

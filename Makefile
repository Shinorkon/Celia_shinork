SHELL := /bin/bash

PROJECT_DIR := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))

.PHONY: up down logs ps migrate health

up:
	docker compose --env-file .env up -d --build

down:
	docker compose --env-file .env down -v

logs:
	docker compose --env-file .env logs -f --tail=100

ps:
	docker compose --env-file .env ps

migrate:
	bash scripts/apply_migrations.sh

health:
	python3 scripts/check_health.py

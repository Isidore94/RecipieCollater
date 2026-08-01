# 0001 — FastAPI + Jinja2/htmx/Alpine + SQLite, bare systemd, no Docker

Date: backfilled 2026-08-01 · Canonical: D1 (`docs/00-overview.md`), `docs/02-architecture.md`

## Context
The whole platform must run comfortably on a 5 W Intel N95 mini PC with <200 MB
RSS idle and <1 s LAN page loads, and be buildable across many AI sessions.

## Decision
Server-rendered FastAPI + Jinja2 with htmx (Alpine for local niceties only),
SQLite for all data, one web process + one worker process under bare systemd.
No Docker, no Redis, no Postgres, no Node toolchain.

## Rationale
Documented in D1: the ingestion libraries are Python; htmx is the most
LLM-buildable frontend; dockerd costs real idle watts on a 5 W box.

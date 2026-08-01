# 0004 — Dual AI providers behind one interface; AI writes only via proposals

Date: backfilled 2026-08-01 · Canonical: D5 D11 D14 (`docs/00-overview.md`), CONVENTIONS §10, `docs/05-ai-integration.md`

## Context
The app uses LLMs for extraction, TLDRs, planning, and a conversational
assistant — over irreplaceable family data, with either vendor's key.

## Decision
Anthropic and OpenAI sit behind one small capability interface, selected per
task; no automatic cross-provider failover in v1; OpenAI Responses calls set
`store: false`. The AI proposes recipe IDs and structured artifacts only —
deterministic application services perform all scaling, conversion, pantry,
shopping, and DB mutation, and the user accepts proposals explicitly.

## Rationale
Documented in D5/D11/D14: either key can run the app without vendor coupling;
deferred failover avoids duplicate tool loops and hidden double-spend; "the
assistant never silently mutates family data"; schema-valid model output is not
necessarily numerically or factually correct.

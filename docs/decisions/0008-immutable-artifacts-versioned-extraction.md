# 0008 — Immutable ingestion artifacts and versioned extraction runs

Date: backfilled 2026-08-01 · Canonical: D12 (`docs/00-overview.md`), CONVENTIONS §9

## Context
Extraction prompts improve over time, but family members hand-correct recipes;
re-running a better extractor must never destroy those corrections.

## Decision
Ingestion artifacts (supplied/fetched HTML, transcripts, extraction inputs) are
written once, content-addressed by SHA-256, and never mutated or deleted outside
backup rotation. Extraction runs are versioned records; re-extraction creates a
comparison draft and never overwrites family edits. JSON/markdown export stays
first-class.

## Rationale
Documented in D12: data portability plus prompt improvements "without destroying
corrections or provenance."

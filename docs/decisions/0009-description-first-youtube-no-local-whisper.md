# 0009 — Description-first YouTube ingestion; no local Whisper

Date: backfilled 2026-08-01 · Canonical: D4 (`docs/00-overview.md`), `docs/04-ingestion-pipeline.md`

## Context
YouTube cooking videos must become structured recipes on a 4-core N95 with no
GPU, from the home IP.

## Decision
Pipeline order: video metadata/description → captions → one provider-neutral
structured extraction call; comments are fetched only when description and
captions are thin. No local Whisper ASR; ingestion stays on the home IP.

## Rationale
Documented in D4: captions already exist for most videos; comments add latency
and throttling risk; "N95 CPU can't perform useful ASR." TikTok/Instagram-style
ASR ingestion is an explicitly deferred open question (overview §Open questions).

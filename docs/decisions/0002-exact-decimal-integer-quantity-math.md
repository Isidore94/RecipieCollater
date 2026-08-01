# 0002 — Exact quantity math: Decimal parsing, integer canonical units, no pint

Date: backfilled 2026-08-01 · Canonical: D2 (`docs/00-overview.md`), CONVENTIONS §1

## Context
Serving scaling, pantry deduction, and shopping aggregation compound arithmetic
across recipes; floating-point drift would corrupt family-facing numbers.

## Decision
Every quantity enters as a string and is parsed with `decimal.Decimal`; canonical
storage is exact integers (mg / µL / milli-each) with exact integer unit factors.
A hand-rolled unit table with per-food unit bridges — not the `pint` library.
SQL never does recipe/pantry/shopping arithmetic in `REAL`.

## Rationale
Documented in D2: unit handling was "Tandoor's #1 regret"; the math must not
accumulate binary floating-point drift. Rounding policy is explicit and central.

## Alternatives rejected
`pint` — rejected in D2 in favor of exact integer factors.

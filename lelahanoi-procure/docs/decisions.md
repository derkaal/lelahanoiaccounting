# Decisions

Each entry: what was decided, why, in two or three sentences. Newest at the bottom.

**D-001 Separate project directory next to the classifier.** The procurement tool lives in
`lelahanoi-procure/` with its own `pyproject.toml`, not inside `lelahanoi-classifier/`.
They share a Supabase project but nothing else, and the classifier has no package structure to import from.

**D-002 Schema inspection is a script, not a doc written from memory.** The sandbox cannot
reach Supabase, so `scripts/inspect_supabase.py` prints structure only (names, types, counts,
date ranges) that is safe to paste into `docs/data-sources.md`. Guessing column names would
have cost a rewrite of every adapter.

**D-003 Amazon defaults to manual.** Amazon's Conditions of Use forbid robots that extract
prices for a database, and `price_observations` is one. A logged-out single-page fetch stays
behind an explicit flag so the operator, not the tool, takes that decision.

**D-004 A shop is only fetched after a dated ToS check.** `config/suppliers.yaml` carries
`tos_checked: <date>` per shop; the web adapter refuses to fetch while it is null and falls back
to manual offers. This keeps hard constraint 2 enforceable in code rather than in a doc.

**D-005 Decimal money, gross at the adapter edge.** Every adapter returns gross EUR as `Decimal`
plus the VAT rate; net is derived once in `money.py` when `vat_reclaimable` is true. Floats never
touch prices, so the comparison is reproducible and testable to the cent.

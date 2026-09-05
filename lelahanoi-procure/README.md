# lelahanoi-procure

Ingredient procurement helper for Lê La Hanoi (Landshut). Takes a shopping list from the
recipe GPT, finds the cheapest sensible way to buy each item across invoice history, the
inventory catalog and allow-listed public shop pages, and prepares baskets and pickup lists
for a human to order. It never places an order and never logs into anything.

**Status: Phase 0 (discovery) delivered.** See `docs/plan.md` for the phases and
`docs/suppliers.md` and `docs/data-sources.md` for findings and the open questions that
gate Phase 1.

## Things you can run now

```bash
cp .env.example .env            # fill in, never commit
python scripts/inspect_supabase.py    # prints the Supabase schema as markdown (no data)
python scripts/check_robots.py        # robots.txt check for every candidate shop
```

Both scripts are standard library only, except `psycopg[binary]` for direct Postgres access.

## Documents

- `docs/plan.md` — architecture, phases, assumptions
- `docs/suppliers.md` — shops, shipping rules, ToS and robots findings, allow-list proposal
- `docs/data-sources.md` — Supabase tables the tool reads and writes, `price_observations`
- `docs/decisions.md` — non-obvious choices, two or three sentences each
- `docs/shopping-list-schema.md` — arrives with Phase 1

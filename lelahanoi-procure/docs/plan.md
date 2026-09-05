# Plan: ingredient procurement tool for Lê La Hanoi

Status: **Phase 0 delivered, waiting for review.** Nothing in Phase 1 starts until the
questions at the end of `docs/data-sources.md` and `docs/suppliers.md` are answered.

## What the tool does

Takes a JSON shopping list from the recipe GPT ("Sirup lab"), normalises each free-text
line into a canonical product and unit, collects offers from three price sources, and
computes the cheapest sensible way to buy everything as whole supplier baskets. It
prepares baskets, deep links and pickup lists. A human places every order.

## Architecture (small, typed, boring)

```
lelahanoi-procure/
├── procure/
│   ├── cli.py              procure run list.json  [--format md|csv] [--offline]
│   ├── schema.py           ShoppingList / ShoppingLine (pydantic) + validator
│   ├── normalise.py        free text -> CanonicalItem (LLM, logged, overridable)
│   ├── llm/                one adapter: base.py, anthropic.py, kimi.py, fake.py
│   ├── sources/            PriceSource protocol + adapters
│   │   ├── invoices.py     Supabase invoice line items (source 1)
│   │   ├── catalog.py      Supabase inventory catalog, METRO prices (source 2)
│   │   ├── web/            allow-listed public pages (source 3): koro.py, amazon.py, ...
│   │   └── fixtures/       every adapter runs offline against these
│   ├── money.py            Decimal-only gross/net, VAT, per-unit normalisation
│   ├── engine.py           pack rounding, landed cost per basket, split vs single-supplier
│   ├── observations.py     write-back to price_observations + price trend query
│   └── report.py           markdown + CSV output, plain-language notes
├── config/
│   ├── procure.yaml        vat_reclaimable, per-km rate, tolerance, max price age, LLM provider
│   └── suppliers.yaml      allow-list, addresses, shipping rules, ToS check date
├── docs/                   this folder
├── scripts/                discovery helpers (schema inspection, robots check)
└── tests/                  fixtures for every adapter and the engine
```

Money is `Decimal` everywhere, VAT is applied once at the edge of each adapter (gross in,
gross out; net available when `vat_reclaimable: true`). The LLM is only reached through
`procure.llm` and only from `normalise.py` and the brand-constraint judge in `engine.py`.

## Phases

| Phase | Deliverable | Gate |
|---|---|---|
| 0 | `docs/suppliers.md`, `docs/data-sources.md`, scripts to finish the checks the sandbox could not run | **your answers** |
| 1 | CLI: schema + validator + 3 examples, normalisation, 3 adapters with fixtures, landed-cost engine with tests, `price_observations`, markdown/CSV report | your review of a run on a real list |
| 2 | HTTP endpoint with shared-secret header + OpenAPI for the GPT Action; MCP server (`normalise_items`, `find_offers`, `compare_baskets`, `price_history`); hosting notes | your review |
| 3 | Cart preparation: pre-filled deep links where public URLs allow, printable pickup list | your review |

## Assumptions carried until you say otherwise

- Gross comparison, `vat_reclaimable: false`. Food VAT 7 %, non-food 19 %.
- Pickup trip cost = round trip km × per-km rate (proposed 0.30 EUR/km, the German
  Kilometerpauschale, until you name a rate). No labour cost.
- One trip per supplier per run; all pickup items at that supplier share it.
- Price age warning at 60 days, consolidation tolerance 3 EUR.
- Amazon is manual by default (see `docs/suppliers.md`).

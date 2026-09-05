# Data sources

Phase 0 findings (2026-09-05). The Supabase project could **not** be inspected: the
development sandbox has no Supabase connection variables and no network route to it,
and this repository contains no Supabase code, migrations or schema dumps to read
instead. The bank-statement classifier in `lelahanoi-classifier/` writes CSV and HTML
only; its receipt extractor produces totals and VAT breakdowns per receipt, not line
items. So the invoice line items and the inventory catalog live in a pipeline outside
this repository.

**Action for you:** on a machine with the variables set, run

```bash
pip install 'psycopg[binary]'
export SUPABASE_DB_URL='postgresql://...'      # or SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY
python scripts/inspect_supabase.py > /tmp/schema.md
```

and paste `/tmp/schema.md` into the "Observed schema" section below (it contains
names, types, counts and date ranges only, no invoice data). Alternatively add the
variables to the Claude Code environment and I will run it in Phase 1.

## Connection contract

| Variable | Used for | Required |
|---|---|---|
| `SUPABASE_DB_URL` | direct Postgres: schema inspection, invoice and catalog reads, `price_observations` writes | preferred |
| `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` | PostgREST fallback for the same reads and writes | if no DB URL |

The tool reads with plain SQL through one small module (`procure/sources/supabase.py`)
so the table and column names live in exactly one place and can be renamed after
inspection without touching the adapters.

## What the adapters need from each table

### Source 1: invoice history (backbone)

Expected shape (to be mapped to your real names):

| concept | expected column(s) | why |
|---|---|---|
| supplier | `invoices.supplier` / `vendor` | groups offers into pickup baskets and trips |
| invoice date | `invoices.date` | price age; the 60-day re-check warning |
| line description | `invoice_line_items.description` | matched against the canonical item |
| quantity + unit | `quantity`, `unit` or parsed from description | pack size |
| gross or net line total + VAT rate | `gross_amount` / `net_amount`, `vat_rate` | gross per pack; net if the café leaves §19 |
| catalog link (optional) | `invoice_line_items.catalog_item_id` | skips fuzzy matching when present |

If line items only carry a net amount and a VAT rate, gross is recomputed in `money.py`
and the derivation is recorded on the observation.

### Source 2: inventory catalog (METRO prices)

| concept | expected column(s) | why |
|---|---|---|
| item name, canonical unit | `name`, `unit` | normalisation target; the LLM maps free text onto these first |
| pack size | `pack_size`, `pack_unit` | pack rounding |
| METRO price | `metro_price`, gross or net flag, `price_updated_at` | source-2 offer, price age |
| supplier, article number | `supplier`, `metro_article_no` | pickup list; **never** exported outside the pickup list |

### New table: `price_observations` (created by the tool in Phase 1)

```sql
create table if not exists price_observations (
  id              bigint generated always as identity primary key,
  observed_at     timestamptz not null default now(),
  source          text not null,            -- invoice | catalog | web:<supplier> | manual
  supplier        text not null,
  canonical_item  text not null,            -- normalised product key
  raw_label       text,                     -- what the source called it
  pack_qty        numeric not null,         -- 2.5
  pack_unit       text not null,            -- kg | l | pcs
  gross_price_eur numeric(12,4) not null,
  vat_rate        numeric(5,2) not null,    -- 7.00 / 19.00
  net_price_eur   numeric(12,4) generated always as (gross_price_eur / (1 + vat_rate/100)) stored,
  gross_per_base  numeric(12,4) not null,   -- per kg / l / pc
  url             text,
  price_date      date not null,            -- invoice date, catalog update, or fetch date
  run_id          uuid not null,
  meta            jsonb not null default '{}'
);
create index on price_observations (canonical_item, observed_at desc);
```

"Price trend for item X" = `select price_date, supplier, gross_per_base from price_observations
where canonical_item = $1 order by price_date`, exposed on the CLI and later as MCP `price_history`.
Invoice rows are re-observed once per invoice line (deduplicated on
`(source, supplier, raw_label, pack_qty, pack_unit, price_date)`), so the table grows
only with new information.

## Observed schema

_(paste the output of `scripts/inspect_supabase.py` here)_

## Open questions for you

1. Run the inspection script, or add the connection variables to this environment.
2. Are invoice line items already itemised (one row per product) for METRO, or only
   totals per invoice? The backbone only works with itemised rows.
3. Is the maintained METRO price in the catalog gross or net?
4. Does the catalog already have a canonical unit and pack size per item, or free text?
5. Will the recipe GPT paste JSON into the CLI (Phase 1) or call the endpoint (Phase 2)?
6. Runtime LLM provider: Anthropic Haiku or Kimi K2? (The adapter supports both; one is default.)

# Le La Hanoi — Bank Statement Classifier

A Python CLI tool that classifies German Comdirect bank statement transactions
for **Le La Hanoi**, a Vietnamese café in Landshut, Germany (Altstadt 369).

The café owner (Andreas Donath) paid café expenses from his personal Comdirect
account during setup and early operations. This tool separates café business
expenses from personal ones, preparing the data for import into the accounting
system.

---

## Features

- Parses Comdirect CSV exports (ISO-8859-1, semicolon-separated, 4-row header skip)
- Parses Amazon Order History CSV and cross-references order IDs found in bank text
- Applies **deterministic rules** for ~30 known vendors/patterns (no API needed)
- Sends remaining ambiguous transactions to **Claude API** in batches of 20
- Produces a classified CSV with full audit trail (rule matched, confidence, reason)
- Prints a human-readable summary with totals by classification and category

---

## Installation

```bash
cd lelahanoi-classifier
pip install -r requirements.txt
```

Set your Anthropic API key:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

---

## Usage

### Basic

```bash
python classify.py \
  --bank umsaetze_feb2026.csv \
  --amazon Order_History.csv \
  --output classified_feb2026.csv \
  --month 2026-02
```

### Without Amazon CSV

```bash
python classify.py \
  --bank umsaetze_feb2026.csv \
  --output classified_feb2026.csv \
  --month 2026-02
```

### Dry run (no output file written)

```bash
python classify.py \
  --bank umsaetze_feb2026.csv \
  --amazon Order_History.csv \
  --month 2026-02 \
  --dry-run
```

### Skip API calls (rules-only classification)

```bash
python classify.py \
  --bank umsaetze_feb2026.csv \
  --output classified_feb2026.csv \
  --month 2026-02 \
  --no-api
```

### Verbose output

```bash
python classify.py \
  --bank umsaetze_feb2026.csv \
  --output classified_feb2026.csv \
  --month 2026-02 \
  -v
```

---

## CLI Options

| Option | Description |
|---|---|
| `--bank` | Path to Comdirect CSV export (required) |
| `--amazon` | Path to Amazon Order History CSV (optional) |
| `--output` | Output CSV path (default: `classified_output.csv`) |
| `--month` | Filter to YYYY-MM (e.g. `2026-02`) |
| `--dry-run` | Print summary, skip writing output file |
| `--no-api` | Only apply deterministic rules, skip Claude API |
| `--model` | Claude model (default: `claude-sonnet-4-5`) |
| `--api-key` | Anthropic API key (or use `ANTHROPIC_API_KEY` env var) |
| `-v` / `--verbose` | Detailed progress output |

---

## Output CSV Columns

| Column | Description |
|---|---|
| `date` | Transaction date (YYYY-MM-DD) |
| `vendor` | Extracted payee/vendor name |
| `buchungstext` | Full bank statement text |
| `amount_eur` | Amount in EUR (negative = debit) |
| `order_id` | Amazon order ID if found |
| `product_names` | Pipe-separated Amazon product names |
| `classification` | See classifications below |
| `category` | See categories below |
| `confidence` | 0.0–1.0 confidence score |
| `reason` | Short explanation |
| `needs_receipt` | True if receipt required for tax |
| `flag_note` | Note for accountant (German) |
| `matched_rule` | Which deterministic rule matched (if any) |

---

## Classifications

| Value | Meaning |
|---|---|
| `cafe_expense` | Café business expense |
| `personal` | Private/personal expense |
| `transfer` | Internal money movement |
| `salary` | Salary income from Capgemini |
| `needs_review` | Ambiguous — owner must verify |
| `ignore` | Bank fees, internal settlements |

## Expense Categories

| Category | Description |
|---|---|
| `exp_miete` | Rent (Manfred Michl) |
| `exp_wareneinkauf` | Food/beverage purchasing |
| `exp_energie` | Electricity, gas, utilities |
| `exp_bankgebuehr` | Bank/payment processing fees |
| `exp_marketing` | Advertising, music licences (GEMA) |
| `exp_buerobedarf` | Office supplies, POS software |
| `exp_versicherung` | Business insurance |
| `exp_instandhaltung` | Repairs and maintenance |
| `asset_gwg` | Low-value assets (GWG) < €800 net |
| `exp_bewirtung` | Business hospitality |

---

## Input CSV Formats

### Comdirect Bank CSV

- Encoding: **ISO-8859-1**
- Separator: **semicolon (`;`)**
- Skip first **4 rows** (account info header)
- Date column: `Buchungstag` (DD.MM.YYYY format)
- Columns: `Buchungstag`, `Wertstellung`, `Vorgang`, `Buchungstext`, `Umsatz in EUR`
- Amounts: German decimal format (`1.234,56` = 1234.56 EUR)

### Amazon Order History CSV

- Encoding: **UTF-8**
- Standard Amazon order export columns:
  `Order ID`, `Order Date`, `Product Name`, `Unit Price`, `Total Amount`, `Shipping Address`

---

## Architecture

```
classify.py       ← CLI entry point, orchestration
rules.py          ← Deterministic classification rules (~30 rules)
amazon.py         ← Amazon CSV parser + order ID extractor
api_client.py     ← Claude API batching (batches of 20)
output.py         ← CSV writer + summary printer
requirements.txt
README.md
```

### Processing Pipeline

1. Parse bank CSV → list of transaction dicts
2. Load Amazon CSV → `{order_id: [product_names]}` lookup dict
3. Enrich Amazon transactions with product names
4. Apply deterministic rules → classify most transactions immediately
5. Collect unclassified transactions → batch send to Claude API (20 per call)
6. Merge rule results + API results
7. Write output CSV + print summary

---

## Amazon Order ID Extraction

Amazon order IDs embedded in Comdirect Buchungstext are extracted with a regex
that handles:
- Standard formats: `028-XXXXXXX-XXXXXXX`, `305-XXXXXXX-XXXXXXX`
- Prefix variants: `028`, `302`, `304`, `305`, `306`
- Space-split IDs (bank text wrapping)
- Both `AMAZON EU S.A R.L.` and `AMAZON PAYMENTS EUROPE S.C.A.` transaction types

---

## Deterministic Rules Reference

| Pattern | Classification | Category | Confidence |
|---|---|---|---|
| MANFRED MICHL (outgoing) | cafe_expense | exp_miete | 1.0 |
| MICHL MANFRED (incoming) | transfer | transfer | 1.0 |
| ready2order GmbH | cafe_expense | exp_buerobedarf | 1.0 |
| CAPGEMINI DEUTSCHLAND | salary | salary | 1.0 |
| Baeckerei Johann Tafelmaier | cafe_expense | exp_wareneinkauf | 1.0 |
| METRO SAGT DANKE | cafe_expense | exp_wareneinkauf | 0.9 |
| DELI TADKA GMBH | cafe_expense | exp_wareneinkauf | 0.9 |
| GEMA | cafe_expense | exp_marketing | 1.0 |
| SumUp *Le La Hanoi | cafe_expense | exp_bankgebuehr | 1.0 |
| Kontoführungsentgelt | ignore | ignore | 1.0 |
| Comdirect visa settlement | ignore | ignore | 1.0 |
| Bundesagentur Familienkasse | ignore | ignore | 1.0 |
| LVM VERSICHERUNG | personal | personal | 1.0 |
| Alte Leipziger | personal | personal | 1.0 |
| LEBENSVERS.VON 1871 | personal | personal | 1.0 |
| Mercedes-Benz Bank | personal | personal | 1.0 |
| SANTANDER CONSUMER BANK | personal | personal | 1.0 |
| MARCO ALTINGER / Karate | personal | personal | 1.0 |
| Rundfunk ARD ZDF | personal | personal | 1.0 |
| Gemeinde Buch am Erlbach | personal | personal | 1.0 |
| TO: Andreas Donath | transfer | transfer | 1.0 |
| Andreas Donath Huong Tra DonathPham | transfer | transfer | 1.0 |
| OBI | needs_review | exp_instandhaltung | 0.4 |
| BAUHAUS | needs_review | exp_instandhaltung | 0.4 |
| HAGEBAU | needs_review | exp_instandhaltung | 0.4 |
| PayPal / Apple Services | personal | personal | 1.0 |
| PayPal / Netflix | personal | personal | 1.0 |
| PayPal / DisneyPlus | personal | personal | 1.0 |
| PayPal / Roblox | personal | personal | 1.0 |
| PayPal / Best Secret | personal | personal | 1.0 |
| PayPal / INFINITE STYLES | personal | personal | 1.0 |
| PayPal / Lifestyle Brands | personal | personal | 1.0 |
| PayPal / YSSKINCARE | personal | personal | 1.0 |
| PayPal / Xandrie/qobuz | personal | personal | 1.0 |
| PayPal / mc-eur-plux-issuing | ignore | ignore | 1.0 |
| PayPal / Google | needs_review | exp_buerobedarf | 0.5 |

---

## Notes for Accountant

- Transactions with `needs_receipt=True` require physical or digital receipts
- Transactions with `confidence < 0.7` are automatically flagged for review
- `flag_note` column contains German-language notes for the accountant
- All amounts are in EUR (float). Supabase import step converts to integer cents.
- The `--month` filter does not exclude transactions dated outside the month from
  being parsed — it strictly filters on `Buchungstag` date.

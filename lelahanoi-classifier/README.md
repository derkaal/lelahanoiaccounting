# Le La Hanoi — Bank Statement Classifier

A Python CLI tool that classifies German Comdirect bank statement transactions
for **Le La Hanoi**, a Vietnamese café in Landshut, Germany (Altstadt 369).

The café owner (Andreas Donath) paid café expenses from his personal Comdirect
account during setup and early operations. This tool separates café business
expenses from personal ones, preparing the data for import into the accounting
system.

---

## Folder structure

```
lelahanoi-classifier/
├── classify.py          # Main CLI
├── rules.py             # Deterministic classification rules
├── amazon.py            # Amazon CSV parser + order ID extractor
├── api_client.py        # Claude API batch classifier
├── output.py            # CSV writer + summary printer
├── requirements.txt
├── README.md
│
└── data/                # ← gitignored; never committed to GitHub
    ├── bank/            # Comdirect CSV exports, one per month
    │   └── .gitkeep
    ├── amazon/          # Amazon Order History CSV exports
    │   └── .gitkeep
    ├── invoices/        # PDF invoices and receipts
    │   ├── amazon/      # Amazon PDF invoices, named by order ID
    │   ├── suppliers/   # Metro, Tafelmaier, Deli Tadka, etc.
    │   └── other/       # Everything else
    └── output/          # Classified CSVs written here
        └── .gitkeep
```

> **Important:** All files inside `data/` are gitignored.
> Never commit bank statements, invoices, or order history to GitHub.
> The `.gitkeep` files are the only thing that preserves the folder structure in git.

---

## Setup

```bash
git clone <repo-url>
cd lelahanoi-classifier
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
```

---

## Preparing your data

1. **Bank statement** — Export your Comdirect account as CSV:
   - In the Comdirect app/website: Konto → Umsätze → Export → CSV
   - Save it to `data/bank/`, e.g. `data/bank/umsaetze_2026-02.csv`
   - The filename should contain the month (`2026-02` or `202602`) so
     the tool can find it automatically.

2. **Amazon orders** (optional but recommended) — Export your order history:
   - Go to [amazon.de/gp/b2b/reports](https://www.amazon.de/gp/b2b/reports)
     or use the "Order History Reports" page
   - Save it to `data/amazon/`, e.g. `data/amazon/Order_History.csv`

3. **Invoices** (for your records, not used by the classifier):
   - `data/invoices/amazon/`    — Amazon PDF receipts, one per order ID
   - `data/invoices/suppliers/` — Metro, Tafelmaier, Deli Tadka, etc.
   - `data/invoices/other/`     — Everything else

---

## Running

### Minimal — auto-detect files from data/ folders

```bash
python classify.py --month 2026-02
```

This will:
- Find `data/bank/*2026-02*.csv` (or the only CSV in `data/bank/`)
- Find the most recently modified CSV in `data/amazon/`
- Write output to `data/output/classified_2026-02.csv`
- Print a summary to the terminal

### Dry run — summary only, no file written

```bash
python classify.py --month 2026-02 --dry-run
```

### Explicit paths

```bash
python classify.py \
  --bank data/bank/umsaetze_feb2026.csv \
  --amazon data/amazon/Order_History.csv \
  --output data/output/classified_feb2026.csv \
  --month 2026-02
```

### Rules only — skip Claude API

```bash
python classify.py --month 2026-02 --no-api
```

### All options

| Option | Default | Description |
|---|---|---|
| `--bank` | `data/bank/` | CSV file or directory. Directory → auto-selects by month. |
| `--amazon` | `data/amazon/` | CSV file or directory. Directory → uses newest file. |
| `--output` | `data/output/classified_{month}.csv` | Output file path. |
| `--month` | *(none)* | Filter to YYYY-MM, e.g. `2026-02`. Strongly recommended. |
| `--dry-run` | off | Print summary, don't write output file. |
| `--no-api` | off | Rules-only mode — unmatched transactions → `needs_review`. |
| `--model` | `claude-sonnet-4-5` | Claude model for ambiguous transactions. |
| `--api-key` | env `ANTHROPIC_API_KEY` | Anthropic API key. |
| `-v` / `--verbose` | off | Detailed progress output. |

---

## Output CSV columns

| Column | Description |
|---|---|
| `date` | Transaction date (YYYY-MM-DD) |
| `vendor` | Extracted payee/vendor name |
| `buchungstext` | Full bank statement text |
| `amount_eur` | Amount in EUR (negative = debit) |
| `order_id` | Amazon order ID if found in Buchungstext |
| `product_names` | Pipe-separated Amazon product names (if matched) |
| `classification` | See below |
| `category` | See below |
| `confidence` | 0.0–1.0 |
| `reason` | Short explanation |
| `needs_receipt` | `True` if a receipt is required for tax purposes |
| `flag_note` | Note for the accountant (German) |
| `matched_rule` | Which deterministic rule matched (empty = API result) |

---

## Classifications

| Value | Meaning |
|---|---|
| `cafe_expense` | Café business expense |
| `personal` | Private/personal expense |
| `transfer` | Internal money movement (no P&L impact) |
| `salary` | Salary income (Capgemini) |
| `needs_review` | Ambiguous — owner must verify manually |
| `ignore` | Bank fees, internal settlements |

## Expense categories

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

## Architecture

```
classify.py       ← CLI entry point, orchestration, path resolution
rules.py          ← ~35 deterministic rules (no API call needed)
amazon.py         ← Amazon CSV parser + order ID regex extractor
api_client.py     ← Claude API batching (20 transactions per call)
output.py         ← CSV writer + summary printer
```

### Processing pipeline

1. Resolve `--bank` / `--amazon` directory args → concrete file paths
2. Parse bank CSV → list of transaction dicts
3. Load Amazon CSV → `{order_id: [product_names]}` lookup dict
4. Enrich Amazon transactions with product names
5. Apply deterministic rules → classify most transactions immediately
6. Collect unclassified transactions → batch send to Claude API (20 per call)
7. Merge rule results + API results
8. Write `data/output/classified_{month}.csv` + print summary

---

## Deterministic rules reference

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

## Notes for the accountant

- Transactions with `needs_receipt=True` require physical or digital receipts
- Transactions with `confidence < 0.7` are automatically flagged for review
- `flag_note` contains German-language notes explaining what to check
- All amounts are in EUR (float). The Supabase import step converts to integer cents.
- Amazon order ID extraction handles IDs split across bank text lines with spaces

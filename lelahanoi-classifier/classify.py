#!/usr/bin/env python3
"""
Le La Hanoi — Bank Statement Classifier
========================================
Classifies Comdirect bank statement transactions for the Le La Hanoi café,
separating café business expenses from personal ones.

Usage:
    # Minimal — relies on data/ folder defaults
    python classify.py --month 2026-02

    # Explicit paths
    python classify.py \\
        --bank data/bank/umsaetze_feb2026.csv \\
        --amazon data/amazon/Order_History.csv \\
        --output data/output/classified_feb2026.csv \\
        --month 2026-02

    # Dry run (no file written)
    python classify.py --month 2026-02 --dry-run

Drop your Comdirect CSV in data/bank/ and your Amazon CSV in data/amazon/,
then run:  python classify.py --month 2026-02
"""

import argparse
import re
import sys
from pathlib import Path
from typing import Optional

import pandas as pd
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

from amazon import extract_order_id, load_amazon_orders
from api_client import classify_transactions
from output import format_rows_for_output, print_summary, write_output_csv
from rules import RuleResult, apply_rules


# ---------------------------------------------------------------------------
# Directory auto-detection helpers
# ---------------------------------------------------------------------------

def check_ready2order_folder(month: str) -> None:
    """
    Check if ready2order folder exists for the given month and log a warning
    if no files are found. This is for future r2o_parser.py module integration.
    """
    r2o_path = Path(f"data/{month}/ready2order")
    if not r2o_path.exists():
        print(f"  Note: ready2order folder not found at {r2o_path} (will be used by future r2o_parser.py)")
        return
    
    # Check for PDF files (ready2order reports are typically PDFs)
    pdf_files = list(r2o_path.glob("*.pdf"))
    if not pdf_files:
        print(f"  Warning: ready2order folder exists but contains no PDF files at {r2o_path}")
    else:
        print(f"  Found {len(pdf_files)} ready2order report(s) in {r2o_path} (for future processing)")


def resolve_bank_path(bank_arg: Optional[str], month: Optional[str]) -> str:
    """
    Resolve bank CSV path with month-based auto-detection.
    
    Priority:
    1. If --bank is explicitly provided, use it (can be file or directory)
    2. If --month is provided, auto-detect from data/{month}/bank/
    3. Fall back to data/bank/ directory
    
    Returns the resolved file path as a string.
    """
    # If explicit bank path provided, use it
    if bank_arg:
        p = Path(bank_arg)
        if p.is_file():
            return str(p)
        if not p.is_dir():
            raise FileNotFoundError(f"Bank path not found: {bank_arg}")
        
        # Directory provided - find CSV inside
        candidates = sorted(p.glob("*.csv"))
        if not candidates:
            raise FileNotFoundError(f"No CSV files found in bank directory: {p}")
        
        if month:
            # Try both YYYY-MM and YYYYMM variants in the filename
            month_compact = month.replace("-", "")
            matches = [f for f in candidates if month in f.name or month_compact in f.name]
            if len(matches) == 1:
                return str(matches[0])
            if len(matches) > 1:
                raise ValueError(
                    f"Multiple bank CSVs match month {month} in {p}: "
                    + ", ".join(f.name for f in matches)
                    + " — please pass an explicit --bank path."
                )
        
        # No month filter or no match - use first CSV if only one exists
        if len(candidates) == 1:
            if month:
                print(f"  Warning: no filename match for {month}; using {candidates[0].name}")
            return str(candidates[0])
        
        raise ValueError(
            f"Multiple CSVs in {p} and no --month given to disambiguate: "
            + ", ".join(f.name for f in candidates)
        )
    
    # No explicit bank path - use month-based auto-detection
    if month:
        month_bank_dir = Path(f"data/{month}/bank")
        if not month_bank_dir.exists():
            raise FileNotFoundError(
                f"Month-based bank directory not found: {month_bank_dir}\n"
                f"Please create the directory structure: data/{month}/bank/"
            )
        
        candidates = sorted(month_bank_dir.glob("*.csv"))
        if not candidates:
            raise FileNotFoundError(
                f"No bank CSV files found in {month_bank_dir}\n"
                f"Please place your Comdirect CSV export in data/{month}/bank/"
            )
        
        if len(candidates) == 1:
            print(f"Auto-detected bank CSV: {candidates[0]}")
            return str(candidates[0])
        
        # Multiple CSVs - try to match by filename
        month_compact = month.replace("-", "")
        matches = [f for f in candidates if month in f.name or month_compact in f.name]
        if len(matches) == 1:
            print(f"Auto-detected bank CSV: {matches[0]}")
            return str(matches[0])
        
        raise ValueError(
            f"Multiple bank CSVs found in {month_bank_dir}: "
            + ", ".join(f.name for f in candidates)
            + "\nPlease specify which one to use with --bank"
        )
    
    # No month and no explicit path - error
    raise ValueError(
        "Either --month or --bank must be provided.\n"
        "Usage: python classify.py --month 2026-02"
    )


def resolve_amazon_path(amazon_arg: Optional[str], month: Optional[str]) -> Optional[str]:
    """
    Resolve Amazon CSV path with month-based auto-detection.
    
    Priority:
    1. If --amazon is explicitly provided, use it (can be file or directory)
    2. If --month is provided, auto-detect from data/{month}/amazon/
    3. Fall back to data/amazon/ directory
    
    Returns the resolved file path or None (Amazon data is optional).
    """
    # If explicit amazon path provided, use it
    if amazon_arg:
        p = Path(amazon_arg)
        if p.is_file():
            return str(p)
        if not p.is_dir():
            return None  # directory doesn't exist - Amazon data is optional
        
        candidates = sorted(p.glob("*.csv"), key=lambda f: f.stat().st_mtime, reverse=True)
        if not candidates:
            return None
        return str(candidates[0])
    
    # No explicit amazon path - use month-based auto-detection
    if month:
        month_amazon_dir = Path(f"data/{month}/amazon")
        if not month_amazon_dir.exists():
            print(f"  Note: Amazon folder not found at {month_amazon_dir} (optional)")
            return None
        
        candidates = sorted(month_amazon_dir.glob("*.csv"), key=lambda f: f.stat().st_mtime, reverse=True)
        if not candidates:
            print(f"  Note: No Amazon CSV found in {month_amazon_dir} (optional)")
            return None
        
        print(f"Auto-detected Amazon CSV: {candidates[0]}")
        return str(candidates[0])
    
    # No month - try default data/amazon/ directory
    default_amazon_dir = Path("data/amazon")
    if not default_amazon_dir.exists():
        return None
    
    candidates = sorted(default_amazon_dir.glob("*.csv"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not candidates:
        return None
    return str(candidates[0])


# ---------------------------------------------------------------------------
# Comdirect CSV parser
# ---------------------------------------------------------------------------

def _parse_german_amount(value: str) -> float:
    """
    Convert German-formatted number string to float.
    e.g. "1.234,56" → 1234.56  or  "-45,00" → -45.0
    """
    if not value or str(value).strip() in ("", "nan"):
        return 0.0
    # Remove thousand separators (periods), replace decimal comma with period
    cleaned = str(value).strip().replace("\xa0", "").replace(" ", "")
    cleaned = cleaned.replace(".", "").replace(",", ".")
    # Strip surrounding quotes added by some CSV exports
    cleaned = cleaned.strip('"').strip("'")
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def parse_bank_csv(
    csv_path: str,
    month_filter: Optional[str] = None,
) -> list[dict]:
    """
    Parse Comdirect CSV export.

    Format:
    - Encoding: ISO-8859-1 (latin-1)
    - Separator: semicolon
    - First 4 rows: headers/metadata to skip
    - Columns: Buchungstag, Wertstellung, Vorgang, Buchungstext, Umsatz in EUR

    Returns list of transaction dicts with keys:
      date, vendor, buchungstext, amount_eur, raw_vorgang
    """
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"Bank CSV not found: {csv_path}")

    # Comdirect CSVs have 4 header/info rows before the actual column headers
    # We skip them and let pandas infer the real header on row 5 (index 4)
    try:
        df = pd.read_csv(
            csv_path,
            encoding="iso-8859-1",
            sep=";",
            skiprows=4,
            dtype=str,
        )
    except Exception as e:
        raise ValueError(f"Failed to parse bank CSV: {e}") from e

    # Strip whitespace from column names
    df.columns = [c.strip().strip('"') for c in df.columns]

    # Find the relevant columns by partial match (handles encoding quirks)
    def find_col(df, *candidates):
        for cand in candidates:
            for col in df.columns:
                if cand.lower() in col.lower():
                    return col
        return None

    col_date = find_col(df, "Buchungstag", "Datum", "Date")
    col_text = find_col(df, "Buchungstext", "Verwendungszweck", "Text")
    col_vorgang = find_col(df, "Vorgang", "Buchungsart", "Art")
    col_amount = find_col(df, "Umsatz", "Betrag", "Amount")

    missing = []
    if not col_date:
        missing.append("Buchungstag")
    if not col_text:
        missing.append("Buchungstext")
    if not col_amount:
        missing.append("Umsatz in EUR")
    if missing:
        raise ValueError(
            f"Bank CSV missing required columns: {missing}. "
            f"Found: {list(df.columns)}"
        )

    transactions = []
    for _, row in df.iterrows():
        date_raw = str(row.get(col_date, "")).strip().strip('"')
        buchungstext = str(row.get(col_text, "")).strip().strip('"')
        vorgang = str(row.get(col_vorgang, "")).strip().strip('"') if col_vorgang else ""
        amount_raw = str(row.get(col_amount, "")).strip().strip('"')

        # Skip completely empty rows and the trailing info rows Comdirect adds
        if not date_raw or date_raw.lower() in ("nan", "", "buchungstag"):
            continue
        # Comdirect sometimes adds a final info row with totals
        if "alter kontostand" in buchungstext.lower() or "neuer kontostand" in buchungstext.lower():
            continue

        # Parse date — Comdirect uses DD.MM.YYYY
        try:
            date_parsed = pd.to_datetime(date_raw, dayfirst=True).strftime("%Y-%m-%d")
        except Exception:
            date_parsed = date_raw

        # Month filter
        if month_filter:
            year, mo = month_filter.split("-")
            if not date_parsed.startswith(f"{year}-{mo}"):
                continue

        amount = _parse_german_amount(amount_raw)

        # Extract vendor/payee from Buchungstext
        vendor = _extract_vendor(buchungstext, vorgang)

        # Extract Amazon order ID if present
        order_id = extract_order_id(buchungstext)

        transactions.append({
            "date": date_parsed,
            "vendor": vendor,
            "buchungstext": buchungstext,
            "amount_eur": amount,
            "raw_vorgang": vorgang,
            "order_id": order_id or "",
        })

    return transactions


def _extract_vendor(buchungstext: str, vorgang: str) -> str:
    """
    Extract a human-readable vendor name from the bank Buchungstext.

    Comdirect encodes payee information differently depending on the
    transaction type (Lastschrift, Überweisung, Kartenzahlung, etc.)
    """
    text = buchungstext.strip()

    # Kartenverfügung / Debit card — vendor usually comes after "Kartenzahlung bei "
    # or is the first non-date word
    patterns = [
        r'Kartenzahlung bei\s+(.+?)(?:\s+Datum|\s+\d{2}\.\d{2}|$)',
        r'Lastschrift\s+(.+?)(?:\s+Gläubiger|\s+Mandat|$)',
        r'Auftraggeber:\s*(.+?)(?:\n|Buchungstext|$)',
        r'Empfänger:\s*(.+?)(?:\n|Buchungstext|$)',
        r'(?:Auftraggeber|Empfänger):\s*(.+)',
    ]

    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        if m:
            vendor = m.group(1).strip()
            # Truncate at next keyword boundary
            vendor = re.split(r'\s+(?:IBAN|BIC|Konto|Mandat|Referenz|Gläubiger)', vendor)[0]
            vendor = vendor.strip()[:80]
            if vendor:
                return vendor

    # Fallback: use first ~50 chars of Buchungstext as vendor hint
    # Remove common prefixes
    cleaned = re.sub(
        r'^(?:Buchungstext:|Auftraggeber:|Empfänger:|Verwendungszweck:)\s*',
        '',
        text,
        flags=re.IGNORECASE,
    )
    # For Kartenverfügung, the first line often has the merchant
    first_line = cleaned.split('\n')[0].strip()
    # Remove trailing date patterns
    first_line = re.sub(r'\s+\d{2}\.\d{2}\.\d{2,4}.*$', '', first_line)
    return first_line[:80].strip()


# ---------------------------------------------------------------------------
# Main classification pipeline
# ---------------------------------------------------------------------------

def classify_all(
    bank_path: str,
    amazon_path: Optional[str],
    output_path: Optional[str],
    month_filter: Optional[str],
    dry_run: bool = False,
    model: str = "claude-sonnet-4-5",
    api_key: Optional[str] = None,
    verbose: bool = False,
) -> list[dict]:
    """
    Full classification pipeline:
    1. Parse bank CSV
    2. Load Amazon orders (if provided)
    3. Enrich Amazon transactions with product names
    4. Apply deterministic rules
    5. Send remaining to Claude API
    6. Merge results
    7. Write output / print summary
    """
    # ------------------------------------------------------------------
    # Step 1: Parse bank CSV
    # ------------------------------------------------------------------
    print(f"Parsing bank CSV: {bank_path}")
    transactions = parse_bank_csv(bank_path, month_filter=month_filter)
    print(f"  {len(transactions)} transactions loaded")

    if not transactions:
        print("No transactions found. Check --month filter or CSV format.")
        return []

    # ------------------------------------------------------------------
    # Step 2: Load Amazon orders
    # ------------------------------------------------------------------
    amazon_lookup: dict[str, list[str]] = {}
    if amazon_path:
        print(f"Loading Amazon order history: {amazon_path}")
        try:
            amazon_lookup = load_amazon_orders(amazon_path, month_filter=month_filter)
            print(f"  {len(amazon_lookup)} Amazon orders loaded")
        except FileNotFoundError:
            print(f"  Warning: Amazon CSV not found at {amazon_path}, skipping.")
        except Exception as e:
            print(f"  Warning: Failed to load Amazon CSV: {e}, skipping.")

    # ------------------------------------------------------------------
    # Step 3: Enrich Amazon transactions with product names
    # ------------------------------------------------------------------
    for tx in transactions:
        oid = tx.get("order_id")
        if oid and oid in amazon_lookup:
            tx["product_names"] = amazon_lookup[oid]

    # ------------------------------------------------------------------
    # Step 4: Apply deterministic rules
    # ------------------------------------------------------------------
    print("Applying deterministic rules...")
    rule_results: list[Optional[RuleResult]] = []
    api_needed_indices: list[int] = []

    for i, tx in enumerate(transactions):
        result = apply_rules(
            vendor=tx.get("vendor", ""),
            buchungstext=tx.get("buchungstext", ""),
            amount=tx.get("amount_eur", 0.0),
        )
        rule_results.append(result)
        if result is None:
            api_needed_indices.append(i)

    rules_matched = len(transactions) - len(api_needed_indices)
    print(f"  {rules_matched} transactions classified by rules")
    print(f"  {len(api_needed_indices)} transactions need API classification")

    # ------------------------------------------------------------------
    # Step 5: API classification for remaining transactions
    # ------------------------------------------------------------------
    api_classifications: dict[int, dict] = {}

    if api_needed_indices:
        api_txns = [transactions[i] for i in api_needed_indices]

        if verbose:
            print("  Transactions going to API:")
            for tx in api_txns:
                print(f"    {tx['date']}  {tx['vendor'][:40]:<40}  {tx['amount_eur']:>9.2f}")

        print(f"Sending {len(api_txns)} transactions to Claude API ({model})...")
        api_results = classify_transactions(
            api_txns,
            model=model,
            api_key=api_key,
            verbose=verbose,
        )

        for idx, result in zip(api_needed_indices, api_results):
            api_classifications[idx] = result

    # ------------------------------------------------------------------
    # Step 6: Merge results
    # ------------------------------------------------------------------
    final_classifications: list[dict] = []
    for i, tx in enumerate(transactions):
        rule_result = rule_results[i]
        if rule_result is not None:
            cl = {
                "classification": rule_result.classification,
                "category": rule_result.category,
                "confidence": rule_result.confidence,
                "reason": f"Deterministic rule: {rule_result.matched_rule}",
                "needs_receipt": rule_result.needs_receipt,
                "flag_note": rule_result.flag_note,
                "matched_rule": rule_result.matched_rule,
            }
        else:
            cl = api_classifications.get(i, {
                "classification": "needs_review",
                "category": "personal",
                "confidence": 0.0,
                "reason": "No classification available",
                "needs_receipt": True,
                "flag_note": "Klassifizierung fehlt — manuell prüfen",
                "matched_rule": "",
            })
            cl["matched_rule"] = ""

        # Enforce: confidence < 0.7 → needs_receipt = True
        if cl.get("confidence", 0.0) < 0.7:
            cl["needs_receipt"] = True

        final_classifications.append(cl)

    # ------------------------------------------------------------------
    # Step 7: Format, write, summarise
    # ------------------------------------------------------------------
    output_rows = format_rows_for_output(transactions, final_classifications)

    print_summary(output_rows)

    if not dry_run and output_path:
        write_output_csv(output_rows, output_path)
    elif dry_run:
        print("(Dry run — output file not written)")

    return output_rows


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Le La Hanoi — Bank Statement Classifier",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--month",
        required=False,
        help=(
            "Month to process in YYYY-MM format (e.g. 2026-02). "
            "Auto-detects files from data/YYYY-MM/ folder structure. "
            "Required unless --bank is explicitly provided."
        ),
    )
    parser.add_argument(
        "--bank",
        default=None,
        help=(
            "Override: Path to Comdirect CSV file or directory. "
            "If not provided, auto-detects from data/{month}/bank/"
        ),
    )
    parser.add_argument(
        "--amazon",
        default=None,
        help=(
            "Override: Path to Amazon Order History CSV or directory. "
            "If not provided, auto-detects from data/{month}/amazon/ "
            "(optional)"
        ),
    )
    parser.add_argument(
        "--output",
        default=None,
        help=(
            "Override: Output CSV path. "
            "Defaults to data/{month}/output/classified_{month}.csv"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print summary without writing the output CSV",
    )
    parser.add_argument(
        "--model",
        default="claude-sonnet-4-5",
        help="Claude model to use for ambiguous transactions (default: claude-sonnet-4-5)",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="Anthropic API key (or set ANTHROPIC_API_KEY env var)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print detailed progress information",
    )
    parser.add_argument(
        "--no-api",
        action="store_true",
        help="Skip API calls — classify only with deterministic rules (unmatched = needs_review)",
    )

    args = parser.parse_args()

    # Validate month format
    if args.month:
        if not re.match(r'^\d{4}-\d{2}$', args.month):
            parser.error("--month must be in YYYY-MM format, e.g. 2026-02")

    # Resolve directory → file paths
    try:
        bank_path = resolve_bank_path(args.bank, args.month)
    except (FileNotFoundError, ValueError) as e:
        parser.error(str(e))

    amazon_path = resolve_amazon_path(args.amazon, args.month)

    # Check ready2order folder if month is specified
    if args.month:
        check_ready2order_folder(args.month)

    # Build default output path when not explicitly set
    output_path = args.output
    if output_path is None:
        if args.month:
            # Month-based output path
            output_dir = Path(f"data/{args.month}/output")
            output_dir.mkdir(parents=True, exist_ok=True)
            output_path = f"data/{args.month}/output/classified_{args.month}.csv"
        else:
            # Fallback to old structure
            output_path = "data/output/classified_output.csv"

    # If --no-api, monkey-patch api_client to skip calls
    if args.no_api:
        import api_client
        def _no_api(transactions, **kwargs):
            return [
                {
                    "classification": "needs_review",
                    "category": "personal",
                    "confidence": 0.0,
                    "reason": "API skipped (--no-api flag)",
                    "needs_receipt": True,
                    "flag_note": "API-Klassifizierung übersprungen — manuell prüfen",
                }
                for _ in transactions
            ]
        api_client.classify_transactions = _no_api

    try:
        classify_all(
            bank_path=bank_path,
            amazon_path=amazon_path,
            output_path=output_path,
            month_filter=args.month,
            dry_run=args.dry_run,
            model=args.model,
            api_key=args.api_key,
            verbose=args.verbose,
        )
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()

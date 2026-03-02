#!/usr/bin/env python3
"""
Le La Hanoi — Receipt Checker
==============================
Compares the classified transactions output against actual receipt files
on disk and reports what is present, what is missing, and what receipts
exist but have no corresponding transaction.
Usage:
    python check_receipts.py --month 2026-02
    python check_receipts.py --month 2026-02 --report    # write HTML report
Receipt folder structure expected:
    data/YYYY-MM/
    └── invoices/
        ├── amazon/        # named by order ID: 028-XXXXXXX-XXXXXXX.pdf
        ├── suppliers/     # named by vendor+date: metro_2026-02-24.pdf
        │                  #   or: tafelmaier_2026-02-11.pdf
        └── other/         # everything else
Matching logic:
    - Amazon transactions:  match by order_id in filename
    - Non-Amazon:           match by vendor keyword + date in filename
                            OR by canonical filename: {vendor_slug}_{date}.pdf
"""
import argparse
import re
import sys
from pathlib import Path
from datetime import datetime
from typing import Optional
import pandas as pd
# ---------------------------------------------------------------------------
# Vendor → filename slug mapping
# ---------------------------------------------------------------------------
VENDOR_SLUGS = {
    "METRO":        "metro",
    "SUMUP":        "sumup",
    "READY2ORDER":  "ready2order",
    "GEMA":         "gema",
    "TAFELMAIER":   "tafelmaier",
    "DELI TADKA":   "delitadka",
    "MANFRED MICHL": "michl_miete",
    "MEDIA MARKT":  "mediamarkt",
    "BAYERSTORFER": "bayerstorfer",
    "BUTLERS":      "butlers",
    "OBI":          "obi",
    "BAUHAUS":      "bauhaus",
    "HAGEBAU":      "hagebau",
}
def vendor_to_slug(vendor: str) -> str:
    """Map a vendor name to its canonical filename slug."""
    v = vendor.upper()
    for key, slug in VENDOR_SLUGS.items():
        if key in v:
            return slug
    # Fallback: lowercase, strip non-alphanum
    slug = re.sub(r'[^a-z0-9]', '_', vendor.lower())
    slug = re.sub(r'_+', '_', slug).strip('_')
    return slug[:30]
# ---------------------------------------------------------------------------
# Receipt file scanner
# ---------------------------------------------------------------------------
def scan_receipts(month: str) -> dict[str, list[Path]]:
    """
    Scan data/{month}/invoices/ for receipt files.
    Returns dict:
        {
          'amazon':    [Path, ...],
          'suppliers': [Path, ...],
          'other':     [Path, ...],
        }
    """
    base = Path(f"data/{month}/invoices")
    result = {"amazon": [], "suppliers": [], "other": []}
    for subfolder in ["amazon", "suppliers", "other"]:
        folder = base / subfolder
        if folder.exists():
            files = sorted(
                f for f in folder.iterdir()
                if f.is_file() and not f.name.startswith(".")
            )
            result[subfolder] = files
    return result
def extract_order_id_from_filename(filename: str) -> Optional[str]:
    """Extract Amazon order ID from a filename like 028-1234567-8901234.pdf"""
    m = re.search(r'((?:0(?:28|02|04|05|06)|3(?:02|04|05|06))-\d{7}-\d{7})', filename)
    return m.group(1) if m else None
# ---------------------------------------------------------------------------
# Load classified transactions
# ---------------------------------------------------------------------------
def load_classified(month: str) -> pd.DataFrame:
    """Load the classified output CSV for the given month."""
    path = Path(f"data/{month}/output/classified_{month}.csv")
    if not path.exists():
        raise FileNotFoundError(
            f"Classified output not found: {path}\n"
            f"Run: python classify.py --month {month}"
        )
    df = pd.read_csv(path, sep=";", dtype=str, keep_default_na=False)
    # Convert amount
    def parse_amount(v):
        if not v or str(v).strip() in ("", "nan"):
            return 0.0
        cleaned = str(v).strip().replace(".", "").replace(",", ".")
        try:
            return float(cleaned)
        except ValueError:
            return 0.0
    df["amount_eur"] = df["amount_eur"].apply(parse_amount)
    return df
# ---------------------------------------------------------------------------
# Matching logic
# ---------------------------------------------------------------------------
def match_amazon_receipt(order_id: str, amazon_files: list[Path]) -> Optional[Path]:
    """Find a receipt file for a given Amazon order ID."""
    if not order_id:
        return None
    for f in amazon_files:
        fid = extract_order_id_from_filename(f.name)
        if fid and fid == order_id:
            return f
        # Also match without hyphens
        if order_id.replace("-", "") in f.name.replace("-", ""):
            return f
    return None
def match_supplier_receipt(
    vendor: str,
    date: str,
    supplier_files: list[Path],
    other_files: list[Path],
) -> Optional[Path]:
    """
    Find a receipt for a non-Amazon vendor transaction.
    Tries:
    1. Canonical name: {slug}_{date}.pdf  e.g. metro_2026-02-24.pdf
    2. Slug anywhere in filename + date anywhere in filename
    3. Slug anywhere in filename (date optional — some receipts just have slug)
    """
    slug = vendor_to_slug(vendor)
    date_nodash = date.replace("-", "")
    date_dots = datetime.strptime(date, "%Y-%m-%d").strftime("%d.%m.%Y") if date else ""
    all_files = supplier_files + other_files
    for f in all_files:
        name_lower = f.name.lower()
        # Canonical match
        if f"{slug}_{date}" in name_lower or f"{slug}_{date_nodash}" in name_lower:
            return f
        # Slug + date (either format)
        has_slug = slug in name_lower
        has_date = (
            date in name_lower or
            date_nodash in name_lower or
            date_dots.replace(".", "") in name_lower.replace(".", "")
        )
        if has_slug and has_date:
            return f
    # Slug only — still better than nothing, flag as partial match
    for f in all_files:
        if slug in f.name.lower():
            return f  # partial — caller should note this
    return None
# ---------------------------------------------------------------------------
# Core checker
# ---------------------------------------------------------------------------
def check_receipts(month: str) -> dict:
    """
    Main receipt check logic.
    Returns a results dict with:
        present:   list of (transaction, receipt_path, match_quality)
        missing:   list of (transaction, expected_filename)
        orphans:   list of Path — receipts with no matching transaction
        stats:     summary counts and totals
    """
    df = load_classified(month)
    receipts = scan_receipts(month)
    # Only check cafe_expense rows that need a receipt
    expense_rows = df[
        (df["classification"] == "cafe_expense") &
        (df["needs_receipt"].str.upper().isin(["TRUE", "1", "YES"]))
    ].copy()
    # Track which receipt files were claimed
    claimed_receipts: set[Path] = set()
    present = []
    missing = []
    for _, row in expense_rows.iterrows():
        order_id = str(row.get("order_id", "") or "").strip()
        vendor = str(row.get("vendor", "") or "").strip()
        date = str(row.get("date", "") or "").strip()[:10]
        amount = float(row.get("amount_eur", 0))
        category = str(row.get("category", "") or "")
        if order_id:
            # Amazon
            receipt = match_amazon_receipt(order_id, receipts["amazon"])
            if receipt:
                claimed_receipts.add(receipt)
                present.append({
                    "date": date,
                    "vendor": vendor,
                    "order_id": order_id,
                    "amount_eur": amount,
                    "category": category,
                    "receipt_path": str(receipt),
                    "match_quality": "exact",
                })
            else:
                expected = f"data/{month}/invoices/amazon/{order_id}.pdf"
                missing.append({
                    "date": date,
                    "vendor": vendor,
                    "order_id": order_id,
                    "amount_eur": amount,
                    "category": category,
                    "expected_path": expected,
                    "how_to_get": f"amazon.de/gp/css/order-details?orderID={order_id}",
                })
        else:
            # Non-Amazon
            receipt = match_supplier_receipt(
                vendor, date,
                receipts["suppliers"],
                receipts["other"],
            )
            slug = vendor_to_slug(vendor)
            if receipt:
                claimed_receipts.add(receipt)
                match_q = "exact" if date in receipt.name else "slug_only"
                present.append({
                    "date": date,
                    "vendor": vendor,
                    "order_id": "",
                    "amount_eur": amount,
                    "category": category,
                    "receipt_path": str(receipt),
                    "match_quality": match_q,
                })
            else:
                expected = f"data/{month}/invoices/suppliers/{slug}_{date}.pdf"
                missing.append({
                    "date": date,
                    "vendor": vendor,
                    "order_id": "",
                    "amount_eur": amount,
                    "category": category,
                    "expected_path": expected,
                    "how_to_get": _get_receipt_hint(vendor),
                })
    # Orphan receipts — exist on disk but no matching transaction
    all_receipt_files = (
        receipts["amazon"] + receipts["suppliers"] + receipts["other"]
    )
    orphans = [f for f in all_receipt_files if f not in claimed_receipts]
    # Stats
    missing_amount = sum(abs(r["amount_eur"]) for r in missing)
    present_amount = sum(abs(r["amount_eur"]) for r in present)
    stats = {
        "total_expense_rows": len(expense_rows),
        "receipts_present": len(present),
        "receipts_missing": len(missing),
        "orphan_receipts": len(orphans),
        "missing_amount_eur": missing_amount,
        "present_amount_eur": present_amount,
        "coverage_pct": (
            round(present_amount / (present_amount + missing_amount) * 100, 1)
            if (present_amount + missing_amount) > 0 else 0.0
        ),
    }
    return {
        "month": month,
        "present": present,
        "missing": missing,
        "orphans": orphans,
        "stats": stats,
    }
def _get_receipt_hint(vendor: str) -> str:
    """Return a hint on how to obtain the receipt for a given vendor."""
    v = vendor.upper()
    hints = {
        "METRO":        "METRO App → Einkäufe → Rechnung herunterladen",
        "GEMA":         "GEMA Mitgliederportal → Rechnungen",
        "READY2ORDER":  "ready2order Dashboard → Abonnement → Rechnungen",
        "SUMUP":        "SumUp App → Konto → Rechnungen",
        "TAFELMAIER":   "Bäckerei Tafelmaier direkt — Papierbeleg einscannen",
        "DELI TADKA":   "Deli Tadka direkt — Papierbeleg einscannen",
        "MANFRED MICHL": "Mietvertrag + Überweisungsbeleg = ausreichend",
        "MEDIA MARKT":  "MediaMarkt App oder Kassenbon einscannen",
        "BAUHAUS":      "BAUHAUS App → Meine Käufe, oder Kassenbon einscannen",
        "OBI":          "OBI App → Einkaufshistorie, oder Kassenbon einscannen",
        "BAYERSTORFER": "Kassenbon einscannen oder bei Bayerstorfer anfragen",
        "BUTLERS":      "Kassenbon einscannen oder Butlers kontaktieren",
    }
    for key, hint in hints.items():
        if key in v:
            return hint
    return "Kassenbon einscannen oder beim Händler anfordern"
# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------
def print_report(results: dict) -> None:
    """Print a human-readable receipt status report to stdout."""
    s = results["stats"]
    month = results["month"]
    print()
    print("=" * 65)
    print(f"  LE LA HANOI — RECEIPT STATUS REPORT  {month}")
    print("=" * 65)
    print(f"  Transactions requiring receipt:  {s['total_expense_rows']:>4}")
    print(f"  Receipts found:                  {s['receipts_present']:>4}  ({s['present_amount_eur']:>8.2f} EUR)")
    print(f"  Receipts MISSING:                {s['receipts_missing']:>4}  ({s['missing_amount_eur']:>8.2f} EUR)")
    print(f"  Coverage:                        {s['coverage_pct']:>5.1f}%")
    if s["orphan_receipts"]:
        print(f"  Unmatched receipt files:         {s['orphan_receipts']:>4}")
    print()
    if results["missing"]:
        print(f"  ── MISSING RECEIPTS ({len(results['missing'])}) ──────────────────────────")
        print()
        # Group by category
        by_cat: dict[str, list] = {}
        for r in results["missing"]:
            cat = r["category"]
            by_cat.setdefault(cat, []).append(r)
        for cat, items in sorted(by_cat.items()):
            cat_total = sum(abs(i["amount_eur"]) for i in items)
            print(f"  [{cat}]  {cat_total:.2f} EUR")
            for item in sorted(items, key=lambda x: x["date"]):
                oid = f"  Order: {item['order_id']}" if item["order_id"] else ""
                print(f"    {item['date']}  {item['vendor'][:35]:<35}  {abs(item['amount_eur']):>8.2f} EUR{oid}")
                print(f"             → Save as: {item['expected_path']}")
                print(f"             → How:     {item['how_to_get']}")
            print()
    if results["present"]:
        slug_only = [r for r in results["present"] if r["match_quality"] == "slug_only"]
        if slug_only:
            print(f"  ── PARTIAL MATCHES (slug only, verify date) ────────────────")
            for r in slug_only:
                print(f"    {r['date']}  {r['vendor'][:35]:<35}  {abs(r['amount_eur']):>8.2f} EUR")
                print(f"             → Matched: {r['receipt_path']}")
            print()
    if results["orphans"]:
        print(f"  ── ORPHAN RECEIPTS (no matching transaction) ────────────────")
        for f in results["orphans"]:
            print(f"    {f}")
        print()
    print("=" * 65)
    print()
def write_html_report(results: dict, output_path: str) -> None:
    """Write an HTML receipt status report."""
    s = results["stats"]
    month = results["month"]
    missing_rows = ""
    for r in sorted(results["missing"], key=lambda x: x["date"]):
        oid_cell = f'<code>{r["order_id"]}</code>' if r["order_id"] else "—"
        link = ""
        if r["order_id"]:
            url = f"https://www.amazon.de/gp/css/order-details?orderID={r['order_id']}"
            link = f'<a href="{url}" target="_blank">Amazon öffnen ↗</a>'
        missing_rows += f"""
        <tr class="missing">
            <td>{r['date']}</td>
            <td>{r['vendor'][:40]}</td>
            <td class="amount">{abs(r['amount_eur']):.2f}</td>
            <td><span class="cat">{r['category']}</span></td>
            <td>{oid_cell}</td>
            <td><code>{r['expected_path']}</code></td>
            <td>{link or r['how_to_get']}</td>
        </tr>"""
    present_rows = ""
    for r in sorted(results["present"], key=lambda x: x["date"]):
        quality_class = "exact" if r["match_quality"] == "exact" else "partial"
        quality_label = "✓" if r["match_quality"] == "exact" else "~ slug only"
        oid_cell = f'<code>{r["order_id"]}</code>' if r["order_id"] else "—"
        present_rows += f"""
        <tr class="{quality_class}">
            <td>{r['date']}</td>
            <td>{r['vendor'][:40]}</td>
            <td class="amount">{abs(r['amount_eur']):.2f}</td>
            <td><span class="cat">{r['category']}</span></td>
            <td>{oid_cell}</td>
            <td><code style="font-size:11px">{r['receipt_path']}</code></td>
            <td class="quality">{quality_label}</td>
        </tr>"""
    orphan_rows = ""
    for f in results["orphans"]:
        orphan_rows += f"<tr><td colspan='7'><code>{f}</code></td></tr>"
    coverage_color = "#22c55e" if s["coverage_pct"] >= 80 else "#f59e0b" if s["coverage_pct"] >= 50 else "#ef4444"

    # Build conditional HTML blocks before the main f-string to avoid
    # nested f-string triple-quote syntax (not valid in Python < 3.12)
    if results["missing"]:
        missing_section = (
            "<table>\n    <tr>\n"
            "      <th>Datum</th><th>Empfänger</th><th>EUR</th><th>Kategorie</th>\n"
            "      <th>Order-ID</th><th>Dateiname (Ziel)</th><th>Beleg holen</th>\n"
            "    </tr>\n    " + missing_rows + "\n  </table>"
        )
    else:
        missing_section = "<p class='empty'>Alle Belege vorhanden. ✓</p>"

    if results["present"]:
        present_section = (
            "<table>\n    <tr>\n"
            "      <th>Datum</th><th>Empfänger</th><th>EUR</th><th>Kategorie</th>\n"
            "      <th>Order-ID</th><th>Datei</th><th>Match</th>\n"
            "    </tr>\n    " + present_rows + "\n  </table>"
        )
    else:
        present_section = "<p class='empty'>Noch keine Belege hochgeladen.</p>"

    if results["orphans"]:
        orphan_section = (
            "<section><h2>Nicht zugeordnete Dateien ("
            + str(len(results["orphans"]))
            + ")</h2><table>" + orphan_rows + "</table></section>"
        )
    else:
        orphan_section = ""

    html = f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<title>Le La Hanoi – Belegstatus {month}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'IBM Plex Mono', monospace, monospace; background: #0d0d0d; color: #d0d0d0; padding: 32px; font-size: 13px; }}
  h1 {{ font-size: 18px; color: #f0f0f0; margin-bottom: 4px; }}
  .subtitle {{ color: #555; font-size: 12px; margin-bottom: 32px; }}
  .stats {{ display: flex; gap: 24px; margin-bottom: 40px; flex-wrap: wrap; }}
  .stat {{ background: #141414; border: 1px solid #222; padding: 16px 24px; min-width: 160px; }}
  .stat-label {{ font-size: 10px; color: #555; letter-spacing: 2px; text-transform: uppercase; margin-bottom: 6px; }}
  .stat-value {{ font-size: 24px; font-weight: 600; }}
  .stat-sub {{ font-size: 11px; color: #444; margin-top: 4px; }}
  section {{ margin-bottom: 40px; }}
  h2 {{ font-size: 13px; color: #888; letter-spacing: 2px; text-transform: uppercase; margin-bottom: 12px; border-bottom: 1px solid #1e1e1e; padding-bottom: 8px; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th {{ text-align: left; padding: 8px; color: #444; font-size: 10px; letter-spacing: 1px; text-transform: uppercase; border-bottom: 1px solid #1e1e1e; }}
  td {{ padding: 7px 8px; border-bottom: 1px solid #181818; vertical-align: top; }}
  tr.missing td {{ color: #fca5a5; }}
  tr.exact td {{ color: #86efac; }}
  tr.partial td {{ color: #fde68a; }}
  .amount {{ text-align: right; font-weight: 500; }}
  .cat {{ background: #1e1e1e; padding: 2px 6px; font-size: 11px; color: #888; border-radius: 2px; }}
  code {{ font-size: 11px; color: #666; }}
  a {{ color: #60a5fa; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .quality {{ font-size: 12px; }}
  .empty {{ color: #333; font-style: italic; padding: 16px 0; }}
</style>
</head>
<body>
<h1>Le La Hanoi — Belegstatus</h1>
<div class="subtitle">Monat: {month} · Erstellt: {datetime.now().strftime('%Y-%m-%d %H:%M')}</div>
<div class="stats">
  <div class="stat">
    <div class="stat-label">Belege vorhanden</div>
    <div class="stat-value" style="color:#86efac">{s['receipts_present']}</div>
    <div class="stat-sub">{s['present_amount_eur']:.2f} EUR</div>
  </div>
  <div class="stat">
    <div class="stat-label">Belege fehlend</div>
    <div class="stat-value" style="color:#fca5a5">{s['receipts_missing']}</div>
    <div class="stat-sub">{s['missing_amount_eur']:.2f} EUR</div>
  </div>
  <div class="stat">
    <div class="stat-label">Abdeckung</div>
    <div class="stat-value" style="color:{coverage_color}">{s['coverage_pct']}%</div>
    <div class="stat-sub">nach Betrag</div>
  </div>
  <div class="stat">
    <div class="stat-label">Nicht zugeordnet</div>
    <div class="stat-value" style="color:#94a3b8">{s['orphan_receipts']}</div>
    <div class="stat-sub">Dateien ohne Buchung</div>
  </div>
</div>
<section>
  <h2>Fehlende Belege ({len(results['missing'])})</h2>
  {missing_section}
</section>
<section>
  <h2>Vorhandene Belege ({len(results['present'])})</h2>
  {present_section}
</section>
{orphan_section}
</body>
</html>"""
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"HTML report written to: {output_path}")
def write_missing_csv(results: dict, output_path: str) -> None:
    """Write just the missing receipts list as a CSV for easy follow-up."""
    import csv
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["date", "vendor", "amount_eur", "category", "order_id",
                        "expected_path", "how_to_get"],
            delimiter=";",
        )
        writer.writeheader()
        for r in sorted(results["missing"], key=lambda x: x["date"]):
            writer.writerow(r)
    print(f"Missing receipts CSV written to: {output_path}")
# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Le La Hanoi — Receipt Checker",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--month", required=True,
        help="Month in YYYY-MM format, e.g. 2026-02",
    )
    parser.add_argument(
        "--report", action="store_true",
        help="Write HTML report to data/{month}/output/receipt_status_{month}.html",
    )
    parser.add_argument(
        "--csv", action="store_true",
        help="Write missing-receipts CSV to data/{month}/output/missing_receipts_{month}.csv",
    )
    args = parser.parse_args()
    if not re.match(r'^\d{4}-\d{2}$', args.month):
        parser.error("--month must be YYYY-MM, e.g. 2026-02")
    try:
        results = check_receipts(args.month)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    print_report(results)
    if args.report:
        html_path = f"data/{args.month}/output/receipt_status_{args.month}.html"
        write_html_report(results, html_path)
    if args.csv:
        csv_path = f"data/{args.month}/output/missing_receipts_{args.month}.csv"
        write_missing_csv(results, csv_path)
if __name__ == "__main__":
    main()

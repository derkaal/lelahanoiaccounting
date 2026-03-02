"""
Output writer and summary printer for Le La Hanoi classifier.

Writes the classified transactions to a CSV and prints a summary to stdout.
"""

import csv
from pathlib import Path
from typing import Optional


OUTPUT_COLUMNS = [
    "date",
    "vendor",
    "buchungstext",
    "amount_eur",
    "order_id",
    "product_names",
    "classification",
    "category",
    "confidence",
    "reason",
    "needs_receipt",
    "flag_note",
    "matched_rule",
]


def write_output_csv(rows: list[dict], output_path: str) -> None:
    """Write classified transactions to a CSV file."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=OUTPUT_COLUMNS,
            extrasaction="ignore",
        )
        writer.writeheader()
        for row in rows:
            # Normalise product_names list → pipe-separated string
            products = row.get("product_names")
            if isinstance(products, list):
                row = {**row, "product_names": " | ".join(products)}
            writer.writerow(row)

    print(f"\nOutput written to: {path}")


def print_summary(rows: list[dict]) -> None:
    """Print a structured summary of classification results to stdout."""
    if not rows:
        print("No transactions to summarise.")
        return

    # ---------------------------------------------------------------
    # Aggregate by classification
    # ---------------------------------------------------------------
    by_class: dict[str, dict] = {}
    for row in rows:
        cl = row.get("classification", "unknown")
        amt = float(row.get("amount_eur", 0.0))
        if cl not in by_class:
            by_class[cl] = {"count": 0, "total": 0.0}
        by_class[cl]["count"] += 1
        by_class[cl]["total"] += amt

    # ---------------------------------------------------------------
    # Aggregate by category (only for cafe_expense rows)
    # ---------------------------------------------------------------
    by_cat: dict[str, dict] = {}
    for row in rows:
        if row.get("classification") != "cafe_expense":
            continue
        cat = row.get("category", "unknown")
        amt = float(row.get("amount_eur", 0.0))
        if cat not in by_cat:
            by_cat[cat] = {"count": 0, "total": 0.0}
        by_cat[cat]["count"] += 1
        by_cat[cat]["total"] += amt

    # ---------------------------------------------------------------
    # Needs-review list
    # ---------------------------------------------------------------
    needs_review = [r for r in rows if r.get("needs_receipt") or r.get("classification") == "needs_review"]

    # ---------------------------------------------------------------
    # Print
    # ---------------------------------------------------------------
    total_transactions = len(rows)
    print()
    print("=" * 60)
    print("  LE LA HANOI — TRANSACTION CLASSIFICATION SUMMARY")
    print("=" * 60)
    print(f"  Total transactions processed: {total_transactions}")
    print()

    print("  BY CLASSIFICATION")
    print("  " + "-" * 50)
    order = ["cafe_expense", "personal", "transfer", "salary", "needs_review", "ignore", "unknown"]
    for cl in order:
        if cl not in by_class:
            continue
        info = by_class[cl]
        print(
            f"  {cl:<20} {info['count']:>4} txns   {info['total']:>10.2f} EUR"
        )
    # Print any that weren't in the ordered list
    for cl, info in sorted(by_class.items()):
        if cl not in order:
            print(
                f"  {cl:<20} {info['count']:>4} txns   {info['total']:>10.2f} EUR"
            )

    print()
    if by_cat:
        print("  CAFÉ EXPENSES BY CATEGORY")
        print("  " + "-" * 50)
        cat_order = [
            "exp_miete", "exp_wareneinkauf", "exp_energie", "exp_bankgebuehr",
            "exp_marketing", "exp_buerobedarf", "exp_versicherung",
            "exp_instandhaltung", "asset_gwg", "exp_bewirtung",
        ]
        for cat in cat_order:
            if cat not in by_cat:
                continue
            info = by_cat[cat]
            print(
                f"  {cat:<22} {info['count']:>4} txns   {info['total']:>10.2f} EUR"
            )
        for cat, info in sorted(by_cat.items()):
            if cat not in cat_order:
                print(
                    f"  {cat:<22} {info['count']:>4} txns   {info['total']:>10.2f} EUR"
                )

        cafe_total = sum(v["total"] for v in by_cat.values())
        cafe_count = sum(v["count"] for v in by_cat.values())
        print("  " + "-" * 50)
        print(f"  {'TOTAL CAFÉ EXPENSES':<22} {cafe_count:>4} txns   {cafe_total:>10.2f} EUR")

    print()
    if needs_review:
        print(f"  TRANSACTIONS NEEDING REVIEW ({len(needs_review)})")
        print("  " + "-" * 50)
        for r in needs_review:
            flag = r.get("flag_note") or r.get("reason") or ""
            print(
                f"  {r.get('date',''):>10}  {str(r.get('vendor',''))[:25]:<25}"
                f"  {float(r.get('amount_eur', 0)):>9.2f} EUR"
                f"  [{r.get('classification','')}]"
            )
            if flag:
                print(f"             → {flag}")

    print()
    print("=" * 60)


def format_rows_for_output(
    transactions: list[dict],
    classifications: list[dict],
) -> list[dict]:
    """
    Merge parsed transaction rows with their classification results.

    transactions: list of dicts from bank CSV parser
    classifications: list of classification dicts (same order)

    Returns merged list ready for write_output_csv() / print_summary().
    """
    assert len(transactions) == len(classifications), (
        f"Transaction count ({len(transactions)}) != "
        f"classification count ({len(classifications)})"
    )

    rows = []
    for tx, cl in zip(transactions, classifications):
        row = {
            "date": tx.get("date", ""),
            "vendor": tx.get("vendor", ""),
            "buchungstext": tx.get("buchungstext", ""),
            "amount_eur": tx.get("amount_eur", 0.0),
            "order_id": tx.get("order_id", ""),
            "product_names": tx.get("product_names", ""),
            "classification": cl.get("classification", ""),
            "category": cl.get("category", ""),
            "confidence": cl.get("confidence", 0.0),
            "reason": cl.get("reason", ""),
            "needs_receipt": cl.get("needs_receipt", False),
            "flag_note": cl.get("flag_note", ""),
            "matched_rule": cl.get("matched_rule", ""),
        }
        # Low confidence always triggers needs_receipt
        if row["confidence"] < 0.7:
            row["needs_receipt"] = True
        rows.append(row)

    return rows

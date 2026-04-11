#!/usr/bin/env python3
"""
Le La Hanoi — Receipt Processor
=================================
Extracts tax-relevant data from PDF receipts using Claude AI (vision for
scanned PDFs, text for digital ones), matches them to classified bank/credit
transactions, and produces an EÜR-ready report.

Usage:
    # Analyse + terminal report (no files moved)
    python process_receipts.py --month 2026-02

    # Also write HTML report and EÜR CSV
    python process_receipts.py --month 2026-02 --report --eur-csv

    # After reviewing the report: actually move the files
    python process_receipts.py --month 2026-02 --apply

    # Custom inbox folder
    python process_receipts.py --month 2026-02 --inbox /path/to/pdfs/

Inbox:  data/inbox/   ← drop ALL PDFs here, regardless of month

After --apply, PDFs are moved to:
    data/{month}/invoices/accounted_for/     ← matched to a bank/credit transaction
    data/{month}/invoices/not_accounted_for/ ← cash payment or from a different month

Output files (data/{month}/output/):
    receipt_analysis_{month}.html   HTML report (--report)
    eur_summary_{month}.csv         EÜR Betriebsausgaben summary (--eur-csv)
"""

import argparse
import base64
import csv
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import fitz  # PyMuPDF
import pandas as pd
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()


# ---------------------------------------------------------------------------
# EÜR category → Anlage-EÜR line number mapping
# ---------------------------------------------------------------------------

EUR_LINES: dict[str, dict] = {
    "exp_wareneinkauf":   {"line": 46, "label": "Wareneinkauf"},
    "exp_miete":          {"line": 50, "label": "Miete"},
    "exp_energie":        {"line": 52, "label": "Energie (Strom, Gas, Wasser)"},
    "exp_versicherung":   {"line": 54, "label": "Versicherungen"},
    "exp_marketing":      {"line": 56, "label": "Werbung / GEMA / Marketing"},
    "exp_buerobedarf":    {"line": 57, "label": "Bürobedarf / Porto / Telefon"},
    "exp_instandhaltung": {"line": 60, "label": "Instandhaltung / Reparatur"},
    "asset_gwg":          {"line": 62, "label": "GWG (< 800 € netto)"},
    "exp_bankgebuehr":    {"line": 73, "label": "Bankgebühren"},
    "exp_bewirtung":      {"line": 73, "label": "Bewirtung (70 % abzugsfähig, § 4 V Nr. 2)"},
}

# ---------------------------------------------------------------------------
# Claude prompt for receipt extraction
# ---------------------------------------------------------------------------

EXTRACTION_PROMPT = """\
Du bist ein Experte für die Analyse von deutschen Kassenbons und Rechnungen.
Extrahiere die steuerrelevanten Daten aus dem beigefügten Beleg.

Antworte NUR mit einem validen JSON-Objekt – keinerlei weiterer Text:
{
  "vendor": "Firmenname wie auf dem Beleg",
  "date": "YYYY-MM-DD oder null",
  "invoice_number": "Rechnungsnummer oder null",
  "payment_method": "bar|karte|rechnung|unbekannt",
  "vat_breakdown": [
    {"vat_rate": 19.0, "net_amount": 45.00, "vat_amount": 8.55, "gross_amount": 53.55}
  ],
  "total_gross": 53.55,
  "currency": "EUR",
  "confidence": "high|medium|low"
}

Hinweise:
- vat_breakdown: je einen Eintrag pro MwSt-Satz (7 % Lebensmittel, 19 % sonstiges)
- METRO-Kassenbons: Steuertabelle am Ende auslesen (A = 19 %, B = 7 %)
- total_gross muss der Summe aller gross_amounts in vat_breakdown entsprechen
- payment_method: "bar" für Bargeld, "karte" für EC/Kreditkarte, "rechnung" für Überweisung
- confidence: "high" alle Felder klar lesbar, "medium" Schätzungen nötig, "low" unleserlich
- Kein Beleg erkennbar → confidence = "low", alle anderen Felder null
"""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class VatLine:
    rate: float         # e.g. 7.0 or 19.0
    net_amount: float
    vat_amount: float
    gross_amount: float


@dataclass
class ReceiptData:
    pdf_path: Path
    vendor: str = ""
    date: str = ""              # YYYY-MM-DD or ""
    invoice_number: str = ""
    payment_method: str = "unbekannt"
    vat_lines: list[VatLine] = field(default_factory=list)
    total_gross: float = 0.0
    currency: str = "EUR"
    confidence: str = "low"
    extraction_error: str = ""


@dataclass
class MatchResult:
    receipt: ReceiptData
    matched_tx: Optional[dict]   # row from classified CSV, or None
    match_quality: str           # "exact" | "amount_only" | "none"
    proposed_folder: str         # "accounted_for" | "not_accounted_for"
    notes: str = ""


# ---------------------------------------------------------------------------
# PDF → Claude content blocks
# ---------------------------------------------------------------------------

def _pdf_to_content(pdf_path: Path) -> list[dict]:
    """
    Convert a PDF to Claude API message content.

    Digital PDFs (extractable text): sent as a text block (cheaper, faster).
    Scanned PDFs (no/little text):   rendered to PNG images at 150 DPI,
                                      up to 3 pages, sent as image blocks.
    """
    doc = fitz.open(str(pdf_path))
    max_pages = min(3, len(doc))

    # Try text extraction first — works for digital PDFs (Amazon, GEMA, etc.)
    full_text = ""
    for i in range(max_pages):
        full_text += doc[i].get_text("text")

    if len(full_text.strip()) > 150:
        # Sufficient text extracted — send as text (faster + cheaper)
        return [{
            "type": "text",
            "text": f"<receipt_text>\n{full_text.strip()}\n</receipt_text>\n\n{EXTRACTION_PROMPT}",
        }]

    # Scanned PDF — render pages as PNG images and send via vision
    content: list[dict] = []
    for i in range(max_pages):
        pix = doc[i].get_pixmap(dpi=150)
        b64 = base64.standard_b64encode(pix.tobytes("png")).decode("utf-8")
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": b64},
        })

    content.append({"type": "text", "text": EXTRACTION_PROMPT})
    return content


# ---------------------------------------------------------------------------
# Receipt extraction
# ---------------------------------------------------------------------------

def extract_receipt(pdf_path: Path, client: Anthropic, model: str) -> ReceiptData:
    """Extract structured data from a receipt PDF using Claude."""
    receipt = ReceiptData(pdf_path=pdf_path)

    try:
        content = _pdf_to_content(pdf_path)
    except Exception as e:
        receipt.extraction_error = f"PDF-Fehler: {e}"
        return receipt

    try:
        response = client.messages.create(
            model=model,
            max_tokens=1024,
            messages=[{"role": "user", "content": content}],
        )
        raw = response.content[0].text.strip()
        # Strip markdown code fences if the model adds them
        raw = re.sub(r'^```(?:json)?\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)
        data = json.loads(raw)
        if isinstance(data, list):
            data = data[0] if data else {}
    except json.JSONDecodeError as e:
        receipt.extraction_error = f"JSON-Parse-Fehler: {e}"
        return receipt
    except Exception as e:
        receipt.extraction_error = f"API-Fehler: {e}"
        return receipt

    receipt.vendor         = str(data.get("vendor") or "")
    receipt.date           = str(data.get("date") or "")
    receipt.invoice_number = str(data.get("invoice_number") or "")
    receipt.payment_method = str(data.get("payment_method") or "unbekannt")
    receipt.total_gross    = float(data.get("total_gross") or 0.0)
    receipt.currency       = str(data.get("currency") or "EUR")
    receipt.confidence     = str(data.get("confidence") or "low")

    for vl in data.get("vat_breakdown") or []:
        try:
            receipt.vat_lines.append(VatLine(
                rate=float(vl.get("vat_rate", 0)),
                net_amount=float(vl.get("net_amount", 0)),
                vat_amount=float(vl.get("vat_amount", 0)),
                gross_amount=float(vl.get("gross_amount", 0)),
            ))
        except (TypeError, ValueError):
            continue

    return receipt


# ---------------------------------------------------------------------------
# Load classified expenses from the bank/credit classification output
# ---------------------------------------------------------------------------

def _parse_european_amount(v: str) -> float:
    """Parse a European-formatted number string (1.234,56) to float."""
    if not v or str(v).strip() in ("", "nan"):
        return 0.0
    cleaned = str(v).strip().replace(".", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def load_expenses(month: str) -> list[dict]:
    """
    Load cafe_expense rows from the classified CSV for the given month.
    Returns a list of dicts with float amount_eur values.
    """
    path = Path(f"data/{month}/output/classified_{month}.csv")
    if not path.exists():
        raise FileNotFoundError(
            f"Klassifizierte Ausgaben nicht gefunden: {path}\n"
            f"Zuerst ausführen: python classify.py --month {month}"
        )
    df = pd.read_csv(path, sep=";", dtype=str, keep_default_na=False)
    df = df[df["classification"] == "cafe_expense"].copy()
    df["amount_eur"] = df["amount_eur"].apply(_parse_european_amount)
    return df.to_dict("records")


# ---------------------------------------------------------------------------
# Matching engine
# ---------------------------------------------------------------------------

AMOUNT_TOLERANCE = 0.02  # EUR — allow for rounding differences


def _find_candidates(
    target_gross: float,
    expenses: list[dict],
    claimed: set[int],
) -> list[tuple[int, dict]]:
    """Return unclaimed expenses whose |amount_eur| matches target_gross."""
    return [
        (i, exp) for i, exp in enumerate(expenses)
        if i not in claimed
        and abs(abs(exp.get("amount_eur", 0.0)) - target_gross) <= AMOUNT_TOLERANCE
    ]


def _score_by_vendor(vendor: str, candidates: list[tuple[int, dict]]) -> list[tuple[int, int, dict]]:
    """Score candidates by how many vendor words appear in the bank transaction."""
    vendor_words = [w for w in vendor.lower().split() if len(w) > 2]
    scored = []
    for i, exp in candidates:
        tx_vendor = str(exp.get("vendor", "")).lower()
        score = sum(1 for w in vendor_words if w in tx_vendor)
        scored.append((score, i, exp))
    return sorted(scored, reverse=True)


def match_receipt_to_expense(
    receipt: ReceiptData,
    expenses: list[dict],
    claimed: set[int],
) -> tuple[Optional[dict], str, int]:
    """
    Find the best unclaimed expense match for a receipt.

    Matching rules:
    - Amount must match within AMOUNT_TOLERANCE (date gaps are allowed)
    - If multiple candidates: prefer vendor name overlap, then closest date
    Returns (matched_expense, match_quality, expense_index) or (None, "none", -1).
    """
    if receipt.total_gross <= 0:
        return None, "none", -1

    candidates = _find_candidates(abs(receipt.total_gross), expenses, claimed)
    if not candidates:
        return None, "none", -1

    if len(candidates) == 1:
        i, exp = candidates[0]
        return exp, "exact", i

    # Multiple amount matches — disambiguate
    scored = _score_by_vendor(receipt.vendor, candidates)
    best_score, best_i, best_exp = scored[0]

    # Secondary: date proximity (if receipt date is known)
    if receipt.date and scored[0][0] == scored[1][0]:  # tie on vendor score
        try:
            receipt_dt = pd.to_datetime(receipt.date)
            scored_by_date = sorted(
                candidates,
                key=lambda x: abs((pd.to_datetime(x[1].get("date", "2000-01-01")) - receipt_dt).days)
            )
            best_i, best_exp = scored_by_date[0]
        except Exception:
            pass

    quality = "exact" if best_score > 0 else "amount_only"
    return best_exp, quality, best_i


def run_matching(
    receipts: list[ReceiptData],
    expenses: list[dict],
    target_month: str,
) -> tuple[list[MatchResult], list[dict]]:
    """
    Match all receipts to expenses. Returns (match_results, unmatched_expenses).

    Process receipts in date order for more deterministic results when
    multiple receipts share the same amount.
    """
    claimed: set[int] = set()
    results: list[MatchResult] = []
    year, month = target_month.split("-")

    # Sort by date so earlier receipts claim expenses first
    sorted_receipts = sorted(receipts, key=lambda r: r.date or "9999-99-99")

    for receipt in sorted_receipts:
        if receipt.extraction_error:
            results.append(MatchResult(
                receipt=receipt,
                matched_tx=None,
                match_quality="none",
                proposed_folder="not_accounted_for",
                notes=f"Extraktion fehlgeschlagen: {receipt.extraction_error}",
            ))
            continue

        matched_tx, quality, idx = match_receipt_to_expense(receipt, expenses, claimed)

        if matched_tx is not None:
            claimed.add(idx)
            notes = "Händler-Name weicht ab — Betrag stimmt" if quality == "amount_only" else ""
            results.append(MatchResult(
                receipt=receipt,
                matched_tx=matched_tx,
                match_quality=quality,
                proposed_folder="accounted_for",
                notes=notes,
            ))
        else:
            parts: list[str] = []
            if receipt.payment_method == "bar":
                parts.append("Barzahlung — nicht im Kontoauszug erwartet")
            if receipt.date and not receipt.date.startswith(f"{year}-{month}"):
                parts.append(f"Beleg vom {receipt.date} — anderer Monat als {target_month}")
            elif not receipt.date:
                parts.append("Belegdatum nicht lesbar")
            if not parts:
                parts.append("Kein passender Kontoauszug-Eintrag gefunden")
            results.append(MatchResult(
                receipt=receipt,
                matched_tx=None,
                match_quality="none",
                proposed_folder="not_accounted_for",
                notes=" | ".join(parts),
            ))

    unmatched_expenses = [
        exp for i, exp in enumerate(expenses) if i not in claimed
    ]
    return results, unmatched_expenses


# ---------------------------------------------------------------------------
# EÜR summary builder
# ---------------------------------------------------------------------------

@dataclass
class EurLine:
    eur_line: int
    label: str
    category: str
    count: int = 0
    net_7: float = 0.0
    vat_7: float = 0.0
    net_19: float = 0.0
    vat_19: float = 0.0
    gross: float = 0.0
    cash_count: int = 0


def build_eur_summary(
    match_results: list[MatchResult],
    unmatched_expenses: list[dict],
) -> list[EurLine]:
    """
    Build per-category EÜR summary.

    Includes:
    - Receipts matched to bank transactions (accounted_for)
    - Cash receipts (not_accounted_for, payment_method=bar) — still valid expenses
    - Bank expenses without a receipt — gross total only, no VAT breakdown
    """
    lines: dict[str, EurLine] = {}

    def get_line(category: str) -> EurLine:
        if category not in lines:
            info = EUR_LINES.get(
                category,
                {"line": 73, "label": "Sonstige Betriebsausgaben"},
            )
            lines[category] = EurLine(
                eur_line=info["line"],
                label=info["label"],
                category=category,
            )
        return lines[category]

    # Receipts we have (both matched and cash)
    for mr in match_results:
        if mr.receipt.extraction_error or mr.receipt.total_gross <= 0:
            continue

        category = (mr.matched_tx or {}).get("category", "") or "exp_wareneinkauf"
        ln = get_line(category)
        ln.count += 1
        ln.gross += abs(mr.receipt.total_gross)

        if mr.receipt.payment_method == "bar":
            ln.cash_count += 1

        for vl in mr.receipt.vat_lines:
            if abs(vl.rate - 7.0) < 0.5:
                ln.net_7 += vl.net_amount
                ln.vat_7 += vl.vat_amount
            elif abs(vl.rate - 19.0) < 0.5:
                ln.net_19 += vl.net_amount
                ln.vat_19 += vl.vat_amount
            # Other rates (e.g. 0%) — add to gross only

    # Bank expenses without a receipt (no VAT breakdown available)
    for exp in unmatched_expenses:
        category = exp.get("category", "") or "exp_wareneinkauf"
        ln = get_line(category)
        ln.count += 1
        ln.gross += abs(exp.get("amount_eur", 0.0))

    return sorted(lines.values(), key=lambda x: (x.eur_line, x.category))


# ---------------------------------------------------------------------------
# Terminal report
# ---------------------------------------------------------------------------

def print_report(
    match_results: list[MatchResult],
    unmatched_expenses: list[dict],
    month: str,
) -> None:
    matched  = [mr for mr in match_results if mr.matched_tx is not None]
    orphans  = [mr for mr in match_results if mr.matched_tx is None and not mr.receipt.extraction_error]
    # Liste 4: extraction failures + low-confidence reads
    unclear  = [
        mr for mr in match_results
        if mr.receipt.extraction_error or mr.receipt.confidence == "low"
    ]

    total_matched = sum(abs(mr.receipt.total_gross) for mr in matched)
    total_unmatched_exp = sum(abs(e.get("amount_eur", 0)) for e in unmatched_expenses)
    total_orphan = sum(abs(mr.receipt.total_gross) for mr in orphans)

    print()
    print("=" * 70)
    print(f"  LE LA HANOI — BELEGABGLEICH  {month}")
    print("=" * 70)
    print(f"  Belege abgeglichen (Liste 1):         {len(matched):>4}  ({total_matched:>10.2f} EUR)")
    print(f"  Ausgaben ohne Beleg (Liste 2):        {len(unmatched_expenses):>4}  ({total_unmatched_exp:>10.2f} EUR)")
    print(f"  Belege ohne Konto (Liste 3):          {len(orphans):>4}  ({total_orphan:>10.2f} EUR)")
    print(f"  Nicht lesbar / Prüfung (Liste 4):     {len(unclear):>4}")
    print()

    # LIST 1 — matched
    if matched:
        print(f"  ── LISTE 1: BELEGE MIT KONTOAUSZUG-EINTRAG ({len(matched)}) " + "─" * 20)
        print()
        for mr in sorted(matched, key=lambda x: x.receipt.date or ""):
            tx = mr.matched_tx
            warn = "  ⚠ Händler abweichend" if mr.match_quality == "amount_only" else ""
            conf = f" [{mr.receipt.confidence}]" if mr.receipt.confidence != "high" else ""
            print(f"    {mr.receipt.date or '?':>10}  {mr.receipt.vendor[:38]:<38}  {abs(mr.receipt.total_gross):>8.2f} EUR{warn}{conf}")
            print(f"             → Konto: {tx.get('date',''):>10}  {str(tx.get('vendor',''))[:35]:<35}  [{tx.get('source','')}/{tx.get('category','')}]")
            vat_parts = [f"  {vl.rate:.0f}%: Netto {vl.net_amount:.2f} / MwSt {vl.vat_amount:.2f} / Brutto {vl.gross_amount:.2f}" for vl in mr.receipt.vat_lines]
            if vat_parts:
                print("             → MwSt:" + " |".join(vat_parts))
            if mr.notes:
                print(f"             ⚠ {mr.notes}")
        print()

    # LIST 2 — expenses without receipt
    if unmatched_expenses:
        print(f"  ── LISTE 2: AUSGABEN IM KONTO OHNE BELEG ({len(unmatched_expenses)}) " + "─" * 16)
        print()
        by_cat: dict[str, list] = {}
        for exp in unmatched_expenses:
            by_cat.setdefault(exp.get("category", "?"), []).append(exp)
        for cat, items in sorted(by_cat.items()):
            cat_total = sum(abs(i.get("amount_eur", 0)) for i in items)
            print(f"  [{cat}]  {cat_total:.2f} EUR")
            for exp in sorted(items, key=lambda x: x.get("date", "")):
                print(f"    {exp.get('date','?'):>10}  {str(exp.get('vendor',''))[:45]:<45}  {abs(exp.get('amount_eur', 0)):>8.2f} EUR")
        print()

    # LIST 3 — orphan receipts
    if orphans:
        print(f"  ── LISTE 3: BELEGE OHNE KONTOAUSZUG — BAR / ANDERER MONAT ({len(orphans)}) " + "─" * 5)
        print()
        for mr in sorted(orphans, key=lambda x: x.receipt.date or ""):
            pm = f"  [{mr.receipt.payment_method}]" if mr.receipt.payment_method != "unbekannt" else ""
            print(f"    {mr.receipt.date or '?':>10}  {mr.receipt.vendor[:40]:<40}  {abs(mr.receipt.total_gross):>8.2f} EUR{pm}")
            print(f"             → {mr.receipt.pdf_path.name}")
            if mr.notes:
                print(f"             → {mr.notes}")
        print()

    # LIST 4 — unreadable / low confidence
    if unclear:
        print(f"  ── LISTE 4: NICHT LESBAR / PRÜFUNG ERFORDERLICH ({len(unclear)}) " + "─" * 10)
        print()
        for mr in sorted(unclear, key=lambda x: x.receipt.pdf_path.name):
            if mr.receipt.extraction_error:
                reason = f"Fehler: {mr.receipt.extraction_error}"
            else:
                reason = f"confidence={mr.receipt.confidence} — Daten evtl. unvollständig"
            gross = f"  {abs(mr.receipt.total_gross):.2f} EUR" if mr.receipt.total_gross else ""
            vendor = mr.receipt.vendor[:35] if mr.receipt.vendor else "(kein Händler)"
            print(f"    {mr.receipt.pdf_path.name}")
            print(f"             → {vendor}{gross}")
            print(f"             ⚠ {reason}")
        print()

    # EÜR summary
    eur_lines = build_eur_summary(match_results, unmatched_expenses)
    if eur_lines:
        print(f"  ── EÜR-ZUSAMMENFASSUNG (Anlage EÜR Betriebsausgaben) " + "─" * 15)
        print()
        hdr = f"  {'Kategorie (EÜR)':<38} {'Zeile':>5}  {'Netto 7%':>9}  {'MwSt 7%':>8}  {'Netto 19%':>10}  {'MwSt 19%':>9}  {'Brutto':>9}"
        print(hdr)
        print("  " + "─" * 96)
        tot_gross = tot_n7 = tot_v7 = tot_n19 = tot_v19 = 0.0
        for ln in eur_lines:
            bar = f" ({ln.cash_count}× bar)" if ln.cash_count else ""
            print(
                f"  {(ln.label + bar)[:38]:<38} {ln.eur_line:>5}  "
                f"{ln.net_7:>9.2f}  {ln.vat_7:>8.2f}  "
                f"{ln.net_19:>10.2f}  {ln.vat_19:>9.2f}  "
                f"{ln.gross:>9.2f}"
            )
            tot_gross += ln.gross
            tot_n7 += ln.net_7; tot_v7 += ln.vat_7
            tot_n19 += ln.net_19; tot_v19 += ln.vat_19
        print("  " + "─" * 96)
        print(
            f"  {'GESAMT':<38} {'':>5}  "
            f"{tot_n7:>9.2f}  {tot_v7:>8.2f}  "
            f"{tot_n19:>10.2f}  {tot_v19:>9.2f}  "
            f"{tot_gross:>9.2f}"
        )
        print()
        print("  Hinweis: Bewirtungskosten nur zu 70 % abzugsfähig (§ 4 Abs. 5 Nr. 2 EStG).")
        print("           'bar' = Barzahlung, im Kontoauszug nicht erfasst, aber absetzbar.")
    print()
    print("=" * 70)
    print()


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------

def write_html_report(
    match_results: list[MatchResult],
    unmatched_expenses: list[dict],
    month: str,
    output_path: str,
) -> None:
    matched  = [mr for mr in match_results if mr.matched_tx is not None]
    orphans  = [mr for mr in match_results if mr.matched_tx is None and not mr.receipt.extraction_error]
    unclear  = [
        mr for mr in match_results
        if mr.receipt.extraction_error or mr.receipt.confidence == "low"
    ]
    eur_lines = build_eur_summary(match_results, unmatched_expenses)

    total_matched = sum(abs(mr.receipt.total_gross) for mr in matched)
    total_unmatched_exp = sum(abs(e.get("amount_eur", 0)) for e in unmatched_expenses)
    total_orphan = sum(abs(mr.receipt.total_gross) for mr in orphans)

    def vat_detail_str(vat_lines: list[VatLine]) -> str:
        return "; ".join(
            f"{vl.rate:.0f}%: {vl.net_amount:.2f}+{vl.vat_amount:.2f}"
            for vl in vat_lines
        ) or "—"

    # Section 1 rows
    list1_rows = ""
    for mr in sorted(matched, key=lambda x: x.receipt.date or ""):
        tx = mr.matched_tx
        warn = '<span class="warn">⚠</span>' if mr.match_quality == "amount_only" else "✓"
        conf_html = "" if mr.receipt.confidence == "high" else f'<span class="conf-{mr.receipt.confidence}">[{mr.receipt.confidence}]</span>'
        pm = f'<span class="pm pm-{mr.receipt.payment_method}">{mr.receipt.payment_method}</span>'
        list1_rows += f"""
        <tr class="matched">
            <td>{mr.receipt.date or "?"}</td>
            <td>{mr.receipt.vendor[:40]}</td>
            <td class="num">{abs(mr.receipt.total_gross):.2f}</td>
            <td>{pm}</td>
            <td class="small">{vat_detail_str(mr.receipt.vat_lines)}</td>
            <td>{tx.get("date","")}</td>
            <td>{str(tx.get("vendor",""))[:35]}</td>
            <td><span class="cat">{tx.get("source","")}/{tx.get("category","")}</span></td>
            <td class="small">{warn} {conf_html}</td>
        </tr>"""

    # Section 2 rows
    list2_rows = ""
    for exp in sorted(unmatched_expenses, key=lambda x: x.get("date", "")):
        list2_rows += f"""
        <tr class="missing">
            <td>{exp.get("date","")}</td>
            <td>{str(exp.get("vendor",""))[:40]}</td>
            <td class="num">{abs(exp.get("amount_eur",0)):.2f}</td>
            <td><span class="cat">{exp.get("source","")}/{exp.get("category","")}</span></td>
            <td colspan="5" class="small">{exp.get("buchungstext","")[:80]}</td>
        </tr>"""

    # Section 3 rows
    list3_rows = ""
    for mr in sorted(orphans, key=lambda x: x.receipt.date or ""):
        pm = f'<span class="pm pm-{mr.receipt.payment_method}">{mr.receipt.payment_method}</span>'
        list3_rows += f"""
        <tr class="orphan">
            <td>{mr.receipt.date or "?"}</td>
            <td>{mr.receipt.vendor[:40]}</td>
            <td class="num">{abs(mr.receipt.total_gross):.2f}</td>
            <td>{pm}</td>
            <td class="small">{vat_detail_str(mr.receipt.vat_lines)}</td>
            <td colspan="4" class="small">{mr.notes} | <code>{mr.receipt.pdf_path.name}</code></td>
        </tr>"""

    # EÜR rows
    eur_rows = ""
    tot_gross = tot_n7 = tot_v7 = tot_n19 = tot_v19 = 0.0
    for ln in eur_lines:
        bar_note = f' <small>({ln.cash_count}× bar)</small>' if ln.cash_count else ""
        eur_rows += f"""
        <tr>
            <td>{ln.eur_line}</td>
            <td>{ln.label}{bar_note}</td>
            <td>{ln.count}</td>
            <td class="num">{ln.net_7:.2f}</td>
            <td class="num">{ln.vat_7:.2f}</td>
            <td class="num">{ln.net_19:.2f}</td>
            <td class="num">{ln.vat_19:.2f}</td>
            <td class="num"><strong>{ln.gross:.2f}</strong></td>
        </tr>"""
        tot_gross += ln.gross
        tot_n7 += ln.net_7; tot_v7 += ln.vat_7
        tot_n19 += ln.net_19; tot_v19 += ln.vat_19
    eur_rows += f"""
        <tr class="total-row">
            <td colspan="2"><strong>GESAMT</strong></td>
            <td></td>
            <td class="num"><strong>{tot_n7:.2f}</strong></td>
            <td class="num"><strong>{tot_v7:.2f}</strong></td>
            <td class="num"><strong>{tot_n19:.2f}</strong></td>
            <td class="num"><strong>{tot_v19:.2f}</strong></td>
            <td class="num"><strong>{tot_gross:.2f}</strong></td>
        </tr>"""

    list4_rows = ""
    for mr in sorted(unclear, key=lambda x: x.receipt.pdf_path.name):
        if mr.receipt.extraction_error:
            reason = f'<span class="conf-low">Fehler: {mr.receipt.extraction_error}</span>'
        else:
            reason = f'<span class="conf-low">confidence={mr.receipt.confidence} — Daten evtl. unvollständig</span>'
        gross = f"{abs(mr.receipt.total_gross):.2f}" if mr.receipt.total_gross else "?"
        vendor = mr.receipt.vendor[:40] if mr.receipt.vendor else "<em>(kein Händler erkannt)</em>"
        in_list = "Liste 1" if mr.matched_tx else ("Liste 3" if not mr.receipt.extraction_error else "—")
        list4_rows += f"""
        <tr class="unclear">
            <td><code>{mr.receipt.pdf_path.name}</code></td>
            <td>{vendor}</td>
            <td class="num">{gross}</td>
            <td>{in_list}</td>
            <td>{reason}</td>
        </tr>"""

    def _empty(msg: str) -> str:
        return f"<p class='empty'>{msg}</p>"

    def _table(headers: list[str], rows: str) -> str:
        ths = "".join(f"<th>{h}</th>" for h in headers)
        return f"<table><tr>{ths}</tr>{rows}</table>"

    list4_section = ""
    if unclear:
        list4_section = f"""
<section>
  <h2>Liste 4 — Nicht lesbar / Prüfung erforderlich ({len(unclear)})</h2>
  {_table(["Datei","Händler","Brutto","Auch in","Grund"], list4_rows)}
  <p class="eur-note">Diese Belege wurden trotzdem verarbeitet, soweit möglich.<br>
  Bitte Scan-Qualität prüfen oder Daten manuell erfassen.</p>
</section>"""

    html = f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<title>Le La Hanoi – Belegabgleich {month}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'IBM Plex Mono', monospace; background: #0d0d0d; color: #d0d0d0; padding: 32px; font-size: 12px; }}
  h1 {{ font-size: 18px; color: #f0f0f0; margin-bottom: 4px; }}
  .subtitle {{ color: #555; font-size: 12px; margin-bottom: 32px; }}
  .stats {{ display: flex; gap: 20px; margin-bottom: 40px; flex-wrap: wrap; }}
  .stat {{ background: #141414; border: 1px solid #222; padding: 14px 20px; min-width: 150px; }}
  .stat-label {{ font-size: 10px; color: #555; letter-spacing: 2px; text-transform: uppercase; margin-bottom: 4px; }}
  .stat-value {{ font-size: 22px; font-weight: 600; }}
  .stat-sub {{ font-size: 10px; color: #444; margin-top: 2px; }}
  section {{ margin-bottom: 40px; }}
  h2 {{ font-size: 12px; color: #888; letter-spacing: 2px; text-transform: uppercase; margin-bottom: 10px;
         border-bottom: 1px solid #1e1e1e; padding-bottom: 6px; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th {{ text-align: left; padding: 6px 8px; color: #444; font-size: 10px; letter-spacing: 1px;
        text-transform: uppercase; border-bottom: 1px solid #1e1e1e; }}
  td {{ padding: 5px 8px; border-bottom: 1px solid #181818; vertical-align: top; }}
  tr.matched td {{ color: #86efac; }}
  tr.missing td {{ color: #fca5a5; }}
  tr.orphan  td {{ color: #fde68a; }}
  tr.unclear td {{ color: #f87171; }}
  tr.total-row td {{ border-top: 1px solid #333; color: #f0f0f0; }}
  .num {{ text-align: right; white-space: nowrap; }}
  .small {{ font-size: 10px; color: inherit; opacity: 0.75; }}
  .cat {{ background: #1e1e1e; padding: 1px 5px; font-size: 10px; color: #888; border-radius: 2px; white-space: nowrap; }}
  .warn {{ color: #f59e0b; }}
  .conf-medium {{ color: #f59e0b; font-size: 10px; }}
  .conf-low {{ color: #ef4444; font-size: 10px; }}
  .pm {{ padding: 1px 5px; border-radius: 2px; font-size: 10px; }}
  .pm-bar {{ background: #3f2f00; color: #fde68a; }}
  .pm-karte {{ background: #003f1a; color: #86efac; }}
  .pm-rechnung {{ background: #001a3f; color: #93c5fd; }}
  .pm-unbekannt {{ background: #1e1e1e; color: #666; }}
  code {{ font-size: 10px; color: #666; }}
  small {{ font-size: 10px; opacity: 0.6; }}
  .empty {{ color: #333; font-style: italic; padding: 12px 0; }}
  .eur-note {{ color: #444; font-size: 11px; margin-top: 12px; line-height: 1.6; }}
</style>
</head>
<body>
<h1>Le La Hanoi — Belegabgleich</h1>
<div class="subtitle">Monat: {month} · Erstellt: {datetime.now().strftime('%Y-%m-%d %H:%M')}</div>

<div class="stats">
  <div class="stat">
    <div class="stat-label">Liste 1 — abgeglichen</div>
    <div class="stat-value" style="color:#86efac">{len(matched)}</div>
    <div class="stat-sub">{total_matched:.2f} EUR</div>
  </div>
  <div class="stat">
    <div class="stat-label">Liste 2 — Konto ohne Beleg</div>
    <div class="stat-value" style="color:#fca5a5">{len(unmatched_expenses)}</div>
    <div class="stat-sub">{total_unmatched_exp:.2f} EUR</div>
  </div>
  <div class="stat">
    <div class="stat-label">Liste 3 — Beleg ohne Konto</div>
    <div class="stat-value" style="color:#fde68a">{len(orphans)}</div>
    <div class="stat-sub">{total_orphan:.2f} EUR bar / anderer Monat</div>
  </div>
  <div class="stat">
    <div class="stat-label">Liste 4 — nicht lesbar</div>
    <div class="stat-value" style="color:#f87171">{len(unclear)}</div>
    <div class="stat-sub">Prüfung erforderlich</div>
  </div>
  <div class="stat">
    <div class="stat-label">EÜR Brutto gesamt</div>
    <div class="stat-value" style="color:#f0f0f0">{tot_gross:.2f}</div>
    <div class="stat-sub">EUR (alle Belege + unbelegte Ausgaben)</div>
  </div>
</div>

<section>
  <h2>Liste 1 — Belege mit Kontoauszug-Eintrag ({len(matched)})</h2>
  {_empty("Keine abgeglichenen Belege.") if not matched else
   _table(["Beleg-Datum","Händler (Beleg)","Brutto","Zahlung","MwSt (Netto+MwSt)",
            "Konto-Datum","Händler (Konto)","Kategorie","Status"], list1_rows)}
</section>

<section>
  <h2>Liste 2 — Ausgaben im Konto ohne Beleg ({len(unmatched_expenses)})</h2>
  {_empty("Alle Kontoausgaben haben einen Beleg. ✓") if not unmatched_expenses else
   _table(["Datum","Händler","Betrag","Kategorie","Buchungstext"], list2_rows)}
</section>

<section>
  <h2>Liste 3 — Belege ohne Kontoauszug — bar / anderer Monat ({len(orphans)})</h2>
  {_empty("Alle Belege haben eine Kontoauszugs-Entsprechung. ✓") if not orphans else
   _table(["Beleg-Datum","Händler","Brutto","Zahlung","MwSt","Hinweis + Datei"], list3_rows)}
</section>

<section>
  <h2>EÜR-Zusammenfassung — Anlage EÜR Betriebsausgaben</h2>
  {_table(["Zeile","Kategorie","Anz.","Netto 7%","VorSt 7%","Netto 19%","VorSt 19%","Brutto gesamt"], eur_rows)}
  <p class="eur-note">
    Vorsteuer = abzugsfähige Mehrwertsteuer aus Eingangsrechnungen.<br>
    Bewirtungskosten (exp_bewirtung) nur zu 70 % abzugsfähig (§ 4 Abs. 5 Nr. 2 EStG).<br>
    <span style="color:#fde68a">■</span> bar = Barzahlung, nicht im Kontoauszug, aber als Betriebsausgabe absetzbar (Beleg vorhanden).<br>
    Positionen ohne Vorsteuer-Aufteilung (fehlender Beleg): nur Brutto-Betrag erfasst.
  </p>
</section>
{list4_section}
</body>
</html>"""

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"HTML-Bericht gespeichert: {output_path}")


# ---------------------------------------------------------------------------
# EÜR CSV
# ---------------------------------------------------------------------------

def write_eur_csv(
    match_results: list[MatchResult],
    unmatched_expenses: list[dict],
    output_path: str,
) -> None:
    """Write EÜR summary as a semicolon-delimited CSV."""
    eur_lines = build_eur_summary(match_results, unmatched_expenses)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    def _fmt(v: float) -> str:
        return f"{v:.2f}".replace(".", ",")

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow([
            "eur_zeile", "kategorie", "anzahl",
            "netto_7pct", "vorsteuer_7pct",
            "netto_19pct", "vorsteuer_19pct",
            "brutto_gesamt", "davon_bar",
        ])
        for ln in eur_lines:
            writer.writerow([
                ln.eur_line, ln.label, ln.count,
                _fmt(ln.net_7), _fmt(ln.vat_7),
                _fmt(ln.net_19), _fmt(ln.vat_19),
                _fmt(ln.gross), ln.cash_count,
            ])
    print(f"EÜR-CSV gespeichert: {output_path}")


# ---------------------------------------------------------------------------
# Apply file moves
# ---------------------------------------------------------------------------

def apply_moves(match_results: list[MatchResult], month: str) -> None:
    """Move PDFs from inbox to accounted_for / not_accounted_for."""
    acct     = Path(f"data/{month}/invoices/accounted_for")
    not_acct = Path(f"data/{month}/invoices/not_accounted_for")
    acct.mkdir(parents=True, exist_ok=True)
    not_acct.mkdir(parents=True, exist_ok=True)

    moved = 0
    for mr in match_results:
        src = mr.receipt.pdf_path
        if not src.exists():
            print(f"  Übersprungen (nicht gefunden): {src.name}")
            continue

        dest_dir = acct if mr.proposed_folder == "accounted_for" else not_acct
        dest = dest_dir / src.name

        # Avoid name collisions
        if dest.exists():
            counter = 1
            while dest.exists():
                dest = dest_dir / f"{src.stem}_{counter}{src.suffix}"
                counter += 1

        shutil.move(str(src), str(dest))
        moved += 1
        label = "✓" if mr.proposed_folder == "accounted_for" else "○"
        print(f"  {label} {src.name}  →  {mr.proposed_folder}/")

    print(f"\n  {moved} Datei(en) verschoben.")


# ---------------------------------------------------------------------------
# Receipt-match sidecar  (read by editor.py)
# ---------------------------------------------------------------------------

def write_receipt_matches(
    match_results: list,
    month: str,
) -> None:
    """
    Write a small JSON sidecar so editor.py can show receipt status.

    Format: list of objects, one per matched receipt:
      { "tx_date", "tx_amount", "tx_vendor", "receipt_file", "match_quality" }

    Keyed on (tx_date, tx_amount) – same tolerance the matcher uses.
    """
    out_dir = Path(f"data/{month}/output")
    out_dir.mkdir(parents=True, exist_ok=True)
    records = []
    for mr in match_results:
        if mr.matched_tx is None:
            continue
        records.append({
            "tx_date":       mr.matched_tx.get("date", ""),
            "tx_amount":     round(float(mr.matched_tx.get("amount_eur", 0)), 2),
            "tx_vendor":     mr.matched_tx.get("vendor", ""),
            "receipt_file":  mr.receipt.pdf_path.name,
            "match_quality": mr.match_quality,
        })
    path = out_dir / f"receipt_matches_{month}.json"
    path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Le La Hanoi — Receipt Processor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--month", required=True,
        help="Zielmonat im Format YYYY-MM, z.B. 2026-02",
    )
    parser.add_argument(
        "--inbox", default="data/inbox",
        help="Ordner mit den zu verarbeitenden PDFs (Standard: data/inbox/)",
    )
    parser.add_argument(
        "--report", action="store_true",
        help="HTML-Bericht schreiben nach data/{month}/output/receipt_analysis_{month}.html",
    )
    parser.add_argument(
        "--eur-csv", action="store_true",
        help="EÜR-Zusammenfassung als CSV schreiben nach data/{month}/output/eur_summary_{month}.csv",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Dateien tatsächlich verschieben (nach Prüfung des Berichts ausführen)",
    )
    parser.add_argument(
        "--model", default="claude-sonnet-4-6",
        help="Claude-Modell für Belegextraktion (Standard: claude-sonnet-4-6)",
    )
    parser.add_argument(
        "--api-key", default=None,
        help="Anthropic API Key (oder ANTHROPIC_API_KEY env var)",
    )

    args = parser.parse_args()

    if not re.match(r'^\d{4}-\d{2}$', args.month):
        parser.error("--month muss im Format YYYY-MM sein, z.B. 2026-02")

    # Collect PDFs from inbox
    inbox = Path(args.inbox)
    if not inbox.exists():
        print(f"Fehler: Inbox-Ordner nicht gefunden: {inbox}", file=sys.stderr)
        sys.exit(1)

    pdf_files = sorted(p for p in inbox.iterdir() if p.suffix.lower() == ".pdf" and p.is_file())
    if not pdf_files:
        print(f"Keine PDF-Dateien in {inbox} gefunden.")
        sys.exit(0)

    print(f"PDFs im Inbox: {len(pdf_files)}")

    # Load classified expenses
    try:
        expenses = load_expenses(args.month)
    except FileNotFoundError as e:
        print(f"Fehler: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Betriebsausgaben aus Kontoauszug ({args.month}): {len(expenses)}")

    # Extract receipt data via Claude
    api_key = args.api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Fehler: ANTHROPIC_API_KEY nicht gesetzt.", file=sys.stderr)
        sys.exit(1)

    client = Anthropic(api_key=api_key)
    receipts: list[ReceiptData] = []

    print()
    for idx, pdf_path in enumerate(pdf_files, 1):
        print(f"  [{idx:>2}/{len(pdf_files)}] {pdf_path.name[:50]:<50} ...", end=" ", flush=True)
        receipt = extract_receipt(pdf_path, client, args.model)
        receipts.append(receipt)
        if receipt.extraction_error:
            print(f"FEHLER — {receipt.extraction_error}")
        else:
            print(f"{receipt.vendor[:28]:<28}  {abs(receipt.total_gross):>8.2f} EUR  [{receipt.confidence}]")

    # Match
    print("\nAbgleich mit Kontoauszug-Ausgaben ...")
    match_results, unmatched_expenses = run_matching(receipts, expenses, args.month)

    # Write sidecar for editor.py
    write_receipt_matches(match_results, args.month)

    # Reports
    print_report(match_results, unmatched_expenses, args.month)

    if args.report:
        html_path = f"data/{args.month}/output/receipt_analysis_{args.month}.html"
        write_html_report(match_results, unmatched_expenses, args.month, html_path)

    if args.eur_csv:
        csv_path = f"data/{args.month}/output/eur_summary_{args.month}.csv"
        write_eur_csv(match_results, unmatched_expenses, csv_path)

    if args.apply:
        print("Verschiebe Dateien ...")
        apply_moves(match_results, args.month)
    else:
        n_acct = sum(1 for mr in match_results if mr.proposed_folder == "accounted_for")
        n_not  = sum(1 for mr in match_results if mr.proposed_folder == "not_accounted_for")
        print(f"Tipp: Mit --apply werden {n_acct} PDFs nach accounted_for/ und {n_not} nach not_accounted_for/ verschoben.")
        print("      Mit --report wird ein HTML-Bericht erstellt.")
        print("      Mit --eur-csv wird eine EÜR-CSV geschrieben.")


if __name__ == "__main__":
    main()

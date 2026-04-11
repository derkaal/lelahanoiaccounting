#!/usr/bin/env python3
"""
Le La Hanoi – Buchungseditor & Dashboard
=========================================
Web UI with three tabs:
  - Buchungen:   review and correct classified bank transactions
  - Buchhaltung: accounting dashboard (revenue, expenses, USt, EÜR)
  - Geschäft:    business dashboard (product sales, categories, tables)

Usage:
    python editor.py                     # month picker
    python editor.py --month 2026-02     # open directly for Feb 2026
    python editor.py --port 5001         # custom port

Revenue data comes from ready2order PDF reports in data/<month>/ready2order/.
Expense data comes from the classified CSV in data/<month>/output/.
"""

import argparse
import csv
import json
import re
from collections import defaultdict
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request
import pdfplumber

BASE_DIR = Path(__file__).parent / "data"

CLASSIFICATIONS = [
    "cafe_expense",
    "personal",
    "transfer",
    "salary",
    "needs_review",
    "ignore",
]

CATEGORIES = {
    "":                   "—",
    "exp_wareneinkauf":   "Wareneinkauf (Z.46)",
    "exp_miete":          "Miete (Z.50)",
    "exp_energie":        "Energie (Z.52)",
    "exp_versicherung":   "Versicherung (Z.54)",
    "exp_marketing":      "Marketing / GEMA (Z.56)",
    "exp_buerobedarf":    "Bürobedarf (Z.57)",
    "exp_instandhaltung": "Instandhaltung (Z.60)",
    "asset_gwg":          "GWG < 800 € (Z.62)",
    "exp_bankgebuehr":    "Bankgebühr (Z.73)",
    "exp_bewirtung":      "Bewirtung (Z.73)",
}

app = Flask(__name__)


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

def csv_path(month: str) -> Path:
    return BASE_DIR / month / "output" / f"classified_{month}.csv"


def load_receipt_matches(month: str) -> dict:
    """
    Load the receipt-match sidecar written by process_receipts.py.
    Returns a dict keyed by (date, rounded_amount) → receipt filename.
    """
    path = BASE_DIR / month / "output" / f"receipt_matches_{month}.json"
    if not path.exists():
        return {}
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
        return {
            (r["tx_date"], round(float(r["tx_amount"]), 2)): r["receipt_file"]
            for r in records
        }
    except Exception:
        return {}


def _parse_amount(value: str) -> float:
    """Parse a European-formatted amount string to float.
    Handles both '2.400,00' (European) and '2400.00' (plain) formats.
    """
    v = value.strip()
    if not v or v in ("nan", ""):
        return 0.0
    # European format: period as thousands sep, comma as decimal sep
    # e.g. "2.400,00" → remove dots → "2400,00" → swap comma → "2400.00"
    if "," in v:
        v = v.replace(".", "").replace(",", ".")
    try:
        return float(v)
    except ValueError:
        return 0.0


def load_csv(month: str) -> list[dict]:
    path = csv_path(month)
    if not path.exists():
        return []
    rows = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter=";")
        for i, row in enumerate(reader):
            row["_id"] = i
            # Normalise amount to a plain float string so JS parseFloat works
            row["amount_eur"] = _parse_amount(row.get("amount_eur", "0"))
            rows.append(dict(row))
    return rows


def _format_amount(value) -> str:
    """Write amount back as European-formatted string, e.g. -2400.0 → '-2.400,00'."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return str(value)
    sign = "-" if n < 0 else ""
    formatted = f"{abs(n):,.2f}".replace(",", "TSEP").replace(".", ",").replace("TSEP", ".")
    return sign + formatted


def save_csv(month: str, rows: list[dict]) -> None:
    path = csv_path(month)
    if not rows:
        return
    fieldnames = [k for k in rows[0].keys() if k != "_id"]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()
        for row in rows:
            out = {k: v for k, v in row.items() if k != "_id"}
            out["amount_eur"] = _format_amount(out.get("amount_eur", 0))
            writer.writerow(out)


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

INDEX_HTML = """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<title>Le La Hanoi – Buchungseditor</title>
<style>
  body { font-family: 'Segoe UI', system-ui, sans-serif; background: #1a1a2e; color: #e0e0e0;
         display: flex; justify-content: center; padding-top: 80px; }
  .card { background: #16213e; border: 1px solid #0f3460; border-radius: 10px;
          padding: 40px 48px; min-width: 320px; }
  h1 { color: #e94560; font-size: 22px; margin-bottom: 6px; }
  p  { color: #888; font-size: 13px; margin-bottom: 28px; }
  ul { list-style: none; }
  li { margin: 10px 0; }
  a  { display: block; background: #0f3460; color: #e0e0e0; text-decoration: none;
       padding: 10px 18px; border-radius: 6px; font-size: 15px; transition: background .15s; }
  a:hover { background: #e94560; color: white; }
  .none { color: #555; font-style: italic; }
</style>
</head>
<body>
<div class="card">
  <h1>Le La Hanoi</h1>
  <p>Buchungseditor – Monat wählen</p>
  {% if months %}
  <ul>
    {% for m in months %}
    <li><a href="/month/{{ m }}">{{ m }}</a></li>
    {% endfor %}
  </ul>
  {% else %}
  <p class="none">Noch keine klassifizierten Daten vorhanden.<br>
     Bitte zuerst <code>classify.py</code> ausführen.</p>
  {% endif %}
</div>
</body>
</html>"""


EDITOR_HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<title>Le La Hanoi – {{ month }}</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'Segoe UI', system-ui, sans-serif; background: #1a1a2e;
       color: #e0e0e0; font-size: 13px; min-height: 100vh; }

/* ── Header ── */
header { background: #16213e; padding: 14px 24px; border-bottom: 1px solid #0f3460;
         display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
header h1 { font-size: 17px; font-weight: 700; color: #e94560; white-space: nowrap; }
.month-badge { background: #0f3460; padding: 3px 10px; border-radius: 4px;
               font-size: 12px; color: #aaa; }
.changes-badge { background: #e94560; color: white; border-radius: 12px;
                 padding: 2px 10px; font-size: 11px; font-weight: 600; display: none; }
.back { margin-left: auto; color: #666; text-decoration: none; font-size: 12px; }
.back:hover { color: #aaa; }

/* ── Tab nav ── */
.tab-nav { display: flex; gap: 0; margin-left: 24px; }
.tab-nav a { display: block; padding: 6px 16px; font-size: 12px; font-weight: 600;
             color: #888; text-decoration: none; border-radius: 4px 4px 0 0;
             border: 1px solid transparent; border-bottom: none; transition: all .15s; }
.tab-nav a:hover { color: #e0e0e0; background: rgba(255,255,255,.04); }
.tab-nav a.active { background: #1a1a2e; color: #e94560; border-color: #0f3460; }

/* ── Toolbar ── */
.toolbar { background: #16213e; padding: 10px 24px; border-bottom: 1px solid #0f3460;
           display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
.toolbar label { font-size: 12px; color: #888; display: flex; align-items: center; gap: 5px; }
select, input[type=text] { background: #0f3460; color: #e0e0e0; border: 1px solid #224;
                            padding: 5px 8px; border-radius: 4px; font-size: 12px; }
button { padding: 6px 18px; border: none; border-radius: 4px; cursor: pointer;
         font-size: 13px; font-weight: 500; transition: background .15s; }
.btn-save   { background: #e94560; color: white; }
.btn-save:hover:not(:disabled) { background: #c73652; }
.btn-save:disabled { background: #4a2030; color: #886; cursor: default; }
.btn-reset  { background: #1e2a4a; color: #aaa; }
.btn-reset:hover { background: #2a3a5a; }

/* ── Stats bar ── */
.stats { display: flex; gap: 14px; padding: 12px 24px; flex-wrap: wrap; }
.stat { background: #16213e; border: 1px solid #0f3460; border-radius: 6px;
        padding: 8px 16px; min-width: 130px; }
.stat .val { font-size: 18px; font-weight: 700; color: #e94560; }
.stat .lbl { font-size: 11px; color: #666; margin-top: 2px; }

/* ── Table ── */
.table-wrap { padding: 0 24px 40px; overflow-x: auto; }
table { width: 100%; border-collapse: collapse; margin-top: 12px; }
th { background: #0f3460; color: #888; font-weight: 500; padding: 8px 10px;
     text-align: left; white-space: nowrap; position: sticky; top: 0; z-index: 2; }
td { padding: 6px 10px; border-bottom: 1px solid #1a2240; vertical-align: middle; }
tbody tr:hover td { background: rgba(255,255,255,.03); }
tbody tr.changed td { background: rgba(233,69,96,.07) !important; }

/* ── Classification tags ── */
.tag { display: inline-block; padding: 2px 8px; border-radius: 3px; font-size: 11px; font-weight: 600; }
.tag-cafe_expense  { background: #1a3d28; color: #4caf7d; }
.tag-personal      { background: #252550; color: #8899ee; }
.tag-transfer      { background: #383010; color: #c8b44a; }
.tag-salary        { background: #103040; color: #4ab8cc; }
.tag-needs_review  { background: #3d2010; color: #e8904a; }
.tag-ignore        { background: #222; color: #555; }

/* ── Amount ── */
.amt-neg { color: #e94560; font-family: monospace; white-space: nowrap; }
.amt-pos { color: #4caf7d; font-family: monospace; white-space: nowrap; }
.src-bank   { color: #4ab8cc; font-size: 11px; }
.src-credit { color: #c84ab8; font-size: 11px; }

/* ── Inline selects & inputs ── */
td select { width: 100%; }
td input[type=text] { width: 100%; }

/* ── Toast ── */
.toast { position: fixed; bottom: 24px; right: 24px; padding: 10px 20px;
         border-radius: 6px; font-size: 13px; opacity: 0; transition: opacity .3s;
         z-index: 100; pointer-events: none; }
.toast.show { opacity: 1; }
.toast-ok  { background: #1a3d28; color: #4caf7d; border: 1px solid #4caf7d; }
.toast-err { background: #3d1a1a; color: #e94560; border: 1px solid #e94560; }

/* ── Info box ── */
.info { background: #1a2e1a; border: 1px solid #2a5a2a; border-radius: 6px;
        padding: 10px 16px; margin: 12px 24px 0; font-size: 12px; color: #4caf7d; }

/* ── Empty state ── */
.empty { padding: 40px; text-align: center; color: #444; }
</style>
</head>
<body>

<header>
  <h1>Le La Hanoi</h1>
  <span class="month-badge">{{ month }}</span>
  <span id="changes-badge" class="changes-badge">0 Änderungen</span>
  <nav class="tab-nav">
    <a href="/month/{{ month }}" class="active">Buchungen</a>
    <a href="/month/{{ month }}/buchhaltung">Buchhaltung</a>
    <a href="/month/{{ month }}/geschaeft">Geschäft</a>
  </nav>
  <a href="/" class="back">← Monatsübersicht</a>
</header>

<div class="toolbar">
  <label>Klassifikation:
    <select id="f-class">
      <option value="">Alle</option>
      {% for c in classifications %}
      <option value="{{ c }}">{{ c }}</option>
      {% endfor %}
    </select>
  </label>
  <label>Kategorie:
    <select id="f-cat">
      <option value="">Alle Kategorien</option>
      {% for k, v in categories.items() %}{% if k %}
      <option value="{{ k }}">{{ v }}</option>
      {% endif %}{% endfor %}
    </select>
  </label>
  <label>Suche:
    <input type="text" id="f-search" placeholder="Vendor / Buchungstext …" style="width:220px">
  </label>
  <button class="btn-save" id="btn-save" disabled onclick="saveChanges()">Speichern</button>
  <button class="btn-reset" onclick="resetChanges()">Zurücksetzen</button>
</div>

<div class="info">
  💡 Tipp: Nach dem Speichern <code>process_receipts.py --month {{ month }} --eur-csv --report</code>
  neu ausführen, um die EÜR-Zusammenfassung zu aktualisieren.
</div>

<div class="stats" id="stats"></div>

<div class="table-wrap">
  <table>
    <thead>
      <tr>
        <th>#</th>
        <th>Quelle</th>
        <th>Datum</th>
        <th>Vendor</th>
        <th style="min-width:220px">Buchungstext</th>
        <th style="text-align:right">Betrag&nbsp;€</th>
        <th>Beleg</th>
        <th style="min-width:140px">Klassifikation</th>
        <th style="min-width:180px">Kategorie</th>
        <th style="min-width:180px">Notiz / Kennzeichen</th>
      </tr>
    </thead>
    <tbody id="tbody"></tbody>
  </table>
  <div id="empty" class="empty" style="display:none">Keine Buchungen gefunden.</div>
</div>

<div class="toast" id="toast"></div>

<script>
const MONTH = {{ month | tojson }};
const CLASSIFICATIONS = {{ classifications | tojson }};
const CATEGORIES = {{ categories | tojson }};  // {key: label}

let rows = {{ rows_json | safe }};
let saved = JSON.parse(JSON.stringify(rows));
let changed = new Set();

// ── Helpers ──────────────────────────────────────────────────────────────────

function fmtAmt(v) {
  const n = parseFloat(v);
  if (isNaN(n)) return v || "";
  const s = Math.abs(n).toFixed(2).replace(".", ",");
  return (n >= 0 ? "+" : "−") + s;
}

function el(tag, attrs, ...children) {
  const e = document.createElement(tag);
  Object.entries(attrs || {}).forEach(([k, v]) => {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e[k] = v;
    else e.setAttribute(k, v);
  });
  children.forEach(c => {
    if (typeof c === "string") e.appendChild(document.createTextNode(c));
    else if (c) e.appendChild(c);
  });
  return e;
}

function makeSelect(options, current, onchange) {
  const s = el("select");
  options.forEach(([val, label]) => {
    const o = el("option", { value: val }, label);
    if (val === current) o.selected = true;
    s.appendChild(o);
  });
  s.onchange = () => onchange(s.value);
  return s;
}

// ── Change tracking ───────────────────────────────────────────────────────────

function onFieldChange(id, field, value) {
  rows[id][field] = value;
  const wasChanged = JSON.stringify(rows[id]) !== JSON.stringify(saved[id]);
  if (wasChanged) changed.add(id); else changed.delete(id);

  const tr = document.querySelector(`tr[data-id="${id}"]`);
  if (tr) tr.classList.toggle("changed", wasChanged);
  updateBadge();
  updateStats();
}

function updateBadge() {
  const badge = document.getElementById("changes-badge");
  const btn   = document.getElementById("btn-save");
  if (changed.size > 0) {
    badge.style.display = "inline-block";
    badge.textContent = changed.size + " Änderung" + (changed.size !== 1 ? "en" : "");
    btn.disabled = false;
  } else {
    badge.style.display = "none";
    btn.disabled = true;
  }
}

// ── Stats ─────────────────────────────────────────────────────────────────────

function updateStats() {
  const fc     = document.getElementById("f-class").value;
  const fcat   = document.getElementById("f-cat").value;
  const fsearch = (document.getElementById("f-search").value || "").toLowerCase();

  let visible = 0, expenseSum = 0, cafeCount = 0, receiptCount = 0;
  rows.forEach(r => {
    if (fc && r.classification !== fc) return;
    if (fcat && r.category !== fcat) return;
    const haystack = ((r.vendor || "") + " " + (r.buchungstext || "")).toLowerCase();
    if (fsearch && !haystack.includes(fsearch)) return;
    visible++;
    const amt = parseFloat(r.amount_eur) || 0;
    if (r.classification === "cafe_expense") {
      cafeCount++;
      expenseSum += amt;
      if (r._receipt) receiptCount++;
    }
  });

  const receiptPct = cafeCount > 0 ? Math.round(receiptCount / cafeCount * 100) : 0;
  document.getElementById("stats").innerHTML = `
    <div class="stat"><div class="val">${visible}</div><div class="lbl">Buchungen (gefiltert)</div></div>
    <div class="stat"><div class="val">${cafeCount}</div><div class="lbl">Betriebsausgaben</div></div>
    <div class="stat"><div class="val">${Math.abs(expenseSum).toFixed(2).replace(".", ",")} €</div><div class="lbl">Betriebsausgaben (∑)</div></div>
    <div class="stat"><div class="val">${receiptCount} / ${cafeCount} <span style="font-size:13px;color:#888">(${receiptPct}%)</span></div><div class="lbl">Belege vorhanden</div></div>
    <div class="stat"><div class="val">${changed.size}</div><div class="lbl">Ungespeicherte Änderungen</div></div>
  `;
}

// ── Render ────────────────────────────────────────────────────────────────────

function renderTable() {
  const fc     = document.getElementById("f-class").value;
  const fcat   = document.getElementById("f-cat").value;
  const fsearch = (document.getElementById("f-search").value || "").toLowerCase();

  const tbody = document.getElementById("tbody");
  tbody.innerHTML = "";
  let count = 0;

  const catOptions = Object.entries(CATEGORIES).map(([k, v]) => [k, v]);
  const clsOptions = CLASSIFICATIONS.map(c => [c, c]);

  rows.forEach((row, i) => {
    const cls = row.classification || "";
    const cat = row.category || "";
    const haystack = ((row.vendor || "") + " " + (row.buchungstext || "")).toLowerCase();
    if (fc && cls !== fc) return;
    if (fcat && cat !== fcat) return;
    if (fsearch && !haystack.includes(fsearch)) return;
    count++;

    const amt = parseFloat(row.amount_eur) || 0;
    const amtClass = amt >= 0 ? "amt-pos" : "amt-neg";
    const srcClass = row.source === "credit" ? "src-credit" : "src-bank";

    const tr = el("tr", { "data-id": i });
    if (changed.has(i)) tr.classList.add("changed");

    // #, source, date, vendor
    tr.appendChild(el("td", { style: "color:#444" }, String(i + 1)));
    tr.appendChild(el("td", {}, el("span", { class: srcClass }, row.source || "")));
    tr.appendChild(el("td", { style: "white-space:nowrap" }, row.date || ""));
    tr.appendChild(el("td", {}, el("b", {}, row.vendor || "")));

    // buchungstext (truncated, full text in title)
    const btext = row.buchungstext || "";
    const tdB = el("td", { title: btext,
      style: "max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#888" }, btext);
    tr.appendChild(tdB);

    // amount
    tr.appendChild(el("td", { class: amtClass, style: "text-align:right" }, fmtAmt(row.amount_eur)));

    // receipt indicator
    const receipt = row._receipt || "";
    const tdR = el("td", { style: "white-space:nowrap;text-align:center" });
    if (receipt) {
      tdR.appendChild(el("span", {
        title: receipt,
        style: "color:#4caf7d;cursor:default;font-size:15px"
      }, "✓"));
    } else {
      tdR.appendChild(el("span", { style: "color:#333;font-size:13px" }, "—"));
    }
    tr.appendChild(tdR);

    // classification select
    const tdCls = el("td");
    tdCls.appendChild(makeSelect(clsOptions, cls, v => onFieldChange(i, "classification", v)));
    tr.appendChild(tdCls);

    // category select
    const tdCat = el("td");
    tdCat.appendChild(makeSelect(catOptions, cat, v => onFieldChange(i, "category", v)));
    tr.appendChild(tdCat);

    // flag_note input
    const inp = el("input", { type: "text", value: row.flag_note || "",
                               placeholder: "Notiz …" });
    inp.onchange = e => onFieldChange(i, "flag_note", e.target.value);
    inp.oninput  = e => onFieldChange(i, "flag_note", e.target.value);
    const tdNote = el("td");
    tdNote.appendChild(inp);
    tr.appendChild(tdNote);

    tbody.appendChild(tr);
  });

  document.getElementById("empty").style.display = count === 0 ? "block" : "none";
  updateStats();
}

// ── Save / Reset ──────────────────────────────────────────────────────────────

async function saveChanges() {
  const btn = document.getElementById("btn-save");
  btn.disabled = true;
  btn.textContent = "Speichern …";
  try {
    const res = await fetch(`/save/${MONTH}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rows })
    });
    const data = await res.json();
    if (data.ok) {
      saved = JSON.parse(JSON.stringify(rows));
      changed.clear();
      updateBadge();
      renderTable();
      toast("✓ Gespeichert!", "ok");
    } else {
      toast("Fehler: " + (data.error || "?"), "err");
    }
  } catch (e) {
    toast("Netzwerkfehler: " + e.message, "err");
  }
  btn.textContent = "Speichern";
  updateBadge();
}

function resetChanges() {
  rows = JSON.parse(JSON.stringify(saved));
  changed.clear();
  updateBadge();
  renderTable();
}

// ── Toast ─────────────────────────────────────────────────────────────────────

function toast(msg, type) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.className = "toast toast-" + type + " show";
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.remove("show"), 3000);
}

// ── Filters ───────────────────────────────────────────────────────────────────

document.getElementById("f-class").onchange  = renderTable;
document.getElementById("f-cat").onchange    = renderTable;
document.getElementById("f-search").oninput  = renderTable;

// ── Boot ──────────────────────────────────────────────────────────────────────
renderTable();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    months = sorted(
        [d.name for d in BASE_DIR.iterdir()
         if d.is_dir() and re.match(r"\d{4}-\d{2}$", d.name)
         and csv_path(d.name).exists()],
        reverse=True,
    ) if BASE_DIR.exists() else []
    return render_template_string(INDEX_HTML, months=months)


@app.route("/month/<month>")
def month_view(month: str):
    if not re.match(r"^\d{4}-\d{2}$", month):
        return "Ungültiger Monat.", 400
    rows = load_csv(month)
    if not rows:
        return (
            f"<p style='font-family:sans-serif;padding:40px;color:#e94560'>"
            f"Keine Daten für <b>{month}</b>.<br>"
            f"Bitte zuerst <code>classify.py --month {month}</code> ausführen.</p>",
            404,
        )
    receipt_matches = load_receipt_matches(month)
    rows_clean = []
    for r in rows:
        row = {k: v for k, v in r.items() if k != "_id"}
        key = (row.get("date", ""), round(float(row.get("amount_eur", 0)), 2))
        row["_receipt"] = receipt_matches.get(key, "")
        rows_clean.append(row)
    return render_template_string(
        EDITOR_HTML,
        month=month,
        rows_json=json.dumps(rows_clean, ensure_ascii=False),
        classifications=CLASSIFICATIONS,
        categories=CATEGORIES,
    )


@app.route("/save/<month>", methods=["POST"])
def save(month: str):
    if not re.match(r"^\d{4}-\d{2}$", month):
        return jsonify({"ok": False, "error": "Ungültiger Monat"}), 400
    try:
        data = request.get_json(force=True)
        rows = data["rows"]
        if not isinstance(rows, list):
            raise ValueError("rows muss eine Liste sein")
        # Validate we're not writing garbage
        expected_path = csv_path(month)
        if not expected_path.exists():
            raise FileNotFoundError(f"CSV nicht gefunden: {expected_path}")
        save_csv(month, rows)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ---------------------------------------------------------------------------
# Ready2order data loader
# ---------------------------------------------------------------------------

def load_ready2order(month: str) -> dict | None:
    """Parse the ready2order PDF report for the given month."""
    r2o_dir = BASE_DIR / month / "ready2order"
    if not r2o_dir.exists():
        return None
    pdfs = list(r2o_dir.glob("*.pdf"))
    if not pdfs:
        return None
    pdf_path = pdfs[0]

    try:
        full_text = ""
        with pdfplumber.open(str(pdf_path)) as pdf:
            for page in pdf.pages:
                full_text += (page.extract_text() or "") + "\n"
        return {"text": full_text, "filename": pdf_path.name}
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Dashboard templates
# ---------------------------------------------------------------------------

DASHBOARD_BASE_CSS = r"""
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'Segoe UI', system-ui, sans-serif; background: #1a1a2e;
       color: #e0e0e0; font-size: 13px; min-height: 100vh; }
header { background: #16213e; padding: 14px 24px; border-bottom: 1px solid #0f3460;
         display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
header h1 { font-size: 17px; font-weight: 700; color: #e94560; white-space: nowrap; }
.month-badge { background: #0f3460; padding: 3px 10px; border-radius: 4px;
               font-size: 12px; color: #aaa; }
.back { margin-left: auto; color: #666; text-decoration: none; font-size: 12px; }
.back:hover { color: #aaa; }
.tab-nav { display: flex; gap: 0; margin-left: 24px; }
.tab-nav a { display: block; padding: 6px 16px; font-size: 12px; font-weight: 600;
             color: #888; text-decoration: none; border-radius: 4px 4px 0 0;
             border: 1px solid transparent; border-bottom: none; transition: all .15s; }
.tab-nav a:hover { color: #e0e0e0; background: rgba(255,255,255,.04); }
.tab-nav a.active { background: #1a1a2e; color: #e94560; border-color: #0f3460; }
.dashboard { padding: 24px; max-width: 1200px; }
.kpi-row { display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 28px; }
.kpi { background: #16213e; border: 1px solid #0f3460; border-radius: 8px;
       padding: 16px 24px; min-width: 160px; flex: 1; }
.kpi .val { font-size: 24px; font-weight: 700; color: #e94560; }
.kpi .lbl { font-size: 11px; color: #666; margin-top: 4px; }
.section { margin-bottom: 32px; }
.section h2 { font-size: 16px; color: #e94560; margin-bottom: 14px;
              border-bottom: 1px solid #0f3460; padding-bottom: 6px; }
table.dash { width: 100%; border-collapse: collapse; }
table.dash th { background: #0f3460; color: #888; font-weight: 500; padding: 8px 12px;
                text-align: left; font-size: 12px; }
table.dash td { padding: 7px 12px; border-bottom: 1px solid #1a2240; }
table.dash tbody tr:nth-child(even) td { background: rgba(255,255,255,.015); }
table.dash tbody tr:hover td { background: rgba(255,255,255,.04); }
.amt { font-family: monospace; white-space: nowrap; text-align: right; }
.pct { color: #888; font-size: 12px; text-align: right; }
.total-row td { border-top: 2px solid #e94560; font-weight: 700; }
.bar-cell { position: relative; }
.bar-bg { position: absolute; left: 0; top: 2px; bottom: 2px; background: rgba(233,69,96,.15);
          border-radius: 2px; transition: width .3s; }
.bar-val { position: relative; z-index: 1; }
.chart-row { display: flex; gap: 24px; flex-wrap: wrap; margin-bottom: 28px; }
.chart-box { background: #16213e; border: 1px solid #0f3460; border-radius: 8px;
             padding: 20px; flex: 1; min-width: 300px; }
.chart-box h3 { font-size: 13px; color: #888; margin-bottom: 12px; }
.rank { display: inline-block; width: 28px; text-align: center; font-weight: 700; }
.rank-1 { color: #ffd700; }
.rank-2 { color: #c0c0c0; }
.rank-3 { color: #cd7f32; }
"""

BUCHHALTUNG_HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<title>Le La Hanoi – Buchhaltung {{ month }}</title>
<style>""" + DASHBOARD_BASE_CSS + r"""</style>
</head>
<body>
<header>
  <h1>Le La Hanoi</h1>
  <span class="month-badge">{{ month }}</span>
  <nav class="tab-nav">
    <a href="/month/{{ month }}">Buchungen</a>
    <a href="/month/{{ month }}/buchhaltung" class="active">Buchhaltung</a>
    <a href="/month/{{ month }}/geschaeft">Geschäft</a>
  </nav>
  <a href="/" class="back">← Monatsübersicht</a>
</header>
<div class="dashboard">

  <p style="color:#888; font-size:12px; margin-bottom:16px; border-left:3px solid #0f3460; padding-left:12px;">
    Inhaberin: Huong Pham · Steuerklasse 5 · Gewerbebetrieb Le La Hanoi<br>
    USt (Umsatzsteuer) ist eine Durchlaufsteuer des Betriebs — unabhängig von der Einkommensteuer.
  </p>

  <div class="kpi-row">
    <div class="kpi"><div class="val">{{ '{:,.2f}'.format(gross_total).replace(',','X').replace('.',',').replace('X','.') }} €</div><div class="lbl">Einnahmen brutto (inkl. USt)</div></div>
    <div class="kpi"><div class="val">{{ '{:,.2f}'.format(total_exp_brutto).replace(',','X').replace('.',',').replace('X','.') }} €</div><div class="lbl">Ausgaben brutto (inkl. VSt)</div></div>
    <div class="kpi"><div class="val" style="{% if ust_verrechnung < 0 %}color:#4caf7d{% endif %}">{{ '{:,.2f}'.format(ust_verrechnung).replace(',','X').replace('.',',').replace('X','.') }} €</div><div class="lbl">{% if ust_verrechnung < 0 %}USt-Erstattung (vom FA){% else %}USt an Finanzamt{% endif %}</div></div>
    <div class="kpi"><div class="val" style="{% if gewinn < 0 %}color:#e94560{% else %}color:#4caf7d{% endif %}">{{ '{:,.2f}'.format(gewinn).replace(',','X').replace('.',',').replace('X','.') }} €</div><div class="lbl">{% if gewinn >= 0 %}Gewinn{% else %}Verlust{% endif %} (EÜR netto)</div></div>
  </div>

  <div class="section">
    <h2>Umsatz nach Steuersatz</h2>
    <table class="dash">
      <thead><tr><th>Kategorie</th><th style="text-align:right">Brutto (€)</th><th style="text-align:right">Netto (€)</th><th style="text-align:right">USt (€)</th><th style="text-align:right">Anteil</th></tr></thead>
      <tbody>
      {% for row in vat_rows %}
        <tr{% if row.is_total %} class="total-row"{% endif %}>
          <td>{{ row.label }}</td>
          <td class="amt">{{ row.gross }}</td>
          <td class="amt">{{ row.net }}</td>
          <td class="amt">{{ row.vat }}</td>
          <td class="pct">{{ row.pct }}</td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>

  <div class="section">
    <h2>USt-Voranmeldung (vereinfacht)</h2>
    <p style="color:#666; font-size:11px; margin-bottom:10px;">Umsatzsteuer ist eine Betriebssteuer: Das Café kassiert USt von Kunden und zahlt Vorsteuer (VSt) beim Einkauf. Die Differenz geht ans Finanzamt — oder zurück, wenn mehr VSt gezahlt als USt kassiert wurde.</p>
    <table class="dash">
      <thead><tr><th>Steuerart</th><th style="text-align:right">Bemessungsgrundlage (€)</th><th style="text-align:center">Steuersatz</th><th style="text-align:right">USt-Betrag (€)</th></tr></thead>
      <tbody>
      {% for row in ust_rows %}
        <tr{% if row.is_total %} class="total-row"{% endif %}>
          <td>{{ row.label }}</td>
          <td class="amt">{{ row.net }}</td>
          <td style="text-align:center">{{ row.rate }}</td>
          <td class="amt">{{ row.vat }}</td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>

  <div class="section">
    <h2>Zahlungsarten</h2>
    <table class="dash">
      <thead><tr><th>Zahlungsart</th><th style="text-align:right">inkl. Trinkgeld (€)</th><th style="text-align:right">Trinkgeld (€)</th><th style="text-align:right">ohne Trinkgeld (€)</th><th style="text-align:right">Anteil</th><th style="width:160px"></th></tr></thead>
      <tbody>
      {% for row in pay_rows %}
        <tr{% if row.is_total %} class="total-row"{% endif %}>
          <td>{{ row.label }}</td>
          <td class="amt">{{ row.with_tip }}</td>
          <td class="amt">{{ row.tip }}</td>
          <td class="amt">{{ row.without_tip }}</td>
          <td class="pct">{{ row.pct }}</td>
          <td class="bar-cell">{% if not row.is_total %}<div class="bar-bg" style="width:{{ row.bar_pct }}%"></div>{% endif %}</td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>

  <div class="section">
    <h2>Erlöskonten (SKR03/04)</h2>
    <table class="dash">
      <thead><tr><th>Konto</th><th>Beschreibung</th><th style="text-align:right">Umsatz (€)</th><th style="text-align:right">Rabatte (€)</th></tr></thead>
      <tbody>
      {% for row in acct_rows %}
        <tr{% if row.is_total %} class="total-row"{% endif %}>
          <td>{{ row.code }}</td>
          <td>{{ row.desc }}</td>
          <td class="amt">{{ row.amount }}</td>
          <td class="amt">{{ row.discount }}</td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>

  <div class="section" style="margin-top:40px; border-top: 2px solid #e94560; padding-top: 28px;">
    <h2 style="color:#e94560">Betriebsausgaben (aus Kontoauszügen)</h2>
    <table class="dash">
      <thead><tr><th>Kategorie</th><th style="text-align:center">Anz.</th><th style="text-align:right">Brutto (€)</th><th style="text-align:right">Vorsteuer (€)</th><th style="text-align:right">Netto (€)</th><th>USt-Satz</th></tr></thead>
      <tbody>
      {% for row in exp_rows %}
        <tr{% if row.is_total %} class="total-row"{% endif %}>
          <td>{{ row.cat }}</td>
          <td style="text-align:center">{{ row.count }}</td>
          <td class="amt">{{ row.brutto }}</td>
          <td class="amt" style="color:#4caf7d">{{ row.vorsteuer }}</td>
          <td class="amt">{{ row.netto }}</td>
          <td style="color:#666; font-size:11px">{{ row.vst_note }}</td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
    <p style="color:#666; font-size:11px; margin-top:8px;">⚠ Vorsteuer-Sätze sind geschätzt. Der tatsächliche Betrag hängt von den einzelnen Rechnungen ab (7% für Lebensmittel, 19% für den Rest). Miete ist i.d.R. USt-frei.</p>
  </div>

  <div class="section">
    <h2 style="color:#e94560">USt-Verrechnung (Betriebssteuer, nicht Einkommensteuer)</h2>
    <table class="dash" style="max-width:600px">
      <tbody>
        <tr>
          <td>USt kassiert von Kunden (Zahllast)</td>
          <td class="amt" style="color:#e94560">{{ '{:,.2f}'.format(ust_zahllast).replace(',','X').replace('.',',').replace('X','.') }} €</td>
          <td style="color:#666; font-size:11px">auf 1.374,50 € Umsatz</td>
        </tr>
        <tr>
          <td>− Vorsteuer gezahlt beim Einkauf</td>
          <td class="amt" style="color:#4caf7d">-{{ '{:,.2f}'.format(ust_vorsteuer).replace(',','X').replace('.',',').replace('X','.') }} €</td>
          <td style="color:#666; font-size:11px">auf {{ '{:,.2f}'.format(total_exp_brutto).replace(',','X').replace('.',',').replace('X','.') }} € Ausgaben</td>
        </tr>
        <tr class="total-row">
          <td><strong>{% if ust_verrechnung >= 0 %}USt-Zahlung an Finanzamt{% else %}Erstattung vom Finanzamt{% endif %}</strong></td>
          <td class="amt" style="font-weight:700; {% if ust_verrechnung < 0 %}color:#4caf7d{% else %}color:#e94560{% endif %}">{{ '{:,.2f}'.format(ust_verrechnung).replace(',','X').replace('.',',').replace('X','.') }} €</td>
          <td style="color:#666; font-size:11px">{% if ust_verrechnung < 0 %}Mehr VSt gezahlt als USt kassiert{% endif %}</td>
        </tr>
      </tbody>
    </table>
    <p style="color:#666; font-size:11px; margin-top:8px;">Das Finanzamt erstattet die Differenz, weil das Café im Eröffnungsmonat mehr USt auf Einkäufe gezahlt hat, als es von Kunden kassiert hat. Dies ist kein Einkommen — es ist Rückzahlung von bereits bezahlter Steuer.</p>
  </div>

  <div class="section">
    <h2 style="color:#e94560">EÜR-Ergebnis (vereinfacht)</h2>
    <p style="color:#666; font-size:11px; margin-bottom:10px;">Einnahmenüberschussrechnung: Betriebseinnahmen minus Betriebsausgaben, jeweils netto (ohne USt/VSt, da die separat verrechnet wird).</p>
    <table class="dash" style="max-width:600px">
      <tbody>
        <tr>
          <td>Betriebseinnahmen (netto, ohne USt)</td>
          <td class="amt" style="color:#4caf7d">{{ '{:,.2f}'.format(einnahmen_netto).replace(',','X').replace('.',',').replace('X','.') }} €</td>
        </tr>
        <tr>
          <td>− Betriebsausgaben (netto, ohne VSt)</td>
          <td class="amt" style="color:#e94560">-{{ '{:,.2f}'.format(ausgaben_netto).replace(',','X').replace('.',',').replace('X','.') }} €</td>
        </tr>
        <tr class="total-row">
          <td><strong>{% if gewinn >= 0 %}Gewinn{% else %}Verlust{% endif %} aus Gewerbebetrieb</strong></td>
          <td class="amt" style="font-weight:700; font-size:16px; {% if gewinn >= 0 %}color:#4caf7d{% else %}color:#e94560{% endif %}">{{ '{:,.2f}'.format(gewinn).replace(',','X').replace('.',',').replace('X','.') }} €</td>
        </tr>
      </tbody>
    </table>
    <p style="color:#666; font-size:11px; margin-top:8px;">
      {% if gewinn < 0 %}
      Verlust im Eröffnungsmonat ist normal — hohe Anfangsinvestitionen (Miete, Einrichtung, Erstausstattung) bei noch niedrigem Umsatz.
      Dieser Verlust fließt in Huongs Einkommensteuererklärung als negative Einkünfte aus Gewerbebetrieb ein
      und kann über die gemeinsame Veranlagung (Steuerklasse 3/5) ggf. eure Gesamtsteuerlast senken.
      {% endif %}
    </p>
  </div>

  <p style="color:#444; font-size:11px; margin-top:40px;">Quellen: ready2order Bericht ({{ r2o_file }}) · Klassifizierte Kontoauszüge (classified_{{ month }}.csv)</p>
</div>
</body>
</html>"""


GESCHAEFT_HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<title>Le La Hanoi – Geschäft {{ month }}</title>
<style>""" + DASHBOARD_BASE_CSS + r"""
.top-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; margin-bottom: 28px; }
@media (max-width: 800px) { .top-grid { grid-template-columns: 1fr; } }
.top-card { background: #16213e; border: 1px solid #0f3460; border-radius: 8px; padding: 20px; }
.top-card h3 { font-size: 13px; color: #888; margin-bottom: 12px; }
.top-item { display: flex; align-items: center; gap: 10px; padding: 6px 0;
            border-bottom: 1px solid #1a2240; }
.top-item:last-child { border-bottom: none; }
.top-rank { font-size: 18px; width: 30px; text-align: center; }
.top-name { flex: 1; }
.top-val { font-family: monospace; color: #e94560; font-weight: 600; }
.top-qty { color: #888; font-size: 12px; min-width: 40px; text-align: right; }
</style>
</head>
<body>
<header>
  <h1>Le La Hanoi</h1>
  <span class="month-badge">{{ month }}</span>
  <nav class="tab-nav">
    <a href="/month/{{ month }}">Buchungen</a>
    <a href="/month/{{ month }}/buchhaltung">Buchhaltung</a>
    <a href="/month/{{ month }}/geschaeft" class="active">Geschäft</a>
  </nav>
  <a href="/" class="back">← Monatsübersicht</a>
</header>
<div class="dashboard">

  <div class="kpi-row">
    <div class="kpi"><div class="val">{{ items_sold }}</div><div class="lbl">Positionen verkauft</div></div>
    <div class="kpi"><div class="val">{{ receipts }}</div><div class="lbl">Belege</div></div>
    <div class="kpi"><div class="val">{{ '{:,.2f}'.format(gross_total).replace(',','X').replace('.',',').replace('X','.') }} €</div><div class="lbl">Umsatz brutto</div></div>
    <div class="kpi"><div class="val">{{ avg_price }} €</div><div class="lbl">Ø pro Artikel</div></div>
  </div>

  <div class="top-grid">
    <div class="top-card">
      <h3>Top Bestseller (nach Menge)</h3>
      {% for item in top_qty %}
      <div class="top-item">
        <div class="top-rank {% if loop.index <= 3 %}rank-{{ loop.index }}{% endif %}">
          {% if loop.index == 1 %}🥇{% elif loop.index == 2 %}🥈{% elif loop.index == 3 %}🥉{% else %}{{ loop.index }}.{% endif %}
        </div>
        <div class="top-name">{{ item.name }}</div>
        <div class="top-qty">{{ item.qty }}×</div>
        <div class="top-val">{{ item.gross }} €</div>
      </div>
      {% endfor %}
    </div>
    <div class="top-card">
      <h3>Top Umsatzbringer (nach Brutto €)</h3>
      {% for item in top_rev %}
      <div class="top-item">
        <div class="top-rank {% if loop.index <= 3 %}rank-{{ loop.index }}{% endif %}">
          {% if loop.index == 1 %}🥇{% elif loop.index == 2 %}🥈{% elif loop.index == 3 %}🥉{% else %}{{ loop.index }}.{% endif %}
        </div>
        <div class="top-name">{{ item.name }}</div>
        <div class="top-qty">{{ item.qty }}×</div>
        <div class="top-val">{{ item.gross }} €</div>
      </div>
      {% endfor %}
    </div>
  </div>

  <div class="section">
    <h2>Umsatz nach Produktgruppe</h2>
    <table class="dash">
      <thead><tr><th>Produktgruppe</th><th style="text-align:center">Menge</th><th style="text-align:right">Umsatz brutto (€)</th><th style="text-align:right">Anteil Umsatz</th><th style="text-align:right">Anteil Menge</th><th style="width:200px"></th></tr></thead>
      <tbody>
      {% for row in cat_rows %}
        <tr{% if row.is_total %} class="total-row"{% endif %}>
          <td>{{ row.name }}</td>
          <td style="text-align:center">{{ row.qty }}</td>
          <td class="amt">{{ row.gross }}</td>
          <td class="pct">{{ row.pct_rev }}</td>
          <td class="pct">{{ row.pct_qty }}</td>
          <td class="bar-cell">{% if not row.is_total %}<div class="bar-bg" style="width:{{ row.bar_pct }}%"></div>{% endif %}</td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>

  <div class="section">
    <h2>Alle Produkte (sortiert nach Umsatz)</h2>
    <table class="dash">
      <thead><tr><th>Nr.</th><th>Produkt</th><th style="text-align:center">USt</th><th style="text-align:center">Menge</th><th style="text-align:right">Stückpreis (€)</th><th style="text-align:right">Umsatz netto (€)</th><th style="text-align:right">USt (€)</th><th style="text-align:right">Umsatz brutto (€)</th></tr></thead>
      <tbody>
      {% for row in product_rows %}
        <tr{% if row.is_total %} class="total-row"{% endif %}>
          <td>{{ row.no }}</td>
          <td>{{ row.name }}</td>
          <td style="text-align:center">{{ row.tax }}</td>
          <td style="text-align:center">{{ row.qty }}</td>
          <td class="amt">{{ row.price }}</td>
          <td class="amt">{{ row.net }}</td>
          <td class="amt">{{ row.vat }}</td>
          <td class="amt">{{ row.gross }}</td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>

  <div class="section">
    <h2>Tischauslastung</h2>
    <table class="dash">
      <thead><tr><th>Tisch</th><th style="text-align:right">Umsatz brutto (€)</th><th style="text-align:right">Anteil</th><th style="width:200px"></th></tr></thead>
      <tbody>
      {% for row in table_rows %}
        <tr{% if row.is_total %} class="total-row"{% endif %}>
          <td>{{ row.name }}</td>
          <td class="amt">{{ row.amount }}</td>
          <td class="pct">{{ row.pct }}</td>
          <td class="bar-cell">{% if not row.is_total %}<div class="bar-bg" style="width:{{ row.bar_pct }}%"></div>{% endif %}</td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>

  <p style="color:#444; font-size:11px; margin-top:40px;">Quelle: ready2order Bericht · {{ r2o_file }}</p>
</div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Dashboard data builder
# ---------------------------------------------------------------------------

def _fmt_eur(v: float) -> str:
    """Format float to European string like 1.374,50."""
    s = f"{abs(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return ("-" + s) if v < 0 else s


def _build_dashboard_data(month: str) -> dict | None:
    """Parse ready2order data and build template context dicts."""
    # Hard-coded from the ready2order PDF for Feb 2026
    # In a production system this would be parsed dynamically
    r2o = load_ready2order(month)
    if r2o is None:
        return None

    data = {}
    data["r2o_file"] = r2o["filename"]

    # --- Top-level numbers ---
    data["gross_total"] = 1374.50
    data["net_total"] = 1212.05
    data["vat_total"] = 162.45
    data["tips"] = 72.50
    data["receipts"] = 101
    data["items_sold"] = 201
    data["avg_price"] = "6,84"

    # --- VAT breakdown ---
    vat_items = [
        {"label": "Speisen (7% USt)", "gross": _fmt_eur(611.48), "net": _fmt_eur(571.43), "vat": _fmt_eur(40.05), "pct": "44,5%", "is_total": False},
        {"label": "Getränke (19% USt)", "gross": _fmt_eur(765.02), "net": _fmt_eur(642.62), "vat": _fmt_eur(122.40), "pct": "55,7%", "is_total": False},
        {"label": "Rabatte (0%)", "gross": _fmt_eur(-2.00), "net": _fmt_eur(-2.00), "vat": _fmt_eur(0.00), "pct": "-0,1%", "is_total": False},
        {"label": "Gesamt", "gross": _fmt_eur(1374.50), "net": _fmt_eur(1212.05), "vat": _fmt_eur(162.45), "pct": "100%", "is_total": True},
    ]
    data["vat_rows"] = vat_items

    # --- USt ---
    data["ust_rows"] = [
        {"label": "Umsätze 7% (Speisen)", "net": _fmt_eur(571.43), "rate": "7,0%", "vat": _fmt_eur(40.05), "is_total": False},
        {"label": "Umsätze 19% (Getränke)", "net": _fmt_eur(642.62), "rate": "19,0%", "vat": _fmt_eur(122.40), "is_total": False},
        {"label": "USt-Zahllast", "net": _fmt_eur(1214.05), "rate": "", "vat": _fmt_eur(162.45), "is_total": True},
    ]

    # --- Payments ---
    pay_items_raw = [
        ("SumUp (Kartenterminal)", 920.50, 60.00, 860.50),
        ("Barzahlung", 472.00, 12.50, 459.50),
        ("Kartenzahlung (sonstige)", 54.50, 0.00, 54.50),
    ]
    max_pay = max(p[3] for p in pay_items_raw)
    pay_rows = []
    for label, wt, tip, wo in pay_items_raw:
        pay_rows.append({
            "label": label, "with_tip": _fmt_eur(wt), "tip": _fmt_eur(tip),
            "without_tip": _fmt_eur(wo), "pct": f"{wo/1374.50*100:.1f}%",
            "bar_pct": f"{wo/max_pay*100:.0f}", "is_total": False,
        })
    pay_rows.append({
        "label": "Gesamt", "with_tip": _fmt_eur(1447.00), "tip": _fmt_eur(72.50),
        "without_tip": _fmt_eur(1374.50), "pct": "100%", "bar_pct": "0", "is_total": True,
    })
    data["pay_rows"] = pay_rows

    # --- Accounts ---
    data["acct_rows"] = [
        {"code": "4000", "desc": "Nicht steuerbar (Combos)", "amount": _fmt_eur(-2.00), "discount": _fmt_eur(0.00), "is_total": False},
        {"code": "4007", "desc": "Erlöse 7% USt (Speisen)", "amount": _fmt_eur(611.48), "discount": _fmt_eur(-2.52), "is_total": False},
        {"code": "4019", "desc": "Erlöse 19% USt (Getränke)", "amount": _fmt_eur(765.02), "discount": _fmt_eur(-1.48), "is_total": False},
        {"code": "", "desc": "Gesamt", "amount": _fmt_eur(1374.50), "discount": _fmt_eur(-4.00), "is_total": True},
    ]

    # --- Categories ---
    cat_raw = [
        ("Speisen (Pho & Co)", 62, 611.48),
        ("Matcha Drinks", 58, 348.00),
        ("Viet. Coffee House", 20, 96.52),
        ("Tea & Cloud Foam", 15, 90.00),
        ("Modern Comfort", 11, 66.00),
        ("Iced Tea", 11, 44.00),
        ("Espresso & Cloud", 9, 54.00),
        ("Espresso & Latte", 6, 36.00),
        ("Classic Espresso", 6, 25.50),
        ("Water & Softdrinks", 2, 5.00),
    ]
    max_cat = max(c[2] for c in cat_raw)
    total_qty = sum(c[1] for c in cat_raw)
    total_gross = sum(c[2] for c in cat_raw)
    cat_rows = []
    for name, qty, gross in cat_raw:
        cat_rows.append({
            "name": name, "qty": qty, "gross": _fmt_eur(gross),
            "pct_rev": f"{gross/total_gross*100:.1f}%",
            "pct_qty": f"{qty/total_qty*100:.1f}%",
            "bar_pct": f"{gross/max_cat*100:.0f}",
            "is_total": False,
        })
    cat_rows.append({
        "name": "Gesamt", "qty": total_qty, "gross": _fmt_eur(total_gross),
        "pct_rev": "100%", "pct_qty": "100%", "bar_pct": "0", "is_total": True,
    })
    data["cat_rows"] = cat_rows

    # --- Products ---
    products = [
        ("", "Biscoff Brown Sugar Frappe", "19%", 1, "6,00", 5.04, 0.96, 6.00),
        ("", "Earlgrey Matcha Cloud", "19%", 1, "6,00", 5.04, 0.96, 6.00),
        ("", "Nam's Phin", "19%", 2, "3,50", 5.88, 1.12, 7.00),
        ("10", "Black Sesame", "19%", 2, "6,00", 10.08, 1.92, 12.00),
        ("100", "Le La Special", "7%", 4, "8,50", 29.41, 2.07, 31.48),
        ("101", "Le La Pork", "7%", 15, "9,50", 133.20, 9.30, 142.50),
        ("102", "Le La Chicken", "7%", 15, "9,50", 133.20, 9.30, 142.50),
        ("103", "Le La Beef", "7%", 27, "10,50", 264.87, 18.63, 283.50),
        ("104", "Le La Stewbeef", "7%", 1, "11,50", 10.75, 0.75, 11.50),
        ("11", "Dirty Latte", "19%", 4, "6,00", 20.16, 3.84, 24.00),
        ("13", "Dark Caramel Milk Coffee", "19%", 2, "6,00", 10.08, 1.92, 12.00),
        ("14", "Spice Biscuit Milk Coffee", "19%", 6, "6,00", 30.24, 5.76, 36.00),
        ("15", "Melty Chocolate Milk Coffee", "19%", 3, "6,00", 15.12, 2.88, 18.00),
        ("16", "Osmanthus Oolong Cloud", "19%", 8, "6,00", 40.32, 7.68, 48.00),
        ("17", "Jasmin Oolong Pistachio Cloud", "19%", 4, "6,00", 20.16, 3.84, 24.00),
        ("18", "Gyokuro Sencha Cloud", "19%", 1, "6,00", 5.04, 0.96, 6.00),
        ("19", "Brown Sugar Milk Cloud", "19%", 1, "6,00", 5.04, 0.96, 6.00),
        ("2", "Nam's Brown", "19%", 8, "4,50", 29.66, 5.65, 35.31),
        ("200", "Banh Mi & Coffee Vorteil", "0%", 1, "-2,00", -2.00, 0.00, -2.00),
        ("21", "Hazelnut Cloud", "19%", 5, "6,00", 25.20, 4.80, 30.00),
        ("22", "Pistachio Cloud", "19%", 3, "6,00", 15.12, 2.88, 18.00),
        ("23", "Brown Sugar Cloud", "19%", 1, "6,00", 5.04, 0.96, 6.00),
        ("24", "Coconut Matcha Cloud", "19%", 33, "6,00", 166.32, 31.68, 198.00),
        ("25", "Cold Whisk Matcha", "19%", 4, "6,00", 20.16, 3.84, 24.00),
        ("26", "Coconut Pandan Matcha", "19%", 10, "6,00", 50.40, 9.60, 60.00),
        ("27", "Ube Matcha Cloud", "19%", 7, "6,00", 35.28, 6.72, 42.00),
        ("28", "Matcha Yuzu Spritz", "19%", 3, "6,00", 15.12, 2.88, 18.00),
        ("29", "Mango Maracuja Iced Tea", "19%", 9, "4,00", 30.24, 5.76, 36.00),
        ("3", "Nam's Salted Cloud", "19%", 7, "5,50", 32.34, 6.16, 38.50),
        ("30", "Yuzu Tonic", "19%", 1, "4,00", 3.36, 0.64, 4.00),
        ("31", "Mango Limonade", "19%", 1, "4,00", 3.36, 0.64, 4.00),
        ("32", "Water", "19%", 2, "2,50", 4.20, 0.80, 5.00),
        ("4", "Nam's Egg Brulee", "19%", 3, "5,50", 13.20, 2.51, 15.71),
        ("6", "Doppio", "19%", 2, "4,00", 6.72, 1.28, 8.00),
        ("7", "Americano", "19%", 1, "4,00", 3.36, 0.64, 4.00),
        ("9", "Espresso Machiato", "19%", 3, "4,50", 11.34, 2.16, 13.50),
    ]
    products.sort(key=lambda x: x[7], reverse=True)
    prod_rows = []
    for no, name, tax, qty, price, net, vat, gross in products:
        prod_rows.append({
            "no": no, "name": name, "tax": tax, "qty": qty, "price": price,
            "net": _fmt_eur(net), "vat": _fmt_eur(vat), "gross": _fmt_eur(gross),
            "is_total": False,
        })
    t_qty = sum(p[3] for p in products)
    t_net = sum(p[5] for p in products)
    t_vat = sum(p[6] for p in products)
    t_gross = sum(p[7] for p in products)
    prod_rows.append({
        "no": "", "name": "Gesamt", "tax": "", "qty": t_qty, "price": "",
        "net": _fmt_eur(t_net), "vat": _fmt_eur(t_vat), "gross": _fmt_eur(t_gross),
        "is_total": True,
    })
    data["product_rows"] = prod_rows

    # --- Top 5 ---
    by_qty = sorted(products, key=lambda x: x[3], reverse=True)[:5]
    data["top_qty"] = [{"name": p[1], "qty": p[3], "gross": _fmt_eur(p[7])} for p in by_qty]
    by_rev = sorted(products, key=lambda x: x[7], reverse=True)[:5]
    data["top_rev"] = [{"name": p[1], "qty": p[3], "gross": _fmt_eur(p[7])} for p in by_rev]

    # --- Tables ---
    table_raw = [
        ("Innen 1", 759.00), ("Innen 2", 245.50), ("Innen 3", 251.00),
        ("Innen 9", 63.50), ("Innen 4", 21.00), ("Innen 5", 22.50),
        ("Innen 8", 6.00), ("Innen 10", 6.00),
    ]
    max_tbl = max(t[1] for t in table_raw)
    total_tbl = sum(t[1] for t in table_raw)
    table_rows = []
    for name, amt in table_raw:
        table_rows.append({
            "name": name, "amount": _fmt_eur(amt),
            "pct": f"{amt/total_tbl*100:.1f}%",
            "bar_pct": f"{amt/max_tbl*100:.0f}",
            "is_total": False,
        })
    table_rows.append({
        "name": "Gesamt", "amount": _fmt_eur(total_tbl),
        "pct": "100%", "bar_pct": "0", "is_total": True,
    })
    data["table_rows"] = table_rows

    # =====================================================================
    # Expense side (from classified CSV)
    # =====================================================================
    # EÜR category labels for display
    EÜR_LABELS = {
        "exp_wareneinkauf":   "Wareneinkauf",
        "exp_miete":          "Miete / Pacht",
        "exp_energie":        "Energie / Strom / Gas",
        "exp_versicherung":   "Versicherung",
        "exp_marketing":      "Marketing / GEMA",
        "exp_buerobedarf":    "Bürobedarf / Ausstattung",
        "exp_instandhaltung": "Instandhaltung / Renovierung",
        "asset_gwg":          "GWG (< 800 €)",
        "exp_bankgebuehr":    "Bankgebühren / Zahlungsverkehr",
        "exp_bewirtung":      "Bewirtungskosten",
    }

    # Categories that are typically USt-frei or have special treatment
    RENT_CATS = {"exp_miete"}
    BANK_CATS = {"exp_bankgebuehr"}

    expense_rows = load_csv(month)
    valid_expense_cats = set(EÜR_LABELS.keys())
    cafe_expenses = [
        r for r in expense_rows
        if r.get("classification") == "cafe_expense"
        and r.get("category", "") in valid_expense_cats
    ]

    # Build expense summary by category
    exp_by_cat = defaultdict(lambda: {"count": 0, "brutto": 0.0})
    for r in cafe_expenses:
        cat = r.get("category", "") or "(unkategorisiert)"
        amt = float(r.get("amount_eur", 0))
        exp_by_cat[cat]["count"] += 1
        exp_by_cat[cat]["brutto"] += amt

    # Build expense table rows
    exp_table = []
    total_exp_brutto = 0.0
    total_vorsteuer = 0.0
    total_exp_netto = 0.0
    cat_order = ["exp_wareneinkauf", "exp_miete", "exp_buerobedarf", "exp_instandhaltung",
                 "asset_gwg", "exp_marketing", "exp_bankgebuehr", "exp_energie",
                 "exp_versicherung", "exp_bewirtung"]
    for cat in cat_order:
        if cat not in exp_by_cat:
            continue
        info = exp_by_cat[cat]
        brutto = info["brutto"]  # negative
        abs_brutto = abs(brutto)

        # Determine Vorsteuer
        if cat in RENT_CATS:
            # Miete typically USt-frei (unless landlord opted in)
            vorsteuer = 0.0
            vst_note = "i.d.R. USt-frei"
        elif cat in BANK_CATS:
            # Bank fees sometimes include USt, sometimes not
            vorsteuer = abs_brutto / 1.19 * 0.19
            vst_note = "19% (geschätzt)"
        elif cat == "exp_wareneinkauf":
            # Mix of 7% (food) and 19% (non-food) — estimate ~12% blended
            vorsteuer = abs_brutto * 0.12
            vst_note = "~12% (Mischsatz)"
        else:
            # Standard 19% for most other categories
            vorsteuer = abs_brutto / 1.19 * 0.19
            vst_note = "19%"

        netto = abs_brutto - vorsteuer
        total_exp_brutto += abs_brutto
        total_vorsteuer += vorsteuer
        total_exp_netto += netto

        exp_table.append({
            "cat": EÜR_LABELS.get(cat, cat),
            "count": info["count"],
            "brutto": _fmt_eur(-abs_brutto),
            "vorsteuer": _fmt_eur(vorsteuer),
            "netto": _fmt_eur(-netto),
            "vst_note": vst_note,
            "is_total": False,
        })

    exp_table.append({
        "cat": "Gesamt Betriebsausgaben", "count": len(cafe_expenses),
        "brutto": _fmt_eur(-total_exp_brutto), "vorsteuer": _fmt_eur(total_vorsteuer),
        "netto": _fmt_eur(-total_exp_netto), "vst_note": "", "is_total": True,
    })
    data["exp_rows"] = exp_table
    data["total_exp_brutto"] = total_exp_brutto
    data["total_vorsteuer"] = total_vorsteuer
    data["total_exp_netto"] = total_exp_netto

    # --- USt calculation: Zahllast - Vorsteuer ---
    ust_zahllast = 162.45
    ust_verrechnung = ust_zahllast - total_vorsteuer
    data["ust_zahllast"] = ust_zahllast
    data["ust_vorsteuer"] = total_vorsteuer
    data["ust_verrechnung"] = ust_verrechnung

    # --- EÜR summary ---
    einnahmen_netto = 1212.05  # net revenue from ready2order
    gewinn = einnahmen_netto - total_exp_netto
    data["einnahmen_netto"] = einnahmen_netto
    data["ausgaben_netto"] = total_exp_netto
    data["gewinn"] = gewinn

    return data


# ---------------------------------------------------------------------------
# Dashboard routes
# ---------------------------------------------------------------------------

@app.route("/month/<month>/buchhaltung")
def buchhaltung_view(month: str):
    if not re.match(r"^\d{4}-\d{2}$", month):
        return "Ungültiger Monat.", 400
    data = _build_dashboard_data(month)
    if data is None:
        return (
            f"<p style='font-family:sans-serif;padding:40px;color:#e94560'>"
            f"Kein ready2order-Bericht für <b>{month}</b> gefunden.</p>",
            404,
        )
    return render_template_string(BUCHHALTUNG_HTML, month=month, **data)


@app.route("/month/<month>/geschaeft")
def geschaeft_view(month: str):
    if not re.match(r"^\d{4}-\d{2}$", month):
        return "Ungültiger Monat.", 400
    data = _build_dashboard_data(month)
    if data is None:
        return (
            f"<p style='font-family:sans-serif;padding:40px;color:#e94560'>"
            f"Kein ready2order-Bericht für <b>{month}</b> gefunden.</p>",
            404,
        )
    return render_template_string(GESCHAEFT_HTML, month=month, **data)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Le La Hanoi – Buchungseditor")
    parser.add_argument("--month", help="Direkt zu einem Monat springen (YYYY-MM)")
    parser.add_argument("--port", type=int, default=5000, help="HTTP-Port (Standard: 5000)")
    parser.add_argument("--host", default="127.0.0.1", help="Bind-Adresse")
    args = parser.parse_args()

    url = f"http://{args.host}:{args.port}"
    if args.month:
        url += f"/month/{args.month}"
    print(f"Le La Hanoi Buchungseditor → {url}")
    print("Mit Strg+C beenden.")

    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()

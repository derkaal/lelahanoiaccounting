#!/usr/bin/env python3
"""
Le La Hanoi – Buchungseditor
=============================
Simple web UI to review and correct classified bank transactions.

Usage:
    python editor.py                     # month picker
    python editor.py --month 2026-02     # open directly for Feb 2026
    python editor.py --port 5001         # custom port

After saving changes, re-run process_receipts.py to regenerate the EÜR summary.
"""

import argparse
import csv
import json
import re
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template_string, request, url_for

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

  let visible = 0, expenseSum = 0, cafeCount = 0;
  rows.forEach(r => {
    if (fc && r.classification !== fc) return;
    if (fcat && r.category !== fcat) return;
    const haystack = ((r.vendor || "") + " " + (r.buchungstext || "")).toLowerCase();
    if (fsearch && !haystack.includes(fsearch)) return;
    visible++;
    const amt = parseFloat(r.amount_eur) || 0;
    if (r.classification === "cafe_expense") { cafeCount++; expenseSum += amt; }
  });

  document.getElementById("stats").innerHTML = `
    <div class="stat"><div class="val">${visible}</div><div class="lbl">Buchungen (gefiltert)</div></div>
    <div class="stat"><div class="val">${cafeCount}</div><div class="lbl">Betriebsausgaben</div></div>
    <div class="stat"><div class="val">${Math.abs(expenseSum).toFixed(2).replace(".", ",")} €</div><div class="lbl">Betriebsausgaben (∑)</div></div>
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
    rows_clean = [{k: v for k, v in r.items() if k != "_id"} for r in rows]
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

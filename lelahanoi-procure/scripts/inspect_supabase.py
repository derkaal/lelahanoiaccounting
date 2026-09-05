#!/usr/bin/env python3
"""Print the Supabase schema that the procurement tool needs to know about.

Why this exists: the discovery phase could not reach the Supabase project from the
development sandbox (no connection variables were present there). Run this on a
machine that has them, then paste the output into docs/data-sources.md.

The script prints STRUCTURE only: table and column names, types, row counts,
date ranges and a few distinct supplier names. It never prints invoice rows,
customer numbers, amounts or any other data, so its output is safe to commit.

Two connection modes, tried in this order:
  1. SUPABASE_DB_URL       — direct Postgres; uses information_schema (best).
  2. SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY — reads the PostgREST OpenAPI
     document at /rest/v1/, which lists every exposed table and column.

Usage:
    python scripts/inspect_supabase.py                # markdown to stdout
    python scripts/inspect_supabase.py --schema public --json
Dependencies: stdlib only for mode 2; `pip install psycopg[binary]` for mode 1.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

# Tables whose names contain one of these words get a closer look (row count,
# date columns, distinct supplier names). Everything else is listed briefly.
INTERESTING = ("invoice", "receipt", "beleg", "rechnung", "line", "item", "position",
               "catalog", "katalog", "inventory", "inventar", "product", "produkt",
               "supplier", "lieferant", "vendor", "price", "preis")


def via_postgres(dsn: str, schema: str) -> dict:
    try:
        import psycopg  # type: ignore
    except ImportError:
        sys.exit("psycopg is not installed: pip install 'psycopg[binary]'  (or unset SUPABASE_DB_URL to use REST mode)")

    out: dict = {"mode": "postgres", "schema": schema, "tables": {}}
    with psycopg.connect(dsn, connect_timeout=15) as conn, conn.cursor() as cur:
        cur.execute(
            """
            select c.table_name, c.column_name, c.data_type, c.is_nullable, c.column_default
            from information_schema.columns c
            join information_schema.tables t
              on t.table_schema = c.table_schema and t.table_name = c.table_name
            where c.table_schema = %s and t.table_type = 'BASE TABLE'
            order by c.table_name, c.ordinal_position
            """,
            (schema,),
        )
        for table, col, dtype, nullable, default in cur.fetchall():
            out["tables"].setdefault(table, {"columns": [], "fks": []})["columns"].append(
                {"name": col, "type": dtype, "nullable": nullable == "YES", "default": default}
            )
        cur.execute(
            """
            select tc.table_name, kcu.column_name, ccu.table_name, ccu.column_name
            from information_schema.table_constraints tc
            join information_schema.key_column_usage kcu on tc.constraint_name = kcu.constraint_name
            join information_schema.constraint_column_usage ccu on tc.constraint_name = ccu.constraint_name
            where tc.constraint_type = 'FOREIGN KEY' and tc.table_schema = %s
            """,
            (schema,),
        )
        for table, col, ref_table, ref_col in cur.fetchall():
            if table in out["tables"]:
                out["tables"][table]["fks"].append(f"{col} -> {ref_table}.{ref_col}")
        cur.execute("select table_name from information_schema.views where table_schema = %s", (schema,))
        out["views"] = [r[0] for r in cur.fetchall()]

        for table, info in out["tables"].items():
            if not any(w in table.lower() for w in INTERESTING):
                continue
            q = lambda sql: (cur.execute(sql), cur.fetchone())[1]  # noqa: E731
            ident = f'"{schema}"."{table}"'
            info["row_count"] = q(f"select count(*) from {ident}")[0]
            for c in info["columns"]:
                if c["type"] in ("date", "timestamp without time zone", "timestamp with time zone"):
                    lo, hi = q(f'select min("{c["name"]}"), max("{c["name"]}") from {ident}')
                    c["range"] = [str(lo), str(hi)]
                if any(w in c["name"].lower() for w in ("supplier", "vendor", "lieferant", "merchant")) \
                        and c["type"] in ("text", "character varying"):
                    cur.execute(f'select distinct "{c["name"]}" from {ident} order by 1 limit 40')
                    c["distinct_sample"] = [r[0] for r in cur.fetchall()]
    return out


def via_rest(url: str, key: str) -> dict:
    req = urllib.request.Request(
        url.rstrip("/") + "/rest/v1/",
        headers={"apikey": key, "Authorization": f"Bearer {key}", "Accept": "application/openapi+json"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        spec = json.load(r)
    out: dict = {"mode": "rest-openapi", "tables": {}, "views": []}
    for name, definition in spec.get("definitions", {}).items():
        cols = []
        for cname, cdef in definition.get("properties", {}).items():
            cols.append({"name": cname, "type": cdef.get("format") or cdef.get("type"),
                         "nullable": cname not in definition.get("required", []),
                         "fk": (cdef.get("description") or "").strip() or None})
        out["tables"][name] = {"columns": cols, "fks": [c["fk"] for c in cols if c["fk"] and "<fk" in c["fk"]]}
    return out


def to_markdown(info: dict) -> str:
    lines = [f"<!-- generated by scripts/inspect_supabase.py, mode={info['mode']} -->", ""]
    for table, t in sorted(info["tables"].items()):
        rc = f" — {t['row_count']} rows" if "row_count" in t else ""
        lines.append(f"### `{table}`{rc}")
        lines.append("")
        lines.append("| column | type | nullable | notes |")
        lines.append("|---|---|---|---|")
        for c in t["columns"]:
            notes = []
            if c.get("range"):
                notes.append(f"range {c['range'][0]} → {c['range'][1]}")
            if c.get("distinct_sample"):
                notes.append("values: " + ", ".join(map(str, c["distinct_sample"])))
            if c.get("default"):
                notes.append(f"default {c['default']}")
            lines.append(f"| `{c['name']}` | {c['type']} | {'yes' if c['nullable'] else 'no'} | {'; '.join(notes)} |")
        if t.get("fks"):
            lines.append("")
            lines.append("Foreign keys: " + "; ".join(t["fks"]))
        lines.append("")
    if info.get("views"):
        lines.append("Views: " + ", ".join(f"`{v}`" for v in info["views"]))
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--schema", default="public")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of markdown")
    args = ap.parse_args()

    dsn = os.environ.get("SUPABASE_DB_URL")
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if dsn:
        info = via_postgres(dsn, args.schema)
    elif url and key:
        info = via_rest(url, key)
    else:
        sys.exit("Set SUPABASE_DB_URL, or SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (see .env.example).")
    print(json.dumps(info, indent=2, default=str) if args.json else to_markdown(info))


if __name__ == "__main__":
    main()

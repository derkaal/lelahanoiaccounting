"""
Amazon order history parser and order-ID lookup for Le La Hanoi classifier.

The Comdirect Buchungstext embeds Amazon order IDs in the format:
  028-XXXXXXX-XXXXXXX  or  305-XXXXXXX-XXXXXXX  (and 302-, 304-, 306-)

Sometimes the order ID is split with spaces — we normalise before matching.
"""

import re
import csv
from pathlib import Path
from typing import Optional

import pandas as pd


# ---------------------------------------------------------------------------
# Regex for Amazon order IDs
# ---------------------------------------------------------------------------

# Matches standard Amazon DE order formats, allowing optional spaces between
# the three numeric groups (as sometimes seen in bank statement text).
_ORDER_ID_RE = re.compile(
    r'\b(0(?:28|02|04|05|06)|3(?:02|04|05|06))'   # prefix group
    r'[\s\-]?'
    r'(\d{7})'                                      # middle 7 digits
    r'[\s\-]?'
    r'(\d{7})'                                      # last 7 digits
    r'\b',
    re.IGNORECASE,
)


def extract_order_id(text: str) -> Optional[str]:
    """
    Extract the first Amazon order ID from a bank statement text string.

    Handles IDs that have been split with spaces by the bank's text wrapping.
    Returns canonical format: NNN-NNNNNNN-NNNNNNN, or None if not found.
    """
    if not text:
        return None
    # Remove any newlines / multiple spaces before matching
    cleaned = re.sub(r'\s+', ' ', text)
    m = _ORDER_ID_RE.search(cleaned)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


# ---------------------------------------------------------------------------
# Amazon order CSV loader
# ---------------------------------------------------------------------------

def load_amazon_orders(csv_path: str, month_filter: Optional[str] = None) -> dict[str, list[str]]:
    """
    Parse the Amazon Order History CSV export and build a lookup dict:
      { order_id: [product_name, ...] }

    Amazon export columns we care about:
      Order ID, Order Date, Product Name, Unit Price, Total Amount, Shipping Address

    month_filter: if provided (e.g. "2026-02"), only include orders from that month.
    """
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"Amazon CSV not found: {csv_path}")

    # Try reading with pandas first; fall back to csv module for unusual encodings
    try:
        df = pd.read_csv(
            csv_path,
            encoding="utf-8",
            dtype=str,
        )
    except UnicodeDecodeError:
        df = pd.read_csv(
            csv_path,
            encoding="latin-1",
            dtype=str,
        )

    # Normalise column names (strip whitespace, lower)
    df.columns = [c.strip() for c in df.columns]

    # Map various column name spellings to canonical names
    col_map = {}
    for col in df.columns:
        cl = col.lower().replace(" ", "_")
        if cl in ("order_id", "bestellnummer"):
            col_map[col] = "order_id"
        elif cl in ("order_date", "bestelldatum"):
            col_map[col] = "order_date"
        elif cl in ("product_name", "produktname", "title", "item", "items"):
            col_map[col] = "product_name"
        elif cl in ("unit_price", "einzelpreis"):
            col_map[col] = "unit_price"
        elif cl in ("total_amount", "gesamtbetrag"):
            col_map[col] = "total_amount"
    df = df.rename(columns=col_map)

    if "order_id" not in df.columns:
        raise ValueError(
            "Amazon CSV must have an 'Order ID' column. "
            f"Found columns: {list(df.columns)}"
        )

    if "product_name" not in df.columns:
        # Some exports use 'Title' or put products in a different column
        raise ValueError(
            "Amazon CSV must have a 'Product Name' (or similar) column. "
            f"Found columns: {list(df.columns)}"
        )

    # Optional month filter on order_date
    if month_filter and "order_date" in df.columns:
        df["order_date"] = pd.to_datetime(df["order_date"], errors="coerce", dayfirst=False)
        # Try dayfirst=True as well for European date formats
        mask = df["order_date"].isna()
        if mask.any():
            df.loc[mask, "order_date"] = pd.to_datetime(
                df.loc[mask, df.columns[df.columns.get_loc("order_date")]],
                errors="coerce",
                dayfirst=True,
            )
        year, mo = month_filter.split("-")
        df = df[
            (df["order_date"].dt.year == int(year)) &
            (df["order_date"].dt.month == int(mo))
        ]

    # Build lookup dict
    lookup: dict[str, list[str]] = {}
    for _, row in df.iterrows():
        oid = str(row.get("order_id", "")).strip()
        if not oid:
            continue
        product = str(row.get("product_name", "")).strip()
        if not product or product.lower() in ("nan", ""):
            continue
        if oid not in lookup:
            lookup[oid] = []
        if product not in lookup[oid]:
            lookup[oid].append(product)

    return lookup

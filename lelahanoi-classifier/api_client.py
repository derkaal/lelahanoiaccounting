"""
Claude API client for batch-classifying ambiguous bank transactions.

Sends transactions in batches of up to 20 to the Claude API with full context,
including resolved Amazon product names where available.
"""

import json
import os
import time
from typing import Optional

import anthropic

BATCH_SIZE = 20

SYSTEM_PROMPT = """You are an expert accountant and tax advisor classifying bank transactions for Le La Hanoi, a Vietnamese café in Landshut, Germany (opened February 2026).

The account is the personal Comdirect bank account of owner Andreas Donath. During the café setup and early operations, café expenses were paid from this personal account alongside normal private expenses. Your job is to determine whether each transaction is a café business expense, a personal expense, a transfer, or needs manual review.

Key context:
- Home address: Einberg 49, Buch am Erlbach (private)
- Café address: Altstadt 369, Landshut (business)
- The café sells Vietnamese food and drinks
- Andreas Donath also works at Capgemini (salary payments appear here)

Classification values:
  cafe_expense   — clearly a café business expense
  personal       — clearly a private/personal expense
  transfer       — internal money movement (no P&L impact)
  salary         — salary income from Capgemini
  needs_review   — ambiguous, requires owner to manually verify
  ignore         — bank fees, internal settlements, irrelevant

Expense category values (use ONLY these):
  exp_miete          — rent
  exp_wareneinkauf   — food/beverage purchasing
  exp_energie        — electricity, gas, utilities
  exp_bankgebuehr    — bank/payment fees
  exp_marketing      — advertising, music, promotions
  exp_buerobedarf    — office supplies, software, POS systems
  exp_versicherung   — business insurance
  exp_instandhaltung — repairs and maintenance
  asset_gwg          — low-value assets (GWG) < €800 net
  exp_bewirtung      — business hospitality
  transfer           — for transfer classification
  salary             — for salary classification
  personal           — for personal classification
  ignore             — for ignore classification

Classification guidelines:
- EDEKA / KAUFLAND / LIDL / REWE: needs_review (could be café ingredient run or home shopping — flag it)
- E.ON / BAYERNWERK electricity: needs_review — flag which address (home vs café)
- Deutsche Glasfaser / Vodafone / Telekom internet: needs_review — flag home vs café
- AMAZON with product names provided: classify based on products
    * café supplies (cookware, commercial kitchen items, café equipment, cleaning supplies for business) → cafe_expense
    * personal items (clothing, books, toys, personal care) → personal
    * ambiguous items → needs_review
- Restaurants / dining out (La Anatolia, Bami House, Ristorante, McDonalds, etc.): personal unless explicitly for Bewirtung
- Fuel (ARAL, TopTank, Jet, Shell): needs_review — business travel or personal
- MUELLER drugstore: needs_review — café cleaning supplies or personal
- DM Drogerie Markt: needs_review
- MEDIA MARKT / Saturn: needs_review — could be café equipment (POS display, tablet, etc.)
- Butlers: needs_review — could be café decoration/tableware
- SENFTL GMBH: needs_review
- BAYERSTORFER: needs_review
- GALERIA Kaufhof: personal
- Cash withdrawals at ATM / Sparkasse Landshut: needs_review — possible café cash float
- EasyPark: needs_review — could be parking for café-related travel
- Google / G SUITE / Google Workspace: needs_review — could be café Google account
- AMAZON BUS* prefix: likely business Amazon purchase — classify based on products if known
- Any transaction at Altstadt 369 or "Landshut" context: lean toward cafe_expense
- Any transaction at Einberg or "Buch am Erlbach" context: lean toward personal

For confidence:
  1.0 = absolutely certain
  0.9 = very likely
  0.7 = probable
  0.5 = uncertain
  0.3 = mostly guessing

Set needs_receipt=true if the transaction would require a receipt for tax purposes.
"""

USER_PROMPT_TEMPLATE = """Classify the following {count} bank transactions. Return a JSON array with exactly {count} objects in the same order as the input.

Each object must have these fields:
  "classification": one of [cafe_expense, personal, transfer, salary, needs_review, ignore]
  "category": one of [exp_miete, exp_wareneinkauf, exp_energie, exp_bankgebuehr, exp_marketing, exp_buerobedarf, exp_versicherung, exp_instandhaltung, asset_gwg, exp_bewirtung, transfer, salary, personal, ignore]
  "confidence": float 0.0-1.0
  "reason": brief explanation in English (max 100 chars)
  "needs_receipt": boolean
  "flag_note": string (empty string if none, otherwise a note for the accountant in German)

Transactions:
{transactions_json}

Return ONLY a valid JSON array, no other text."""


def _build_transaction_entry(tx: dict) -> dict:
    """Build a compact dict for the API prompt."""
    entry = {
        "date": tx.get("date", ""),
        "vendor": tx.get("vendor", ""),
        "buchungstext": tx.get("buchungstext", "")[:300],  # truncate very long texts
        "amount_eur": tx.get("amount_eur", 0.0),
    }
    if tx.get("order_id"):
        entry["amazon_order_id"] = tx["order_id"]
    if tx.get("product_names"):
        entry["amazon_products"] = tx["product_names"]
    return entry


def _parse_api_response(content: str, expected_count: int) -> list[dict]:
    """
    Parse JSON array from Claude's response.
    Handles cases where the response is wrapped in markdown code fences.
    """
    # Strip markdown code fences if present
    text = content.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first and last line (``` markers)
        inner = []
        in_block = False
        for line in lines:
            if line.startswith("```") and not in_block:
                in_block = True
                continue
            elif line.startswith("```") and in_block:
                break
            elif in_block:
                inner.append(line)
        text = "\n".join(inner)

    results = json.loads(text)

    if not isinstance(results, list):
        raise ValueError(f"Expected JSON array, got {type(results)}")

    if len(results) != expected_count:
        raise ValueError(
            f"Expected {expected_count} results, got {len(results)}"
        )

    return results


def _validate_result(r: dict) -> dict:
    """Ensure required fields exist and have valid values."""
    valid_classifications = {
        "cafe_expense", "personal", "transfer", "salary",
        "needs_review", "ignore"
    }
    valid_categories = {
        "exp_miete", "exp_wareneinkauf", "exp_energie", "exp_bankgebuehr",
        "exp_marketing", "exp_buerobedarf", "exp_versicherung",
        "exp_instandhaltung", "asset_gwg", "exp_bewirtung", "transfer",
        "salary", "personal", "ignore"
    }

    classification = r.get("classification", "needs_review")
    if classification not in valid_classifications:
        classification = "needs_review"

    category = r.get("category", "personal")
    if category not in valid_categories:
        category = "personal"

    confidence = float(r.get("confidence", 0.5))
    confidence = max(0.0, min(1.0, confidence))

    needs_receipt = bool(r.get("needs_receipt", False))
    # Low confidence always requires review
    if confidence < 0.7:
        needs_receipt = True

    return {
        "classification": classification,
        "category": category,
        "confidence": confidence,
        "reason": str(r.get("reason", ""))[:200],
        "needs_receipt": needs_receipt,
        "flag_note": str(r.get("flag_note", "")),
    }


def classify_batch(
    transactions: list[dict],
    model: str = "claude-sonnet-4-5",
    api_key: Optional[str] = None,
    max_retries: int = 3,
) -> list[dict]:
    """
    Classify a batch of transactions using the Claude API.

    transactions: list of dicts with keys:
      date, vendor, buchungstext, amount_eur,
      order_id (optional), product_names (optional list of str)

    Returns a list of classification dicts in the same order.
    """
    if not transactions:
        return []

    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise EnvironmentError(
            "ANTHROPIC_API_KEY not set. "
            "Set the environment variable or pass api_key= to classify_batch()."
        )

    client = anthropic.Anthropic(api_key=key)

    tx_entries = [_build_transaction_entry(tx) for tx in transactions]
    tx_json = json.dumps(tx_entries, ensure_ascii=False, indent=2)

    prompt = USER_PROMPT_TEMPLATE.format(
        count=len(transactions),
        transactions_json=tx_json,
    )

    last_error = None
    for attempt in range(max_retries):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            raw_content = response.content[0].text
            results = _parse_api_response(raw_content, len(transactions))
            return [_validate_result(r) for r in results]

        except (json.JSONDecodeError, ValueError, IndexError) as e:
            last_error = e
            wait = 2 ** attempt
            print(f"  [api] Attempt {attempt + 1} failed ({e}), retrying in {wait}s...")
            time.sleep(wait)

        except anthropic.APIStatusError as e:
            last_error = e
            if e.status_code in (429, 529):  # rate limit / overload
                wait = 2 ** (attempt + 1)
                print(f"  [api] Rate limited, waiting {wait}s...")
                time.sleep(wait)
            else:
                raise

    # If all retries failed, return needs_review for all transactions
    print(f"  [api] All retries failed: {last_error}. Marking batch as needs_review.")
    return [
        {
            "classification": "needs_review",
            "category": "personal",
            "confidence": 0.0,
            "reason": f"API classification failed: {last_error}",
            "needs_receipt": True,
            "flag_note": "API-Klassifizierung fehlgeschlagen — manuell prüfen",
        }
        for _ in transactions
    ]


def classify_transactions(
    transactions: list[dict],
    model: str = "claude-sonnet-4-5",
    api_key: Optional[str] = None,
    batch_size: int = BATCH_SIZE,
    verbose: bool = False,
) -> list[dict]:
    """
    Classify a list of transactions in batches.

    Returns results in the same order as the input list.
    """
    results = []
    total = len(transactions)

    for i in range(0, total, batch_size):
        batch = transactions[i: i + batch_size]
        batch_num = i // batch_size + 1
        total_batches = (total + batch_size - 1) // batch_size

        if verbose:
            print(
                f"  [api] Classifying batch {batch_num}/{total_batches} "
                f"({len(batch)} transactions)..."
            )

        batch_results = classify_batch(batch, model=model, api_key=api_key)
        results.extend(batch_results)

    return results

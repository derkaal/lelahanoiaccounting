"""
Deterministic classification rules for Le La Hanoi bank statement classifier.

Rules are applied in order. The first matching rule wins.
Each rule returns a dict with keys:
  classification, category, confidence, needs_receipt, flag_note
"""

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RuleResult:
    classification: str
    category: str
    confidence: float
    needs_receipt: bool = False
    flag_note: str = ""
    matched_rule: str = ""


# ---------------------------------------------------------------------------
# PayPal sub-vendor helpers
# ---------------------------------------------------------------------------

PAYPAL_PERSONAL = [
    ("Apple", "personal"),
    ("Apple Services", "personal"),
    ("Netflix", "personal"),
    ("Disney", "personal"),
    ("DisneyPlus", "personal"),
    ("Roblox", "personal"),
    ("BestSecret", "personal"),
    ("Best Secret", "personal"),
    ("INFINITE STYLES", "personal"),
    ("Lifestyle Brands", "personal"),
    ("YSSKINCARE", "personal"),
    ("Xandrie", "personal"),
    ("qobuz", "personal"),
    ("GALERIA", "personal"),
]

PAYPAL_IGNORE = [
    ("mc-eur-plux-issuing", "ignore"),
    ("MCEUR", "ignore"),
]

PAYPAL_NEEDS_REVIEW = [
    ("Google", "exp_buerobedarf"),
]


def _classify_paypal(buchungstext: str) -> Optional[RuleResult]:
    """Inspect PayPal sub-vendor from Buchungstext."""
    upper = buchungstext.upper()

    for keyword, _ in PAYPAL_IGNORE:
        if keyword.upper() in upper:
            return RuleResult(
                classification="ignore",
                category="ignore",
                confidence=1.0,
                matched_rule=f"PayPal / {keyword}",
            )

    for keyword, _ in PAYPAL_PERSONAL:
        if keyword.upper() in upper:
            return RuleResult(
                classification="personal",
                category="personal",
                confidence=1.0,
                matched_rule=f"PayPal / {keyword}",
            )

    for keyword, category in PAYPAL_NEEDS_REVIEW:
        if keyword.upper() in upper:
            return RuleResult(
                classification="needs_review",
                category=category,
                confidence=0.5,
                flag_note="Könnte Google Workspace (Café) oder privat sein — prüfen",
                matched_rule=f"PayPal / {keyword}",
            )

    # Generic PayPal — fall through to API
    return None


# ---------------------------------------------------------------------------
# Main rule engine
# ---------------------------------------------------------------------------

# Each entry: (match_fn, RuleResult factory)
# match_fn receives (vendor_upper: str, buchungstext_upper: str, amount: float)
# and returns True/False.

def _contains(text: str, *keywords: str) -> bool:
    return any(kw.upper() in text for kw in keywords)


def apply_rules(
    vendor: str,
    buchungstext: str,
    amount: float,
) -> Optional[RuleResult]:
    """
    Apply deterministic rules to a transaction.

    Returns a RuleResult if a rule matched, else None (→ send to API).
    """
    v = vendor.upper()
    b = buchungstext.upper()

    # ------------------------------------------------------------------
    # RENT
    # ------------------------------------------------------------------
    if _contains(v, "MANFRED MICHL") or _contains(b, "MANFRED MICHL"):
        if amount < 0:
            return RuleResult("cafe_expense", "exp_miete", 1.0, matched_rule="MANFRED MICHL (outgoing)")
        # Incoming +2409.75 — reclassification as transfer (deposit refund etc.)
        if _contains(v, "MICHL MANFRED") or _contains(b, "MICHL MANFRED"):
            return RuleResult("transfer", "transfer", 1.0, matched_rule="MICHL MANFRED (incoming)")

    if _contains(v, "MICHL MANFRED") or _contains(b, "MICHL MANFRED"):
        if amount > 0:
            return RuleResult("transfer", "transfer", 1.0, matched_rule="MICHL MANFRED (incoming)")

    # ------------------------------------------------------------------
    # POS SYSTEM
    # ------------------------------------------------------------------
    if _contains(v, "READY2ORDER") or _contains(b, "READY2ORDER"):
        return RuleResult("cafe_expense", "exp_buerobedarf", 1.0, matched_rule="ready2order GmbH")

    # ------------------------------------------------------------------
    # SALARY
    # ------------------------------------------------------------------
    if _contains(v, "CAPGEMINI") or _contains(b, "CAPGEMINI DEUTSCHLAND"):
        return RuleResult("salary", "salary", 1.0, matched_rule="CAPGEMINI DEUTSCHLAND")

    # ------------------------------------------------------------------
    # FOOD / WARENEINKAUF
    # ------------------------------------------------------------------
    # Plastic cups, straws, and café inventory items (not office supplies)
    if _contains(b, "STROHHALM") or _contains(b, "PLASTIKBECHER") or _contains(b, "BECHER") or \
       _contains(b, "CUPS") or _contains(b, "STRAWS") or _contains(b, "EUPAKO"):
        return RuleResult("cafe_expense", "exp_wareneinkauf", 1.0,
                         matched_rule="Plastic cups/straws/café inventory")

    if _contains(v, "TAFELMAIER") or _contains(b, "TAFELMAIER"):
        return RuleResult("cafe_expense", "exp_wareneinkauf", 1.0, matched_rule="Baeckerei Johann Tafelmaier")

    if _contains(v, "METRO SAGT DANKE") or _contains(b, "METRO SAGT DANKE") or _contains(v, "METRO") and _contains(b, "METRO"):
        return RuleResult(
            "cafe_expense", "exp_wareneinkauf", 0.9,
            needs_receipt=True,
            matched_rule="METRO SAGT DANKE",
        )

    if _contains(v, "DELI TADKA") or _contains(b, "DELI TADKA"):
        return RuleResult("cafe_expense", "exp_wareneinkauf", 0.9, matched_rule="DELI TADKA GMBH")

    # ------------------------------------------------------------------
    # MARKETING / MUSIC
    # ------------------------------------------------------------------
    if _contains(v, "GEMA") or _contains(b, "GEMA"):
        return RuleResult("cafe_expense", "exp_marketing", 1.0, matched_rule="GEMA")

    # ------------------------------------------------------------------
    # BANK FEES
    # ------------------------------------------------------------------
    if _contains(v, "SUMUP") or _contains(b, "SUMUP") or _contains(b, "SUM UP"):
        if _contains(v, "LE LA HANOI") or _contains(b, "LE LA HANOI") or _contains(b, "LELAHANO"):
            return RuleResult("cafe_expense", "exp_bankgebuehr", 1.0, matched_rule="SumUp *Le La Hanoi")

    if _contains(b, "KONTOFÜHRUNGSENTGELT") or _contains(b, "KONTOFUHRUNGSENTGELT") or _contains(v, "KONTOFÜHRUNGSENTGELT"):
        return RuleResult("ignore", "ignore", 1.0, matched_rule="Kontoführungsentgelt")

    # comdirect visa settlement
    if (_contains(v, "COMDIRECT") or _contains(b, "COMDIRECT")) and (
        _contains(b, "VISA") or _contains(b, "KREDITKARTE") or _contains(b, "AUSGLEICH")
    ):
        return RuleResult("ignore", "ignore", 1.0, matched_rule="Comdirect visa settlement")

    # ------------------------------------------------------------------
    # GOVERNMENT / IGNORE
    # ------------------------------------------------------------------
    if _contains(v, "BUNDESAGENTUR") or _contains(b, "BUNDESAGENTUR") or _contains(b, "FAMILIENKASSE"):
        return RuleResult("ignore", "ignore", 1.0, matched_rule="Bundesagentur Familienkasse")

    # ------------------------------------------------------------------
    # PERSONAL — insurance / finance
    # ------------------------------------------------------------------
    if _contains(v, "LVM VERSICHERUNG") or _contains(b, "LVM VERSICHERUNG"):
        return RuleResult("personal", "personal", 1.0, matched_rule="LVM VERSICHERUNG")

    if _contains(v, "ALTE LEIPZIGER") or _contains(b, "ALTE LEIPZIGER"):
        return RuleResult("personal", "personal", 1.0, matched_rule="Alte Leipziger")

    if _contains(v, "LEBENSVERS") or _contains(b, "LEBENSVERS"):
        return RuleResult("personal", "personal", 1.0, matched_rule="LEBENSVERS.VON 1871")

    if _contains(v, "MERCEDES-BENZ BANK") or _contains(b, "MERCEDES-BENZ BANK") or _contains(v, "MERCEDES BENZ BANK"):
        return RuleResult("personal", "personal", 1.0, matched_rule="Mercedes-Benz Bank")

    if _contains(v, "SANTANDER") or _contains(b, "SANTANDER CONSUMER"):
        return RuleResult("personal", "personal", 1.0, matched_rule="SANTANDER CONSUMER BANK")

    if _contains(v, "MARCO ALTINGER") or _contains(b, "MARCO ALTINGER") or (
        _contains(b, "ALTINGER") and _contains(b, "KARATE")
    ):
        return RuleResult("personal", "personal", 1.0, matched_rule="MARCO ALTINGER / Karate")

    if _contains(v, "RUNDFUNK") or _contains(b, "RUNDFUNK ARD ZDF") or _contains(b, "ARD ZDF"):
        return RuleResult("personal", "personal", 1.0, matched_rule="Rundfunk ARD ZDF")

    if _contains(v, "GEMEINDE BUCH") or _contains(b, "GEMEINDE BUCH AM ERLBACH"):
        return RuleResult("personal", "personal", 1.0, matched_rule="Gemeinde Buch am Erlbach")

    # Fuel station
    if _contains(v, "SENFTL") or _contains(b, "SENFTL"):
        return RuleResult("personal", "personal", 1.0, matched_rule="SENFTL GmbH (fuel)")

    # Clothing/fashion retailer (non-PayPal transactions)
    if _contains(v, "BEST SECRET") or _contains(b, "BEST SECRET") or \
       _contains(v, "BESTSECRET") or _contains(b, "BESTSECRET"):
        return RuleResult("personal", "personal", 1.0, matched_rule="Best Secret")

    # Parking fees
    if _contains(v, "EASYPARK") or _contains(b, "EASYPARK"):
        return RuleResult("personal", "personal", 1.0, matched_rule="EasyPark")

    # ------------------------------------------------------------------
    # TRANSFERS
    # ------------------------------------------------------------------
    if _contains(b, "ANDREAS DONATH") and amount > 0:
        return RuleResult("transfer", "transfer", 1.0, matched_rule="TO: Andreas Donath")

    if (_contains(v, "ANDREAS DONATH") or _contains(b, "ANDREAS DONATH")) and (
        _contains(b, "HUONG TRA") or _contains(b, "DONATHPHAM") or _contains(b, "DONATH-PHAM")
    ):
        return RuleResult("transfer", "transfer", 1.0, matched_rule="Andreas Donath Huong Tra DonathPham")

    # ------------------------------------------------------------------
    # DIY / HARDWARE STORES — needs_review
    # ------------------------------------------------------------------
    if _contains(v, "OBI") or _contains(b, "OBI GMBH") or re.search(r'\bOBI\b', b):
        return RuleResult(
            "needs_review", "exp_instandhaltung", 0.4,
            flag_note="Abgrenzung Herstellungskosten/Instandhaltung prüfen — Belege erforderlich",
            matched_rule="OBI",
        )

    if _contains(v, "BAUHAUS") or _contains(b, "BAUHAUS"):
        return RuleResult(
            "needs_review", "exp_instandhaltung", 0.4,
            flag_note="Abgrenzung Herstellungskosten/Instandhaltung prüfen — Belege erforderlich",
            matched_rule="BAUHAUS",
        )

    if _contains(v, "HAGEBAU") or _contains(b, "HAGEBAU"):
        return RuleResult(
            "needs_review", "exp_instandhaltung", 0.4,
            flag_note="Abgrenzung Herstellungskosten/Instandhaltung prüfen — Belege erforderlich",
            matched_rule="HAGEBAU",
        )

    # ------------------------------------------------------------------
    # PAYPAL — inspect sub-vendor
    # ------------------------------------------------------------------
    if _contains(v, "PAYPAL") or _contains(b, "PAYPAL"):
        result = _classify_paypal(buchungstext)
        if result:
            return result
        # Unknown PayPal vendor → fall through to API

    # ------------------------------------------------------------------
    # No rule matched
    # ------------------------------------------------------------------
    return None

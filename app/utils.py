"""Parsing / normalisation helpers (amounts, dates, identifiers, fuzzy text)."""
from __future__ import annotations

import re
from datetime import date, datetime
from difflib import SequenceMatcher
from typing import Optional

AMOUNT_RE = re.compile(r"^-?(?:\d{1,3}(?:,\d{2,3})+|\d+)(?:\.\d+)?$")
GSTIN_RE = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]\b")
IFSC_RE = re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b")
_GST_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

DATE_FORMATS = (
    "%Y-%m-%d", "%d-%b-%Y", "%d-%B-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d-%m-%Y",
    "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%d/%m/%y", "%d-%b-%y",
)
DATE_TOKEN = r"(\d{4}-\d{2}-\d{2}|\d{1,2}[-/. ](?:\d{1,2}|[A-Za-z]{3,9})[-/. ,]+\d{2,4}|[A-Za-z]{3,9} \d{1,2}, \d{4})"


def parse_amount(token: str | None) -> Optional[float]:
    if token is None:
        return None
    t = str(token).strip()
    for prefix in ("INR", "Rs.", "Rs", "₹"):
        if t.startswith(prefix):
            t = t[len(prefix):].strip()
    t = t.rstrip("/-").strip()
    if not AMOUNT_RE.match(t):
        return None
    try:
        return round(float(t.replace(",", "")), 2)
    except ValueError:
        return None


def parse_date(value: str | None) -> Optional[date]:
    """Parse the date formats seen on Indian invoices. Numeric dates are dd/mm (Indian convention)."""
    if not value:
        return None
    v = re.sub(r"\s+", " ", str(value).strip().rstrip("."))
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return None


def gstin_checksum_ok(gstin: str | None) -> bool:
    if not gstin or not GSTIN_RE.fullmatch(gstin):
        return False
    total = 0
    for i, ch in enumerate(gstin[:14]):
        p = _GST_CHARS.index(ch) * (1 if i % 2 == 0 else 2)
        total += p // 36 + p % 36
    return _GST_CHARS[(36 - total % 36) % 36] == gstin[14]



# OCR mix-ups between look-alike characters. A GSTIN has fixed digit/letter
# positions and a check digit, so a misread can usually be put right safely.
_TO_DIGIT = {"O": "0", "D": "0", "Q": "0", "I": "1", "L": "1", "Z": "2", "S": "5", "B": "8", "G": "6"}
_TO_LETTER = {v: k for k, v in reversed(list(_TO_DIGIT.items()))}
_DIGIT_POS = {0, 1, 7, 8, 9, 10}
_LETTER_POS = {2, 3, 4, 5, 6, 11}
_LOOSE_GSTIN = re.compile(r"[0-9A-Z]{15}")


def repair_gstin(candidate: str) -> Optional[str]:
    """Return a checksum-valid GSTIN for an OCR'd 15-character token, or None."""
    if gstin_checksum_ok(candidate):
        return candidate
    chars = list(candidate)
    for i in _DIGIT_POS:
        chars[i] = _TO_DIGIT.get(chars[i], chars[i])
    for i in _LETTER_POS:
        chars[i] = _TO_LETTER.get(chars[i], chars[i])
    if chars[13] == "2":
        chars[13] = "Z"
    # positions 12 and 14 can be a letter or a digit, so try both readings
    swap = {**_TO_DIGIT, **_TO_LETTER}
    for a in {chars[12], swap.get(chars[12], chars[12])}:
        for b in {chars[14], swap.get(chars[14], chars[14])}:
            g = "".join(chars[:12] + [a, chars[13], b])
            if gstin_checksum_ok(g):
                return g
    return None


def repair_gstin_tokens(tokens: list[str]) -> list[str]:
    out = []
    for t in tokens:
        m = _LOOSE_GSTIN.search(t)
        if m and not GSTIN_RE.fullmatch(m.group(0)) or (m and not gstin_checksum_ok(m.group(0))):
            fixed = repair_gstin(m.group(0))
            if fixed:
                t = t[:m.start()] + fixed + t[m.end():]
        out.append(t)
    return out

def norm_doc_number(value: str | None) -> str:
    """Canonical form of an invoice number so cosmetic re-formatting can't hide a duplicate.

    'SPI/26-27/0457', 'SPI-26-27-457' and 'spi 26 27 457' all -> 'SPI|26|27|457'
    """
    if not value:
        return ""
    parts = re.split(r"[^A-Za-z0-9]+", value.upper())
    out = []
    for p in parts:
        if not p:
            continue
        # split letter/digit boundaries: INV00457 -> INV, 457
        for chunk in re.findall(r"[A-Z]+|\d+", p):
            out.append(str(int(chunk)) if chunk.isdigit() else chunk)
    return "|".join(out)


def norm_po(value: str | None) -> str:
    if not value:
        return ""
    v = value.upper().replace("P0", "PO")  # common OCR confusion
    return re.sub(r"[^A-Z0-9]", "", v)


_COMPANY_SUFFIXES = re.compile(
    r"\b(private|pvt|limited|ltd|llp|inc|co|company|corp|corporation|the|and|&)\b\.?", re.I
)


def norm_company(name: str | None) -> str:
    if not name:
        return ""
    n = _COMPANY_SUFFIXES.sub(" ", name.lower())
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def similarity(a: str | None, b: str | None) -> float:
    """Blend of token overlap and character similarity, 0..1."""
    a_n = re.sub(r"[^a-z0-9 ]", " ", (a or "").lower())
    b_n = re.sub(r"[^a-z0-9 ]", " ", (b or "").lower())
    ta, tb = set(a_n.split()), set(b_n.split())
    if not ta or not tb:
        return 0.0
    jacc = len(ta & tb) / len(ta | tb)
    seq = SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio()
    contain = len(ta & tb) / min(len(ta), len(tb))
    return round(0.4 * jacc + 0.3 * seq + 0.3 * contain, 3)


def money(x: float | None) -> str:
    if x is None:
        return "—"
    neg = x < 0
    whole, frac = f"{abs(x):.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"{'-' if neg else ''}₹{whole}.{frac}"


def close(a: float | None, b: float | None, tol: float) -> bool:
    return a is not None and b is not None and abs(a - b) <= tol

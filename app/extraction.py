"""Document intake and extraction.

Everything runs locally, with no API key and no network:

* Digital PDFs: words and their positions come from the PDF text layer.
* Scanned PDFs: words and their positions come from local OCR (see ocr.py).

Both feed the same layout-agnostic parser (`_parse`), which fills the
`InvoiceExtraction` contract that the checks run on.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from itertools import combinations
from typing import Any, Optional

import fitz  # PyMuPDF

from .config import BUYER
from .models import InvoiceExtraction, LineItem
from .ocr import OCRError, ocr_pdf
from .utils import DATE_TOKEN, GSTIN_RE, IFSC_RE, parse_amount, parse_date, repair_gstin_tokens


class ExtractionError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Intake
# --------------------------------------------------------------------------- #
@dataclass
class DocInfo:
    sha256: str
    pages: int
    text_chars: int
    has_text_layer: bool
    image_count: int
    size_kb: float
    kind: str  # "digital" | "scanned" | "hybrid"


def inspect_pdf(data: bytes) -> DocInfo:
    if not data.startswith(b"%PDF"):
        raise ExtractionError("File is not a PDF (missing %PDF header).")
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as e:  # corrupt PDF
        raise ExtractionError(f"PDF could not be opened: {e}") from e
    if doc.page_count == 0:
        raise ExtractionError("PDF has no pages.")
    chars = sum(len(p.get_text().strip()) for p in doc)
    images = sum(len(p.get_images()) for p in doc)
    has_text = chars >= 40 * doc.page_count
    kind = "digital" if has_text and images == 0 else ("hybrid" if has_text else "scanned")
    return DocInfo(
        sha256=hashlib.sha256(data).hexdigest(), pages=doc.page_count, text_chars=chars,
        has_text_layer=has_text, image_count=images, size_kb=round(len(data) / 1024, 1), kind=kind,
    )


# --------------------------------------------------------------------------- #
# Rules-based extractor
# --------------------------------------------------------------------------- #
Row = list[tuple[float, str]]
Span = tuple[str, float]   # (text, size) - used to find the biggest text on page 1


def _rows_from_pdf(doc: fitz.Document) -> list[Row]:
    """Group words into visual rows (same baseline), left-to-right."""
    rows: list[Row] = []
    for page in doc:
        words = page.get_text("words")  # x0, y0, x1, y1, word, block, line, wno
        words.sort(key=lambda w: (round(w[3], 0), w[0]))
        current: list[tuple[float, float, str]] = []
        for w in words:
            if current and abs(w[3] - current[-1][1]) > 3.0:
                rows.append([(x, t) for x, _, t in sorted(current)])
                current = []
            current.append((w[0], w[3], w[4]))
        if current:
            rows.append([(x, t) for x, _, t in sorted(current)])
    return rows


def _spans_from_pdf(doc: fitz.Document) -> list[Span]:
    return [(span["text"].strip(), span["size"])
            for block in doc[0].get_text("dict")["blocks"]
            for line in block.get("lines", []) for span in line["spans"]]


def _rows_from_ocr(pages) -> list[Row]:
    """Same idea for OCR words. Rows are grouped by (deskewed) vertical centre."""
    rows: list[Row] = []
    for words in pages:
        if not words:
            continue
        tol = 0.45 * sorted(w.height for w in words)[len(words) // 2]
        words = sorted(words, key=lambda w: (w.y, w.x0))
        current = [words[0]]
        for w in words[1:]:
            if abs(w.y - current[-1].y) > tol:
                rows.append([(c.x0, c.text) for c in sorted(current, key=lambda c: c.x0)])
                current = []
            current.append(w)
        rows.append([(c.x0, c.text) for c in sorted(current, key=lambda c: c.x0)])
    return rows


def _spans_from_ocr(pages) -> list[Span]:
    lines: dict[int, list] = {}
    for w in (pages[0] if pages else []):
        lines.setdefault(w.line, []).append(w)
    return [(" ".join(w.text for w in ws), ws[0].height) for ws in lines.values()]


_HEADING = re.compile(r"^(tax\s+)?invoice$|^bill$|^tax invoice", re.I)


def _vendor_name(spans: list[Span]) -> Optional[str]:
    """Supplier name = biggest text on page 1 that isn't the word 'INVOICE'."""
    best, best_size = None, 0.0
    for t, size in spans:
        if len(t) < 3 or _HEADING.match(t) or any(ch.isdigit() for ch in t[:3]):
            continue
        if size > best_size + 0.1:
            best, best_size = t, size
    if best and best.isupper():
        best = best.title().replace("Llp", "LLP")
    return best


def _first(patterns: list[str], text: str, flags=re.I) -> Optional[str]:
    for p in patterns:
        m = re.search(p, text, flags)
        if m:
            return m.group(1).strip()
    return None


def _row_amount(row_text: str) -> Optional[float]:
    toks = row_text.split()
    for t in reversed(toks):
        a = parse_amount(t)
        if a is not None:
            return a
    return None


def _line_item(tokens: list[str]) -> Optional[LineItem]:
    """A row is a line item if three of its numbers satisfy qty x rate = amount."""
    nums = [(i, parse_amount(t)) for i, t in enumerate(tokens)]
    nums = [(i, v) for i, v in nums if v is not None]
    if len(nums) < 3:
        return None
    last_i, amount = nums[-1]
    if amount <= 0:
        return None
    candidates = [(a, b) for a, b in combinations(nums[:-1], 2)]
    candidates.sort(key=lambda ab: -(ab[0][0] + ab[1][0]))  # prefer the columns nearest the amount
    for (qi, q), (ri, r) in candidates:
        if q > 0 and r > 0 and abs(q * r - amount) <= 0.05 + 0.0005 * amount:
            desc_tokens = tokens[:qi]
            hsn = None
            if desc_tokens and re.fullmatch(r"\d{4,8}", desc_tokens[-1]):
                hsn = desc_tokens.pop()
            if desc_tokens and re.fullmatch(r"\d{1,3}", desc_tokens[0]) and len(desc_tokens) > 1:
                desc_tokens = desc_tokens[1:]  # serial number column
            desc = " ".join(desc_tokens).strip(" -|")
            if not re.search(r"[A-Za-z]{3}", desc):
                return None
            return LineItem(description=desc, hsn_sac=hsn, quantity=q, unit_price=r, amount=amount)
    return None


def extract_with_rules(data: bytes) -> InvoiceExtraction:
    """Digital PDF: read the text layer."""
    doc = fitz.open(stream=data, filetype="pdf")
    return _parse(_rows_from_pdf(doc), _spans_from_pdf(doc))


def extract_with_ocr(data: bytes, sha256: str) -> tuple[InvoiceExtraction, dict[str, Any]]:
    """Scanned PDF: OCR the page images, then run the same parser."""
    try:
        pages, meta = ocr_pdf(data, sha256)
    except OCRError as e:
        raise ExtractionError(str(e)) from e
    except Exception as e:  # engine crash, bad image, etc.
        raise ExtractionError(f"OCR failed: {e}") from e
    if not any(pages):
        raise ExtractionError("OCR found no text on the page")
    rows = [[(x, t) for x, t in zip([x for x, _ in r], repair_gstin_tokens([t for _, t in r]))] for r in _rows_from_ocr(pages)]
    return _parse(rows, _spans_from_ocr(pages)), meta


def _parse(rows: list[Row], spans: list[Span]) -> InvoiceExtraction:
    row_texts = [" ".join(t for _, t in r) for r in rows]
    text = "\n".join(row_texts)

    gstins = GSTIN_RE.findall(text)
    vendor_gstin = next((g for g in gstins if g != BUYER.gstin), None)
    buyer_gstin = BUYER.gstin if BUYER.gstin in gstins else None

    inv_no = None
    for m in re.finditer(
        r"\b(?:tax\s+invoice|invoice|inv|bill)\.?\s*(?:no|number|num|#)\.?\s*[:#\-]?\s*([A-Z0-9][A-Z0-9/\-_.]*\d[A-Z0-9/\-_]*)",
        text, re.I,
    ):
        inv_no = m.group(1).rstrip(".")
        break

    inv_date = None
    for m in re.finditer(r"(?<![A-Za-z])(due\s+)?(?:invoice\s+date|inv\.?\s*date|bill\s+date|dated|date)\s*[:\-]?\s*" + DATE_TOKEN, text, re.I):
        if m.group(1):
            continue
        d = parse_date(m.group(2))
        if d:
            inv_date = d.isoformat()
            break
    due = _first([r"due\s+date\s*[:\-]?\s*" + DATE_TOKEN], text)
    due_d = parse_date(due) if due else None

    po = _first([
        r"(?:p\.?\s?o\.?|purchase\s+order|order)\s*(?:no\.?|number|#|ref(?:erence)?\.?)\s*[:#\-]?\s*([A-Z0-9][A-Z0-9/\-]*\d[A-Z0-9/\-]*)",
        r"\b(PO[-/]\d{4}[-/]\d{3,5})\b",
    ], text)

    bank = _first([r"(?:a/?c|account)\.?\s*(?:no|number|num)\.?\s*[:\-]?\s*(\d[\d ]{6,22}\d)"], text)
    ifsc_m = IFSC_RE.search(text)

    subtotal = cgst = sgst = igst = total = None
    for rt in row_texts:
        low = rt.lower()
        amt = _row_amount(rt)
        if amt is None:
            continue
        if re.match(r"^(taxable\s+(value|amount)|sub\s*-?\s*total)", low):
            subtotal = subtotal if subtotal is not None else amt
        elif low.startswith("cgst"):
            cgst = amt
        elif low.startswith("sgst") or low.startswith("utgst"):
            sgst = amt
        elif low.startswith("igst"):
            igst = amt
        elif re.match(r"^(grand\s+total|total\s+amount\s+payable|amount\s+payable|total\s+amount|invoice\s+total|total\s+due|balance\s+due|total)\b", low):
            if total is None or "grand" in low or "payable" in low:
                total = amt

    taxes = [t for t in (cgst, sgst, igst) if t is not None]
    tax_total = round(sum(taxes), 2) if taxes else None

    items: list[LineItem] = []
    for r in rows:
        li = _line_item([t for _, t in r])
        if li:
            items.append(li)

    incl = bool(re.search(
        r"inclusive\s+of\s+(gst|tax)|incl\.?\s*(of\s+)?(gst|tax)|(gst|tax)\s+included|@\s*\d+(\.\d+)?%\s*included", text, re.I))

    notes = []
    for rt in row_texts:
        if re.search(r"changed\s+our\s+bank|new\s+bank|update\s+(your\s+)?records|reminder|duplicate|copy", rt, re.I):
            notes.append(rt.strip())

    cur = "INR" if re.search(r"\b(INR|Rs\.?|Rupees)\b|₹", text) else None
    return InvoiceExtraction(
        vendor_name=_vendor_name(spans), vendor_gstin=vendor_gstin, buyer_name=BUYER.name if "halcyon" in text.lower() else None,
        buyer_gstin=buyer_gstin, invoice_number=inv_no, invoice_date=inv_date,
        due_date=due_d.isoformat() if due_d else None, po_reference=po, currency=cur, line_items=items,
        subtotal=subtotal, cgst=cgst, sgst=sgst, igst=igst, tax_total=tax_total, total=total,
        prices_include_tax=incl, bank_account_number=bank.replace(" ", "") if bank else None,
        bank_ifsc=ifsc_m.group(0) if ifsc_m else None, notes=notes,
    )

"""The invoice-to-decision process.

    intake -> extract -> validate -> vendor -> duplicates -> PO match -> 3-way match -> GST -> decide -> post

Each stage emits events (consumed live by the UI over SSE) and produces `Check`s.
A check can carry an *effect* (review / hold / reject); the decision engine is
just "the most severe effect wins", so every outcome traces back to named checks.

Reading the document (text layer or local OCR) is kept separate from judging it.
Every judgement about money is made by the plain, auditable code below.
"""
from __future__ import annotations

import json
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable, Optional

from . import db
from .config import BUYER, POLICY, UPLOADS_DIR, settings
from .decision import decide
from .extraction import DocInfo, ExtractionError, extract_with_ocr, extract_with_rules, inspect_pdf
from .models import Check, InvoiceExtraction
from .reference import POLine, PurchaseOrder, Vendor, ref
from .utils import gstin_checksum_ok, money, norm_doc_number, parse_date, similarity

STAGES = [
    ("intake", "Receive & fingerprint document"),
    ("extract", "Read invoice"),
    ("validate", "Validate completeness & arithmetic"),
    ("vendor", "Verify vendor"),
    ("duplicate", "Duplicate detection"),
    ("po_match", "Match purchase order"),
    ("three_way", "3-way match: PO / receipt / invoice"),
    ("tax", "GST compliance"),
    ("decision", "Decide & route"),
    ("post", "Post to ledger & notify"),
]

# run_id -> list of events (live buffer for SSE); persisted to SQLite when the run ends
LIVE: dict[str, list[dict]] = {}
_process_lock = threading.Lock()  # one invoice at a time: duplicate checks must see each other's postings


class Halt(Exception):
    """Stop the pipeline early; remaining stages are marked skipped."""


@dataclass
class Ctx:
    run_id: str
    filename: str
    data: bytes
    emit: Callable[[dict], None]
    info: Optional[DocInfo] = None
    inv: Optional[InvoiceExtraction] = None
    extractor: str = ""
    extraction_meta: dict = field(default_factory=dict)
    vendor: Optional[Vendor] = None
    po: Optional[PurchaseOrder] = None
    po_inferred: bool = False
    inv_date: Optional[date] = None
    net_subtotal: Optional[float] = None
    tax_total: Optional[float] = None
    matches: list[dict] = field(default_factory=list)  # invoice line -> PO line
    approvable_now: Optional[float] = None
    checks: list[Check] = field(default_factory=list)
    stage_results: dict[str, dict] = field(default_factory=dict)
    decision: Optional[dict] = None
    ledger_id: Optional[int] = None


# --------------------------------------------------------------------------- #
# Stage implementations. Each returns (summary, checks, data).
# --------------------------------------------------------------------------- #
def st_intake(c: Ctx):
    try:
        c.info = inspect_pdf(c.data)
    except ExtractionError as e:
        chk = Check("file_readable", "File is a readable PDF", "fail", str(e), "reject", "Vendor",
                    vendor_ask="Please resend the invoice as a valid PDF.")
        c.checks.append(chk)
        raise Halt(str(e))
    i = c.info
    kind_txt = {"digital": "Digital PDF with a machine-readable text layer",
                "hybrid": "PDF with text layer and embedded images",
                "scanned": "Scanned image - no text layer, needs vision/OCR"}[i.kind]
    checks = [
        Check("file_readable", "File is a readable PDF", "pass", f"{i.pages} page(s), {i.size_kb} KB"),
        Check("doc_kind", "Document type", "info", kind_txt, evidence={"text_chars": i.text_chars, "images": i.image_count}),
        Check("fingerprint", "Content fingerprint", "info", f"SHA-256 {i.sha256[:16]}...", evidence={"sha256": i.sha256}),
    ]
    return f"{kind_txt.split(' -')[0]} · {i.pages} page(s)", checks, {"doc": i.__dict__}


def st_extract(c: Ctx):
    checks: list[Check] = []
    if c.info.has_text_layer:
        c.inv = extract_with_rules(c.data)
        c.extractor = "text layer"
        checks.append(Check("extractor", "Extraction method", "info",
                            "Read from the PDF's text layer with the layout-agnostic parser"))
    else:
        try:
            c.inv, c.extraction_meta = extract_with_ocr(c.data, c.info.sha256)
        except ExtractionError as e:
            checks.append(Check("extractor", "Extraction method", "fail",
                                f"Cannot read the scan automatically: {e}", "review", "AP Data Entry"))
            c.checks.extend(checks)
            raise Halt("Document could not be read automatically - routed for manual keying")
        m = c.extraction_meta
        c.extractor = "OCR"
        took = "cached result" if m.get("cached") else f"{m['ms']} ms"
        checks.append(Check("extractor", "Extraction method", "info",
                            f"Scanned page read with local OCR ({took}, {m['lines']} text lines, "
                            f"average confidence {m['mean_confidence']:.0%})", evidence=m))
        if m["min_confidence"] < POLICY.ocr_min_confidence:
            checks.append(Check("ocr_confidence", "OCR confidence", "fail",
                                f"Some text was read with low confidence ({m['min_confidence']:.0%}, "
                                f"policy minimum {POLICY.ocr_min_confidence:.0%}). Check the figures against the PDF.",
                                "review", "AP Clerk", evidence=m))
        checks.append(Check("cross_check", "Checking the OCR reading", "info",
                            "No text layer to compare against, so the arithmetic checks in the next step "
                            "act as the check on what OCR read"))

    inv = c.inv
    key_fields = ["vendor_name", "invoice_number", "invoice_date", "total", "po_reference", "vendor_gstin"]
    found = [f for f in key_fields if getattr(inv, f) not in (None, "")]
    checks.append(Check("fields_found", "Key fields located", "info",
                        f"{len(found)}/{len(key_fields)} key fields, {len(inv.line_items)} line item(s)",
                        evidence={"missing": [f for f in key_fields if f not in found]}))
    for n in inv.notes:
        checks.append(Check("doc_note", "Noted on document", "info", n))
    summary = f"{'OCR' if c.extractor == 'OCR' else 'Text layer'} · {inv.vendor_name or 'unknown vendor'} · {inv.invoice_number or 'no invoice #'} · {money(inv.total)}"
    return summary, checks, {"invoice": inv.model_dump(), "extractor": c.extractor}


def st_validate(c: Ctx):
    inv, P = c.inv, POLICY
    checks: list[Check] = []
    missing = [lbl for f, lbl in (("invoice_number", "invoice number"), ("invoice_date", "invoice date"),
                                  ("total", "invoice total")) if getattr(inv, f) in (None, "")]
    if missing:
        checks.append(Check("mandatory_fields", "Mandatory fields present", "fail",
                            f"Missing {', '.join(missing)}. A GST tax invoice without these can't be booked or "
                            "used for input-tax credit.", "reject", "Vendor",
                            vendor_ask=f"Please issue a complete tax invoice showing the {', '.join(missing)}."))
    else:
        checks.append(Check("mandatory_fields", "Mandatory fields present", "pass",
                            "Invoice number, date and total are present"))
    if not inv.line_items:
        checks.append(Check("line_items", "Line items present", "fail",
                            "No itemised lines found - can't match quantities or prices to the PO", "review", "AP Clerk"))

    if inv.buyer_gstin and inv.buyer_gstin.upper() != BUYER.gstin:
        checks.append(Check("bill_to", "Billed to our entity", "fail",
                            f"Invoice is addressed to GSTIN {inv.buyer_gstin}, not ours ({BUYER.gstin})", "reject", "Vendor",
                            vendor_ask=f"The invoice is addressed to the wrong GSTIN. Please re-issue it to {BUYER.name}, GSTIN {BUYER.gstin}."))
    elif not inv.buyer_gstin:
        checks.append(Check("bill_to", "Billed to our entity", "warn",
                            "Our GSTIN is not printed on the invoice - input-tax credit may be disallowed"))
    else:
        checks.append(Check("bill_to", "Billed to our entity", "pass", f"Addressed to {BUYER.name} ({BUYER.gstin})"))

    # dates
    c.inv_date = parse_date(inv.invoice_date) if inv.invoice_date else None
    if inv.invoice_date and not c.inv_date:
        checks.append(Check("invoice_date", "Invoice date valid", "fail", f"Unparseable date '{inv.invoice_date}'",
                            "review", "AP Clerk"))
    elif c.inv_date:
        today = date.today()
        if c.inv_date > today:
            checks.append(Check("invoice_date", "Invoice date valid", "fail",
                                f"Invoice is dated in the future ({c.inv_date})", "review", "AP Clerk"))
        elif (today - c.inv_date).days > P.max_invoice_age_days:
            checks.append(Check("invoice_date", "Invoice date valid", "warn",
                                f"Invoice is {(today - c.inv_date).days} days old (> {P.max_invoice_age_days})",
                                "review", "AP Clerk"))
        else:
            checks.append(Check("invoice_date", "Invoice date valid", "pass", f"Dated {c.inv_date:%d %b %Y}"))

    # currency
    if inv.currency and inv.currency.upper() != "INR":
        checks.append(Check("currency", "Currency", "fail", f"Invoice currency {inv.currency} - expected INR",
                            "review", "AP Clerk"))

    # arithmetic
    tol = P.rounding_tolerance
    bad_lines = [li for li in inv.line_items
                 if None not in (li.quantity, li.unit_price, li.amount)
                 and abs(li.quantity * li.unit_price - li.amount) > tol]
    line_sum = round(sum(li.amount or 0 for li in inv.line_items), 2)
    taxes = [t for t in (inv.cgst, inv.sgst, inv.igst) if t is not None]
    tax = inv.tax_total if inv.tax_total is not None else (round(sum(taxes), 2) if taxes else None)
    issues = []
    if bad_lines:
        issues.append(f"{len(bad_lines)} line(s) where qty x rate != amount")
    if inv.prices_include_tax:
        if inv.total is not None and inv.line_items and abs(line_sum - inv.total) > tol:
            issues.append(f"lines sum to {money(line_sum)} but total is {money(inv.total)}")
        if inv.total is not None:
            c.net_subtotal = inv.subtotal if inv.subtotal is not None else None
            if c.net_subtotal is not None:
                c.tax_total = round(inv.total - c.net_subtotal, 2)
    else:
        sub = inv.subtotal if inv.subtotal is not None else (line_sum if inv.line_items else None)
        if inv.subtotal is not None and inv.line_items and abs(line_sum - inv.subtotal) > tol:
            issues.append(f"lines sum to {money(line_sum)} but subtotal is {money(inv.subtotal)}")
        if sub is not None and tax is not None and inv.total is not None and abs(sub + tax - inv.total) > tol:
            issues.append(f"subtotal {money(sub)} + tax {money(tax)} != total {money(inv.total)}")
        c.net_subtotal, c.tax_total = sub, tax
    if issues:
        checks.append(Check("arithmetic", "Invoice arithmetic reconciles", "fail", "; ".join(issues), "review",
                            "AP Clerk", evidence={"line_sum": line_sum, "subtotal": inv.subtotal, "tax": tax,
                                                  "total": inv.total}))
    elif inv.total is not None and inv.line_items:
        detail = (f"Tax-inclusive lines sum to total {money(inv.total)}" if inv.prices_include_tax else
                  f"Lines {money(line_sum)} + tax {money(tax)} = total {money(inv.total)}")
        checks.append(Check("arithmetic", "Invoice arithmetic reconciles", "pass", detail))
    worst = _worst(checks)
    return {"pass": "Document is complete and internally consistent"}.get(worst, f"{sum(ch.status == 'fail' for ch in checks)} problem(s) found"), checks, {
        "net_subtotal": c.net_subtotal, "tax_total": c.tax_total, "prices_include_tax": inv.prices_include_tax}


def _mask(acct: str | None) -> str:
    return f"XXXX{acct[-4:]}" if acct and len(acct) > 4 else (acct or "—")


def st_vendor(c: Ctx):
    inv, R = c.inv, ref()
    checks: list[Check] = []
    v = R.vendor_by_gstin(inv.vendor_gstin)
    how = "GSTIN"
    if v is None:
        v, score = R.vendor_by_name(inv.vendor_name)
        how = f"name similarity {score:.0%}"
        if score < 0.85:
            v = None
    if v is None:
        checks.append(Check("vendor_master", "Vendor in master data", "fail",
                            f"No approved vendor matches '{inv.vendor_name}' / GSTIN {inv.vendor_gstin or '—'}",
                            "review", "Vendor Management"))
        return "Vendor not found in master", checks, {}
    c.vendor = v
    checks.append(Check("vendor_master", "Vendor in master data", "pass" if how == "GSTIN" else "warn",
                        f"{v.legal_name} ({v.vendor_id}) matched by {how}",
                        evidence={"vendor_id": v.vendor_id}))
    if how != "GSTIN" and inv.vendor_gstin and inv.vendor_gstin != v.gstin:
        checks.append(Check("gstin_match", "GSTIN matches vendor master", "fail",
                            f"Invoice GSTIN {inv.vendor_gstin} differs from master {v.gstin} for {v.legal_name}",
                            "hold", "Vendor Management"))
    elif inv.vendor_gstin:
        name_sim = similarity(inv.vendor_name, v.legal_name) if inv.vendor_name else 1.0
        if how == "GSTIN" and inv.vendor_name and name_sim < 0.5:
            checks.append(Check("gstin_match", "GSTIN matches vendor name", "fail",
                                f"GSTIN belongs to {v.legal_name} but invoice is issued by '{inv.vendor_name}'",
                                "hold", "Vendor Management"))
        else:
            checks.append(Check("gstin_match", "GSTIN matches vendor master", "pass", f"{inv.vendor_gstin}"))
    if inv.vendor_gstin and not gstin_checksum_ok(inv.vendor_gstin):
        checks.append(Check("gstin_valid", "GSTIN format & checksum", "fail",
                            f"{inv.vendor_gstin} fails the GSTIN checksum", "hold", "Vendor Management"))
    elif inv.vendor_gstin:
        checks.append(Check("gstin_valid", "GSTIN format & checksum", "pass", "Valid structure and check digit"))

    if v.status == "BLOCKED":
        checks.append(Check("vendor_status", "Vendor is active", "fail", f"Vendor is BLOCKED: {v.status_reason}",
                            "reject", "Procurement",
                            vendor_ask="Your vendor account is currently suspended; we are unable to process invoices. "
                                       "Please contact our Procurement team."))
    elif v.status != "ACTIVE":
        checks.append(Check("vendor_status", "Vendor is active", "fail", f"Vendor status is {v.status}", "review",
                            "Procurement"))
    else:
        checks.append(Check("vendor_status", "Vendor is active", "pass", "ACTIVE"))

    # Bank details - payment diversion control
    if inv.bank_account_number:
        acct_ok = inv.bank_account_number.lstrip("0") == v.bank_account.lstrip("0")
        ifsc_ok = not inv.bank_ifsc or inv.bank_ifsc.upper() == v.bank_ifsc
        if acct_ok and ifsc_ok:
            checks.append(Check("bank_match", "Bank details match master", "pass",
                                f"Remit-to account {_mask(v.bank_account)} ({v.bank_name}) matches vendor master"))
        else:
            checks.append(Check(
                "bank_match", "Bank details match master", "fail",
                f"Invoice asks for payment to account {_mask(inv.bank_account_number)} / {inv.bank_ifsc or '—'}, "
                f"but master has {_mask(v.bank_account)} / {v.bank_ifsc} ({v.bank_name}). "
                "Unverified bank changes are the #1 invoice-fraud pattern.",
                "hold", "Treasury / Fraud Control",
                evidence={"invoice_account": _mask(inv.bank_account_number), "invoice_ifsc": inv.bank_ifsc,
                          "master_account": _mask(v.bank_account), "master_ifsc": v.bank_ifsc}))
    else:
        checks.append(Check("bank_match", "Bank details match master", "info",
                            f"No bank details on invoice - payment will go to master account {_mask(v.bank_account)}"))
    return f"{v.legal_name} · {v.vendor_id} · {v.status}", checks, {
        "vendor": {"vendor_id": v.vendor_id, "name": v.legal_name, "gstin": v.gstin, "status": v.status,
                   "terms_days": v.payment_terms_days, "bank": f"{v.bank_name} {_mask(v.bank_account)}"}}


def _net_price(li, pol: POLine, inclusive: bool) -> Optional[float]:
    if li.unit_price is None:
        return None
    return round(li.unit_price / (1 + pol.tax_rate), 2) if inclusive else li.unit_price


def _match_lines(c: Ctx, po: PurchaseOrder) -> tuple[list[dict], float]:
    """Assign each invoice line to its best PO line (description + price). Returns matches and mean score."""
    used: set[int] = set()
    out, scores = [], []
    for idx, li in enumerate(c.inv.line_items):
        best, best_s = None, 0.0
        for pol in po.lines:
            if pol.line_no in used:
                continue
            s = similarity(li.description, pol.description)
            net = _net_price(li, pol, c.inv.prices_include_tax)
            if net and pol.unit_price:
                s += 0.25 * max(0.0, 1 - abs(net - pol.unit_price) / pol.unit_price * 5)
            if s > best_s:
                best, best_s = pol, s
        if best is not None and best_s >= 0.45:
            used.add(best.line_no)
            out.append({"inv_idx": idx, "po_line": best.line_no, "score": round(min(best_s, 1.0), 2)})
            scores.append(min(best_s, 1.0))
        else:
            out.append({"inv_idx": idx, "po_line": None, "score": round(best_s, 2)})
            scores.append(0.0)
    return out, (sum(scores) / len(scores) if scores else 0.0)


def st_po_match(c: Ctx):
    inv, R, P = c.inv, ref(), POLICY
    checks: list[Check] = []
    po = R.po(inv.po_reference) if inv.po_reference else None
    candidates_view = []
    if inv.po_reference and po is None:
        checks.append(Check("po_exists", "Quoted PO exists", "warn",
                            f"PO '{inv.po_reference}' quoted on invoice was not found - trying to infer"))
    if po is not None:
        checks.append(Check("po_exists", "Quoted PO exists", "pass", f"{po.po_number} ({po.department}, raised by {po.requester})"))
    elif c.vendor is not None:
        # Infer: score every open PO of this vendor on line-item fit + amount fit.
        scored = []
        for cand in R.open_pos_for_vendor(c.vendor.vendor_id):
            _, line_score = _match_lines(c, cand)
            remaining = cand.total - _billed_value(cand)
            fit = 1.0 if c.net_subtotal is None or c.net_subtotal <= remaining * 1.02 + 1 else 0.3
            scored.append((round(0.75 * line_score + 0.25 * fit, 3), cand))
        scored.sort(key=lambda t: -t[0])
        candidates_view = [{"po": p.po_number, "score": s} for s, p in scored]
        if scored and scored[0][0] >= P.po_inference_min_score and (
                len(scored) == 1 or scored[0][0] - scored[1][0] >= P.po_inference_min_margin):
            po = scored[0][1]
            c.po_inferred = True
            runner = f" vs next best {scored[1][1].po_number} at {scored[1][0]:.0%}" if len(scored) > 1 else ""
            checks.append(Check(
                "po_inferred", "PO inferred from line items", "warn",
                f"Vendor quoted no PO; matched to {po.po_number} with {scored[0][0]:.0%} confidence{runner}",
                None if P.auto_approve_inferred_po else "review", "AP Clerk",
                evidence={"candidates": candidates_view}))
        else:
            checks.append(Check("po_exists", "PO identified", "fail",
                                "No PO quoted and no open PO for this vendor fits the invoice confidently",
                                "review", "AP Clerk", evidence={"candidates": candidates_view},
                                vendor_ask="Please quote the purchase order number this invoice relates to."))
    else:
        checks.append(Check("po_exists", "PO identified", "fail", "No PO quoted and vendor unknown", "review",
                            "AP Clerk"))

    if po is None:
        return "No purchase order matched", checks, {"candidates": candidates_view}
    c.po = po

    if c.vendor and po.vendor_id != c.vendor.vendor_id:
        checks.append(Check("po_vendor", "PO belongs to this vendor", "fail",
                            f"{po.po_number} was issued to {R.vendors[po.vendor_id].legal_name}, not {c.vendor.legal_name}",
                            "hold", "Procurement"))
    if po.status != "OPEN":
        checks.append(Check("po_status", "PO is open", "fail", f"{po.po_number} is {po.status}", "review", "Procurement"))
    else:
        checks.append(Check("po_status", "PO is open", "pass", f"{po.po_number} is OPEN"))
    if c.inv_date and c.inv_date < po.po_date:
        checks.append(Check("po_date", "Invoice after PO date", "fail",
                            f"Invoice dated {c.inv_date} precedes PO date {po.po_date}", "review", "Procurement"))

    c.matches, _ = _match_lines(c, po)
    unmatched = [m for m in c.matches if m["po_line"] is None]
    if unmatched:
        names = "; ".join(c.inv.line_items[m["inv_idx"]].description for m in unmatched)
        checks.append(Check("po_lines", "Every invoice line is on the PO", "fail",
                            f"{len(unmatched)} line(s) not on {po.po_number}: {names}", "review", "Procurement",
                            vendor_ask="Some billed items are not on our purchase order; please bill them separately against the correct PO."))
    elif c.matches:
        checks.append(Check("po_lines", "Every invoice line is on the PO", "pass",
                            f"All {len(c.matches)} invoice line(s) mapped to PO lines"))
    mapping = []
    for m in c.matches:
        li = c.inv.line_items[m["inv_idx"]]
        pol = next((l for l in po.lines if l.line_no == m["po_line"]), None)
        mapping.append({"invoice_line": li.description, "po_line": f"{pol.line_no}. {pol.description}" if pol else None,
                        "score": m["score"]})
    return (f"{po.po_number}{' (inferred)' if c.po_inferred else ''} · PO value {money(po.total)}", checks,
            {"po_number": po.po_number, "inferred": c.po_inferred, "mapping": mapping, "candidates": candidates_view})


def _billed(po: PurchaseOrder) -> tuple[dict[int, float], list[dict]]:
    """Quantity already billed per PO line by approved/paid invoices."""
    billed: dict[int, float] = {}
    entries = [e for e in db.ledger_for_po(po.po_number) if e["status"] in ("APPROVED", "PAID")]
    for e in entries:
        for ln, q in json.loads(e["lines_json"] or "{}").items():
            billed[int(ln)] = billed.get(int(ln), 0.0) + float(q)
    return billed, entries


def _billed_value(po: PurchaseOrder) -> float:
    billed, _ = _billed(po)
    return round(sum(q * next(l.unit_price for l in po.lines if l.line_no == ln) for ln, q in billed.items()), 2)


def st_three_way(c: Ctx):
    P, po, inv = POLICY, c.po, c.inv
    billed, prior = _billed(po)
    checks: list[Check] = []
    rows = []
    approvable = 0.0
    price_fail = qty_fail = False
    for m in c.matches:
        if m["po_line"] is None:
            continue
        li = inv.line_items[m["inv_idx"]]
        pol = next(l for l in po.lines if l.line_no == m["po_line"])
        net = _net_price(li, pol, inv.prices_include_tax)
        q = li.quantity or 0.0
        prev = billed.get(pol.line_no, 0.0)
        billable = max(0.0, min(pol.received_qty, pol.quantity) - prev)
        var_pct = round((net - pol.unit_price) / pol.unit_price * 100, 2) if net is not None else None
        over_amt = round(max(0.0, (net or 0) - pol.unit_price) * q, 2)
        price_ok = var_pct is not None and (var_pct <= 0.0001 or (var_pct <= P.unit_price_tolerance_pct
                                                                    and over_amt <= P.amount_tolerance_abs))
        row = {"po_line": pol.line_no, "item": pol.description, "po_price": pol.unit_price, "inv_price_net": net,
               "price_var_pct": var_pct, "ordered": pol.quantity, "received": pol.received_qty,
               "billed_before": prev, "invoiced_now": q, "billable_now": billable}
        rows.append(row)
        approvable += min(q, billable) * min(net or 0, pol.unit_price)

        if var_pct is None:
            pass
        elif not price_ok:
            price_fail = True
            checks.append(Check(f"price_l{pol.line_no}", f"Price - {pol.description}", "fail",
                                f"Billed {money(net)}/unit vs PO {money(pol.unit_price)} ({var_pct:+.2f}%, {money(over_amt)} over) "
                                f"- outside tolerance ({P.unit_price_tolerance_pct}% and {money(P.amount_tolerance_abs)})",
                                "review", "Procurement",
                                vendor_ask=f"'{pol.description}' is billed at {money(net)} against the agreed {money(pol.unit_price)}. "
                                           "Please issue a credit note or a corrected invoice."))
        elif abs(var_pct) > 0.0001:
            checks.append(Check(f"price_l{pol.line_no}", f"Price - {pol.description}", "pass",
                                f"{money(net)} vs PO {money(pol.unit_price)} ({var_pct:+.2f}%) - within tolerance"
                                + (" (net of GST)" if inv.prices_include_tax else "")))
        else:
            checks.append(Check(f"price_l{pol.line_no}", f"Price - {pol.description}", "pass",
                                f"{money(net)} matches PO" + (" (net of GST)" if inv.prices_include_tax else "")))

        if prev + q > pol.quantity + 1e-9:
            qty_fail = True
            checks.append(Check(f"qty_po_l{pol.line_no}", f"Qty vs PO - {pol.description}", "fail",
                                f"Ordered {pol.quantity:g}, already billed {prev:g}, now billing {q:g} -> "
                                f"{prev + q - pol.quantity:g} over the PO", "review", "Procurement"))
        if prev + q > pol.received_qty + 1e-9:
            qty_fail = True
            excess = prev + q - max(pol.received_qty, prev)
            checks.append(Check(
                f"qty_grn_l{pol.line_no}", f"Qty vs goods receipt - {pol.description}", "fail",
                f"Received {pol.received_qty:g} of {pol.quantity:g}; {prev:g} already billed; this invoice bills {q:g}. "
                f"Only {billable:g} can be paid now - {excess:g} unit(s) billed ahead of receipt.",
                "review", "Receiving / Procurement",
                evidence=row,
                vendor_ask=(f"'{pol.description}': we have received {pol.received_qty:g} of {pol.quantity:g} units and "
                            f"{prev:g} were already invoiced, so the {q:g} billed now cannot be paid in full. "
                            + (f"Please issue a revised invoice for {billable:g} unit(s) and bill the remainder after delivery."
                               if billable > 0 else "Please remove this line and bill it after delivery / completion."))))
        else:
            checks.append(Check(f"qty_grn_l{pol.line_no}", f"Qty vs goods receipt - {pol.description}", "pass",
                                f"Billing {q:g}; received {pol.received_qty:g}, previously billed {prev:g}"))

    po_billed_value = _billed_value(po)
    if c.net_subtotal is not None:
        after = po_billed_value + c.net_subtotal
        limit = po.total * (1 + P.unit_price_tolerance_pct / 100)
        if after > limit + P.rounding_tolerance:
            checks.append(Check("po_balance", "Within PO value", "fail",
                                f"PO {money(po.total)}; already billed {money(po_billed_value)}; this invoice "
                                f"{money(c.net_subtotal)} -> {money(after - po.total)} over the PO", "review", "Procurement"))
        else:
            checks.append(Check("po_balance", "Within PO value", "pass",
                                f"Billed to date {money(after)} of {money(po.total)} "
                                f"({after / po.total:.0%}) incl. this invoice"))
    c.approvable_now = round(approvable, 2) if (qty_fail and not price_fail) else None
    summary = "Price, quantity and receipt all reconcile" if not (price_fail or qty_fail) else (
        "Billed ahead of goods receipt" if qty_fail and not price_fail else "Price/quantity exceptions found")
    return summary, checks, {"rows": rows, "prior_invoices": [
        {"invoice_number": e["invoice_number"], "invoice_date": e["invoice_date"], "total": e["total"],
         "status": e["status"]} for e in prior], "approvable_now_net": c.approvable_now}


def st_tax(c: Ctx):
    inv, P = c.inv, POLICY
    checks: list[Check] = []
    v_state = (inv.vendor_gstin or (c.vendor.gstin if c.vendor else ""))[:2]
    intra = v_state == BUYER.state_code
    has_split = inv.cgst is not None or inv.sgst is not None
    has_igst = inv.igst is not None
    if not v_state:
        checks.append(Check("tax_type", "Correct GST type", "skip", "Vendor state unknown"))
    elif inv.prices_include_tax and not (has_split or has_igst):
        checks.append(Check("tax_type", "Correct GST type", "warn",
                            "Tax-inclusive invoice without a CGST/SGST/IGST break-up - ITC claim needs the break-up",
                            None, "AP Clerk",
                            vendor_ask="Please show the GST break-up (CGST/SGST or IGST) on the invoice."))
    elif intra and has_igst and not has_split:
        checks.append(Check("tax_type", "Correct GST type", "fail",
                            f"Vendor and buyer are both in state {v_state} (intra-state) but IGST was charged; "
                            "CGST+SGST expected - ITC would be disallowed", "review", "Vendor",
                            vendor_ask="This is an intra-state supply; please re-issue the invoice with CGST + SGST instead of IGST."))
    elif not intra and has_split and not has_igst:
        checks.append(Check("tax_type", "Correct GST type", "fail",
                            f"Inter-state supply (vendor state {v_state} -> buyer {BUYER.state_code}) but CGST+SGST charged; "
                            "IGST expected", "review", "Vendor",
                            vendor_ask="This is an inter-state supply; please re-issue the invoice with IGST."))
    else:
        checks.append(Check("tax_type", "Correct GST type", "pass",
                            f"{'Intra' if intra else 'Inter'}-state ({v_state} -> {BUYER.state_code}): "
                            f"{'CGST + SGST' if intra else 'IGST'} correctly applied"))
    if has_split and inv.cgst is not None and inv.sgst is not None and abs(inv.cgst - inv.sgst) > P.rounding_tolerance:
        checks.append(Check("tax_split", "CGST equals SGST", "fail", f"CGST {money(inv.cgst)} != SGST {money(inv.sgst)}",
                            "review", "AP Clerk"))

    # expected tax from PO tax rates on the matched lines
    if c.po and c.matches and c.tax_total is not None and c.net_subtotal:
        expected = 0.0
        for m in c.matches:
            if m["po_line"] is None:
                continue
            li = inv.line_items[m["inv_idx"]]
            pol = next(l for l in c.po.lines if l.line_no == m["po_line"])
            net_amt = (li.amount or 0) / (1 + pol.tax_rate) if inv.prices_include_tax else (li.amount or 0)
            expected += net_amt * pol.tax_rate
        expected = round(expected, 2)
        eff_rate = c.tax_total / c.net_subtotal * 100
        if abs(c.tax_total - expected) > max(P.rounding_tolerance, c.net_subtotal * P.tax_rate_tolerance_pct / 100):
            checks.append(Check("tax_amount", "Tax amount as per PO rates", "fail",
                                f"Charged {money(c.tax_total)} ({eff_rate:.2f}%), expected {money(expected)} from PO tax rates",
                                "review", "AP Clerk"))
        else:
            checks.append(Check("tax_amount", "Tax amount as per PO rates", "pass",
                                f"{money(c.tax_total)} = {eff_rate:.1f}% of taxable value {money(c.net_subtotal)}"))
    worst = _worst(checks)
    return ("GST type and amount are correct" if worst in ("pass", "info") else "GST issues found"), checks, {
        "vendor_state": v_state, "buyer_state": BUYER.state_code, "intra_state": intra}


def st_duplicate(c: Ctx):
    inv, P = c.inv, POLICY
    checks: list[Check] = []
    same_file = db.ledger_by_hash(c.info.sha256)
    if same_file:
        checks.append(Check("dup_file", "Same file processed before", "fail",
                            f"This exact file was already booked as {same_file['invoice_number']} "
                            f"({same_file['status']})", "reject", "AP Clerk", evidence=same_file))
    if c.vendor is None:
        checks.append(Check("dup_invoice", "Invoice number unique for vendor", "skip", "Vendor unknown"))
        return "Skipped - vendor unknown", checks, {}
    history = db.ledger_for_vendor(c.vendor.vendor_id)
    n = norm_doc_number(inv.invoice_number)
    exact = [h for h in history if n and h["invoice_number_norm"] == n]
    if exact:
        h = exact[0]
        cosmetic = h["invoice_number"] != inv.invoice_number
        checks.append(Check(
            "dup_invoice", "Invoice number unique for vendor", "fail",
            f"Already received {h['invoice_number']} dated {h['invoice_date']} for {money(h['total'])} - status "
            f"{h['status']}." + (f" Number re-formatted ('{inv.invoice_number}' vs '{h['invoice_number']}') but "
                                 "canonically identical." if cosmetic else ""),
            "reject", "AP Clerk", evidence={k: h[k] for k in ("invoice_number", "invoice_date", "total", "status", "po_number")},
            vendor_ask=f"Invoice {h['invoice_number']} dated {h['invoice_date']} for {money(h['total'])} is already "
                       f"recorded in our books (status: {h['status'].replace('_', ' ').lower()}). This copy has not been booked again."))
    elif not n:
        checks.append(Check("dup_invoice", "Invoice number unique for vendor", "skip",
                            "No invoice number on the document - matched on amount and date only"))
    elif not same_file:
        checks.append(Check("dup_invoice", "Invoice number unique for vendor", "pass",
                            f"No prior invoice '{inv.invoice_number}' from this vendor ({len(history)} on file)"))
    if not exact and inv.total is not None:
        near = []
        for h in history:
            hd = parse_date(h["invoice_date"])
            same_amount = h["total"] is not None and abs(h["total"] - inv.total) <= P.rounding_tolerance
            close_in_time = (hd is None or c.inv_date is None
                             or abs((hd - c.inv_date).days) <= P.near_duplicate_window_days)
            if same_amount and close_in_time:
                near.append(h)
        if near:
            h = near[0]
            checks.append(Check("dup_near", "No look-alike invoices", "fail",
                                f"Different number but same amount {money(inv.total)} within "
                                f"{P.near_duplicate_window_days} days: {h['invoice_number']} ({h['invoice_date']})",
                                "review", "AP Clerk", evidence={k: h[k] for k in ("invoice_number", "invoice_date", "total", "status")}))
        else:
            checks.append(Check("dup_near", "No look-alike invoices", "pass",
                                f"No same-amount invoice within {P.near_duplicate_window_days} days"))
    if any(ch.effect == "reject" for ch in checks):
        # A confirmed duplicate makes every downstream check meaningless (the PO is already
        # consumed by the original), so stop here rather than bury the real reason in noise.
        c.checks.extend(checks)
        raise Halt("Duplicate of an invoice already in the ledger - processing stopped")
    worst = _worst(checks)
    return ("No duplicates found" if worst in ("pass", "info", "skip") else "Possible duplicate"), checks, {}


def st_decision(c: Ctx):
    d = decide(c)
    extra = d.pop("_extra_checks")
    c.decision = d
    return d["headline"], extra, {"decision": d}


def st_post(c: Ctx):
    d = c.decision
    checks: list[Check] = []
    if d["outcome"] == "REJECTED" or c.vendor is None:
        checks.append(Check("ledger", "Ledger posting", "info",
                            "Not booked - rejected invoices are returned, never posted"
                            if d["outcome"] == "REJECTED" else "Not booked - vendor unknown; parked in review queue"))
    else:
        lines = {}
        for m in c.matches:
            if m["po_line"] is not None:
                lines[str(m["po_line"])] = lines.get(str(m["po_line"]), 0) + (c.inv.line_items[m["inv_idx"]].quantity or 0)
        c.ledger_id = db.insert_ledger({
            "run_id": c.run_id, "vendor_id": c.vendor.vendor_id, "invoice_number": c.inv.invoice_number,
            "invoice_date": c.inv_date.isoformat() if c.inv_date else c.inv.invoice_date,
            "po_number": c.po.po_number if c.po else None, "subtotal": c.net_subtotal, "tax_total": c.tax_total,
            "total": c.inv.total, "status": d["outcome"], "lines": lines, "file_hash": c.info.sha256,
        })
        msg = (f"Booked to AP ledger #{c.ledger_id}; scheduled for payment on {d['payment_due']}"
               if d["outcome"] == "APPROVED" else f"Parked in ledger #{c.ledger_id} as {d['outcome']} pending {d['queue']}")
        checks.append(Check("ledger", "Ledger posting", "pass", msg))
    if d.get("vendor_message"):
        checks.append(Check("vendor_msg", "Vendor communication drafted", "info",
                            f"Draft to {d['vendor_message']['to']} ready for review - not auto-sent"))
    if d["outcome"] in ("NEEDS_REVIEW", "ON_HOLD"):
        checks.append(Check("routed", "Routed to queue", "info", f"Task created for {d['queue']}"))
    return checks[0].detail, checks, {"ledger_id": c.ledger_id}


STAGE_FUNCS = {
    "intake": st_intake, "extract": st_extract, "validate": st_validate, "vendor": st_vendor,
    "po_match": st_po_match, "three_way": st_three_way, "tax": st_tax, "duplicate": st_duplicate,
    "decision": st_decision, "post": st_post,
}


def _worst(checks: list[Check]) -> str:
    order = {"fail": 4, "warn": 3, "pass": 2, "info": 1, "skip": 0}
    return max((ch.status for ch in checks), key=lambda s: order[s], default="pass")


def _needs(stage: str, c: Ctx) -> Optional[str]:
    """Return a skip-reason if a stage's prerequisites are missing."""
    if stage == "three_way" and c.po is None:
        return "No purchase order matched"
    if stage == "tax" and c.inv is None:
        return "No extraction"
    return None


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #
def run_pipeline(run_id: str, filename: str, data: bytes, source: str) -> dict:
    events = LIVE.setdefault(run_id, [])

    def emit(ev: dict) -> None:
        ev["ts"] = time.time()
        events.append(ev)

    c = Ctx(run_id=run_id, filename=filename, data=data, emit=emit)
    delay = settings().stage_delay_ms / 1000
    t_start = time.perf_counter()
    emit({"type": "run_started", "run_id": run_id, "filename": filename, "source": source,
          "stages": [{"id": s, "title": t} for s, t in STAGES]})
    halted: Optional[str] = None
    status = "completed"
    try:
        with _process_lock:
            for i, (sid, title) in enumerate(STAGES):
                if halted and sid not in ("decision", "post"):
                    emit({"type": "stage_completed", "stage": sid, "status": "skip", "summary": f"Skipped - {halted}",
                          "checks": [], "data": {}, "duration_ms": 0})
                    continue
                skip = _needs(sid, c)
                if skip:
                    emit({"type": "stage_completed", "stage": sid, "status": "skip", "summary": f"Skipped - {skip}",
                          "checks": [], "data": {}, "duration_ms": 0})
                    continue
                emit({"type": "stage_started", "stage": sid, "title": title, "index": i})
                t0 = time.perf_counter()
                n0 = len(c.checks)
                try:
                    summary, stage_checks, data_out = STAGE_FUNCS[sid](c)
                    c.checks.extend(stage_checks)
                    st = _worst(stage_checks) if stage_checks else "pass"
                    if sid == "decision":
                        st = {"APPROVED": "pass", "NEEDS_REVIEW": "warn"}.get(c.decision["outcome"], "fail")
                except Halt as h:  # stage recorded its own checks before halting
                    halted = str(h)
                    stage_checks = c.checks[n0:]
                    summary, data_out, st = str(h), {}, "fail"
                c.stage_results[sid] = {"status": st, "summary": summary}
                elapsed = int((time.perf_counter() - t0) * 1000)
                if delay and sid != "extract":
                    time.sleep(delay)
                emit({"type": "stage_completed", "stage": sid, "status": st, "summary": summary,
                      "checks": [ch.to_dict() for ch in stage_checks], "data": data_out, "duration_ms": elapsed})
    except Exception as e:  # unexpected bug: never lose the run, show it
        status = "error"
        emit({"type": "run_failed", "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-2000:]})
        if c.decision is None:
            c.decision = {"outcome": "NEEDS_REVIEW", "headline": "Processing error - routed to AP for manual handling",
                          "queue": "AP Clerk", "reasons": [{"title": "Internal error", "detail": str(e), "effect": "review"}],
                          "next_actions": ["Process manually; report the error"], "vendor_message": None}

    duration = int((time.perf_counter() - t_start) * 1000)
    result = {
        "run_id": run_id, "filename": filename, "extractor": c.extractor, "extraction_meta": c.extraction_meta,
        "doc": c.info.__dict__ if c.info else None, "invoice": c.inv.model_dump() if c.inv else None,
        "vendor": ({"vendor_id": c.vendor.vendor_id, "name": c.vendor.legal_name} if c.vendor else {}),
        "po_number": c.po.po_number if c.po else None, "po_inferred": c.po_inferred,
        "checks": [ch.to_dict() for ch in c.checks], "decision": c.decision, "stages": c.stage_results,
        "duration_ms": duration, "halted": halted,
    }
    emit({"type": "run_completed", "decision": c.decision, "duration_ms": duration, "status": status})
    db.finish_run(run_id, status=status, result=result, events=events, duration_ms=duration)
    return result


def start_run(filename: str, data: bytes, source: str = "upload", background: bool = True) -> str:
    import hashlib
    run_id = time.strftime("R%y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    (UPLOADS_DIR / f"{run_id}.pdf").write_bytes(data)
    db.create_run(run_id, filename, hashlib.sha256(data).hexdigest(), source)
    LIVE[run_id] = []
    if background:
        threading.Thread(target=run_pipeline, args=(run_id, filename, data, source), daemon=True).start()
    else:
        run_pipeline(run_id, filename, data, source)
    return run_id

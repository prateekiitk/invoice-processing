"""Decision engine: turns check results into one outcome, a reason trail, a work queue
and (when the vendor must act) a ready-to-review message.

Precedence (most severe wins):   REJECTED > ON_HOLD > NEEDS_REVIEW > APPROVED

* REJECTED     - the invoice must not be booked as-is (duplicate, blocked vendor, incomplete
                 tax invoice). Vendor is told why.
* ON_HOLD      - possible fraud / identity mismatch. Payment frozen until verified out-of-band.
                 We deliberately do NOT email the vendor from details on the invoice.
* NEEDS_REVIEW - a specific exception a human resolves (price/qty variance, PO inferred,
                 extraction disagreement, high value). Pre-filled with what to do.
* APPROVED     - every control passed; booked and scheduled per payment terms.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING

from .config import BUYER, POLICY
from .utils import money

if TYPE_CHECKING:  # pragma: no cover
    from .pipeline import Ctx

SEVERITY = {"reject": 3, "hold": 2, "review": 1}
OUTCOME_FOR = {"reject": "REJECTED", "hold": "ON_HOLD", "review": "NEEDS_REVIEW"}


def decide(c: "Ctx") -> dict:
    inv = c.inv
    reasons = sorted((ch for ch in c.checks if ch.effect), key=lambda ch: -SEVERITY[ch.effect])
    extra = []  # checks raised by the decision stage itself
    total = inv.total if inv else None

    # Approval matrix: clean but large invoices still need a human signature.
    if not reasons and total is not None and total > POLICY.auto_approve_limit:
        from .models import Check
        hv = Check("approval_limit", "Within auto-approval limit", "warn",
                   f"{money(total)} exceeds the auto-approval limit of {money(POLICY.auto_approve_limit)}",
                   "review", "Finance Controller")
        extra.append(hv)
        reasons = [hv]

    outcome = OUTCOME_FOR[reasons[0].effect] if reasons else "APPROVED"
    top = reasons[0] if reasons else None
    queue = top.queue if top else "Payments"

    inv_date = c.inv_date or date.today()
    terms = c.vendor.payment_terms_days if c.vendor else 30
    payment_due = (inv_date + timedelta(days=terms)).isoformat() if outcome == "APPROVED" else None

    # Short-pay recommendation for billing-ahead-of-receipt cases
    partial = None
    if outcome == "NEEDS_REVIEW" and c.approvable_now is not None and c.net_subtotal and c.tax_total is not None:
        rate = c.tax_total / c.net_subtotal if c.net_subtotal else 0
        gross = round(c.approvable_now * (1 + rate), 2)
        partial = {"net": c.approvable_now, "gross": gross, "withheld": round((total or 0) - gross, 2)}

    headline = _headline(outcome, top, c, partial)
    actions = _next_actions(outcome, reasons, c, partial, payment_due)
    passed = sum(1 for ch in c.checks if ch.status == "pass")
    failed = sum(1 for ch in c.checks if ch.status == "fail")

    return {
        "outcome": outcome,
        "headline": headline,
        "queue": queue,
        "reasons": [{"check_id": r.id, "title": r.title, "label": label_for(r.id, r.title), "detail": r.detail,
                     "effect": r.effect, "queue": r.queue} for r in reasons],
        "next_actions": actions,
        "amount": total,
        "payment_due": payment_due,
        "partial_approval": partial,
        "vendor_message": _vendor_message(outcome, reasons, c),
        "stats": {"checks_passed": passed, "checks_failed": failed, "checks_total": len(c.checks)},
        "_extra_checks": extra,
    }


def _headline(outcome: str, top, c: "Ctx", partial) -> str:
    vname = c.vendor.legal_name if c.vendor else (c.inv.vendor_name if c.inv else "Unknown vendor")
    amt = money(c.inv.total) if c.inv else ""
    if outcome == "APPROVED":
        return f"Approved {amt} to {vname} - all {sum(ch.status == 'pass' for ch in c.checks)} controls passed"
    label = label_for(top.id, top.title) if top else ""
    if outcome == "REJECTED":
        return f"Rejected - {label}: {_short(top.detail)}"
    if outcome == "ON_HOLD":
        return f"Payment on hold - {label}: {_short(top.detail)}"
    if partial:
        return (f"Needs review - billed ahead of goods receipt: {money(partial['gross'])} payable now, "
                f"{money(partial['withheld'])} to withhold")
    return f"Needs review - {label}: {_short(top.detail)}"


FAIL_LABELS = {
    "dup_invoice": "duplicate invoice", "dup_file": "duplicate file", "dup_near": "possible duplicate",
    "vendor_status": "vendor blocked", "vendor_master": "unknown vendor", "mandatory_fields": "incomplete tax invoice",
    "bank_match": "bank details changed", "gstin_match": "GSTIN mismatch", "gstin_valid": "invalid GSTIN",
    "file_readable": "unreadable file", "bill_to": "billed to wrong entity", "extractor": "manual keying required", "ocr_confidence": "low OCR confidence",
    "po_inferred": "PO not quoted (inferred)", "po_exists": "no purchase order", "po_lines": "item not on PO",
    "po_balance": "exceeds PO value", "cross_check": "extraction disagreement", "arithmetic": "arithmetic mismatch",
    "tax_type": "wrong GST type", "tax_amount": "GST amount mismatch", "approval_limit": "above auto-approval limit",
    "po_vendor": "PO belongs to another vendor", "po_status": "PO not open", "invoice_date": "date problem",
}


PREFIX_LABELS = {"qty_grn": "billed ahead of goods receipt", "qty_po": "quantity exceeds PO", "price_l": "price variance"}


def label_for(check_id: str, title: str) -> str:
    if check_id in FAIL_LABELS:
        return FAIL_LABELS[check_id]
    for p, lbl in PREFIX_LABELS.items():
        if check_id.startswith(p):
            return lbl
    return title


def _short(s: str, n: int = 140) -> str:
    s = s.split(". ")[0]
    return s if len(s) <= n else s[: n - 1] + "…"


def _next_actions(outcome, reasons, c: "Ctx", partial, payment_due) -> list[str]:
    ids = {r.id for r in reasons}
    acts: list[str] = []
    if outcome == "APPROVED":
        acts.append(f"Scheduled for payment on {payment_due} ({c.vendor.payment_terms_days}-day terms) "
                    f"to {c.vendor.bank_name} account on file")
        return acts
    if "bank_match" in ids:
        acts += ["Freeze payment to the new account",
                 "Call the vendor on the phone number in the vendor master (NOT contact details on the invoice) to verify the change",
                 "If verified, update vendor master via the change-control form with a second approver; then release"]
    if ids & {"gstin_match", "gstin_valid", "po_vendor"}:
        acts.append("Verify vendor identity (GST portal lookup) before any payment")
    if ids & {"dup_invoice", "dup_file"}:
        acts.append("Do not book - original is already in the ledger; send the vendor a 'not processed - duplicate' notice")
    if "dup_near" in ids:
        acts.append("Compare with the look-alike invoice; reject one if they are the same supply")
    if "vendor_status" in ids:
        acts.append("Return invoice - vendor is blocked; escalate to Procurement if the supply was genuinely needed")
    if ids & {"mandatory_fields", "bill_to", "file_readable"}:
        acts.append("Return to vendor for a complete, correctly addressed tax invoice")
    if partial:
        acts.append(f"Short-pay: approve {money(partial['gross'])} (units received but not yet billed); "
                    f"withhold {money(partial['withheld'])} until a goods receipt is posted or a credit note arrives")
    if any(i.startswith("qty_grn") for i in ids):
        acts.append("Ask Receiving to confirm whether more units were delivered but not yet entered in the system")
    if any(i.startswith("price_l") for i in ids):
        acts.append("Procurement to confirm the agreed price; request a credit note for the difference")
    if "po_inferred" in ids:
        others = [r for r in reasons if r.id != "po_inferred"]
        acts.append(f"Confirm the inferred PO {c.po.po_number if c.po else ''}"
                    + (" - every other control passed, so approving releases it for payment" if not others else ""))
        acts.append("Remind the vendor to quote the PO number on future invoices")
    if "cross_check" in ids:
        acts.append("Compare the disagreeing fields with the PDF and correct the extraction")
    if "extractor" in ids:
        acts.append("Key the invoice manually from the PDF (the scan could not be read automatically)")
    if "approval_limit" in ids:
        acts.append("Obtain Finance Controller sign-off (above auto-approval limit)")
    if ids & {"tax_type", "tax_amount", "tax_split"}:
        acts.append("Request a corrected tax invoice so GST input credit is not lost")
    if not acts:
        acts.append(f"Review the flagged checks and resolve in {reasons[0].queue if reasons else 'AP'} queue")
    return acts


def _vendor_message(outcome: str, reasons, c: "Ctx"):
    """Draft (never auto-sent) message to the vendor's contact on the MASTER record."""
    if outcome in ("APPROVED", "ON_HOLD"):
        # ON_HOLD is a possible fraud signal: verification happens by phone via master data, not email reply.
        return None
    asks = []
    for r in reasons:
        if r.vendor_ask and r.vendor_ask not in asks:
            asks.append(r.vendor_ask)
    if not asks:
        return None
    to = c.vendor.contact_email if c.vendor else "(vendor contact not on file)"
    vname = c.vendor.legal_name if c.vendor else (c.inv.vendor_name if c.inv else "Supplier")
    inv_no = (c.inv.invoice_number if c.inv else None) or "(no invoice number)"
    amount = money(c.inv.total) if c.inv and c.inv.total is not None else ""
    status_line = {"REJECTED": "could not be processed and has been returned",
                   "NEEDS_REVIEW": "is on hold pending the clarification below"}[outcome]
    body = [f"Dear {vname} accounts team,", "",
            f"Your invoice {inv_no} {('for ' + amount) if amount else ''} {status_line}.".replace("  ", " "), ""]
    body += [f"{i}. {a}" for i, a in enumerate(asks, 1)]
    only_dup = all(r.id in ("dup_invoice", "dup_file") for r in reasons if r.vendor_ask)
    closing = ("No action is needed if this was sent as a reminder - the original will be settled per the agreed terms."
               if only_dup else "Please reply to this email with the corrected document and we will process it promptly.")
    body += ["", closing, "", "Regards,", "Accounts Payable", BUYER.name]
    subject = f"Invoice {inv_no} - {'not processed' if outcome == 'REJECTED' else 'action needed'}"
    return {"to": to, "subject": subject, "body": "\n".join(body)}

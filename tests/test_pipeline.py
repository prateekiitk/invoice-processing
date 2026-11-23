"""End-to-end process tests: real PDFs in, decisions out."""
import json

import pytest

from app import db, pipeline
from app.extraction import ExtractionError
from app.models import LineItem
from conftest import SAMPLES, check_ids, happy_extraction, run


def decision(r):
    return r["result"]["decision"]


# --------------------------------------------------------------------------- the sample invoices
def test_happy_path_approved_and_booked():
    r = run("01_happy_path_apex_office.pdf")
    d = decision(r)
    assert d["outcome"] == "APPROVED"
    assert d["payment_due"] == "2026-12-14"          # 14 Nov + 30-day terms
    assert not d["reasons"]
    assert any(l["invoice_number"] == "AOS/2026/1187" and l["status"] == "APPROVED" for l in db.all_ledger())


def test_split_billing_ahead_of_receipt_short_pay():
    d = decision(run("02_split_billing_overrun_coldline.pdf"))
    assert d["outcome"] == "NEEDS_REVIEW"
    ids = {x["check_id"] for x in d["reasons"]}
    assert {"qty_grn_l1", "qty_grn_l2"} <= ids
    # received 6 chillers, 4 already billed -> only 2 payable now: 2 x 68,500 + 18% IGST
    assert d["partial_approval"]["gross"] == pytest.approx(161660.0)
    assert d["partial_approval"]["withheld"] == pytest.approx(509760.0 - 161660.0)
    assert d["vendor_message"] and "revised invoice for 2 unit(s)" in d["vendor_message"]["body"]


def test_duplicate_in_disguise_rejected_and_halts():
    r = run("03_duplicate_resubmission_sterling.pdf")
    d = decision(r)
    assert d["outcome"] == "REJECTED"
    assert d["reasons"][0]["check_id"] == "dup_invoice"
    assert len(d["reasons"]) == 1                       # no downstream noise
    assert r["result"]["stages"].get("three_way") is None  # skipped after the halt
    assert not any(l["invoice_number"] == "SPI-26-27-457" for l in db.all_ledger())


def test_bank_change_on_hold_and_no_vendor_email():
    d = decision(run("04_bank_change_kaveri_it.pdf"))
    assert d["outcome"] == "ON_HOLD"
    assert [x["check_id"] for x in d["reasons"]] == ["bank_match"]
    assert d["vendor_message"] is None                  # never reply to possibly-spoofed contact details
    assert "phone number in the vendor master" in " ".join(d["next_actions"])


def test_scanned_invoice_read_with_ocr_infers_po_and_nets_out_gst():
    r = run("05_scanned_no_po_greenleaf.pdf")
    d = decision(r)
    assert r["result"]["extractor"] == "OCR"
    assert d["outcome"] == "NEEDS_REVIEW"
    assert [x["check_id"] for x in d["reasons"]] == ["po_inferred"]   # the only exception
    assert r["result"]["po_number"] == "PO-2026-0405"
    price = next(c for c in r["result"]["checks"] if c["id"] == "price_l1")
    assert price["status"] == "pass" and "+0.64%" in price["detail"]   # 760 incl. GST = 644.07 net vs 640


def test_ocr_failure_routes_to_manual_keying(monkeypatch):
    def broken(data, sha):
        raise ExtractionError("OCR engine not installed")
    monkeypatch.setattr(pipeline, "extract_with_ocr", broken)
    r = run("05_scanned_no_po_greenleaf.pdf")
    d = decision(r)
    assert d["outcome"] == "NEEDS_REVIEW" and d["queue"] == "AP Data Entry"
    assert r["result"]["halted"]


def test_low_ocr_confidence_goes_to_review(monkeypatch):
    from app.extraction import extract_with_ocr
    def shaky(data, sha):
        inv, meta = extract_with_ocr(data, sha)
        return inv, {**meta, "min_confidence": 0.55}
    monkeypatch.setattr(pipeline, "extract_with_ocr", shaky)
    r = run("05_scanned_no_po_greenleaf.pdf")
    assert "ocr_confidence" in check_ids(r, "review")


def test_blocked_vendor_rejected():
    d = decision(run("06_blocked_vendor_orbit.pdf"))
    assert d["outcome"] == "REJECTED"
    assert d["reasons"][0]["check_id"] == "vendor_status"


def test_missing_fields_rejected_with_vendor_request():
    d = decision(run("07_missing_fields_apex.pdf"))
    assert d["outcome"] == "REJECTED"
    assert "invoice number" in d["vendor_message"]["body"]


def test_resubmitting_same_file_is_caught():
    assert decision(run("01_happy_path_apex_office.pdf"))["outcome"] == "APPROVED"
    r2 = run("01_happy_path_apex_office.pdf")
    assert decision(r2)["outcome"] == "REJECTED"
    assert {"dup_file", "dup_invoice"} <= check_ids(r2, "reject")


def test_not_a_pdf_is_rejected_gracefully():
    r = run(b"just some text pretending to be an invoice", filename="invoice.pdf")
    assert r["status"] == "completed"
    assert decision(r)["outcome"] == "REJECTED"
    assert decision(r)["reasons"][0]["check_id"] == "file_readable"


def test_scenarios_catalogue_expectations_hold():
    """Every scenario produces its documented outcome, fully offline."""
    for s in json.loads((SAMPLES / "scenarios.json").read_text(encoding="utf-8")):
        db.reset_all()
        assert decision(run(s["file"]))["outcome"] == s["expected"], s["file"]


# --------------------------------------------------------------------------- rule tests with a hand-made reading
@pytest.fixture
def reads_as(monkeypatch):
    """Make the parser return a given reading, to test individual rules."""
    def install(extraction):
        monkeypatch.setattr(pipeline, "extract_with_rules", lambda data: extraction)
    return install


def test_price_above_tolerance_needs_review(reads_as):
    lines = happy_extraction().line_items
    lines[2] = LineItem(description="Toner Cartridge 88A Compatible", hsn_sac="8443", quantity=6, unit_price=3100.0, amount=18600.0)
    reads_as(happy_extraction(line_items=lines, subtotal=73000.0, cgst=6570.0, sgst=6570.0, tax_total=13140.0, total=86140.0))
    monkey_run = run("01_happy_path_apex_office.pdf")
    d = decision(monkey_run)
    assert d["outcome"] == "NEEDS_REVIEW"
    assert any(x["check_id"] == "price_l3" for x in d["reasons"])


def test_wrong_gst_type_flagged(reads_as):
    # Apex (Karnataka) -> Halcyon (Karnataka) is intra-state; IGST is wrong.
    reads_as(happy_extraction(cgst=None, sgst=None, igst=12870.0))
    d = decision(run("01_happy_path_apex_office.pdf"))
    assert d["outcome"] == "NEEDS_REVIEW"
    assert any(x["check_id"] == "tax_type" for x in d["reasons"])


def test_gstin_of_another_vendor_is_held(reads_as):
    # Name says Apex, GSTIN belongs to Kaveri IT -> identity mismatch
    reads_as(happy_extraction(vendor_gstin="29AADCK8834N1ZS"))
    d = decision(run("01_happy_path_apex_office.pdf"))
    assert d["outcome"] == "ON_HOLD"


def test_high_value_needs_controller(reads_as, monkeypatch):
    from app import decision as dec_mod
    from app.config import Policy
    monkeypatch.setattr(dec_mod, "POLICY", Policy(auto_approve_limit=50_000.0))
    reads_as(happy_extraction())
    d = decision(run("01_happy_path_apex_office.pdf"))
    assert d["outcome"] == "NEEDS_REVIEW"
    assert d["reasons"][0]["check_id"] == "approval_limit"

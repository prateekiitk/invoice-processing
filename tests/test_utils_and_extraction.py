import hashlib
from datetime import date

import pytest

from app.extraction import ExtractionError, extract_with_ocr, extract_with_rules, inspect_pdf
from app.utils import gstin_checksum_ok, norm_doc_number, parse_amount, parse_date, repair_gstin, similarity
from conftest import SAMPLES


def test_gstin_checksum():
    assert gstin_checksum_ok("27AAPFU0939F1ZV")          # publicly documented valid example
    assert gstin_checksum_ok("29AACCA7315M1ZF")
    assert not gstin_checksum_ok("29AACCA7315M1ZG")      # wrong check digit
    assert not gstin_checksum_ok("29AACCA7315M1Z")       # too short
    assert not gstin_checksum_ok(None)


@pytest.mark.parametrize("a,b", [("SPI/26-27/0457", "SPI-26-27-457"), ("INV00457", "inv-457"), ("AOS/2026/1187", "aos 2026 1187")])
def test_invoice_number_canonicalisation(a, b):
    assert norm_doc_number(a) == norm_doc_number(b)


def test_invoice_number_distinct():
    assert norm_doc_number("CRL/2026/118") != norm_doc_number("CRL/2026/131")


@pytest.mark.parametrize("tok,val", [("1,23,456.50", 123456.5), ("123,456.50", 123456.5), ("INR 5,09,760.00", 509760.0),
                                     ("Rs.420", 420.0), ("18.50", 18.5), ("abc", None), ("12x10x8", None)])
def test_parse_amount(tok, val):
    assert parse_amount(tok) == val


@pytest.mark.parametrize("s", ["14-Nov-2026", "14/11/2026", "14.11.2026", "2026-11-14", "14 Nov 2026", "14 November 2026"])
def test_parse_date_indian_formats(s):
    assert parse_date(s) == date(2026, 11, 14)


def test_similarity_orders_sensibly():
    assert similarity("Floor Cleaner Concentrate 5 Ltr", "Floor Cleaner Concentrate 5L") > \
           similarity("Floor Cleaner Concentrate 5 Ltr", "Garbage Bags XL (Roll of 30)")


def test_inspect_rejects_non_pdf():
    with pytest.raises(ExtractionError):
        inspect_pdf(b"hello, not a pdf")


def test_inspect_detects_scanned():
    assert inspect_pdf((SAMPLES / "05_scanned_no_po_greenleaf.pdf").read_bytes()).kind == "scanned"
    assert inspect_pdf((SAMPLES / "01_happy_path_apex_office.pdf").read_bytes()).kind == "digital"


@pytest.mark.parametrize("name,inv_no,date_,po,total,n_lines", [
    ("01_happy_path_apex_office.pdf", "AOS/2026/1187", "2026-11-14", "PO-2026-0412", 84370.0, 3),
    ("02_split_billing_overrun_coldline.pdf", "CRL/2026/131", "2026-11-18", "PO-2026-0398", 509760.0, 2),
    ("03_duplicate_resubmission_sterling.pdf", "SPI-26-27-457", "2026-10-28", "PO-2026-0377", 184670.0, 2),
    ("04_bank_change_kaveri_it.pdf", "KIT/1124/2026", "2026-11-20", "PO-2026-0420", 212400.0, 2),
    ("07_missing_fields_apex.pdf", None, None, "PO-2026-0433", 30562.0, 2),
])
def test_rules_extractor_across_three_layouts(name, inv_no, date_, po, total, n_lines):
    e = extract_with_rules((SAMPLES / name).read_bytes())
    assert e.invoice_number == inv_no
    assert e.invoice_date == date_
    assert e.po_reference == po
    assert e.total == total
    assert len(e.line_items) == n_lines
    for li in e.line_items:
        assert abs(li.quantity * li.unit_price - li.amount) < 0.05


def test_rules_extractor_reads_bank_change_note():
    e = extract_with_rules((SAMPLES / "04_bank_change_kaveri_it.pdf").read_bytes())
    assert e.bank_account_number == "7781200045612"
    assert any("changed our bankers" in n for n in e.notes)


def test_ocr_reads_the_scanned_invoice():
    data = (SAMPLES / "05_scanned_no_po_greenleaf.pdf").read_bytes()
    e, meta = extract_with_ocr(data, hashlib.sha256(data).hexdigest())
    assert e.vendor_name == "Greenleaf Hygiene Solutions"
    assert e.vendor_gstin == "33AAJFG6619Q1ZU" and e.invoice_number == "GHS/0931" and e.invoice_date == "2026-11-12"
    assert e.po_reference is None and e.prices_include_tax
    assert [(li.quantity, li.unit_price) for li in e.line_items] == [(60, 760.0), (40, 613.6)]
    assert e.total == 70144.0 and e.igst == 10699.93 and e.bank_account_number == "6012004500789"
    assert meta["mean_confidence"] > 0.9


def test_gstin_ocr_misreads_are_repaired_by_check_digit():
    assert repair_gstin("33AAJFG6619QIZU") == "33AAJFG6619Q1ZU"   # I read for 1
    assert repair_gstin("29AAHCH482IK1ZA") == "29AAHCH4821K1ZA"   # I read for 1 in a digit position
    assert repair_gstin("29AAHCH4821K1ZA") == "29AAHCH4821K1ZA"   # already valid
    assert repair_gstin("29AAHCH4821K1ZB") is None                # wrong check digit stays wrong


def test_invoice_number_label_does_not_jump_lines():
    """A big 'INVOICE' heading above an address like 'No. 88, 2nd Cross' must not be read as invoice number 88."""
    from app.extraction import _parse
    rows = [[(0, w) for w in line.split()] for line in [
        "Apex Office Supplies Pvt Ltd", "INVOICE", "No. 88, 2nd Cross, Jayanagar 4th Block",
        "GSTIN 29AACCA7315M1ZF Invoice # AOS/2026/1214", "Date 20/11/2026", "Your PO Ref PO-2026-0433",
    ]]
    e = _parse(rows, [])
    assert e.invoice_number == "AOS/2026/1214" and e.po_reference == "PO-2026-0433"

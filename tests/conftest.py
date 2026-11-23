import os
import sys
import tempfile
from pathlib import Path

import pytest

# Isolated runtime dir and no artificial pause between stages.
os.environ["AP_RUNTIME_DIR"] = tempfile.mkdtemp(prefix="ap_autopilot_test_")
os.environ["STAGE_DELAY_MS"] = "0"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.models import InvoiceExtraction, LineItem  # noqa: E402

SAMPLES = ROOT / "samples"


@pytest.fixture(autouse=True)
def clean_db():
    db.reset_all()
    yield


@pytest.fixture
def sample_bytes():
    return lambda name: (SAMPLES / name).read_bytes()


def run(name_or_bytes, filename="test.pdf"):
    from app.pipeline import start_run
    data = (SAMPLES / name_or_bytes).read_bytes() if isinstance(name_or_bytes, str) else name_or_bytes
    rid = start_run(filename if not isinstance(name_or_bytes, str) else name_or_bytes, data, "test", background=False)
    return db.get_run(rid)


def check_ids(r, effect=None):
    return {c["id"] for c in r["result"]["checks"] if effect is None or c["effect"] == effect}


def happy_extraction(**overrides) -> InvoiceExtraction:
    """What a perfect reading returns for samples/01_happy_path_apex_office.pdf."""
    base = dict(
        vendor_name="Apex Office Supplies Pvt Ltd", vendor_gstin="29AACCA7315M1ZF", buyer_name="Halcyon Foods Pvt Ltd",
        buyer_gstin="29AAHCH4821K1ZA", invoice_number="AOS/2026/1187", invoice_date="2026-11-14", due_date=None,
        po_reference="PO-2026-0412", currency="INR",
        line_items=[
            LineItem(description="A4 Copier Paper 75 GSM (Box of 5 reams)", hsn_sac="4802", quantity=40, unit_price=1150.0, amount=46000.0),
            LineItem(description="Ballpoint Pens Blue (Pack of 50)", hsn_sac="9608", quantity=20, unit_price=420.0, amount=8400.0),
            LineItem(description="Toner Cartridge 88A Compatible", hsn_sac="8443", quantity=6, unit_price=2850.0, amount=17100.0),
        ],
        subtotal=71500.0, cgst=6435.0, sgst=6435.0, igst=None, tax_total=12870.0, total=84370.0,
        prices_include_tax=False, bank_account_number="50200012345678", bank_ifsc="HDFC0001234", notes=[],
    )
    base.update(overrides)
    return InvoiceExtraction(**base)

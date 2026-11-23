"""Procurement master data: vendor master, purchase orders, PO lines, goods receipts.

In production these would come from the ERP (SAP/Oracle/Tally/NetSuite). Here they are
CSV files in /data so the demo is self-contained and the reviewer can open them.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from .config import DATA_DIR
from .utils import norm_company, norm_po, similarity


@dataclass
class Vendor:
    vendor_id: str
    legal_name: str
    aliases: list[str]
    gstin: str
    state_code: str
    status: str
    status_reason: str
    bank_account: str
    bank_ifsc: str
    bank_name: str
    payment_terms_days: int
    contact_email: str


@dataclass
class POLine:
    po_number: str
    line_no: int
    item_code: str
    description: str
    quantity: float
    uom: str
    unit_price: float
    tax_rate: float
    received_qty: float = 0.0

    @property
    def amount(self) -> float:
        return round(self.quantity * self.unit_price, 2)


@dataclass
class PurchaseOrder:
    po_number: str
    vendor_id: str
    po_date: date
    currency: str
    status: str
    department: str
    requester: str
    lines: list[POLine] = field(default_factory=list)

    @property
    def total(self) -> float:
        return round(sum(l.amount for l in self.lines), 2)


def _read(name: str) -> list[dict]:
    with open(DATA_DIR / name, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


class ReferenceData:
    def __init__(self) -> None:
        self.vendors: dict[str, Vendor] = {}
        for r in _read("vendors.csv"):
            self.vendors[r["vendor_id"]] = Vendor(
                vendor_id=r["vendor_id"], legal_name=r["legal_name"],
                aliases=[a for a in r["aliases"].split("|") if a], gstin=r["gstin"],
                state_code=r["state_code"], status=r["status"], status_reason=r["status_reason"],
                bank_account=r["bank_account"], bank_ifsc=r["bank_ifsc"], bank_name=r["bank_name"],
                payment_terms_days=int(r["payment_terms_days"]), contact_email=r["contact_email"],
            )
        self.pos: dict[str, PurchaseOrder] = {}
        for r in _read("purchase_orders.csv"):
            self.pos[r["po_number"]] = PurchaseOrder(
                po_number=r["po_number"], vendor_id=r["vendor_id"],
                po_date=date.fromisoformat(r["po_date"]), currency=r["currency"], status=r["status"],
                department=r["department"], requester=r["requester"],
            )
        for r in _read("po_lines.csv"):
            self.pos[r["po_number"]].lines.append(POLine(
                po_number=r["po_number"], line_no=int(r["line_no"]), item_code=r["item_code"],
                description=r["description"], quantity=float(r["quantity"]), uom=r["uom"],
                unit_price=float(r["unit_price"]), tax_rate=float(r["tax_rate"]),
            ))
        self.receipts: dict[str, list[dict]] = defaultdict(list)
        for r in _read("goods_receipts.csv"):
            self.receipts[r["po_number"]].append(r)
            po = self.pos[r["po_number"]]
            for l in po.lines:
                if l.line_no == int(r["line_no"]):
                    l.received_qty += float(r["quantity_received"])
        self._po_by_norm = {norm_po(k): k for k in self.pos}

    # ---- lookups ---------------------------------------------------------
    def vendor_by_gstin(self, gstin: str | None) -> Optional[Vendor]:
        if not gstin:
            return None
        return next((v for v in self.vendors.values() if v.gstin == gstin.upper()), None)

    def vendor_by_name(self, name: str | None) -> tuple[Optional[Vendor], float]:
        """Best fuzzy match on legal name + known aliases."""
        if not name:
            return None, 0.0
        target = norm_company(name)
        best, best_score = None, 0.0
        for v in self.vendors.values():
            for cand in [v.legal_name, *v.aliases]:
                c = norm_company(cand)
                score = 1.0 if c == target else similarity(c, target)
                if score > best_score:
                    best, best_score = v, score
        return best, best_score

    def po(self, ref: str | None) -> Optional[PurchaseOrder]:
        if not ref:
            return None
        key = self._po_by_norm.get(norm_po(ref))
        return self.pos.get(key) if key else None

    def open_pos_for_vendor(self, vendor_id: str) -> list[PurchaseOrder]:
        return [p for p in self.pos.values() if p.vendor_id == vendor_id and p.status == "OPEN"]


_REF: Optional[ReferenceData] = None


def ref() -> ReferenceData:
    global _REF
    if _REF is None:
        _REF = ReferenceData()
    return _REF

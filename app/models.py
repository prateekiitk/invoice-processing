"""Data contracts shared by the extractors, checks and the API."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# Extraction contract. The parser fills this from either the PDF text layer
# or the OCR output, and every check reads from it.
# All fields are required-but-nullable: "not on the document" must be null,
# never guessed.
# --------------------------------------------------------------------------- #
class LineItem(BaseModel):
    description: str = Field(description="Item / service description exactly as printed")
    hsn_sac: Optional[str] = Field(description="HSN or SAC code if printed, else null")
    quantity: Optional[float] = Field(description="Billed quantity")
    unit_price: Optional[float] = Field(description="Unit price exactly as printed (may include tax if the invoice says prices are tax-inclusive)")
    amount: Optional[float] = Field(description="Line amount exactly as printed")


class InvoiceExtraction(BaseModel):
    vendor_name: Optional[str] = Field(description="Legal name of the supplier issuing the invoice")
    vendor_gstin: Optional[str] = Field(description="Supplier GSTIN (15 chars) if printed")
    buyer_name: Optional[str] = Field(description="Bill-to company name")
    buyer_gstin: Optional[str] = Field(description="Bill-to GSTIN if printed")
    invoice_number: Optional[str] = Field(description="Invoice / bill number exactly as printed; null if absent")
    invoice_date: Optional[str] = Field(description="Invoice date as ISO YYYY-MM-DD; Indian dd/mm/yyyy convention for numeric dates; null if absent")
    due_date: Optional[str] = Field(description="Due date as ISO YYYY-MM-DD if printed")
    po_reference: Optional[str] = Field(description="Purchase order number referenced on the invoice; null if none is printed (do not infer)")
    currency: Optional[str] = Field(description="ISO currency code, e.g. INR")
    line_items: list[LineItem]
    subtotal: Optional[float] = Field(description="Taxable value / sub total before tax, if printed")
    cgst: Optional[float]
    sgst: Optional[float]
    igst: Optional[float]
    tax_total: Optional[float] = Field(description="Total tax amount if printed or clearly derivable from printed tax lines")
    total: Optional[float] = Field(description="Final amount payable (grand total)")
    prices_include_tax: bool = Field(description="True if the invoice states that prices/amounts are inclusive of GST/tax")
    bank_account_number: Optional[str] = Field(description="Remittance bank account number printed on the invoice, digits only")
    bank_ifsc: Optional[str] = Field(description="IFSC code printed on the invoice")
    notes: list[str] = Field(description="Anything on the document an AP clerk should notice (e.g. bank change notices, 'copy', 'reminder', handwritten corrections, illegible fields)")


# --------------------------------------------------------------------------- #
# Check results + decision
# --------------------------------------------------------------------------- #
Status = Literal["pass", "warn", "fail", "info", "skip"]
Effect = Optional[Literal["review", "hold", "reject"]]


@dataclass
class Check:
    id: str
    title: str
    status: Status
    detail: str
    effect: Effect = None          # what this check forces on the decision, if anything
    queue: Optional[str] = None    # who has to act
    evidence: dict[str, Any] = field(default_factory=dict)
    vendor_ask: Optional[str] = None  # what we need from the vendor to fix this, if anything

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


DECISIONS = ("APPROVED", "NEEDS_REVIEW", "ON_HOLD", "REJECTED")

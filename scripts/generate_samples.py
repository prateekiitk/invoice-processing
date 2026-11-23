"""Generate realistic sample vendor invoices (PDF) for the demo scenarios.

Three different vendor layouts are used on purpose so the extraction step can't
rely on a single template, and one invoice is rasterised into an image-only
"scanned" PDF (no text layer) to exercise the OCR path.

Run:  python scripts/generate_samples.py
"""
from __future__ import annotations

import io
import random
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image, ImageFilter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "samples"

BUYER = {
    "name": "Halcyon Foods Pvt Ltd",
    "address": ["Plot 14, Peenya Industrial Area, Phase II", "Bengaluru, Karnataka 560058"],
    "gstin": "29AAHCH4821K1ZA",
}


def inr(x: float) -> str:
    """Indian digit grouping: 1,23,456.00"""
    neg = x < 0
    s = f"{abs(x):.2f}"
    whole, frac = s.split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return ("-" if neg else "") + whole + "." + frac


def western(x: float) -> str:
    return f"{x:,.2f}"


# --------------------------------------------------------------------------- #
# Layout A - "classic" boxed GST tax invoice (grid table, Indian number format)
# --------------------------------------------------------------------------- #
def layout_classic(c: canvas.Canvas, s: dict) -> None:
    W, H = A4
    m = 15 * mm
    c.setStrokeColor(colors.black)
    c.rect(m, m, W - 2 * m, H - 2 * m)

    c.setFont("Helvetica-Bold", 15)
    c.drawCentredString(W / 2, H - m - 10 * mm, "TAX INVOICE")
    c.setFont("Helvetica-Bold", 12)
    y = H - m - 20 * mm
    c.drawString(m + 4 * mm, y, s["vendor"]["name"])
    c.setFont("Helvetica", 9)
    for line in s["vendor"]["address"]:
        y -= 4.5 * mm
        c.drawString(m + 4 * mm, y, line)
    y -= 4.5 * mm
    c.drawString(m + 4 * mm, y, f"GSTIN: {s['vendor']['gstin']}")
    y -= 4.5 * mm
    c.drawString(m + 4 * mm, y, f"Email: {s['vendor']['email']}")

    # meta box on right
    rx = W / 2 + 10 * mm
    ry = H - m - 20 * mm
    c.setFont("Helvetica", 9)
    for label, val in s["meta"]:
        c.drawString(rx, ry, f"{label}")
        c.drawString(rx + 32 * mm, ry, f": {val}")
        ry -= 5 * mm

    y = min(y, ry) - 8 * mm
    c.line(m, y + 4 * mm, W - m, y + 4 * mm)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(m + 4 * mm, y - 1 * mm, "Bill To:")
    c.setFont("Helvetica", 9)
    c.drawString(m + 4 * mm, y - 6 * mm, BUYER["name"])
    yy = y - 6 * mm
    for line in BUYER["address"]:
        yy -= 4.5 * mm
        c.drawString(m + 4 * mm, yy, line)
    yy -= 4.5 * mm
    c.drawString(m + 4 * mm, yy, f"GSTIN: {BUYER['gstin']}")

    # table
    y = yy - 10 * mm
    cols = [m, m + 10 * mm, m + 92 * mm, m + 112 * mm, m + 130 * mm, m + 152 * mm, W - m]
    heads = ["S.No", "Description of Goods", "HSN", "Qty", "Rate", "Amount"]
    c.setFont("Helvetica-Bold", 9)
    c.line(m, y + 5 * mm, W - m, y + 5 * mm)
    for i, h in enumerate(heads):
        c.drawString(cols[i] + 1.5 * mm, y, h)
    c.line(m, y - 2.5 * mm, W - m, y - 2.5 * mm)
    c.setFont("Helvetica", 9)
    top = y + 5 * mm
    for i, ln in enumerate(s["lines"], 1):
        y -= 7 * mm
        c.drawString(cols[0] + 1.5 * mm, y, str(i))
        c.drawString(cols[1] + 1.5 * mm, y, ln["desc"])
        c.drawString(cols[2] + 1.5 * mm, y, ln.get("hsn", ""))
        c.drawRightString(cols[4] - 2 * mm, y, f"{ln['qty']:g}")
        c.drawRightString(cols[5] - 2 * mm, y, inr(ln["rate"]))
        c.drawRightString(cols[6] - 2 * mm, y, inr(ln["qty"] * ln["rate"]))
    y -= 5 * mm
    c.line(m, y, W - m, y)
    for x in cols[1:-1]:
        c.line(x, top, x, y)

    # totals
    c.setFont("Helvetica", 9.5)
    for label, val, bold in s["totals"]:
        y -= 6 * mm
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 9.5)
        c.drawRightString(cols[5] - 2 * mm, y, label)
        c.drawRightString(cols[6] - 2 * mm, y, inr(val))
    y -= 8 * mm
    c.setFont("Helvetica-Oblique", 8.5)
    c.drawString(m + 4 * mm, y, s.get("words", ""))

    # bank + notes
    y -= 12 * mm
    c.setFont("Helvetica-Bold", 9)
    c.drawString(m + 4 * mm, y, "Bank Details")
    c.setFont("Helvetica", 9)
    for line in s["bank"]:
        y -= 4.5 * mm
        c.drawString(m + 4 * mm, y, line)
    for note in s.get("notes", []):
        y -= 7 * mm
        c.setFont("Helvetica-Bold", 9)
        c.drawString(m + 4 * mm, y, note)
    c.setFont("Helvetica", 8)
    c.drawString(m + 4 * mm, m + 6 * mm, "This is a computer generated invoice.")
    c.drawRightString(W - m - 4 * mm, m + 6 * mm, f"For {s['vendor']['name']}  -  Authorised Signatory")


# --------------------------------------------------------------------------- #
# Layout B - "modern" minimal, colour band, western number format
# --------------------------------------------------------------------------- #
def layout_modern(c: canvas.Canvas, s: dict) -> None:
    W, H = A4
    m = 18 * mm
    accent = colors.HexColor(s.get("accent", "#1f4e79"))
    c.setFillColor(accent)
    c.rect(0, H - 32 * mm, W, 32 * mm, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(m, H - 17 * mm, s["vendor"]["name"])
    c.setFont("Helvetica", 9)
    c.drawString(m, H - 24 * mm, "  |  ".join(s["vendor"]["address"]))
    c.setFont("Helvetica-Bold", 22)
    c.drawRightString(W - m, H - 18 * mm, "INVOICE")
    c.setFillColor(colors.black)

    y = H - 44 * mm
    c.setFont("Helvetica", 9)
    c.drawString(m, y, f"GSTIN {s['vendor']['gstin']}")
    c.drawString(m, y - 5 * mm, s["vendor"]["email"])
    ry = y
    for label, val in s["meta"]:
        c.setFont("Helvetica", 9)
        c.drawRightString(W - m - 40 * mm, ry, label)
        c.setFont("Helvetica-Bold", 9)
        c.drawRightString(W - m, ry, val)
        ry -= 5 * mm

    y = min(y - 5 * mm, ry) - 10 * mm
    c.setFont("Helvetica-Bold", 9)
    c.setFillColor(accent)
    c.drawString(m, y, "BILLED TO")
    c.setFillColor(colors.black)
    c.setFont("Helvetica", 9)
    c.drawString(m, y - 5 * mm, f"{BUYER['name']}, {', '.join(BUYER['address'])}")
    c.drawString(m, y - 10 * mm, f"GSTIN {BUYER['gstin']}")

    y -= 22 * mm
    c.setFillColor(colors.HexColor("#eef2f7"))
    c.rect(m, y - 2.5 * mm, W - 2 * m, 8 * mm, fill=1, stroke=0)
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 9)
    xs = [m + 2 * mm, m + 88 * mm, m + 115 * mm, m + 145 * mm, W - m - 2 * mm]
    c.drawString(xs[0], y, "Item")
    c.drawString(xs[1], y, "SAC/HSN")
    c.drawRightString(xs[2], y, "Qty")
    c.drawRightString(xs[3], y, "Unit Price")
    c.drawRightString(xs[4], y, "Line Total")
    c.setFont("Helvetica", 9)
    for ln in s["lines"]:
        y -= 8 * mm
        c.drawString(xs[0], y, ln["desc"])
        c.drawString(xs[1], y, ln.get("hsn", ""))
        c.drawRightString(xs[2], y, f"{ln['qty']:g}")
        c.drawRightString(xs[3], y, western(ln["rate"]))
        c.drawRightString(xs[4], y, western(ln["qty"] * ln["rate"]))
        c.setStrokeColor(colors.HexColor("#dddddd"))
        c.line(m, y - 3 * mm, W - m, y - 3 * mm)
    c.setStrokeColor(colors.black)
    y -= 6 * mm
    for label, val, bold in s["totals"]:
        y -= 6 * mm
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 10 if bold else 9)
        c.drawRightString(W - m - 40 * mm, y, label)
        c.drawRightString(W - m, y, ("INR " if bold else "") + western(val))

    y -= 16 * mm
    c.setFont("Helvetica-Bold", 9)
    c.setFillColor(accent)
    c.drawString(m, y, "PAYMENT INFORMATION")
    c.setFillColor(colors.black)
    c.setFont("Helvetica", 9)
    for line in s["bank"]:
        y -= 5 * mm
        c.drawString(m, y, line)
    for note in s.get("notes", []):
        y -= 8 * mm
        c.setFont("Helvetica-Bold", 9.5)
        c.setFillColor(colors.HexColor("#b00020"))
        c.drawString(m, y, note)
        c.setFillColor(colors.black)
    c.setFont("Helvetica", 8)
    c.drawCentredString(W / 2, 12 * mm, s.get("footer", "Thank you for your business."))


# --------------------------------------------------------------------------- #
# Layout C - "compact" two-column letter style (labels after values, no grid)
# --------------------------------------------------------------------------- #
def layout_compact(c: canvas.Canvas, s: dict) -> None:
    W, H = A4
    m = 20 * mm
    y = H - 22 * mm
    c.setFont("Times-Bold", 16)
    c.drawString(m, y, s["vendor"]["name"].upper())
    c.setFont("Times-Roman", 10)
    for line in s["vendor"]["address"]:
        y -= 5 * mm
        c.drawString(m, y, line)
    y -= 5 * mm
    c.drawString(m, y, f"GST Reg. No. {s['vendor']['gstin']}   |   {s['vendor']['email']}")
    y -= 4 * mm
    c.line(m, y, W - m, y)
    y -= 9 * mm
    c.setFont("Times-Bold", 13)
    c.drawString(m, y, s.get("title", "Tax Invoice"))
    c.setFont("Times-Roman", 10)
    ry = y
    for label, val in s["meta"]:
        c.drawRightString(W - m, ry, f"{label} {val}")
        ry -= 5 * mm
    y = min(y, ry) - 8 * mm
    c.drawString(m, y, f"To: {BUYER['name']}")
    c.drawString(m, y - 5 * mm, ", ".join(BUYER["address"]))
    c.drawString(m, y - 10 * mm, f"GSTIN {BUYER['gstin']}")
    y -= 22 * mm
    c.setFont("Times-Bold", 10)
    c.drawString(m, y, "Particulars")
    c.drawRightString(m + 118 * mm, y, "Qty")
    c.drawRightString(m + 145 * mm, y, "Rate")
    c.drawRightString(W - m, y, "Amount")
    c.line(m, y - 2 * mm, W - m, y - 2 * mm)
    c.setFont("Times-Roman", 10)
    for ln in s["lines"]:
        y -= 7 * mm
        c.drawString(m, y, ln["desc"])
        c.drawRightString(m + 118 * mm, y, f"{ln['qty']:g}")
        c.drawRightString(m + 145 * mm, y, inr(ln["rate"]))
        c.drawRightString(W - m, y, inr(ln["qty"] * ln["rate"]))
    y -= 4 * mm
    c.line(m, y, W - m, y)
    for label, val, bold in s["totals"]:
        y -= 6 * mm
        c.setFont("Times-Bold" if bold else "Times-Roman", 10.5)
        c.drawRightString(m + 145 * mm, y, label)
        c.drawRightString(W - m, y, inr(val))
    y -= 10 * mm
    c.setFont("Times-Italic", 9.5)
    for note in s.get("notes", []):
        c.drawString(m, y, note)
        y -= 5 * mm
    y -= 6 * mm
    c.setFont("Times-Bold", 10)
    c.drawString(m, y, "Remit to:")
    c.setFont("Times-Roman", 10)
    for line in s["bank"]:
        y -= 5 * mm
        c.drawString(m, y, line)
    c.drawRightString(W - m, 25 * mm, "Authorised Signatory")


LAYOUTS = {"classic": layout_classic, "modern": layout_modern, "compact": layout_compact}


def build_pdf(spec: dict) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle(spec.get("doc_title", "Invoice"))
    c.setAuthor(spec["vendor"]["name"])
    LAYOUTS[spec["layout"]](c, spec)
    c.showPage()
    c.save()
    return buf.getvalue()


def rasterise(pdf_bytes: bytes, seed: int = 7) -> bytes:
    """Turn a PDF into an image-only 'scanned' PDF: grey, slightly skewed, noisy."""
    rnd = random.Random(seed)
    src = fitz.open(stream=pdf_bytes, filetype="pdf")
    out = fitz.open()
    for page in src:
        pix = page.get_pixmap(dpi=140)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples).convert("L")
        img = img.rotate(-0.9, resample=Image.BICUBIC, expand=False, fillcolor=245)
        px = img.load()
        for _ in range(int(img.width * img.height * 0.004)):
            x, y = rnd.randrange(img.width), rnd.randrange(img.height)
            px[x, y] = rnd.choice((90, 140, 200))
        img = img.filter(ImageFilter.GaussianBlur(0.6))
        # warm paper tint
        img = Image.merge("RGB", (img.point(lambda v: min(255, v + 4)), img, img.point(lambda v: max(0, v - 8))))
        jb = io.BytesIO()
        img.save(jb, format="JPEG", quality=55)
        p = out.new_page(width=page.rect.width, height=page.rect.height)
        p.insert_image(p.rect, stream=jb.getvalue())
    return out.tobytes()


def gst_totals(lines, mode, rate=0.18):
    sub = round(sum(l["qty"] * l["rate"] for l in lines), 2)
    tax = round(sub * rate, 2)
    if mode == "cgst_sgst":
        half = round(tax / 2, 2)
        return sub, [("Taxable Value", sub, False), ("CGST @ 9%", half, False),
                     ("SGST @ 9%", half, False), ("Grand Total", round(sub + 2 * half, 2), True)]
    return sub, [("Sub Total", sub, False), ("IGST @ 18%", tax, False),
                 ("Total Amount Payable", round(sub + tax, 2), True)]


SCENARIOS: list[dict] = []

# 01 - Happy path: Apex Office Supplies, clean digital, explicit PO, 3-way match passes
lines = [
    {"desc": "A4 Copier Paper 75 GSM (Box of 5 reams)", "hsn": "4802", "qty": 40, "rate": 1150.00},
    {"desc": "Ballpoint Pens Blue (Pack of 50)", "hsn": "9608", "qty": 20, "rate": 420.00},
    {"desc": "Toner Cartridge 88A Compatible", "hsn": "8443", "qty": 6, "rate": 2850.00},
]
_, totals = gst_totals(lines, "cgst_sgst")
SCENARIOS.append({
    "file": "01_happy_path_apex_office.pdf", "layout": "classic",
    "vendor": {"name": "Apex Office Supplies Pvt Ltd", "gstin": "29AACCA7315M1ZF",
               "address": ["No. 88, 2nd Cross, Jayanagar 4th Block", "Bengaluru, Karnataka 560011"],
               "email": "billing@apexoffice.example"},
    "meta": [("Invoice No.", "AOS/2026/1187"), ("Invoice Date", "14-Nov-2026"),
             ("PO Number", "PO-2026-0412"), ("Payment Terms", "Net 30 days")],
    "lines": lines, "totals": totals,
    "words": "Rupees Eighty Four Thousand Three Hundred Seventy Only",
    "bank": ["A/c Name: Apex Office Supplies Pvt Ltd", "A/c No: 50200012345678",
             "IFSC: HDFC0001234   Bank: HDFC Bank, Jayanagar"],
})

# 02 - Split billing over-run: Coldline bills 6 chillers + 6 installs, but only 6 chillers
#      were ever received and 4 were already billed on CRL/2026/118.
lines = [
    {"desc": "Glass Door Display Chiller 400L", "hsn": "8418", "qty": 6, "rate": 68500.00},
    {"desc": "Installation & Commissioning (per unit)", "hsn": "9954", "qty": 6, "rate": 3500.00},
]
_, totals = gst_totals(lines, "igst")
SCENARIOS.append({
    "file": "02_split_billing_overrun_coldline.pdf", "layout": "modern", "accent": "#0b6e6e",
    "vendor": {"name": "Coldline Refrigeration LLP", "gstin": "27AAKFC2290P1ZX",
               "address": ["Unit 4, Bhiwandi Logistics Park", "Thane, Maharashtra 421302"],
               "email": "accounts@coldline.example"},
    "meta": [("Invoice #", "CRL/2026/131"), ("Date", "18/11/2026"), ("Your PO Ref", "PO-2026-0398"),
             ("Billing", "Tranche 2 of 2 (balance)")],
    "lines": lines, "totals": totals,
    "bank": ["Beneficiary: Coldline Refrigeration LLP", "Account Number: 912020034567890",
             "IFSC: UTIB0000456 (Axis Bank, Bhiwandi)"],
    "footer": "Balance billing against PO-2026-0398. Previous invoice CRL/2026/118.",
})

# 03 - Duplicate in disguise: Sterling re-sends SPI/26-27/0457 (already approved) with a
#      reformatted invoice number and a different template.
lines = [
    {"desc": "3-Ply Corrugated Carton 12x10x8 in", "hsn": "4819", "qty": 5000, "rate": 18.50},
    {"desc": "BOPP Packing Tape 48mm x 65m", "hsn": "3919", "qty": 2000, "rate": 32.00},
]
_, totals = gst_totals(lines, "igst")
SCENARIOS.append({
    "file": "03_duplicate_resubmission_sterling.pdf", "layout": "compact",
    "title": "Tax Invoice (Copy - Payment Reminder)",
    "vendor": {"name": "Sterling Packaging Industries Pvt Ltd", "gstin": "24AAECS5512R1Z0",
               "address": ["Survey No. 221, GIDC Sanand-II", "Ahmedabad, Gujarat 382110"],
               "email": "ar@sterlingpack.example"},
    "meta": [("Inv. No.", "SPI-26-27-457"), ("Dated", "28.10.2026"), ("Order Ref.", "PO-2026-0377")],
    "lines": lines, "totals": totals,
    "notes": ["Payment reminder: kindly arrange remittance at the earliest."],
    "bank": ["ICICI Bank, Sanand Branch", "Account No. 001105023456  IFSC ICIC0000011"],
})

# 04 - Bank detail change (classic payment-diversion fraud pattern). Everything else is perfect.
lines = [
    {"desc": "Managed IT Support Services - November 2026", "hsn": "998313", "qty": 1, "rate": 120000.00},
    {"desc": "Laptop Rental Dell Latitude 5440 (unit-month)", "hsn": "997315", "qty": 25, "rate": 2400.00},
]
_, totals = gst_totals(lines, "cgst_sgst")
SCENARIOS.append({
    "file": "04_bank_change_kaveri_it.pdf", "layout": "modern", "accent": "#5b2a86",
    "vendor": {"name": "Kaveri IT Services Pvt Ltd", "gstin": "29AADCK8834N1ZS",
               "address": ["3rd Floor, Prestige Tech Park, Marathahalli", "Bengaluru, Karnataka 560103"],
               "email": "finance@kaveriit.example"},
    "meta": [("Invoice No", "KIT/1124/2026"), ("Invoice Date", "2026-11-20"), ("PO No", "PO-2026-0420"),
             ("Due Date", "2026-12-20")],
    "lines": lines, "totals": totals,
    "bank": ["Bank: Kotak Mahindra Bank, Whitefield", "Account No: 7781200045612",
             "IFSC: KKBK0000812"],
    "notes": ["IMPORTANT: We have changed our bankers. Please update records and remit to the account above."],
})

# 05 - Scanned, no PO reference, tax-inclusive pricing, small price variance (within tolerance).
lines = [
    {"desc": "Floor Cleaner Concentrate 5 Ltr", "hsn": "3402", "qty": 60, "rate": 760.00},
    {"desc": "Hand Wash Refill 5 Ltr", "hsn": "3401", "qty": 40, "rate": 613.60},
]
gross = round(sum(l["qty"] * l["rate"] for l in lines), 2)
taxable = round(gross / 1.18, 2)
SCENARIOS.append({
    "file": "05_scanned_no_po_greenleaf.pdf", "layout": "compact", "scan": True,
    "vendor": {"name": "Greenleaf Hygiene Solutions", "gstin": "33AAJFG6619Q1ZU",
               "address": ["14/2 Ambattur Industrial Estate", "Chennai, Tamil Nadu 600058"],
               "email": "greenleaf.accounts@example.com"},
    "meta": [("Bill No.", "GHS/0931"), ("Date", "12 Nov 2026")],
    "lines": lines,
    "totals": [("Total (incl. GST)", gross, True), ("Taxable value", taxable, False),
               ("IGST @ 18% included", round(gross - taxable, 2), False)],
    "notes": ["All prices are inclusive of GST.", "Goods delivered to Halcyon Foods Peenya warehouse."],
    "bank": ["Canara Bank, Ambattur", "A/c No. 6012004500789  IFSC CNRB0006012"],
})

# 06 - Blocked vendor
lines = [{"desc": "LED Panel Light 18W", "hsn": "9405", "qty": 120, "rate": 640.00}]
_, totals = gst_totals(lines, "cgst_sgst")
SCENARIOS.append({
    "file": "06_blocked_vendor_orbit.pdf", "layout": "classic",
    "vendor": {"name": "Orbit Electricals Pvt Ltd", "gstin": "29AABCO3307L1ZC",
               "address": ["21, SP Road", "Bengaluru, Karnataka 560002"], "email": "orbit.billing@example.com"},
    "meta": [("Invoice No.", "OE/2026/0662"), ("Invoice Date", "10-Nov-2026"), ("PO Number", "PO-2026-0389")],
    "lines": lines, "totals": totals, "words": "Rupees Ninety Thousand Six Hundred Twenty Four Only",
    "bank": ["A/c No: 10223344556", "IFSC: ICIC0000777"],
})

# 07 - Incomplete invoice: no invoice number, no invoice date
lines = [
    {"desc": "Whiteboard Markers Assorted (Set of 12)", "hsn": "9608", "qty": 30, "rate": 380.00},
    {"desc": "Lever Arch Files FC Size", "hsn": "4820", "qty": 100, "rate": 145.00},
]
_, totals = gst_totals(lines, "cgst_sgst")
SCENARIOS.append({
    "file": "07_missing_fields_apex.pdf", "layout": "classic",
    "vendor": {"name": "Apex Office Supplies Pvt Ltd", "gstin": "29AACCA7315M1ZF",
               "address": ["No. 88, 2nd Cross, Jayanagar 4th Block", "Bengaluru, Karnataka 560011"],
               "email": "billing@apexoffice.example"},
    "meta": [("PO Number", "PO-2026-0433"), ("Payment Terms", "Net 30 days")],
    "lines": lines, "totals": totals, "words": "Rupees Thirty Thousand Five Hundred Sixty Two Only",
    "bank": ["A/c No: 50200012345678", "IFSC: HDFC0001234"],
})


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for spec in SCENARIOS:
        pdf = build_pdf(spec)
        if spec.get("scan"):
            pdf = rasterise(pdf)
        (OUT / spec["file"]).write_bytes(pdf)
        print("wrote", spec["file"], f"{len(pdf)/1024:.0f} KB")


if __name__ == "__main__":
    main()

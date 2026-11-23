# Design notes

## Why I picked PS-1

With invoices, mistakes cost money either way. Pay a duplicate or a fraudster and the money is usually gone. Block a genuine invoice and you've annoyed a supplier who's waiting to be paid. So the questions I had to answer were how much I trust the reading of the document, which parts have to be fixed rules, and how a person takes over when something's off.

It also had the most realistic edge cases of the problem statements. Split billing, resent invoices, bank-detail fraud, bad scans, missing POs and GST mistakes all happen in real AP teams every week.

## Main decisions

### 1. Reading and deciding are separate

One part of the code turns the PDF into a fixed set of fields (`InvoiceExtraction`). Everything after that, the checks, tolerances and final decision, works only on those fields and is plain Python.

An AP decision has to come out the same way every time, and when a controller asks why an invoice was held, the answer should be a specific rule. Keeping the reading separate also means the reading method could be swapped later without touching a single rule.

### 2. Runs fully offline, no API key

I wanted anyone to be able to clone this and run it, with no account, no key, no cost and no network. It also means invoice data never leaves the machine, which matters for something carrying bank details and GSTINs.

- Digital PDFs: words and their positions come straight from the text layer.
- Scanned PDFs: words and their positions come from RapidOCR, which runs PaddleOCR models through ONNX Runtime on the CPU. I picked the "tiny" models (about 6 MB in total, kept in `models/ocr/`). In my tests they were about three times faster than the bigger ones and read the sample scan just as well.

Both kinds of input end up as the same thing, rows of words with positions, so there's one parser for both. It doesn't depend on column positions: a row is a line item if three of its numbers satisfy qty × rate = amount, which works across different invoice layouts.

### 3. Not trusting OCR blindly

OCR makes character mistakes, so I added three safety nets:

- The arithmetic checks. qty × rate, lines adding up to the subtotal, subtotal plus tax equalling the total, and tax matching the PO rates. A misread digit almost always breaks one of these, and the invoice goes to review.
- GSTIN repair. OCR confuses look-alike characters (I and 1, O and 0, S and 5). A GSTIN has fixed digit and letter positions and a check digit, so the app only accepts a corrected version if its check digit is valid. In testing, the tiny model read `…Q1ZU` as `…QIZU`, and this put it right.
- Confidence. If any line of text was read below 80% confidence (a setting in `Policy`), the invoice goes to review with a note to check the figures against the PDF.

If OCR can't run at all, the scan goes to manual entry rather than the app guessing.

### 4. Never guess a missing field

If something isn't on the invoice, its field stays empty. The parser doesn't fill in a PO, invoice number or date. When the PO is missing, a separate step works it out: it scores the vendor's open POs, explains the score, and flags the result for a person to confirm.

### 5. Every check carries its own effect

Each check returns pass, warn, fail, info or skip. A failing check can also carry an effect (review, hold or reject), the team that should act, and what we need from the vendor, if anything. The decision is the most serious effect among the checks, and the explanation is just the list of checks that had one. Adding a new rule means writing one check, and the UI and decision pick it up.

### 6. Duplicates are checked early and stop the run

In my first version the duplicate check ran near the end. The resent invoice came out rejected, but with six extra failures listed ("quantity exceeds PO" and so on) that only happened because it was a duplicate. A reviewer would have had to dig for the real reason. So I moved it before the PO match, and a confirmed duplicate now ends the run. A possible duplicate (same amount, different number) doesn't end it; it goes to review.

### 7. No vendor email on fraud holds

For a bank change, a GSTIN that belongs to another vendor, or a PO from a different vendor, the app doesn't draft an email to the vendor. The person sending a fake invoice may be the one reading that inbox. The right step is to call the number already on the vendor master. Releasing the hold requires a written note of what was verified.

### 8. Pending invoices go in the ledger

Invoices under review or on hold are recorded in the ledger as pending, so if the vendor resends a copy while the first is still being looked at, it's caught. Only approved or paid invoices count against the PO's quantity and value.

### 9. Pay what's owed instead of rejecting the whole invoice

When an invoice bills for goods that haven't arrived yet, the app works out what can be paid now (received minus already billed, at the lower of the invoice and PO price, plus tax) and suggests paying that and holding the rest. A good AP clerk does the same thing.

### 10. Stack: Python, FastAPI, SQLite, plain JS

One process, no front-end build, nothing external. The browser gets each step as it finishes over Server-Sent Events. SQLite stores every event of every run plus the ledger. Runs go one at a time behind a lock, so two copies of the same invoice uploaded together can't both slip past the duplicate check. OCR results are cached by file hash, and the OCR models load in the background when the server starts, so the first scan isn't slower than the rest.

## Assumptions

- There's one buyer, Halcyon Foods Pvt Ltd, GSTIN starting 29 (Karnataka). Everything is in INR, and an invoice with no currency is treated as INR.
- Dates written in numbers are day first (dd/mm/yyyy), as is normal in India.
- The vendor master, POs and goods receipts are the source of truth. In a real setup they'd come from the ERP; here they're CSV files in `data/`.
- Price tolerance: the unit price can be up to 2% above the PO, and the extra on a line can't exceed ₹1,000. Both conditions have to hold. Being cheaper than the PO is always fine. Arithmetic is allowed ₹1 of slack for rounding.
- Anything above ₹10 lakh needs the Finance Controller's sign-off, even if every check passes.
- If the app had to work out the PO itself, a person confirms it before payment (`auto_approve_inferred_po = False`). It's a setting, so a company could turn it off.
- Services use the same receipt logic, with a service entry sheet in `goods_receipts.csv` in place of a goods receipt.
- Payment date is the invoice date plus the vendor's payment terms from the master.
- Scans are roughly upright. Small tilts are corrected; a page scanned sideways or upside down isn't.
- Every company, GSTIN, bank account and email is made up. The GSTINs have valid check digits.

## Limitations

- The parser was built and tested against my three sample layouts. It doesn't use fixed positions, so it should cope with other simple layouts, but a very unusual invoice (multi-column, tables with merged cells, handwriting) would probably end up in review with missing fields. That fails safe, but it isn't automation.
- A line item whose description wraps onto a second line isn't joined back together.
- OCR on a laptop CPU takes around 10 seconds a page. That's fine for a demo or a small team, but a high-volume setup would want a GPU or a queue.
- One buyer, one currency, GST only. No TDS, foreign currency or credit notes.
- Money is stored as floats rounded to 2 decimals. A production version should use `Decimal` throughout.
- Releasing a bank-change hold doesn't update the vendor's bank details. That should go through its own approval process, so I left it out on purpose.
- SQLite with a single processing lock is fine for a demo, or a few hundred invoices a day. It wouldn't scale across several servers.
- No login or user roles.

## What I'd do next

1. Email intake. Most invoices arrive by email, so I'd watch a mailbox (Gmail or Microsoft Graph) and feed attachments straight in.
2. Connect to the ERP (Tally, SAP, NetSuite) instead of CSVs, and push approved invoices back as bills.
3. Handle messier layouts. Either a layout-aware model or a language model for the reading step only. Because reading and deciding are separate, the rules wouldn't change.
4. Credit notes and partial payments: link a held-back amount to the later goods receipt or credit note and release it automatically.
5. Learn from reviewers. If reviewers always approve a particular exception, like one vendor's habit of rounding up by 1%, suggest a policy change for a person to accept.
6. Show where each field was found on the PDF, with its OCR confidence, highlighted in the UI.
7. Roles and separation of duties, so the person who releases a hold can't be the same person who changed the vendor's bank details.

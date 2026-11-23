# Demo walkthrough (5 minutes)

## Before starting

1. Start the server with `uvicorn app.main:app --port 8000` and open http://localhost:8000. Browser zoom around 90% fits everything on screen.
2. Check the pill in the top-right says "Runs locally · OCR ready".
3. Click **Reset demo data**. Running a sample twice gets it caught as a duplicate, so reset before every take.
4. Leave `STAGE_DELAY_MS=350` so each step is visible. Setting it to 0 shows how fast it really is.

Tip: run the scanned sample once before recording, then reset. The OCR result is cached by file, so on camera it reads instantly instead of taking 10 seconds. If you'd rather show the real OCR time, delete `runtime/ocr_cache/` first.

## 0:00 to 0:30 – Intro

> "In most AP teams someone opens each invoice, finds the PO, checks the numbers and decides whether to pay. I've built that whole process, and it runs entirely on this laptop, with no API and no internet. One part reads the invoice, from the text layer or with OCR if it's a scan. Fixed rules make the decision, so every approval, hold or rejection points back to a specific check that a controller or auditor can follow."

Show the ten steps on the home screen, then the Reference data tab (POs with ordered, received and billed quantities).

## 0:30 to 1:15 – Normal invoice

Run "Clean invoice, full 3-way match".

- Talk through the steps as they turn green: file check, read, validate, vendor, duplicates, PO, 3-way match, GST, decision, ledger.
- Open the Read invoice step to show the fields and line items it pulled out.
- Result: approved, all checks passed, payment scheduled for 14 Dec on Net 30, booked to the ledger.

## 1:15 to 2:15 – Billing for goods that haven't arrived

Run "Split billing".

> "There's nothing wrong with this invoice on its own. Prices match the PO and the maths is right. A checker that only looks at the invoice would approve it."

- Open the 3-way match step: 10 ordered, 6 received, 4 already billed on an earlier invoice, 6 billed now. Only 2 are actually payable.
- The decision card says pay ₹1,61,660 now and hold back ₹3,48,100. There's a drafted email asking the vendor for a revised invoice, which someone approves before it's sent.

## 2:15 to 3:00 – A resent invoice in disguise

Run "Duplicate in disguise".

- "It's a different layout, the number changed from `SPI/26-27/0457` to `SPI-26-27-457`, and it's labelled as a payment reminder."
- The duplicate step strips the formatting, finds the original that was already approved, and stops. The steps after it show as skipped.
- "This check used to run last, and the real reason got buried under PO errors that only happened because it was a duplicate. So I moved it earlier."

## 3:00 to 3:50 – Changed bank account

Run "Bank account changed".

- Every check passes except the bank one. The invoice asks for payment to a new Kotak account, while the vendor master has SBI.
- It goes on hold, in the Treasury queue. Point out that no vendor email is drafted, on purpose. The right step is to call the number we already have for the vendor.
- Click *Verified – release* with the note empty. It gets refused. Type a note, release it, and show the resolution saved.

## 3:50 to 4:30 – Scanned invoice with no PO

Run "Scanned, no PO". Click *Preview PDF* first so they can see it's a tilted, noisy image.

- Read step: OCR reads the scan locally. Point out the confidence figure and that the GSTIN was checked against its check digit.
- "There's no text layer to compare against, so the arithmetic checks are what confirm OCR read the numbers right."
- PO step: it compares the vendor's two open POs and picks PO-2026-0405, 96% against 8%.
- 3-way match: the prices include GST, so it removes 18% before comparing. The floor cleaner is 0.64% above the PO, which is within tolerance.
- It's in review for one reason only: someone needs to confirm the PO it picked.

## 4:30 to 5:00 – Dashboard and wrap-up

- Go through the numbers: straight-through rate, value approved, value protected (duplicates, holds and held-back amounts), open exceptions, average processing time.
- Click any row to replay the full record of that run.

> "All the thresholds live in one file. Next I'd add email intake and ERP connections, and learn from what reviewers approve. Because reading and deciding are separate, I could also swap in a stronger reader for messy layouts without touching any of the rules."

## If something breaks during the demo

`python scripts/process_invoice.py samples/*.pdf --reset` runs the same steps in the terminal.

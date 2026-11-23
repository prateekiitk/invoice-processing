"""HTTP API + static UI.

    uvicorn app.main:app --reload
"""
from __future__ import annotations

import asyncio
import json
import shutil
import threading
from collections import Counter
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, ocr
from .config import BUYER, POLICY, SAMPLES_DIR, UPLOADS_DIR, ensure_dirs
from .pipeline import LIVE, STAGES, _billed, start_run
from .reference import ref

STATIC = Path(__file__).parent / "static"
MAX_UPLOAD = 15 * 1024 * 1024

app = FastAPI(title="AP Autopilot", version="1.0")
ensure_dirs()
db.conn()

# Load the OCR models in the background so the first scanned invoice isn't slow.
threading.Thread(target=ocr.warm_up, daemon=True).start()


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/api/health")
def health():
    ocr_ok, ocr_problem = ocr.available()
    return {"ok": True, "ocr_available": ocr_ok, "ocr_problem": ocr_problem,
            "buyer": {"name": BUYER.name, "gstin": BUYER.gstin}, "stages": [{"id": i, "title": t} for i, t in STAGES],
            "policy": POLICY.__dict__}


# ---- samples ---------------------------------------------------------------
def _scenarios() -> list[dict]:
    meta = SAMPLES_DIR / "scenarios.json"
    items = json.loads(meta.read_text(encoding="utf-8")) if meta.exists() else []
    return [i for i in items if (SAMPLES_DIR / i["file"]).exists()]


@app.get("/api/samples")
def samples():
    return _scenarios()


def _sample_path(name: str) -> Path:
    if name not in {s["file"] for s in _scenarios()}:
        raise HTTPException(404, "Unknown sample")
    return SAMPLES_DIR / name


@app.get("/api/samples/{name}")
def sample_file(name: str):
    return FileResponse(_sample_path(name), media_type="application/pdf")


# ---- runs ------------------------------------------------------------------
@app.post("/api/runs")
async def create_run(file: UploadFile = File(...)):
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file")
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "File larger than 15 MB")
    run_id = start_run(Path(file.filename or "upload.pdf").name, data, source="upload")
    return {"run_id": run_id}


@app.post("/api/runs/sample/{name}")
def create_sample_run(name: str):
    p = _sample_path(name)
    return {"run_id": start_run(p.name, p.read_bytes(), source="sample")}


@app.get("/api/runs")
def runs():
    return db.list_runs()


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str):
    r = db.get_run(run_id)
    if not r:
        raise HTTPException(404, "Run not found")
    if r["status"] == "running":
        r["events"] = LIVE.get(run_id, [])
    return r


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str):
    """Server-sent events: replays everything so far, then streams new events until the run completes."""
    if not db.get_run(run_id):
        raise HTTPException(404, "Run not found")

    async def gen():
        idx = 0
        while True:
            evs = LIVE.get(run_id)
            if evs is None:  # finished before this server process started
                r = db.get_run(run_id)
                evs = r["events"] or []
            while idx < len(evs):
                ev = evs[idx]
                idx += 1
                yield f"data: {json.dumps(ev, default=str)}\n\n"
                if ev["type"] == "run_completed":
                    return
            await asyncio.sleep(0.08)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/runs/{run_id}/document")
def run_document(run_id: str):
    p = UPLOADS_DIR / f"{run_id}.pdf"
    if not p.exists():
        raise HTTPException(404, "Document not found")
    return FileResponse(p, media_type="application/pdf")


@app.get("/api/runs/{run_id}/export")
def run_export(run_id: str):
    r = db.get_run(run_id)
    if not r:
        raise HTTPException(404, "Run not found")
    body = {"run_id": r["id"], "created_at": r["created_at"], "filename": r["filename"],
            "decision": (r["result"] or {}).get("decision"), "invoice": (r["result"] or {}).get("invoice"),
            "checks": (r["result"] or {}).get("checks"), "resolution": r["resolution"]}
    return JSONResponse(body, headers={"Content-Disposition": f'attachment; filename="{run_id}.json"'})


class Resolution(BaseModel):
    action: str            # approve | reject
    note: str = ""
    reviewer: str = "AP Reviewer"


@app.post("/api/runs/{run_id}/resolve")
def resolve(run_id: str, body: Resolution):
    r = db.get_run(run_id)
    if not r:
        raise HTTPException(404, "Run not found")
    decision = (r["result"] or {}).get("decision") or {}
    if decision.get("outcome") not in ("NEEDS_REVIEW", "ON_HOLD"):
        raise HTTPException(409, "Only invoices in review or on hold can be resolved")
    if r["resolution"]:
        raise HTTPException(409, "Already resolved")
    if body.action not in ("approve", "reject"):
        raise HTTPException(400, "action must be approve or reject")
    if decision.get("outcome") == "ON_HOLD" and body.action == "approve" and len(body.note.strip()) < 10:
        raise HTTPException(400, "Releasing a held payment requires a verification note (min 10 chars)")
    final = "APPROVED" if body.action == "approve" else "REJECTED"
    db.update_ledger_status_for_run(run_id, final)
    res = {"action": body.action, "final_status": final, "note": body.note, "reviewer": body.reviewer,
           "at": db.now_iso()}
    db.set_resolution(run_id, res)
    return res


# ---- dashboard -------------------------------------------------------------
@app.get("/api/stats")
def stats():
    rs = [r for r in db.list_runs(10_000) if r["status"] != "running"]
    by = Counter(r["decision"] for r in rs if r["decision"])
    n = len(rs)
    value = sum(r["total"] or 0 for r in rs)

    def final(r):  # a reviewer's resolution overrides the system decision for money totals
        return (r["resolution"] or {}).get("final_status") or r["decision"]

    approved_value = sum(r["total"] or 0 for r in rs if final(r) == "APPROVED")
    # money stopped from leaving: rejected invoices + payments still frozen on hold (+ short-pay withholdings below)
    protected = sum(r["total"] or 0 for r in rs if final(r) in ("REJECTED", "ON_HOLD"))
    reasons: Counter = Counter()
    for r in rs:
        full = db.get_run(r["id"])
        dec = ((full or {}).get("result") or {}).get("decision") or {}
        for rs_ in dec.get("reasons", [])[:1]:
            reasons[(rs_.get("label") or rs_["title"]).capitalize()] += 1
        pa = dec.get("partial_approval")
        if pa and final(r) == "NEEDS_REVIEW":
            protected += pa["withheld"]
    durations = [r["duration_ms"] for r in rs if r["duration_ms"]]
    open_items = sum(1 for r in rs if r["decision"] in ("NEEDS_REVIEW", "ON_HOLD") and not r["resolution"])
    return {
        "runs": n, "by_decision": {k: by.get(k, 0) for k in ("APPROVED", "NEEDS_REVIEW", "ON_HOLD", "REJECTED")},
        "straight_through_rate": round(by.get("APPROVED", 0) / n, 3) if n else 0,
        "value_processed": round(value, 2), "value_approved": round(approved_value, 2),
        "value_protected": round(protected, 2), "open_exceptions": open_items,
        "avg_duration_ms": int(sum(durations) / len(durations)) if durations else 0,
        "top_reasons": reasons.most_common(6),
    }


@app.get("/api/reference")
def reference():
    R = ref()
    pos = []
    for po in R.pos.values():
        billed, _ = _billed(po)
        pos.append({
            "po_number": po.po_number, "vendor": R.vendors[po.vendor_id].legal_name, "po_date": po.po_date.isoformat(),
            "status": po.status, "department": po.department, "total": po.total,
            "lines": [{"line_no": l.line_no, "description": l.description, "qty": l.quantity, "uom": l.uom,
                       "unit_price": l.unit_price, "tax_rate": l.tax_rate, "received": l.received_qty,
                       "billed": billed.get(l.line_no, 0)} for l in po.lines],
        })
    vendors = [{**v.__dict__, "bank_account": f"XXXX{v.bank_account[-4:]}"} for v in R.vendors.values()]
    return {"vendors": vendors, "purchase_orders": pos, "ledger": db.all_ledger()}


@app.post("/api/reset")
def reset():
    db.reset_all()
    LIVE.clear()
    shutil.rmtree(UPLOADS_DIR, ignore_errors=True)
    ensure_dirs()
    return {"ok": True}

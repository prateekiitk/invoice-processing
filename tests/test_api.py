"""HTTP API, including the reviewer workflow."""
import time

import pytest
from fastapi.testclient import TestClient

from app.main import app
from conftest import SAMPLES

client = TestClient(app)


def wait(run_id, timeout=20):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = client.get(f"/api/runs/{run_id}").json()
        if r["status"] != "running":
            return r
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def test_health_and_samples():
    h = client.get("/api/health").json()
    assert h["ok"] and h["ocr_available"] and len(h["stages"]) == 10
    assert len(client.get("/api/samples").json()) == 7


def test_upload_run_events_and_export():
    with open(SAMPLES / "01_happy_path_apex_office.pdf", "rb") as f:
        rid = client.post("/api/runs", files={"file": ("inv.pdf", f, "application/pdf")}).json()["run_id"]
    r = wait(rid)
    assert r["decision"] == "APPROVED"
    sse = client.get(f"/api/runs/{rid}/events").text
    assert sse.count("stage_completed") == 10 and "run_completed" in sse
    exp = client.get(f"/api/runs/{rid}/export").json()
    assert exp["decision"]["outcome"] == "APPROVED"
    assert client.get(f"/api/runs/{rid}/document").headers["content-type"] == "application/pdf"


def test_reviewer_approves_short_pay_case_and_ledger_updates():
    rid = client.post("/api/runs/sample/02_split_billing_overrun_coldline.pdf").json()["run_id"]
    assert wait(rid)["decision"] == "NEEDS_REVIEW"
    res = client.post(f"/api/runs/{rid}/resolve", json={"action": "approve", "note": "GRN for 4 units posted late"})
    assert res.status_code == 200 and res.json()["final_status"] == "APPROVED"
    ledger = client.get("/api/reference").json()["ledger"]
    assert any(l["invoice_number"] == "CRL/2026/131" and l["status"] == "APPROVED" for l in ledger)
    assert client.post(f"/api/runs/{rid}/resolve", json={"action": "approve"}).status_code == 409  # only once


def test_releasing_a_hold_requires_verification_note():
    rid = client.post("/api/runs/sample/04_bank_change_kaveri_it.pdf").json()["run_id"]
    assert wait(rid)["decision"] == "ON_HOLD"
    assert client.post(f"/api/runs/{rid}/resolve", json={"action": "approve", "note": ""}).status_code == 400
    ok = client.post(f"/api/runs/{rid}/resolve", json={"action": "approve", "note": "Called number on file; CFO confirmed new account"})
    assert ok.status_code == 200


def test_cannot_resolve_an_approved_invoice():
    rid = client.post("/api/runs/sample/01_happy_path_apex_office.pdf").json()["run_id"]
    wait(rid)
    assert client.post(f"/api/runs/{rid}/resolve", json={"action": "reject"}).status_code == 409


def test_stats_and_reset():
    for s in ("01_happy_path_apex_office.pdf", "03_duplicate_resubmission_sterling.pdf"):
        wait(client.post(f"/api/runs/sample/{s}").json()["run_id"])
    st = client.get("/api/stats").json()
    assert st["runs"] == 2 and st["by_decision"]["APPROVED"] == 1 and st["value_protected"] == pytest.approx(184670.0)
    client.post("/api/reset")
    assert client.get("/api/stats").json()["runs"] == 0


def test_rejects_oversized_and_empty_upload():
    assert client.post("/api/runs", files={"file": ("e.pdf", b"", "application/pdf")}).status_code == 400
    assert client.post("/api/runs/sample/../../etc/passwd").status_code == 404

"""End-to-end tests for fertilizer flow reconciliation and approval.

Covers: consumption math across refills, missing-sequence gaps (never
zero), control-consumption and weight-increase anomalies blocking
approval, late insertion/correction re-checking neighbours, plot-area
corrections updating doses, stale/expired approval reports, and the
single-snapshot provenance shared with the trial data version.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


# Two blocks, A = control, B = treated; area 20 m^2 everywhere.
PLOTS = {
    "plots": [
        {"block_id": 1, "treatment": "A", "area": 20.0, "fresh_weight": 1.0},
        {"block_id": 1, "treatment": "B", "area": 20.0, "fresh_weight": 1.0},
        {"block_id": 2, "treatment": "A", "area": 25.0, "fresh_weight": 1.0},
        {"block_id": 2, "treatment": "B", "area": 25.0, "fresh_weight": 1.0},
    ]
}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "trial.db"))
    with TestClient(app) as test_client:
        yield test_client


def import_plots(client) -> None:
    resp = client.post("/imports", json=PLOTS)
    assert resp.status_code == 200


def status(client) -> dict:
    return client.get("/fertilizer/status").json()


def test_consumption_math_with_refill_and_merged_dose(client):
    import_plots(client)
    # operation 1: start 100; B1 uses 30, refill 20; B2 uses 25
    resp = client.post(
        "/fertilizer/operations",
        json={
            "start_weight": 100.0,
            "entries": [
                {"seq": 1, "block_id": 1, "treatment": "B",
                 "remaining_weight": 70.0, "refill": 20.0},
                {"seq": 2, "block_id": 2, "treatment": "B",
                 "remaining_weight": 65.0},
            ],
        },
    )
    assert resp.status_code == 201
    op = status(client)["operations"][0]
    assert op["entries"][0]["previous_weight"] == 100.0
    assert op["entries"][0]["consumption"] == pytest.approx(30.0)
    assert op["entries"][1]["previous_weight"] == pytest.approx(90.0)  # 70 + 20
    assert op["entries"][1]["consumption"] == pytest.approx(25.0)

    # control plots appear as skipped-plot pending, never as zero dose
    st = status(client)
    assert st["anomalies"] == []
    assert st["approvable"] is True
    summaries = {(p["block_id"], p["treatment"]): p for p in st["plot_summaries"]}
    assert summaries[(1, "A")]["status"] == "pending"
    assert summaries[(1, "A")]["pending_reasons"] == ["no_application_record"]
    assert summaries[(1, "A")]["total_consumption"] is None
    b1 = summaries[(1, "B")]
    assert b1["status"] == "ok"
    assert b1["total_consumption"] == pytest.approx(30.0)
    assert b1["dose_per_area"] == pytest.approx(1.5)  # 30 / 20

    # second operation adds more consumption on the same plots -> merged dose
    client.post(
        "/fertilizer/operations",
        json={
            "start_weight": 50.0,
            "entries": [
                {"seq": 1, "block_id": 1, "treatment": "B", "remaining_weight": 40.0},
                {"seq": 2, "block_id": 2, "treatment": "B", "remaining_weight": 30.0},
            ],
        },
    )
    st = status(client)
    summaries = {(p["block_id"], p["treatment"]): p for p in st["plot_summaries"]}
    b1 = summaries[(1, "B")]
    assert b1["n_operations"] == 2
    assert b1["total_operations"] == 2
    assert b1["total_consumption"] == pytest.approx(40.0)  # 30 + 10
    assert b1["dose_per_area"] == pytest.approx(2.0)
    # plot 2B: 25 + 10 = 35 over 25 m^2
    assert summaries[(2, "B")]["dose_per_area"] == pytest.approx(35.0 / 25.0)


def test_missing_sequence_is_pending_not_zero(client):
    import_plots(client)
    # seq 2 missing: entry 3's reference weight is unknown
    resp = client.post(
        "/fertilizer/operations",
        json={
            "start_weight": 100.0,
            "entries": [
                {"seq": 1, "block_id": 1, "treatment": "B", "remaining_weight": 80.0},
                {"seq": 3, "block_id": 2, "treatment": "B", "remaining_weight": 70.0},
            ],
        },
    )
    assert resp.status_code == 201
    st = status(client)
    kinds = [p["kind"] for p in st["pending"]]
    assert "missing_sequence" in kinds
    assert "uncomputable_consumption" in kinds
    entries = st["operations"][0]["entries"]
    assert entries[0]["consumption"] == pytest.approx(20.0)
    assert entries[1]["consumption"] is None
    assert entries[1]["previous_weight"] is None

    # plot 2B total is unknown (not zero!) -> no dose, but approval still allowed
    summaries = {(p["block_id"], p["treatment"]): p for p in st["plot_summaries"]}
    assert summaries[(2, "B")]["total_consumption"] is None
    assert summaries[(2, "B")]["dose_per_area"] is None
    assert st["anomalies"] == []
    assert st["approvable"] is True

    # approval can still be created and preserves the pending state
    approval = client.post("/fertilizer/approvals")
    assert approval.status_code == 201
    saved_kinds = [p["kind"] for p in approval.json()["pending"]]
    assert "missing_sequence" in saved_kinds


def test_control_consumption_blocks_approval(client):
    import_plots(client)
    client.post(
        "/fertilizer/operations",
        json={
            "start_weight": 100.0,
            "entries": [
                # control plot A consumes -> anomaly
                {"seq": 1, "block_id": 1, "treatment": "A", "remaining_weight": 95.0},
                {"seq": 2, "block_id": 1, "treatment": "B", "remaining_weight": 80.0},
            ],
        },
    )
    st = status(client)
    assert [a["kind"] for a in st["anomalies"]] == ["control_consumption"]
    assert st["approvable"] is False

    resp = client.post("/fertilizer/approvals")
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "anomalies block approval"
    assert client.get("/fertilizer/approvals").json() == []

    # correcting the control weighing to zero consumption clears the anomaly
    resp = client.post(
        "/fertilizer/corrections",
        json={
            "operation_no": 1,
            "entries": [
                {"seq": 1, "remaining_weight": 100.0}
            ],
        },
    )
    assert resp.status_code == 200
    assert status(client)["anomalies"] == []
    approval = client.post("/fertilizer/approvals")
    assert approval.status_code == 201


def test_weight_increase_blocks_approval(client):
    import_plots(client)
    # remaining 120 > reference 100 -> impossible weight increase
    client.post(
        "/fertilizer/operations",
        json={
            "start_weight": 100.0,
            "entries": [
                {"seq": 1, "block_id": 1, "treatment": "B", "remaining_weight": 120.0},
            ],
        },
    )
    st = status(client)
    assert [a["kind"] for a in st["anomalies"]] == ["weight_increase"]
    assert client.post("/fertilizer/approvals").status_code == 422

    # refill raises the next reference legitimately: 80 then +50 -> 130; 120 ok
    client.post(
        "/fertilizer/corrections",
        json={
            "operation_no": 1,
            "entries": [
                {"seq": 1, "remaining_weight": 80.0, "refill": 50.0},
                {"seq": 2, "block_id": 2, "treatment": "B", "remaining_weight": 120.0},
            ],
        },
    )
    st = status(client)
    assert [a["kind"] for a in st["anomalies"]] == []
    entries = st["operations"][0]["entries"]
    assert entries[1]["previous_weight"] == pytest.approx(130.0)
    assert entries[1]["consumption"] == pytest.approx(10.0)
    assert client.post("/fertilizer/approvals").status_code == 201


def test_late_insertion_rechecks_neighbours(client):
    import_plots(client)
    # only seq 1 and 3 recorded -> seq 3 consumption uncomputable
    client.post(
        "/fertilizer/operations",
        json={
            "start_weight": 100.0,
            "entries": [
                {"seq": 1, "block_id": 1, "treatment": "B",
                 "remaining_weight": 80.0, "refill": 10.0},
                {"seq": 3, "block_id": 2, "treatment": "B",
                 "remaining_weight": 60.0},
            ],
        },
    )
    st = status(client)
    assert st["operations"][0]["entries"][1]["consumption"] is None

    # late insert seq 2: reference for seq 3 becomes 70+0 = 70, consumption 10
    resp = client.post(
        "/fertilizer/corrections",
        json={
            "operation_no": 1,
            "entries": [
                {"seq": 2, "block_id": 1, "treatment": "A",
                 "remaining_weight": 70.0}
            ],
        },
    )
    assert resp.status_code == 200
    st = status(client)
    assert [p["kind"] for p in st["pending"] if p["kind"] == "missing_sequence"] == []
    entries = st["operations"][0]["entries"]
    assert [e["seq"] for e in entries] == [1, 2, 3]
    assert entries[2]["previous_weight"] == pytest.approx(70.0)
    assert entries[2]["consumption"] == pytest.approx(10.0)
    # control plot A now shows consumption -> anomaly surfaces after re-check
    assert [a["kind"] for a in st["anomalies"]] == ["control_consumption"]


def test_late_insertion_requires_full_weighing(client):
    import_plots(client)
    client.post(
        "/fertilizer/operations",
        json={"start_weight": 100.0, "entries": [
            {"seq": 1, "block_id": 1, "treatment": "B", "remaining_weight": 90.0}
        ]},
    )
    resp = client.post(
        "/fertilizer/corrections",
        json={"operation_no": 1, "entries": [{"seq": 2, "remaining_weight": 80.0}]},
    )
    assert resp.status_code == 422
    assert "late insertion requires" in resp.json()["detail"]


def test_start_weight_correction_propagates(client):
    import_plots(client)
    client.post(
        "/fertilizer/operations",
        json={"start_weight": 100.0, "entries": [
            {"seq": 1, "block_id": 1, "treatment": "B", "remaining_weight": 90.0}
        ]},
    )
    assert status(client)["operations"][0]["entries"][0]["consumption"] == 10.0
    client.post(
        "/fertilizer/corrections",
        json={"operation_no": 1, "start_weight": 110.0},
    )
    entry = status(client)["operations"][0]["entries"][0]
    assert entry["previous_weight"] == 110.0
    assert entry["consumption"] == 20.0


def test_area_correction_updates_dose_and_expires_approval(client):
    import_plots(client)
    client.post(
        "/fertilizer/operations",
        json={"start_weight": 100.0, "entries": [
            {"seq": 1, "block_id": 1, "treatment": "B", "remaining_weight": 80.0}
        ]},
    )
    approval = client.post("/fertilizer/approvals").json()
    assert approval["stale"] is False
    b1 = next(
        p for p in approval["plot_summaries"]
        if p["block_id"] == 1 and p["treatment"] == "B"
    )
    assert b1["dose_per_area"] == pytest.approx(1.0)

    # area correction via the existing /corrections endpoint bumps data_version
    resp = client.post(
        "/corrections",
        json={"corrections": [
            {"block_id": 1, "treatment": "B", "area": 40.0}
        ]},
    )
    assert resp.status_code == 200
    old = client.get(f"/fertilizer/approvals/{approval['id']}").json()
    assert old["stale"] is True
    # frozen report keeps the old area/dose and raw provenance
    old_b1 = next(
        p for p in old["plot_summaries"]
        if p["block_id"] == 1 and p["treatment"] == "B"
    )
    assert old_b1["dose_per_area"] == pytest.approx(1.0)
    assert old["provenance"]["data_version"] == approval["data_version"]

    # re-approval from the new snapshot succeeds with the updated dose
    new = client.post("/fertilizer/approvals").json()
    assert new["stale"] is False
    assert new["data_version"] > approval["data_version"]
    new_b1 = next(
        p for p in new["plot_summaries"]
        if p["block_id"] == 1 and p["treatment"] == "B"
    )
    assert new_b1["area"] == 40.0
    assert new_b1["dose_per_area"] == pytest.approx(0.5)  # 20 / 40

    summaries = client.get("/fertilizer/approvals").json()
    assert [s["stale"] for s in summaries] == [False, True]
    assert client.get("/fertilizer/approvals/latest").json()["id"] == new["id"]


def test_failed_reapproval_keeps_old_report_queryable(client):
    import_plots(client)
    client.post(
        "/fertilizer/operations",
        json={"start_weight": 100.0, "entries": [
            {"seq": 1, "block_id": 1, "treatment": "B", "remaining_weight": 80.0}
        ]},
    )
    approval = client.post("/fertilizer/approvals").json()

    # introduce an anomaly after approval -> old report stale, re-approval fails
    client.post(
        "/fertilizer/corrections",
        json={"operation_no": 1, "entries": [
            {"seq": 1, "treatment": "A", "block_id": 1, "remaining_weight": 70.0}
        ]},
    )
    # that correction changed plot identity onto control A with consumption 30
    st = status(client)
    assert any(a["kind"] == "control_consumption" for a in st["anomalies"])
    resp = client.post("/fertilizer/approvals")
    assert resp.status_code == 422

    old = client.get(f"/fertilizer/approvals/{approval['id']}").json()
    assert old["stale"] is True
    assert len(old["provenance"]["operations"]) == 1
    assert client.get("/fertilizer/approvals/latest").json()["id"] == approval["id"]

    # fix it -> a new version can be approved again
    client.post(
        "/fertilizer/corrections",
        json={"operation_no": 1, "entries": [
            {"seq": 1, "treatment": "B", "block_id": 1, "remaining_weight": 80.0}
        ]},
    )
    assert status(client)["anomalies"] == []
    new = client.post("/fertilizer/approvals").json()
    assert new["id"] != approval["id"] and new["stale"] is False


def test_approval_provenance_is_same_snapshot(client):
    import_plots(client)
    client.post(
        "/fertilizer/operations",
        json={"start_weight": 100.0, "entries": [
            {"seq": 1, "block_id": 1, "treatment": "B",
             "remaining_weight": 80.0, "refill": 5.0},
            {"seq": 2, "block_id": 2, "treatment": "B", "remaining_weight": 70.0},
        ]},
    )
    approval = client.post("/fertilizer/approvals").json()
    current_version = client.get("/plots").json()["data_version"]
    assert approval["data_version"] == current_version
    assert approval["provenance"]["data_version"] == current_version

    # provenance mirrors exactly the raw rows present at approval time
    raw_ops = client.get("/fertilizer/operations").json()["operations"]
    assert approval["provenance"]["operations"] == raw_ops
    raw_plots = client.get("/plots").json()["plots"]
    assert approval["provenance"]["plots"] == raw_plots

    # any later interleaved mutation invalidates immediately
    client.post(
        "/fertilizer/corrections",
        json={"operation_no": 1, "entries": [{"seq": 1, "refill": 9.0}]},
    )
    assert client.get(
        f"/fertilizer/approvals/{approval['id']}"
    ).json()["stale"] is True
    # stored provenance stays frozen at the old snapshot
    frozen = client.get(f"/fertilizer/approvals/{approval['id']}").json()
    assert frozen["provenance"]["operations"][0]["entries"][0]["refill"] == 5.0


def test_operation_numbering_conflicts_and_404s(client):
    import_plots(client)
    payload = {"operation_no": 5, "start_weight": 10.0, "entries": [
        {"seq": 1, "block_id": 1, "treatment": "B", "remaining_weight": 9.0}
    ]}
    assert client.post("/fertilizer/operations", json=payload).status_code == 201
    assert client.post("/fertilizer/operations", json=payload).status_code == 409

    # auto numbering continues after an explicit number
    resp = client.post(
        "/fertilizer/operations",
        json={"start_weight": 10.0, "entries": [
            {"seq": 1, "block_id": 1, "treatment": "B", "remaining_weight": 8.0}
        ]},
    )
    assert resp.json()["operation_no"] == 6

    assert client.post(
        "/fertilizer/corrections",
        json={"operation_no": 99, "start_weight": 1.0},
    ).status_code == 404
    assert client.get("/fertilizer/approvals/999").status_code == 404
    assert client.get("/fertilizer/approvals/latest").status_code == 404


def test_approve_without_operations_fails(client):
    import_plots(client)
    resp = client.post("/fertilizer/approvals")
    assert resp.status_code == 422
    assert "nothing to approve" in resp.json()["detail"]["error"]
    assert client.get("/fertilizer/approvals").json() == []


def test_request_validation(client):
    import_plots(client)
    # duplicate seq
    bad = {"start_weight": 10.0, "entries": [
        {"seq": 1, "block_id": 1, "treatment": "B", "remaining_weight": 9.0},
        {"seq": 1, "block_id": 2, "treatment": "B", "remaining_weight": 8.0},
    ]}
    assert client.post("/fertilizer/operations", json=bad).status_code == 422

    # duplicate plot in one operation
    bad = {"start_weight": 10.0, "entries": [
        {"seq": 1, "block_id": 1, "treatment": "B", "remaining_weight": 9.0},
        {"seq": 2, "block_id": 1, "treatment": "B", "remaining_weight": 8.0},
    ]}
    assert client.post("/fertilizer/operations", json=bad).status_code == 422

    # negative weights
    bad = {"start_weight": -1.0, "entries": [
        {"seq": 1, "block_id": 1, "treatment": "B", "remaining_weight": 9.0}
    ]}
    assert client.post("/fertilizer/operations", json=bad).status_code == 422

    # empty correction
    assert client.post(
        "/fertilizer/corrections", json={"operation_no": 1}
    ).status_code == 422


def test_noop_correction_keeps_approval_fresh(client):
    import_plots(client)
    client.post(
        "/fertilizer/operations",
        json={"start_weight": 100.0, "entries": [
            {"seq": 1, "block_id": 1, "treatment": "B",
             "remaining_weight": 80.0, "refill": 5.0}
        ]},
    )
    approval = client.post("/fertilizer/approvals").json()
    resp = client.post(
        "/fertilizer/corrections",
        json={"operation_no": 1, "entries": [
            {"seq": 1, "remaining_weight": 80.0, "refill": 5.0,
             "block_id": 1, "treatment": "B"}
        ]},
    )
    assert resp.json()["changed"] == 0
    assert client.get(
        f"/fertilizer/approvals/{approval['id']}"
    ).json()["stale"] is False

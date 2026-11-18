"""Routes for fertilizer flow entry, correction, reconciliation, approval.

Every read/write runs inside the same session machinery as the trial data
(``BEGIN IMMEDIATE`` + process-wide write lock) and shares the single
``data_version`` counter, so an approval's flow rows, plot areas and
provenance always come from one committed snapshot.  Any fertilizer or
plot-area change bumps the version and immediately marks earlier approvals
stale; a failed approval inserts nothing and old approvals stay queryable.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException

from . import db
from .fertilizer import DEFAULT_CONTROL_TREATMENT, reconcile_operations
from .fertilizer_schemas import (
    FertilizerApproval,
    FertilizerApprovalSummary,
    FertilizerCorrectionRequest,
    FertilizerMutationResponse,
    FertilizerOperationRequest,
    FertilizerProvenance,
    FertilizerStatus,
)

router = APIRouter(prefix="/fertilizer", tags=["fertilizer"])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def control_treatment() -> str:
    """Control treatment; override with FERT_CONTROL_TREATMENT (default A)."""
    value = os.environ.get("FERT_CONTROL_TREATMENT", DEFAULT_CONTROL_TREATMENT)
    return value if value in ("A", "B") else DEFAULT_CONTROL_TREATMENT


# ---------------------------------------------------------------- helpers

def _get_operation_row(conn, operation_no: int):
    row = conn.execute(
        "SELECT operation_no, start_weight, created_at"
        " FROM fertilizer_operations WHERE operation_no = ?",
        (operation_no,),
    ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"fertilizer operation {operation_no} not found",
        )
    return row


def _entry_row(conn, operation_no: int, seq: int):
    return conn.execute(
        "SELECT seq, block_id, treatment, remaining_weight, refill, updated_at"
        " FROM fertilizer_entries WHERE operation_no = ? AND seq = ?",
        (operation_no, seq),
    ).fetchone()


def _build_status(conn, version: int) -> dict[str, Any]:
    operations = db.fetch_fertilizer_operations(conn)
    plots = [dict(row) for row in db.fetch_plots(conn)]
    result = reconcile_operations(operations, plots, control_treatment())
    result["data_version"] = version
    result["approvable"] = not result["anomalies"]
    return result


def _canonical_source(operations: list[dict[str, Any]], plots: list[dict[str, Any]]) -> str:
    return json.dumps(
        {"operations": operations, "plots": plots}, sort_keys=True, default=str
    )


def _approval_from_row(row, current_version: int) -> FertilizerApproval:
    payload = json.loads(row["payload"])
    return FertilizerApproval(
        id=row["id"],
        created_at=row["created_at"],
        data_version=row["data_version"],
        stale=row["data_version"] != current_version,
        n_operations=row["n_operations"],
        n_plots=row["n_plots"],
        n_anomalies=row["n_anomalies"],
        n_pending=row["n_pending"],
        control_treatment=payload["control_treatment"],
        operations=payload["operations"],
        plot_summaries=payload["plot_summaries"],
        pending=payload["pending"],
        anomalies=payload["anomalies"],
        provenance=FertilizerProvenance(**payload["provenance"]),
    )


# ---------------------------------------------------------------- mutations

@router.post("/operations", response_model=FertilizerMutationResponse, status_code=201)
def record_operation(req: FertilizerOperationRequest) -> FertilizerMutationResponse:
    """Record one fertilizer operation: hopper start weight + plot weighings."""
    with db.session(write=True) as conn:
        if req.operation_no is None:
            operation_no = db.next_fertilizer_operation_no(conn)
        else:
            operation_no = req.operation_no
            existing = conn.execute(
                "SELECT 1 FROM fertilizer_operations WHERE operation_no = ?",
                (operation_no,),
            ).fetchone()
            if existing:
                raise HTTPException(
                    status_code=409,
                    detail=f"fertilizer operation {operation_no} already exists;"
                    " correct it via /fertilizer/corrections",
                )
        now = _now()
        conn.execute(
            "INSERT INTO fertilizer_operations (operation_no, start_weight, created_at)"
            " VALUES (?, ?, ?)",
            (operation_no, req.start_weight, now),
        )
        for entry in req.entries:
            conn.execute(
                "INSERT INTO fertilizer_entries"
                " (operation_no, seq, block_id, treatment, remaining_weight,"
                "  refill, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (operation_no, entry.seq, entry.block_id, entry.treatment,
                 entry.remaining_weight, entry.refill, now),
            )
        version = db.bump_data_version(conn)
    return FertilizerMutationResponse(
        data_version=version, operation_no=operation_no, changed=len(req.entries) + 1
    )


@router.post("/corrections", response_model=FertilizerMutationResponse)
def correct_operation(req: FertilizerCorrectionRequest) -> FertilizerMutationResponse:
    """Correct a start weight or an entry; a missing seq is inserted late.

    Inserting/correcting an entry re-checks its neighbouring weighings on
    the next reconciliation (the whole operation chain is recomputed).
    """
    with db.session(write=True) as conn:
        _get_operation_row(conn, req.operation_no)
        now = _now()
        changed = 0

        if req.start_weight is not None:
            row = conn.execute(
                "SELECT start_weight FROM fertilizer_operations"
                " WHERE operation_no = ?",
                (req.operation_no,),
            ).fetchone()
            if row["start_weight"] != req.start_weight:
                conn.execute(
                    "UPDATE fertilizer_operations SET start_weight = ?"
                    " WHERE operation_no = ?",
                    (req.start_weight, req.operation_no),
                )
                changed += 1

        for correction in req.entries:
            existing = _entry_row(conn, req.operation_no, correction.seq)
            provided = correction.model_fields_set - {"seq"}
            if existing is None:
                # late/supplementary insertion: full weighing required
                missing = [
                    name for name in ("block_id", "treatment", "remaining_weight")
                    if getattr(correction, name) is None
                ]
                if missing:
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"seq {correction.seq} does not exist in operation"
                            f" {req.operation_no}; late insertion requires"
                            f" {', '.join(missing)}"
                        ),
                    )
                try:
                    conn.execute(
                        "INSERT INTO fertilizer_entries"
                        " (operation_no, seq, block_id, treatment,"
                        "  remaining_weight, refill, updated_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (req.operation_no, correction.seq, correction.block_id,
                         correction.treatment, correction.remaining_weight,
                         correction.refill if correction.refill is not None else 0.0,
                         now),
                    )
                except sqlite3.IntegrityError as exc:  # unique (block, treatment)
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"plot {correction.block_id}{correction.treatment} is"
                            " already weighed in this operation"
                        ),
                    ) from exc
                changed += 1
                continue

            updates: dict[str, Any] = {}
            for field in ("block_id", "treatment", "remaining_weight", "refill"):
                if field in provided and getattr(correction, field) is not None:
                    if existing[field] != getattr(correction, field):
                        updates[field] = getattr(correction, field)
            if not updates:
                continue
            # a corrected plot identity must not collide with another entry
            if "block_id" in updates or "treatment" in updates:
                block_id = updates.get("block_id", existing["block_id"])
                treatment = updates.get("treatment", existing["treatment"])
                clash = conn.execute(
                    "SELECT 1 FROM fertilizer_entries"
                    " WHERE operation_no = ? AND block_id = ? AND treatment = ?"
                    " AND seq <> ?",
                    (req.operation_no, block_id, treatment, correction.seq),
                ).fetchone()
                if clash:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"plot {block_id}{treatment} is already weighed in"
                            f" operation {req.operation_no}"
                        ),
                    )
            assignments = ", ".join(f"{field} = ?" for field in updates)
            conn.execute(
                f"UPDATE fertilizer_entries SET {assignments}, updated_at = ?"
                " WHERE operation_no = ? AND seq = ?",
                (*updates.values(), now, req.operation_no, correction.seq),
            )
            changed += 1

        if changed == 0:
            version = db.get_data_version(conn)
        else:
            version = db.bump_data_version(conn)
    return FertilizerMutationResponse(
        data_version=version, operation_no=req.operation_no, changed=changed
    )


# ---------------------------------------------------------------- status

@router.get("/status", response_model=FertilizerStatus)
def fertilizer_status() -> FertilizerStatus:
    """Current reconciled flow: consumptions, merged doses, pending, anomalies."""
    with db.session() as conn:
        return FertilizerStatus(**_build_status(conn, db.get_data_version(conn)))


@router.get("/operations")
def list_operations() -> dict[str, Any]:
    """Raw fertilizer operations and entries with the current data version."""
    with db.session() as conn:
        return {
            "data_version": db.get_data_version(conn),
            "operations": db.fetch_fertilizer_operations(conn),
        }


# ---------------------------------------------------------------- approvals

@router.post("/approvals", response_model=FertilizerApproval, status_code=201)
def approve() -> FertilizerApproval:
    """Approve the current flow and freeze the reconciliation report.

    Fails with 422 when no operation exists or when an anomaly blocks
    approval (control consumption or impossible remaining weight).  Pending
    items (gaps, skipped plots, unknown areas) do not block.  A failed
    attempt inserts nothing; earlier approvals remain untouched.
    """
    with db.session(write=True) as conn:
        version = db.get_data_version(conn)
        status = _build_status(conn, version)
        if status["n_operations"] == 0:
            raise HTTPException(
                status_code=422,
                detail={"error": "no fertilizer operation recorded: nothing to approve"},
            )
        if status["anomalies"]:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "anomalies block approval",
                    "anomalies": status["anomalies"],
                },
            )

        raw_operations = db.fetch_fertilizer_operations(conn)
        raw_plots = [dict(row) for row in db.fetch_plots(conn)]
        source_hash = hashlib.sha256(
            _canonical_source(raw_operations, raw_plots).encode()
        ).hexdigest()
        payload = {
            "control_treatment": status["control_treatment"],
            "operations": status["operations"],
            "plot_summaries": status["plot_summaries"],
            "pending": status["pending"],
            "anomalies": status["anomalies"],
            "provenance": {
                "data_version": version,
                "source_hash": source_hash,
                "operations": raw_operations,
                "plots": raw_plots,
            },
        }
        cursor = conn.execute(
            "INSERT INTO fertilizer_approvals"
            " (created_at, data_version, n_operations, n_plots, n_anomalies,"
            "  n_pending, payload)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_now(), version, status["n_operations"],
             len(status["plot_summaries"]), len(status["anomalies"]),
             len(status["pending"]), json.dumps(payload)),
        )
        row = conn.execute(
            "SELECT * FROM fertilizer_approvals WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return _approval_from_row(row, version)


@router.get("/approvals", response_model=list[FertilizerApprovalSummary])
def list_approvals() -> list[FertilizerApprovalSummary]:
    """All approval reports, newest first, with staleness flags."""
    with db.session() as conn:
        version = db.get_data_version(conn)
        rows = conn.execute(
            "SELECT * FROM fertilizer_approvals ORDER BY id DESC"
        ).fetchall()
        return [
            FertilizerApprovalSummary(
                id=row["id"],
                created_at=row["created_at"],
                data_version=row["data_version"],
                stale=row["data_version"] != version,
                n_operations=row["n_operations"],
                n_plots=row["n_plots"],
                n_anomalies=row["n_anomalies"],
                n_pending=row["n_pending"],
            )
            for row in rows
        ]


@router.get("/approvals/latest", response_model=FertilizerApproval)
def latest_approval() -> FertilizerApproval:
    """The most recent approval report (may already be stale)."""
    with db.session() as conn:
        row = conn.execute(
            "SELECT * FROM fertilizer_approvals ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="no fertilizer approvals yet")
        return _approval_from_row(row, db.get_data_version(conn))


@router.get("/approvals/{approval_id}", response_model=FertilizerApproval)
def get_approval(approval_id: int) -> FertilizerApproval:
    """A stored approval report with its frozen flow, areas and provenance."""
    with db.session() as conn:
        row = conn.execute(
            "SELECT * FROM fertilizer_approvals WHERE id = ?", (approval_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=404, detail=f"fertilizer approval {approval_id} not found"
            )
        return _approval_from_row(row, db.get_data_version(conn))

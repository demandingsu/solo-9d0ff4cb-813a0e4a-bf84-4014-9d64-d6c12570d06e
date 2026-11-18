"""Fertilizer application flow reconciliation (pure functions, no I/O).

Flow model for one hopper operation:

* the reference weight before the first weighing is the operation's
  ``start_weight`` (hopper weight at the beginning of the operation);
* before every later weighing it is the previous entry's
  ``remaining_weight + refill``;
* consumption of one entry is ``reference_weight - remaining_weight``.

A missing sequence number breaks the weighing chain: the entry right after
the gap has no trustworthy reference weight, so its consumption stays
*unknown* (pending) and is silently never treated as zero fertilization.

Two anomaly classes block approval:

* ``weight_increase``      - remaining weight exceeds the reference weight
                             (more material in the hopper than the previous
                             remaining plus the recorded refill allows);
* ``control_consumption``  - a control plot (configurable, default treatment
                             A) shows positive consumption.

Missing sequence numbers, plots skipped in some operation and plots whose
area was never imported are *pending* items, not anomalies: they do not
block approval but they keep the affected per-area dose out of the report.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional

WEIGHT_EPS = 1e-9
TREATMENTS = ("A", "B")
DEFAULT_CONTROL_TREATMENT = "A"


def _plot_key(block_id: int, treatment: str) -> tuple[int, str]:
    return int(block_id), treatment


def reconcile_operations(
    operations: Iterable[Mapping[str, Any]],
    plots: Iterable[Mapping[str, Any]],
    control_treatment: str = DEFAULT_CONTROL_TREATMENT,
) -> dict[str, Any]:
    """Recompute every consumption and the merged per-area dose.

    ``operations`` is an iterable of ``{"operation_no", "start_weight",
    "entries": [{"seq", "block_id", "treatment", "remaining_weight",
    "refill"}, ...]}`` mappings; ``plots`` carries the current plot rows
    (only ``block_id``, ``treatment`` and ``area`` are read).

    Returns a dict with ``operations`` (raw rows plus computed
    ``previous_weight``/``consumption`` per entry), ``plot_summaries``
    (merged totals and per-area dose), ``pending`` and ``anomalies``.
    """
    area_by_plot: dict[tuple[int, str], float] = {
        _plot_key(p["block_id"], p["treatment"]): float(p["area"]) for p in plots
    }

    computed_ops: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    anomalies: list[dict[str, Any]] = []
    # plot -> {"operations": set[op_no], "consumptions": list[float|None]}
    per_plot: dict[tuple[int, str], dict[str, Any]] = {}
    all_operation_nos: list[int] = []

    for op in sorted(operations, key=lambda o: int(o["operation_no"])):
        operation_no = int(op["operation_no"])
        all_operation_nos.append(operation_no)
        entries = sorted(op["entries"], key=lambda e: int(e["seq"]))
        computed_entries: list[dict[str, Any]] = []

        expected_seq = 1
        reference_weight: Optional[float] = float(op["start_weight"])
        chain_broken = False

        for entry in entries:
            seq = int(entry["seq"])
            block_id = int(entry["block_id"])
            treatment = entry["treatment"]
            remaining = float(entry["remaining_weight"])
            refill = float(entry["refill"])

            # missing sequence numbers: report each gap, never assume zero
            while expected_seq < seq:
                pending.append(
                    {
                        "kind": "missing_sequence",
                        "operation_no": operation_no,
                        "seq": expected_seq,
                        "message": (
                            f"operation {operation_no}: sequence {expected_seq}"
                            " has no weighing record"
                        ),
                    }
                )
                expected_seq += 1
                chain_broken = True

            if chain_broken:
                # the predecessor weighing is unknown -> consumption unknown
                previous_weight: Optional[float] = None
                consumption: Optional[float] = None
                pending.append(
                    {
                        "kind": "uncomputable_consumption",
                        "operation_no": operation_no,
                        "seq": seq,
                        "block_id": block_id,
                        "treatment": treatment,
                        "message": (
                            f"operation {operation_no} seq {seq}: consumption"
                            " cannot be computed because an earlier weighing"
                            " is missing"
                        ),
                    }
                )
            else:
                previous_weight = reference_weight
                consumption = reference_weight - remaining
                if consumption < -WEIGHT_EPS:
                    anomalies.append(
                        {
                            "kind": "weight_increase",
                            "operation_no": operation_no,
                            "seq": seq,
                            "block_id": block_id,
                            "treatment": treatment,
                            "message": (
                                f"operation {operation_no} seq {seq}: remaining"
                                f" {remaining} exceeds reference weight"
                                f" {reference_weight} (previous remaining plus"
                                " refill)"
                            ),
                            "details": {
                                "reference_weight": reference_weight,
                                "remaining_weight": remaining,
                                "refill": refill,
                            },
                        }
                    )
                if treatment == control_treatment and consumption > WEIGHT_EPS:
                    anomalies.append(
                        {
                            "kind": "control_consumption",
                            "operation_no": operation_no,
                            "seq": seq,
                            "block_id": block_id,
                            "treatment": treatment,
                            "message": (
                                f"operation {operation_no} seq {seq}: control"
                                f" plot {block_id}{treatment} consumed"
                                f" {consumption} fertilizer"
                            ),
                            "details": {"consumption": consumption},
                        }
                    )

            computed_entries.append(
                {
                    "seq": seq,
                    "block_id": block_id,
                    "treatment": treatment,
                    "remaining_weight": remaining,
                    "refill": refill,
                    "previous_weight": previous_weight,
                    "consumption": consumption,
                }
            )
            per_plot.setdefault(
                _plot_key(block_id, treatment),
                {"operations": set(), "consumptions": []},
            )
            per_plot[_plot_key(block_id, treatment)]["operations"].add(operation_no)
            per_plot[_plot_key(block_id, treatment)]["consumptions"].append(
                consumption
            )

            # next entry's reference: this remaining plus its refill
            reference_weight = remaining + refill
            expected_seq = seq + 1

        computed_ops.append(
            {
                "operation_no": operation_no,
                "start_weight": float(op["start_weight"]),
                "created_at": op.get("created_at"),
                "entries": computed_entries,
            }
        )

    # ------------------------------------------------------------- plot merge
    plot_summaries: list[dict[str, Any]] = []
    every_plot = set(area_by_plot) | set(per_plot)
    n_operations = len(all_operation_nos)
    for block_id, treatment in sorted(every_plot):
        key = _plot_key(block_id, treatment)
        seen = per_plot.get(key)
        area = area_by_plot.get(key)
        total_consumption: Optional[float] = None
        dose: Optional[float] = None
        plot_pending: list[str] = []

        if seen is None:
            plot_pending.append("no_application_record")
            pending.append(
                {
                    "kind": "no_application_record",
                    "block_id": block_id,
                    "treatment": treatment,
                    "message": (
                        f"plot {block_id}{treatment} never appears in any"
                        " fertilizer operation"
                    ),
                }
            )
        else:
            missing_ops = sorted(set(all_operation_nos) - seen["operations"])
            if missing_ops:
                plot_pending.append("missing_plot_entry")
                pending.append(
                    {
                        "kind": "missing_plot_entry",
                        "block_id": block_id,
                        "treatment": treatment,
                        "operation_nos": missing_ops,
                        "message": (
                            f"plot {block_id}{treatment} has no weighing in"
                            f" operation(s) {missing_ops}"
                        ),
                    }
                )
            if any(c is None for c in seen["consumptions"]):
                plot_pending.append("uncomputable_consumption")
            else:
                total_consumption = sum(seen["consumptions"])  # type: ignore[arg-type]
            if area is None:
                plot_pending.append("plot_not_imported")
                pending.append(
                    {
                        "kind": "plot_not_imported",
                        "block_id": block_id,
                        "treatment": treatment,
                        "message": (
                            f"plot {block_id}{treatment} is referenced by the"
                            " flow but has no imported area; dose unknown"
                        ),
                    }
                )
            elif total_consumption is not None:
                dose = total_consumption / area

        plot_summaries.append(
            {
                "block_id": block_id,
                "treatment": treatment,
                "area": area,
                "n_operations": 0 if seen is None else len(seen["operations"]),
                "total_operations": n_operations,
                "total_consumption": total_consumption,
                "dose_per_area": dose,
                "status": "pending" if plot_pending else "ok",
                "pending_reasons": plot_pending,
            }
        )

    return {
        "n_operations": n_operations,
        "control_treatment": control_treatment,
        "operations": computed_ops,
        "plot_summaries": plot_summaries,
        "pending": pending,
        "anomalies": anomalies,
    }

"""Pydantic schemas for fertilizer flow entry, correction and approval."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

from .schemas import Treatment


# ---------- entry: record one operation ----------

class FertilizerEntryIn(BaseModel):
    """One plot weighing inside an operation, in sequence order.

    ``refill`` is the material added to the hopper *after* this weighing
    (before the next plot); it raises the reference weight of the next entry.
    """

    seq: int = Field(ge=1, description="weighing sequence number within the operation")
    block_id: int = Field(ge=1, le=12)
    treatment: Treatment
    remaining_weight: float = Field(ge=0, description="hopper weight after this plot, kg")
    refill: float = Field(
        ge=0, default=0.0,
        description="fertilizer added after this weighing, kg (default 0)",
    )


class FertilizerOperationRequest(BaseModel):
    """Record one fertilizer application operation.

    Omit ``operation_no`` to take the next free number (1, 2, 3, ...).
    """

    operation_no: Optional[int] = Field(default=None, ge=1)
    start_weight: float = Field(ge=0, description="hopper weight at the start, kg")
    entries: list[FertilizerEntryIn] = Field(min_length=1)

    @model_validator(mode="after")
    def _no_duplicates(self) -> "FertilizerOperationRequest":
        seqs = [e.seq for e in self.entries]
        if len(seqs) != len(set(seqs)):
            raise ValueError("duplicate seq in entries")
        plot_keys = [(e.block_id, e.treatment) for e in self.entries]
        if len(plot_keys) != len(set(plot_keys)):
            raise ValueError("duplicate (block_id, treatment) in entries")
        return self


# ---------- correction: late insert or fix a weighing ----------

class FertilizerEntryCorrection(BaseModel):
    """Correct one weighing inside an existing operation.

    The entry is matched by ``seq``. When it already exists only the
    supplied fields change; when it is missing it is inserted as a
    late/supplementary record and the full weighing must be given.
    """

    seq: int = Field(ge=1)
    block_id: Optional[int] = Field(default=None, ge=1, le=12)
    treatment: Optional[Treatment] = None
    remaining_weight: Optional[float] = Field(default=None, ge=0)
    refill: Optional[float] = Field(default=None, ge=0)


class FertilizerStartWeightCorrection(BaseModel):
    """Correct the starting hopper weight of an existing operation."""

    start_weight: float = Field(ge=0)


class FertilizerCorrectionRequest(BaseModel):
    operation_no: int = Field(ge=1)
    start_weight: Optional[float] = Field(default=None, ge=0)
    entries: list[FertilizerEntryCorrection] = Field(default_factory=list)

    @model_validator(mode="after")
    def _at_least_one_change(self) -> "FertilizerCorrectionRequest":
        if self.start_weight is None and not self.entries:
            raise ValueError("provide start_weight and/or at least one entry correction")
        seqs = [e.seq for e in self.entries]
        if len(seqs) != len(set(seqs)):
            raise ValueError("duplicate seq in entry corrections")
        return self


# ---------- responses ----------

class FertilizerMutationResponse(BaseModel):
    data_version: int
    operation_no: int
    changed: int


class ComputedEntry(BaseModel):
    seq: int
    block_id: int
    treatment: str
    remaining_weight: float
    refill: float
    previous_weight: Optional[float] = Field(
        description="reference weight before this weighing; null after a gap"
    )
    consumption: Optional[float] = Field(
        description="previous_weight - remaining_weight; null while pending"
    )


class FertilizerOperationOut(BaseModel):
    operation_no: int
    start_weight: float
    created_at: str
    entries: list[ComputedEntry]


class PlotDose(BaseModel):
    block_id: int
    treatment: str
    area: Optional[float]
    n_operations: int
    total_operations: int
    total_consumption: Optional[float]
    dose_per_area: Optional[float] = Field(
        description="merged consumption / area; null while pending"
    )
    status: Literal["ok", "pending"]
    pending_reasons: list[str]


class PendingItem(BaseModel):
    kind: str
    message: str


class AnomalyItem(BaseModel):
    kind: Literal["weight_increase", "control_consumption"]
    operation_no: int
    seq: int
    block_id: int
    treatment: str
    message: str
    details: dict


class FertilizerStatus(BaseModel):
    data_version: int
    control_treatment: str
    n_operations: int
    operations: list[FertilizerOperationOut]
    plot_summaries: list[PlotDose]
    pending: list[PendingItem]
    anomalies: list[AnomalyItem]
    approvable: bool = Field(description="true iff no anomaly blocks approval")


class FertilizerApprovalSummary(BaseModel):
    id: int
    created_at: str
    data_version: int
    stale: bool
    n_operations: int
    n_plots: int
    n_anomalies: int
    n_pending: int


class FertilizerProvenance(BaseModel):
    data_version: int
    source_hash: str = Field(description="sha256 over the canonical raw snapshot")
    operations: list[dict]
    plots: list[dict]


class FertilizerApproval(FertilizerApprovalSummary):
    control_treatment: str
    operations: list[FertilizerOperationOut]
    plot_summaries: list[PlotDose]
    pending: list[PendingItem]
    anomalies: list[AnomalyItem]
    provenance: FertilizerProvenance

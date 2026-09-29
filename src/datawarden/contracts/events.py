"""Machine-readable incident event contract shared by the pipeline, Airflow, and the API."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RunSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")
    run_id: str = Field(max_length=64)
    status: str = Field(max_length=32)
    trigger: str = Field(max_length=32)
    code_commit: str | None = Field(default=None, max_length=64)
    source_watermark: int | None = None
    tasks: dict[str, str] = Field(default_factory=dict)
    error: str | None = Field(default=None, max_length=2000)
    checks: dict[str, int] = Field(default_factory=dict)


class IncidentEventIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Literal["pipeline", "airflow", "check_runner", "api"]
    event_type: Literal["check_result", "pipeline_run", "pipeline_task_failed"]
    occurred_at: datetime
    run: RunSummary | None = None
    check_id: str | None = Field(default=None, max_length=128, pattern=r"^[a-z0-9_.\-]+$")
    check_type: str | None = Field(default=None, max_length=32)
    asset: str | None = Field(default=None, max_length=64)
    status: Literal["pass", "fail", "warn", "error"] | None = None
    severity: Literal["critical", "high", "warning"] = "high"
    message: str = Field(default="", max_length=2000)
    observed: dict = Field(default_factory=dict)
    partition_date: date | None = None

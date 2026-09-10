from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "gateway" / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "gateway" / "src"))

from companion_gateway.project.models import (
    DecisionCard,
    EvidenceRef,
    ProjectContextPackage,
    SourcedFact,
)


ReviewStatus = Literal["accepted", "rejected", "proposed"]


def _aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field}_timezone_required")
    return value


def _non_blank(value: str, field: str) -> str:
    if not value.strip():
        raise ValueError(f"{field}_blank")
    return value


def _canonical(value: BaseModel) -> str:
    return json.dumps(
        value.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class ReviewEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_type: Literal["document", "meeting_note", "message", "task", "calendar"]
    source_time: datetime
    excerpt: str = Field(min_length=1, max_length=150)

    _source_time = field_validator("source_time")(
        lambda value: _aware(value, "source_time")
    )
    _excerpt = field_validator("excerpt")(
        lambda value: _non_blank(value, "excerpt")
    )


class ReviewOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str = Field(min_length=1, max_length=128)
    decision_id: str = Field(min_length=1, max_length=128)
    active_text: str = Field(min_length=1, max_length=2000)
    proposed_text: str = Field(min_length=1, max_length=2000)
    reason: str = Field(min_length=1, max_length=2000)
    status: ReviewStatus
    created_at: datetime
    reviewed_by: str | None = Field(default=None, max_length=256)
    reviewed_at: datetime | None = None
    review_reason: str | None = Field(default=None, max_length=2000)
    evidence: ReviewEvidence | None = None

    @field_validator(
        "candidate_id",
        "decision_id",
        "active_text",
        "proposed_text",
        "reason",
    )
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        return _non_blank(value, "review_field")

    @field_validator("reviewed_by", "review_reason")
    @classmethod
    def validate_optional_text(cls, value: str | None) -> str | None:
        return None if value is None else _non_blank(value, "review_field")

    _created_at = field_validator("created_at")(
        lambda value: _aware(value, "created_at")
    )
    _reviewed_at = field_validator("reviewed_at")(
        lambda value: None if value is None else _aware(value, "reviewed_at")
    )

    @model_validator(mode="after")
    def validate_review_state(self) -> "ReviewOutcome":
        reviewed = (
            self.reviewed_by is not None,
            self.reviewed_at is not None,
            self.review_reason is not None,
        )
        if self.status == "proposed" and any(reviewed):
            raise ValueError("proposed_review_fields_forbidden")
        if self.status in {"accepted", "rejected"} and not all(reviewed):
            raise ValueError("terminal_review_fields_required")
        return self


class PreMeetingArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    artifact_type: Literal["pre_meeting"] = "pre_meeting"
    project_id: str = Field(min_length=1, max_length=128)
    project_name: str = Field(min_length=1, max_length=512)
    generated_at: datetime
    permission_scope: str = Field(min_length=1, max_length=256)
    last_decisions: tuple[DecisionCard, ...] = ()
    unfinished_actions: tuple[SourcedFact, ...] = ()
    current_risks: tuple[SourcedFact, ...] = ()
    verification_items: tuple[SourcedFact, ...] = ()

    _generated_at = field_validator("generated_at")(
        lambda value: _aware(value, "generated_at")
    )


class PostMeetingArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    artifact_type: Literal["post_meeting"] = "post_meeting"
    project_id: str = Field(min_length=1, max_length=128)
    project_name: str = Field(min_length=1, max_length=512)
    generated_at: datetime
    permission_scope: str = Field(min_length=1, max_length=256)
    accepted: tuple[ReviewOutcome, ...] = ()
    rejected: tuple[ReviewOutcome, ...] = ()
    pending: tuple[ReviewOutcome, ...] = ()
    action_items: tuple[SourcedFact, ...] = ()

    _generated_at = field_validator("generated_at")(
        lambda value: _aware(value, "generated_at")
    )

    @model_validator(mode="after")
    def validate_status_buckets(self) -> "PostMeetingArtifact":
        buckets = (
            ("accepted", self.accepted),
            ("rejected", self.rejected),
            ("proposed", self.pending),
        )
        if any(item.status != status for status, items in buckets for item in items):
            raise ValueError("post_meeting_status_mismatch")
        return self


def _validate_identity(
    project_id: str,
    project_name: str,
    permission_scope: str,
    context: ProjectContextPackage,
    error: str,
) -> None:
    if (
        project_id != context.project_id
        or project_name != context.project_name
        or permission_scope != context.permission_scope
    ):
        raise ValueError(error)


def validate_pre_meeting_artifact(
    artifact: PreMeetingArtifact,
    context: ProjectContextPackage,
) -> PreMeetingArtifact:
    _validate_identity(
        artifact.project_id,
        artifact.project_name,
        artifact.permission_scope,
        context,
        "pre_meeting_context_mismatch",
    )
    if (
        tuple(map(_canonical, artifact.last_decisions))
        != tuple(map(_canonical, context.active_decisions))
        or tuple(map(_canonical, artifact.unfinished_actions))
        != tuple(map(_canonical, context.sourced_actions))
        or tuple(map(_canonical, artifact.current_risks))
        != tuple(map(_canonical, context.sourced_risks))
    ):
        raise ValueError("pre_meeting_context_mismatch")
    supported = {
        *map(_canonical, context.sourced_actions),
        *map(_canonical, context.sourced_risks),
    }
    if any(_canonical(item) not in supported for item in artifact.verification_items):
        raise ValueError("pre_meeting_context_mismatch")
    return artifact


def validate_post_meeting_artifact(
    artifact: PostMeetingArtifact,
    context: ProjectContextPackage,
    review_snapshot: Sequence[Mapping[str, Any]],
) -> PostMeetingArtifact:
    _validate_identity(
        artifact.project_id,
        artifact.project_name,
        artifact.permission_scope,
        context,
        "post_meeting_context_mismatch",
    )
    if tuple(map(_canonical, artifact.action_items)) != tuple(
        map(_canonical, context.sourced_actions)
    ):
        raise ValueError("post_meeting_context_mismatch")
    expected_records = [
        ReviewOutcome.model_validate(item)
        for item in review_snapshot
        if item.get("status") in {"accepted", "rejected", "proposed"}
    ]
    actual_records = [*artifact.accepted, *artifact.rejected, *artifact.pending]
    expected = {item.candidate_id: _canonical(item) for item in expected_records}
    actual = {item.candidate_id: _canonical(item) for item in actual_records}
    if (
        len(expected) != len(expected_records)
        or len(actual) != len(actual_records)
        or actual != expected
    ):
        raise ValueError("post_meeting_review_mismatch")
    return artifact

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SourceType = Literal["document", "meeting_note", "message", "task", "calendar"]


class DecisionStatus(StrEnum):
    PROPOSED = "proposed"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"


class ConflictStatus(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class AnswerKind(StrEnum):
    FACT = "fact"
    CURRENT_STATE = "current_state"
    SUGGESTION = "suggestion"
    DECISION_CHECK = "decision_check"


def _require_non_blank(value: str, field_name: str) -> str:
    if not value.strip():
        raise ValueError(f"{field_name} must not be blank")
    return value


def _require_aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


class EvidenceRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_type: SourceType
    source_id: str = Field(min_length=1, max_length=256)
    source_title: str = Field(min_length=1, max_length=512)
    source_url: str = Field(min_length=1, max_length=2048)
    source_time: datetime
    excerpt: str = Field(min_length=1, max_length=2000)
    permission_scope: str = Field(min_length=1, max_length=256)

    _source_id = field_validator("source_id")(
        lambda value: _require_non_blank(value, "source_id")
    )
    _source_title = field_validator("source_title")(
        lambda value: _require_non_blank(value, "source_title")
    )
    _source_url = field_validator("source_url")(
        lambda value: _require_non_blank(value, "source_url")
    )
    _excerpt = field_validator("excerpt")(
        lambda value: _require_non_blank(value, "excerpt")
    )
    _permission_scope = field_validator("permission_scope")(
        lambda value: _require_non_blank(value, "permission_scope")
    )
    _source_time = field_validator("source_time")(
        lambda value: _require_aware(value, "source_time")
    )


class SourcedFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=1, max_length=2000)
    source_refs: tuple[EvidenceRef, ...] = Field(min_length=1)

    _text = field_validator("text")(
        lambda value: _require_non_blank(value, "text")
    )


class DecisionCard(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_id: str = Field(min_length=1, max_length=128)
    project_id: str = Field(min_length=1, max_length=128)
    topic: str = Field(min_length=1, max_length=512)
    decision_text: str = Field(min_length=1, max_length=2000)
    rationale: str = Field(min_length=1, max_length=2000)
    owner: str = Field(min_length=1, max_length=256)
    decided_at: datetime
    source_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    status: DecisionStatus = DecisionStatus.PROPOSED
    confidence: float = Field(ge=0.0, le=1.0)

    _decision_id = field_validator("decision_id")(
        lambda value: _require_non_blank(value, "decision_id")
    )
    _project_id = field_validator("project_id")(
        lambda value: _require_non_blank(value, "project_id")
    )
    _topic = field_validator("topic")(
        lambda value: _require_non_blank(value, "topic")
    )
    _decision_text = field_validator("decision_text")(
        lambda value: _require_non_blank(value, "decision_text")
    )
    _rationale = field_validator("rationale")(
        lambda value: _require_non_blank(value, "rationale")
    )
    _owner = field_validator("owner")(
        lambda value: _require_non_blank(value, "owner")
    )
    _decided_at = field_validator("decided_at")(
        lambda value: _require_aware(value, "decided_at")
    )


class DecisionVersion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_id: str = Field(min_length=1, max_length=128)
    version: int = Field(ge=1)
    replaces_version: int | None = Field(default=None, ge=1)
    change_reason: str = Field(min_length=1, max_length=2000)
    decision_text: str | None = Field(default=None, max_length=2000)
    proposed_by: str = Field(min_length=1, max_length=256)
    approved_by: str | None = Field(default=None, max_length=256)
    approved_at: datetime | None = None
    status: DecisionStatus = DecisionStatus.PROPOSED
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)

    _decision_id = field_validator("decision_id")(
        lambda value: _require_non_blank(value, "decision_id")
    )
    _change_reason = field_validator("change_reason")(
        lambda value: _require_non_blank(value, "change_reason")
    )
    _version_decision_text = field_validator("decision_text")(
        lambda value: _require_non_blank(value, "decision_text")
        if value is not None
        else value
    )
    _proposed_by = field_validator("proposed_by")(
        lambda value: _require_non_blank(value, "proposed_by")
    )
    _approved_by = field_validator("approved_by")(
        lambda value: _require_non_blank(value, "approved_by")
        if value is not None
        else value
    )
    _approved_at = field_validator("approved_at")(
        lambda value: _require_aware(value, "approved_at")
        if value is not None
        else value
    )

    @model_validator(mode="after")
    def validate_active_approval(self) -> "DecisionVersion":
        if self.status is DecisionStatus.ACTIVE:
            if self.approved_by is None or self.approved_at is None:
                raise ValueError("active decision version requires approved fields")
        if self.status is DecisionStatus.PROPOSED:
            if self.approved_by is not None or self.approved_at is not None:
                raise ValueError("proposed decision version cannot have approval")
        return self


class ProjectContextPackage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str = Field(min_length=1, max_length=128)
    project_name: str = Field(min_length=1, max_length=512)
    generated_at: datetime
    source_refs: tuple[EvidenceRef, ...] = ()
    active_decisions: tuple[DecisionCard, ...] = ()
    open_actions: tuple[str, ...] = ()
    current_risks: tuple[str, ...] = ()
    next_meeting: str | None = Field(default=None, max_length=512)
    sourced_actions: tuple[SourcedFact, ...] = ()
    sourced_risks: tuple[SourcedFact, ...] = ()
    sourced_next_meeting: SourcedFact | None = None
    permission_scope: str = Field(min_length=1, max_length=256)
    freshness_seconds: int = Field(default=300, gt=0, le=86400)

    _project_id = field_validator("project_id")(
        lambda value: _require_non_blank(value, "project_id")
    )
    _project_name = field_validator("project_name")(
        lambda value: _require_non_blank(value, "project_name")
    )
    _permission_scope = field_validator("permission_scope")(
        lambda value: _require_non_blank(value, "permission_scope")
    )
    _generated_at = field_validator("generated_at")(
        lambda value: _require_aware(value, "generated_at")
    )

    @field_validator("open_actions", "current_risks")
    @classmethod
    def validate_text_items(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            _require_non_blank(value, "context item")
        return values

    @model_validator(mode="after")
    def validate_project_scope(self) -> "ProjectContextPackage":
        for decision in self.active_decisions:
            if decision.project_id != self.project_id:
                raise ValueError("all decisions must belong to the same project")
            if any(
                source.permission_scope != self.permission_scope
                for source in decision.source_refs
            ):
                raise ValueError(
                    "all decision sources must use the context permission scope"
                )
        for source in self.source_refs:
            if source.permission_scope != self.permission_scope:
                raise ValueError("all sources must use the context permission scope")
        sourced_facts = (*self.sourced_actions, *self.sourced_risks)
        if self.sourced_next_meeting is not None:
            sourced_facts += (self.sourced_next_meeting,)
        if any(
            source.permission_scope != self.permission_scope
            for fact in sourced_facts
            for source in fact.source_refs
        ):
            raise ValueError(
                "all sourced facts must use the context permission scope"
            )
        return self


class ProjectAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: AnswerKind
    text: str = Field(min_length=1, max_length=4000)
    source_refs: tuple[EvidenceRef, ...] = ()
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    _text = field_validator("text")(
        lambda value: _require_non_blank(value, "text")
    )

    @model_validator(mode="after")
    def validate_fact_sources(self) -> "ProjectAnswer":
        requires_sources = {
            AnswerKind.FACT,
            AnswerKind.CURRENT_STATE,
            AnswerKind.DECISION_CHECK,
        }
        if self.kind in requires_sources and not self.source_refs:
            raise ValueError("factual answers require source_refs")
        return self


class ConflictCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str = Field(min_length=1, max_length=128)
    project_id: str = Field(min_length=1, max_length=128)
    decision_id: str = Field(min_length=1, max_length=128)
    base_version: int = Field(default=1, ge=1)
    observed_text: str = Field(min_length=1, max_length=2000)
    active_decision_text: str = Field(min_length=1, max_length=2000)
    reason: str = Field(min_length=1, max_length=2000)
    source_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    status: ConflictStatus = ConflictStatus.PROPOSED
    created_at: datetime
    reviewed_by: str | None = Field(default=None, max_length=256)
    reviewed_at: datetime | None = None
    review_reason: str | None = Field(default=None, max_length=2000)

    _candidate_id = field_validator("candidate_id")(
        lambda value: _require_non_blank(value, "candidate_id")
    )
    _project_id = field_validator("project_id")(
        lambda value: _require_non_blank(value, "project_id")
    )
    _decision_id = field_validator("decision_id")(
        lambda value: _require_non_blank(value, "decision_id")
    )
    _observed_text = field_validator("observed_text")(
        lambda value: _require_non_blank(value, "observed_text")
    )
    _active_decision_text = field_validator("active_decision_text")(
        lambda value: _require_non_blank(value, "active_decision_text")
    )
    _reason = field_validator("reason")(
        lambda value: _require_non_blank(value, "reason")
    )
    _created_at = field_validator("created_at")(
        lambda value: _require_aware(value, "created_at")
    )
    _reviewed_by = field_validator("reviewed_by")(
        lambda value: _require_non_blank(value, "reviewed_by")
        if value is not None
        else value
    )
    _reviewed_at = field_validator("reviewed_at")(
        lambda value: _require_aware(value, "reviewed_at")
        if value is not None
        else value
    )
    _review_reason = field_validator("review_reason")(
        lambda value: _require_non_blank(value, "review_reason")
        if value is not None
        else value
    )

    @model_validator(mode="after")
    def validate_review_fields(self) -> "ConflictCandidate":
        terminal = {ConflictStatus.ACCEPTED, ConflictStatus.REJECTED}
        if self.status in terminal and (
            self.reviewed_by is None or self.reviewed_at is None
        ):
            raise ValueError("reviewed conflict requires reviewed fields")
        if self.status is ConflictStatus.PROPOSED and (
            self.reviewed_by is not None or self.reviewed_at is not None
        ):
            raise ValueError("proposed conflict cannot have review fields")
        return self

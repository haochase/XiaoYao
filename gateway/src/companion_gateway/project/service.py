from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from threading import RLock
from typing import Callable, Literal

from companion_gateway.project.models import (
    AnswerKind,
    ConflictCandidate,
    ConflictStatus,
    DecisionCard,
    DecisionStatus,
    DecisionVersion,
    EvidenceRef,
    ProjectAnswer,
    ProjectContextPackage,
)
from companion_gateway.project.repository import ProjectMemoryRepository


class ProjectMemoryError(RuntimeError):
    """Base error for deterministic project-memory operations."""


class ProjectContextUnavailable(ProjectMemoryError):
    """Raised when a project context cannot safely answer a request."""


class ProjectMemoryService:
    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        repository: ProjectMemoryRepository | None = None,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._repository = repository
        self._contexts: dict[str, ProjectContextPackage] = {}
        self._versions: dict[tuple[str, str], list[DecisionVersion]] = {}
        self._conflicts: dict[str, ConflictCandidate] = {}
        self._lock = RLock()

    def replace_context(self, package: ProjectContextPackage) -> None:
        with self._lock:
            timestamp = self._clock()
            if package.generated_at > timestamp + timedelta(seconds=30):
                raise ProjectMemoryError("context_from_future")
            existing = self._contexts.get(package.project_id)
            if existing is None and self._repository is not None:
                existing = self._repository.get_context(package.project_id)
            if existing is not None and (
                existing.active_decisions != package.active_decisions
            ):
                raise ProjectMemoryError("decision_change_requires_review")
            self._contexts[package.project_id] = package
            if self._repository is not None:
                self._repository.save_context(package)
            for item in package.active_decisions:
                key = (package.project_id, item.decision_id)
                if key not in self._versions:
                    versions = (
                        self._repository.list_versions(*key)
                        if self._repository is not None
                        else []
                    )
                    self._versions[key] = versions or [
                        DecisionVersion(
                            decision_id=item.decision_id,
                            version=1,
                            change_reason="初始项目决策",
                            proposed_by=item.owner,
                            approved_by=item.owner,
                            approved_at=item.decided_at,
                            status=DecisionStatus.ACTIVE,
                            evidence_refs=item.source_refs,
                        )
                    ]
                    if self._repository is not None and not versions:
                        self._repository.save_version(package.project_id, self._versions[key][0])

    def answer(
        self,
        project_id: str,
        query: str,
        *,
        kind: AnswerKind,
        now: datetime | None = None,
    ) -> ProjectAnswer:
        context = self._require_fresh_context(project_id, now)
        normalized_query = self._normalize(query)
        if not normalized_query:
            raise ProjectContextUnavailable("source_not_found")
        with self._lock:
            match = next(
                (
                    item
                    for item in context.active_decisions
                    if item.status is DecisionStatus.ACTIVE
                    and self._matches(item, normalized_query)
                ),
                None,
            )
        if match is None:
            raise ProjectContextUnavailable("source_not_found")

        if kind is AnswerKind.SUGGESTION:
            text = f"建议参考当前决策：{match.decision_text}"
        elif kind is AnswerKind.DECISION_CHECK:
            text = f"当前有效决策：{match.decision_text}"
        else:
            text = match.decision_text
        return ProjectAnswer(
            kind=kind,
            text=text,
            source_refs=match.source_refs,
            confidence=match.confidence,
        )

    def current_decision(
        self, project_id: str, decision_id: str, *, now: datetime | None = None
    ) -> DecisionCard:
        context = self._require_fresh_context(project_id, now)
        with self._lock:
            for item in context.active_decisions:
                if item.decision_id == decision_id and item.status is DecisionStatus.ACTIVE:
                    return item
        raise ProjectContextUnavailable("decision_not_found")

    def propose_conflict(
        self,
        project_id: str,
        *,
        decision_id: str,
        observed_text: str,
        reason: str,
        evidence_refs: tuple[EvidenceRef, ...],
        now: datetime | None = None,
    ) -> tuple[ConflictCandidate, bool]:
        context = self._require_fresh_context(project_id, now)
        active = self.current_decision(project_id, decision_id, now=now)
        if not observed_text.strip():
            raise ValueError("observed_text must not be blank")
        if not reason.strip():
            raise ValueError("reason must not be blank")
        self._require_source_scope(context, evidence_refs)
        timestamp = now or self._clock()
        candidate_id = self._conflict_id(
            project_id,
            decision_id,
            observed_text,
            reason,
            evidence_refs,
        )
        with self._lock:
            existing = self._conflicts.get(candidate_id)
            if existing is not None:
                return existing, False
            candidate = ConflictCandidate(
                candidate_id=candidate_id,
                project_id=project_id,
                decision_id=decision_id,
                observed_text=observed_text,
                active_decision_text=active.decision_text,
                reason=reason,
                source_refs=evidence_refs,
                created_at=timestamp,
            )
            self._conflicts[candidate_id] = candidate
            if self._repository is not None:
                self._repository.save_conflict(candidate)
            return candidate, True

    def get_conflict(self, candidate_id: str) -> ConflictCandidate:
        with self._lock:
            candidate = self._conflicts.get(candidate_id)
        if candidate is None and self._repository is not None:
            candidate = self._repository.get_conflict(candidate_id)
            if candidate is not None:
                with self._lock:
                    self._conflicts[candidate_id] = candidate
        if candidate is None:
            raise ProjectMemoryError("conflict_not_found")
        return candidate

    def review_conflict(
        self,
        candidate_id: str,
        *,
        reviewer_id: str,
        action: Literal["accept", "reject"],
        change_reason: str,
        new_decision_text: str | None = None,
        evidence_refs: tuple[EvidenceRef, ...] = (),
        now: datetime | None = None,
    ) -> ConflictCandidate | tuple[ConflictCandidate, DecisionVersion]:
        timestamp = now or self._clock()
        candidate = self.get_conflict(candidate_id)
        if candidate.status is not ConflictStatus.PROPOSED:
            raise ProjectMemoryError("conflict_already_reviewed")
        context = self._require_fresh_context(candidate.project_id, timestamp)
        if not reviewer_id.strip():
            raise ValueError("reviewer_id must not be blank")
        if not change_reason.strip():
            raise ValueError("change_reason must not be blank")

        if action == "reject":
            reviewed = candidate.model_copy(
                update={
                    "status": ConflictStatus.REJECTED,
                    "reviewed_by": reviewer_id,
                    "reviewed_at": timestamp,
                }
            )
            with self._lock:
                self._conflicts[candidate_id] = reviewed
                if self._repository is not None:
                    self._repository.save_conflict(reviewed)
            return reviewed

        if action != "accept":
            raise ValueError("action must be accept or reject")
        if not new_decision_text or not new_decision_text.strip():
            raise ValueError("new_decision_text is required")
        if not evidence_refs:
            raise ValueError("evidence_refs is required")
        self._require_source_scope(context, evidence_refs)
        active = self.current_decision(
            candidate.project_id, candidate.decision_id, now=timestamp
        )
        key = (candidate.project_id, candidate.decision_id)
        with self._lock:
            versions = self._versions.get(key, [])
            if not versions and self._repository is not None:
                versions = self._repository.list_versions(*key)
            self._versions[key] = versions
            previous_version = versions[-1].version if versions else 1
            version = DecisionVersion(
                decision_id=candidate.decision_id,
                version=previous_version + 1,
                replaces_version=previous_version,
                change_reason=change_reason,
                proposed_by="conflict-detector",
                approved_by=reviewer_id,
                approved_at=timestamp,
                status=DecisionStatus.ACTIVE,
                evidence_refs=evidence_refs,
            )
            versions.append(version)
            updated_decision = active.model_copy(
                update={
                    "decision_text": new_decision_text,
                    "rationale": change_reason,
                    "decided_at": timestamp,
                    "source_refs": evidence_refs,
                    "confidence": min(active.confidence, 1.0),
                }
            )
            updated_decisions = tuple(
                updated_decision
                if item.decision_id == active.decision_id
                else item
                for item in context.active_decisions
            )
            updated_context = ProjectContextPackage.model_validate(
                {
                    **context.model_dump(),
                    "active_decisions": updated_decisions,
                }
            )
            self._contexts[candidate.project_id] = updated_context
            if self._repository is not None:
                self._repository.save_context(updated_context)
            reviewed = candidate.model_copy(
                update={
                    "status": ConflictStatus.ACCEPTED,
                    "reviewed_by": reviewer_id,
                    "reviewed_at": timestamp,
                }
            )
            self._conflicts[candidate_id] = reviewed
            if self._repository is not None:
                self._repository.save_version(candidate.project_id, version)
                self._repository.save_conflict(reviewed)
        return reviewed, version

    def _require_fresh_context(
        self, project_id: str, now: datetime | None
    ) -> ProjectContextPackage:
        timestamp = now or self._clock()
        with self._lock:
            context = self._contexts.get(project_id)
        if context is None and self._repository is not None:
            context = self._repository.get_context(project_id)
            if context is not None:
                with self._lock:
                    self._contexts[project_id] = context
        if context is None:
            raise ProjectContextUnavailable("context_not_found")
        age_seconds = (timestamp - context.generated_at).total_seconds()
        if age_seconds > context.freshness_seconds:
            raise ProjectContextUnavailable("context_expired")
        return context

    @staticmethod
    def _normalize(value: str) -> str:
        return "".join(value.split()).casefold()

    @classmethod
    def _matches(cls, decision: DecisionCard, query: str) -> bool:
        return any(
            query in cls._normalize(value)
            for value in (decision.decision_id, decision.topic, decision.decision_text)
        )

    @staticmethod
    def _require_source_scope(
        context: ProjectContextPackage, evidence_refs: tuple[EvidenceRef, ...]
    ) -> None:
        if not evidence_refs:
            raise ValueError("evidence_refs is required")
        if any(
            item.permission_scope != context.permission_scope for item in evidence_refs
        ):
            raise ProjectMemoryError("source_scope_mismatch")

    @staticmethod
    def _conflict_id(
        project_id: str,
        decision_id: str,
        observed_text: str,
        reason: str,
        evidence_refs: tuple[EvidenceRef, ...],
    ) -> str:
        payload = {
            "project_id": project_id,
            "decision_id": decision_id,
            "observed_text": observed_text.strip(),
            "reason": reason.strip(),
            "source_ids": sorted(item.source_id for item in evidence_refs),
        }
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:24]
        return f"conflict-{digest}"

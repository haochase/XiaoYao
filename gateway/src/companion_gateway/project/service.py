from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from threading import RLock
from typing import Callable, Literal, Protocol

from companion_gateway.project.index import (
    EvidenceSource,
    ProjectRuntimeSnapshot,
)
from companion_gateway.project.evidence_validation import reject_external_approvals

from companion_gateway.project.models import (
    AnswerKind,
    ConflictCandidate,
    ConflictStatus,
    DecisionCard,
    DecisionStatus,
    DecisionVersion,
    EvidenceRef,
    HumanApprovalRef,
    ProjectAnswer,
    ProjectContextPackage,
)
from companion_gateway.project.repository import ProjectMemoryRepository
from companion_gateway.project.sync_models import (
    RetrievalRequest,
    RetrievalRequestStatus,
    SyncSourceType,
)
from companion_gateway.project.sync_service import ProjectSourceUnavailable


class ProjectMemoryError(RuntimeError):
    """Base error for deterministic project-memory operations."""


class ProjectContextUnavailable(ProjectMemoryError):
    """Raised when a project context cannot safely answer a request."""


class ProjectSourcePolicy(Protocol):
    def require_snapshot_sources_fresh(
        self,
        project_id: str,
        snapshot: ProjectRuntimeSnapshot,
        source_refs: tuple[EvidenceRef, ...],
        *,
        now: datetime,
    ) -> None: ...


class ProjectSnapshotReader(Protocol):
    def get(self, project_id: str) -> ProjectRuntimeSnapshot | None: ...


class RetrievalRequestWriter(Protocol):
    def save_retrieval_request(
        self,
        request: RetrievalRequest,
        *,
        expected_generation_id: str,
    ) -> RetrievalRequest: ...


class ProjectMemoryService:
    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        repository: ProjectMemoryRepository | None = None,
        source_policy: ProjectSourcePolicy | None = None,
        snapshot_reader: ProjectSnapshotReader | None = None,
        retrieval_writer: RetrievalRequestWriter | None = None,
        retrieval_ttl_seconds: int = 1800,
    ) -> None:
        integration_dependencies = (
            source_policy,
            snapshot_reader,
            retrieval_writer,
        )
        if sum(item is not None for item in integration_dependencies) not in {0, 3}:
            raise ValueError(
                "project query integration dependencies must be supplied together"
            )
        if source_policy is not None and not callable(
            getattr(source_policy, "require_snapshot_sources_fresh", None)
        ):
            raise ValueError("project source policy must be snapshot-scoped")
        if (
            not isinstance(retrieval_ttl_seconds, int)
            or isinstance(retrieval_ttl_seconds, bool)
            or not 60 <= retrieval_ttl_seconds <= 86_400
        ):
            raise ValueError("retrieval_ttl_seconds_invalid")
        self._clock = clock or (lambda: datetime.now(UTC))
        self._repository = repository
        self._source_policy = source_policy
        self._snapshot_reader = snapshot_reader
        self._retrieval_writer = retrieval_writer
        self._retrieval_ttl_seconds = retrieval_ttl_seconds
        self._contexts: dict[str, ProjectContextPackage] = {}
        self._versions: dict[tuple[str, str], list[DecisionVersion]] = {}
        self._conflicts: dict[str, ConflictCandidate] = {}
        self._lock = RLock()

    def replace_context(
        self,
        package: ProjectContextPackage,
        *,
        expected_permission_scope: str | None = None,
    ) -> None:
        try:
            reject_external_approvals(package)
        except ValueError as exc:
            raise ProjectMemoryError(str(exc)) from None
        with self._lock:
            timestamp = self._clock()
            if package.generated_at > timestamp + timedelta(seconds=30):
                raise ProjectMemoryError("context_from_future")
            existing = (
                self._repository.get_context(package.project_id)
                if self._repository is not None
                else self._contexts.get(package.project_id)
            )
            if expected_permission_scope is None and existing is not None:
                expected_permission_scope = existing.permission_scope
            if (
                existing is not None
                and existing.permission_scope != expected_permission_scope
            ):
                raise ProjectMemoryError("context_permission_changed")
            if existing is not None and (
                existing.active_decisions != package.active_decisions
            ):
                raise ProjectMemoryError("decision_change_requires_review")
            initial_versions = tuple(
                self._initial_version(item) for item in package.active_decisions
            )
            if self._repository is not None:
                result = self._repository.replace_context(
                    package,
                    initial_versions,
                    expected_permission_scope=expected_permission_scope,
                )
                if result == "decision_conflict":
                    raise ProjectMemoryError("decision_change_requires_review")
                if result == "permission_conflict":
                    raise ProjectMemoryError("context_permission_changed")
                if result == "stale_context":
                    raise ProjectMemoryError("context_refresh_stale")
            self._contexts[package.project_id] = package
            for item in package.active_decisions:
                key = (package.project_id, item.decision_id)
                if key not in self._versions:
                    versions = (
                        self._repository.list_versions(*key)
                        if self._repository is not None
                        else []
                    )
                    self._versions[key] = versions or [self._initial_version(item)]

    @staticmethod
    def _initial_version(item: DecisionCard) -> DecisionVersion:
        return DecisionVersion(
            decision_id=item.decision_id,
            version=1,
            change_reason="初始项目决策",
            decision_text=item.decision_text,
            proposed_by=item.owner,
            approved_by=item.owner,
            approved_at=item.decided_at,
            status=DecisionStatus.ACTIVE,
            evidence_refs=item.source_refs,
        )

    def get_context(self, project_id: str) -> ProjectContextPackage:
        if self._repository is not None:
            context = self._repository.get_context(project_id)
            if context is not None:
                with self._lock:
                    self._contexts[project_id] = context
        else:
            with self._lock:
                context = self._contexts.get(project_id)
        if context is None:
            raise ProjectContextUnavailable("context_not_found")
        return context

    def answer(
        self,
        project_id: str,
        query: str,
        *,
        kind: AnswerKind,
        now: datetime | None = None,
    ) -> ProjectAnswer:
        timestamp = now or self._clock()
        try:
            pinned_snapshot = (
                self._snapshot_reader.get(project_id)
                if self._query_integration_enabled
                and self._snapshot_reader is not None
                else None
            )
        except ProjectSourceUnavailable as exc:
            self._raise_context_unavailable(exc)
        if self._query_integration_enabled and pinned_snapshot is None:
            raise ProjectContextUnavailable("source_unavailable")
        if pinned_snapshot is None:
            context = self._require_fresh_context(project_id, timestamp)
        else:
            snapshot_context = self._require_package_fresh(
                pinned_snapshot.context, timestamp
            )
            context = (
                self._require_fresh_context(project_id, timestamp)
                if self._repository is not None
                else snapshot_context
            )
            if context.permission_scope != snapshot_context.permission_scope:
                raise ProjectContextUnavailable("source_unavailable")
        normalized_query = self._normalize(query)
        if not normalized_query:
            raise ProjectContextUnavailable("source_not_found")
        match = self._match_active_decision(
            context,
            normalized_query,
            allow_fragments=kind
            in {AnswerKind.DECISION_CHECK, AnswerKind.SUGGESTION},
        )
        if match is None:
            if not self._query_integration_enabled:
                raise ProjectContextUnavailable("source_not_found")
            return self._answer_from_snapshot(
                project_id,
                query,
                normalized_query,
                timestamp,
                pinned_snapshot,
            )

        if self._source_policy is not None:
            self._require_query_sources_fresh(
                project_id,
                () if match.approval_ref is not None else match.source_refs,
                timestamp,
                pinned_snapshot,
            )

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
            approval_ref=match.approval_ref,
            confidence=match.confidence,
        )

    @property
    def _query_integration_enabled(self) -> bool:
        return self._source_policy is not None

    def _answer_from_snapshot(
        self,
        project_id: str,
        query: str,
        normalized_query: str,
        timestamp: datetime,
        snapshot: ProjectRuntimeSnapshot | None,
    ) -> ProjectAnswer:
        assert self._source_policy is not None
        assert self._snapshot_reader is not None
        assert self._retrieval_writer is not None
        if snapshot is None:
            raise ProjectContextUnavailable("source_not_found")

        source_refs_by_hash, has_expired_source = self._usable_evidence_sources(
            project_id,
            snapshot,
            timestamp,
        )
        allowed_source_hashes = frozenset(sorted(source_refs_by_hash))
        source_types = frozenset(
            {SyncSourceType.DOCUMENT, SyncSourceType.MEETING_NOTE}
        )
        hits = snapshot.evidence_index.search(
            query,
            allowed_source_hashes=allowed_source_hashes,
            source_types=source_types,
            limit=5,
        )
        if hits:
            hit = hits[0]
            source_ref = self._evidence_ref(
                snapshot,
                hit.source,
                excerpt=hit.chunk.text[:2000],
            )
            self._require_query_sources_fresh(
                project_id,
                (source_ref,),
                timestamp,
                snapshot,
            )
            return ProjectAnswer(
                kind=AnswerKind.FACT,
                text=hit.chunk.text,
                source_refs=(source_ref,),
                confidence=min(0.95, 0.5 + hit.score / 4000),
            )

        source_hashes = tuple(sorted(source_refs_by_hash))
        if not source_hashes:
            raise ProjectContextUnavailable(
                "source_expired" if has_expired_source else "source_unavailable"
            )
        query_hash = hashlib.sha256(normalized_query.encode("utf-8")).hexdigest()
        request_material = "\0".join(
            (
                project_id,
                snapshot.generation_id,
                query_hash,
                *source_hashes,
            )
        ).encode("utf-8")
        request_id = "ret_" + hashlib.sha256(request_material).hexdigest()[:32]
        try:
            self._retrieval_writer.save_retrieval_request(
                RetrievalRequest(
                    request_id=request_id,
                    project_id=project_id,
                    query_hash=query_hash,
                    source_id_hashes=source_hashes,
                    status=RetrievalRequestStatus.PENDING,
                    created_at=timestamp,
                    expires_at=timestamp
                    + timedelta(seconds=self._retrieval_ttl_seconds),
                ),
                expected_generation_id=snapshot.generation_id,
            )
        except ProjectSourceUnavailable as exc:
            self._raise_context_unavailable(exc)
        raise ProjectContextUnavailable("evidence_pending")

    def _usable_evidence_sources(
        self,
        project_id: str,
        snapshot: ProjectRuntimeSnapshot,
        timestamp: datetime,
    ) -> tuple[dict[str, EvidenceRef], bool]:
        source_refs_by_hash: dict[str, EvidenceRef] = {}
        has_expired_source = False
        for source in snapshot.sources:
            if source.source_type not in {
                SyncSourceType.DOCUMENT,
                SyncSourceType.MEETING_NOTE,
            }:
                continue
            source_ref = self._evidence_ref(
                snapshot,
                source,
                excerpt=source.source_title,
            )
            try:
                self._require_query_sources_fresh(
                    project_id,
                    (source_ref,),
                    timestamp,
                    snapshot,
                )
            except ProjectContextUnavailable as exc:
                label = str(exc)
                if label in {"clock_resync_required", "clock_untrusted"}:
                    raise
                if label == "source_expired":
                    has_expired_source = True
                continue
            source_refs_by_hash[source.source_id_hash] = source_ref
        return source_refs_by_hash, has_expired_source

    @staticmethod
    def _evidence_ref(
        snapshot: ProjectRuntimeSnapshot,
        source: EvidenceSource,
        *,
        excerpt: str,
    ) -> EvidenceRef:
        return EvidenceRef(
            source_type=source.source_type.value,
            source_id=source.source_id,
            source_title=source.source_title,
            source_url=source.source_url,
            source_time=source.source_time,
            excerpt=excerpt,
            permission_scope=snapshot.context.permission_scope,
        )

    def _require_query_sources_fresh(
        self,
        project_id: str,
        source_refs: tuple[EvidenceRef, ...],
        timestamp: datetime,
        snapshot: ProjectRuntimeSnapshot | None,
    ) -> None:
        assert self._source_policy is not None
        if snapshot is None:
            return
        try:
            self._source_policy.require_snapshot_sources_fresh(
                project_id,
                snapshot,
                source_refs,
                now=timestamp,
            )
        except ProjectSourceUnavailable as exc:
            self._raise_context_unavailable(exc)

    @staticmethod
    def _raise_context_unavailable(exc: ProjectSourceUnavailable) -> None:
        label = str(exc)
        if label == "source_stale":
            raise ProjectContextUnavailable("source_expired") from None
        if label in {
            "source_expired",
            "clock_resync_required",
            "clock_untrusted",
        }:
            raise ProjectContextUnavailable(label) from None
        raise ProjectContextUnavailable("source_unavailable") from None

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
        proposed_decision_text: str,
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
        if evidence_refs or active.approval_ref is None:
            self._require_source_scope(context, evidence_refs)
        if evidence_refs != active.source_refs:
            raise ProjectMemoryError("conflict_basis_mismatch")
        timestamp = now or self._clock()
        with self._lock:
            versions = self._decision_versions(project_id, active)
            base_version = versions[-1].version
            candidate_id = self._conflict_id(
                project_id,
                decision_id,
                base_version,
                observed_text,
                proposed_decision_text,
                reason,
                evidence_refs,
                active.approval_ref,
            )
            if self._repository is None:
                existing = self._conflicts.get(candidate_id)
                if existing is not None:
                    return existing, False
            candidate = ConflictCandidate(
                candidate_id=candidate_id,
                project_id=project_id,
                decision_id=decision_id,
                base_version=base_version,
                observed_text=observed_text,
                proposed_decision_text=proposed_decision_text,
                active_decision_text=active.decision_text,
                reason=reason,
                source_refs=evidence_refs,
                active_approval_ref=active.approval_ref,
                created_at=timestamp,
            )
            if self._repository is not None:
                stored, created = self._repository.create_conflict(candidate)
            else:
                stored, created = candidate, True
            self._conflicts[candidate_id] = stored
            return stored, created

    def propose_conflict_from_statement(
        self,
        project_id: str,
        statement: str,
        *,
        proposed_decision_text: str,
        now: datetime | None = None,
    ) -> tuple[ConflictCandidate, bool]:
        timestamp = now or self._clock()
        if self._query_integration_enabled:
            assert self._snapshot_reader is not None
            try:
                snapshot = self._snapshot_reader.get(project_id)
            except ProjectSourceUnavailable as exc:
                self._raise_context_unavailable(exc)
            if snapshot is None:
                raise ProjectContextUnavailable("source_unavailable")
            snapshot_context = self._require_package_fresh(snapshot.context, timestamp)
            context = (
                self._require_fresh_context(project_id, timestamp)
                if self._repository is not None
                else snapshot_context
            )
            if context.permission_scope != snapshot_context.permission_scope:
                raise ProjectContextUnavailable("source_unavailable")
        else:
            snapshot = None
            context = self._require_fresh_context(project_id, timestamp)
        normalized_statement = self._normalize(statement)
        if not normalized_statement:
            raise ProjectContextUnavailable("source_not_found")
        decision = self._match_active_decision(
            context,
            normalized_statement,
            allow_fragments=True,
        )
        if decision is None:
            raise ProjectContextUnavailable("source_not_found")
        if self._decisions_equivalent(
            decision.decision_text,
            proposed_decision_text,
        ):
            raise ProjectMemoryError("statement_matches_active_decision")
        if self._source_policy is not None:
            self._require_query_sources_fresh(
                project_id,
                () if decision.approval_ref is not None else decision.source_refs,
                timestamp,
                snapshot,
            )
        return self.propose_conflict(
            project_id,
            decision_id=decision.decision_id,
            observed_text=statement,
            proposed_decision_text=proposed_decision_text,
            reason="会议发言可能与当前有效决策不一致",
            evidence_refs=decision.source_refs,
            now=timestamp,
        )

    def get_conflict(self, candidate_id: str) -> ConflictCandidate:
        if self._repository is not None:
            candidate = self._repository.get_conflict(candidate_id)
            if candidate is not None:
                with self._lock:
                    self._conflicts[candidate_id] = candidate
        else:
            with self._lock:
                candidate = self._conflicts.get(candidate_id)
        if candidate is None:
            raise ProjectMemoryError("conflict_not_found")
        return candidate

    def list_conflicts(
        self,
        project_id: str,
        *,
        status: ConflictStatus | None = None,
    ) -> tuple[ConflictCandidate, ...]:
        self.get_context(project_id)
        if self._repository is not None:
            candidates = self._repository.list_conflicts(project_id, status=status)
            with self._lock:
                self._conflicts.update(
                    (candidate.candidate_id, candidate) for candidate in candidates
                )
            return tuple(candidates)
        with self._lock:
            return tuple(
                sorted(
                    (
                        candidate
                        for candidate in self._conflicts.values()
                        if candidate.project_id == project_id
                        and (status is None or candidate.status is status)
                    ),
                    key=lambda candidate: candidate.candidate_id,
                )
            )

    def review_conflict(
        self,
        candidate_id: str,
        *,
        reviewer_id: str,
        action: Literal["accept", "reject"],
        change_reason: str,
        now: datetime | None = None,
    ) -> ConflictCandidate | tuple[ConflictCandidate, DecisionVersion]:
        timestamp = now or self._clock()
        with self._lock:
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
                        "review_reason": change_reason,
                    }
                )
                result = (
                    self._repository.commit_conflict_review(
                        reviewed_candidate=reviewed,
                    )
                    if self._repository is not None
                    else "committed"
                )
                self._raise_for_review_commit(result)
                self._conflicts[candidate_id] = reviewed
                return reviewed

            if action != "accept":
                raise ValueError("action must be accept or reject")
            new_decision_text = candidate.proposed_decision_text
            approval = HumanApprovalRef(
                candidate_id=candidate.candidate_id,
                reviewer_id=reviewer_id,
                approved_at=timestamp,
                reason=change_reason,
                decision_text=new_decision_text,
                permission_scope=context.permission_scope,
            )
            active = self.current_decision(
                candidate.project_id, candidate.decision_id, now=timestamp
            )
            if active.decision_text != candidate.active_decision_text:
                raise ProjectMemoryError("conflict_stale")
            key = (candidate.project_id, candidate.decision_id)
            versions = self._decision_versions(candidate.project_id, active)
            previous = versions[-1]
            previous_version = previous.version
            if candidate.base_version != previous_version:
                raise ProjectMemoryError("conflict_stale")
            superseded = previous.model_copy(
                update={
                    "decision_text": previous.decision_text or active.decision_text,
                    "status": DecisionStatus.SUPERSEDED,
                }
            )
            version = DecisionVersion(
                decision_id=candidate.decision_id,
                version=previous_version + 1,
                replaces_version=previous_version,
                change_reason=change_reason,
                decision_text=new_decision_text,
                proposed_by="conflict-detector",
                approved_by=reviewer_id,
                approved_at=timestamp,
                status=DecisionStatus.ACTIVE,
                evidence_refs=(),
                approval_ref=approval,
            )
            updated_decision = active.model_copy(
                update={
                    "decision_text": new_decision_text,
                    "rationale": change_reason,
                    "decided_at": timestamp,
                    "source_refs": (),
                    "approval_ref": approval,
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
            reviewed = candidate.model_copy(
                update={
                    "status": ConflictStatus.ACCEPTED,
                    "reviewed_by": reviewer_id,
                    "reviewed_at": timestamp,
                    "review_reason": change_reason,
                }
            )
            result = (
                self._repository.commit_conflict_review(
                    reviewed_candidate=reviewed,
                    expected_base_version=candidate.base_version,
                    expected_active_decision_text=candidate.active_decision_text,
                    updated_context=updated_context,
                    previous_version=superseded,
                    new_version=version,
                )
                if self._repository is not None
                else "committed"
            )
            self._raise_for_review_commit(result)
            self._versions[key] = [*versions[:-1], superseded, version]
            self._contexts[candidate.project_id] = updated_context
            self._conflicts[candidate_id] = reviewed
        return reviewed, version

    def _decision_versions(
        self,
        project_id: str,
        active: DecisionCard,
    ) -> list[DecisionVersion]:
        key = (project_id, active.decision_id)
        if self._repository is not None:
            versions = self._repository.list_versions(*key)
            if not versions:
                raise ProjectMemoryError("decision_history_incomplete")
        else:
            versions = self._versions.get(key, [])
        if not versions and self._repository is None:
            versions = [self._initial_version(active)]
        expected_numbers = list(range(1, len(versions) + 1))
        active_versions = [
            item for item in versions if item.status is DecisionStatus.ACTIVE
        ]
        if (
            any(item.decision_text is None for item in versions)
            or [item.version for item in versions] != expected_numbers
            or active_versions != [versions[-1]]
            or versions[-1].decision_text != active.decision_text
        ):
            raise ProjectMemoryError("decision_history_incomplete")
        self._versions[key] = versions
        return versions

    @staticmethod
    def _raise_for_review_commit(result: str) -> None:
        if result == "committed":
            return
        error = {
            "not_found": "conflict_not_found",
            "already_reviewed": "conflict_already_reviewed",
            "stale": "conflict_stale",
        }.get(result, "conflict_review_failed")
        raise ProjectMemoryError(error)

    def _require_fresh_context(
        self, project_id: str, now: datetime | None
    ) -> ProjectContextPackage:
        timestamp = now or self._clock()
        context = self.get_context(project_id)
        age_seconds = (timestamp - context.generated_at).total_seconds()
        if age_seconds > context.freshness_seconds:
            raise ProjectContextUnavailable("context_expired")
        return context

    @staticmethod
    def _require_package_fresh(
        context: ProjectContextPackage,
        timestamp: datetime,
    ) -> ProjectContextPackage:
        age_seconds = (timestamp - context.generated_at).total_seconds()
        if age_seconds > context.freshness_seconds:
            raise ProjectContextUnavailable("context_expired")
        return context

    @staticmethod
    def _normalize(value: str) -> str:
        return "".join(value.split()).casefold()

    @classmethod
    def _decisions_equivalent(cls, active: str, proposed: str) -> bool:
        normalized_active = cls._normalize(active)
        normalized_proposed = cls._normalize(proposed)
        if not normalized_proposed:
            raise ValueError("proposed_decision_text must not be blank")
        return normalized_active == normalized_proposed

    @staticmethod
    def _chinese_bigrams(value: str) -> set[str]:
        fragments: set[str] = set()
        run = ""
        for character in value:
            if "\u4e00" <= character <= "\u9fff":
                run += character
                continue
            fragments.update(run[index : index + 2] for index in range(len(run) - 1))
            run = ""
        fragments.update(run[index : index + 2] for index in range(len(run) - 1))
        return fragments

    @classmethod
    def _matches_exactly(cls, decision: DecisionCard, query: str) -> bool:
        values = (
            decision.decision_id,
            decision.topic,
            decision.decision_text,
        )
        for value in values:
            normalized_value = cls._normalize(value)
            if query in normalized_value or (
                len(normalized_value) >= 2 and normalized_value in query
            ):
                return True
        return False

    @classmethod
    def _match_active_decision(
        cls,
        context: ProjectContextPackage,
        normalized_query: str,
        *,
        allow_fragments: bool,
    ) -> DecisionCard | None:
        active_decisions = tuple(
            item
            for item in context.active_decisions
            if item.status is DecisionStatus.ACTIVE
        )
        match = next(
            (
                item
                for item in active_decisions
                if cls._matches_exactly(item, normalized_query)
            ),
            None,
        )
        if match is not None or not allow_fragments:
            return match
        scored_matches = tuple(
            (score, item)
            for item in active_decisions
            if (score := cls._fragment_match_score(item, normalized_query)) is not None
        )
        if not scored_matches:
            return None
        best_score = max(score for score, _item in scored_matches)
        best_matches = tuple(
            item for score, item in scored_matches if score == best_score
        )
        return best_matches[0] if len(best_matches) == 1 else None

    @classmethod
    def _fragment_match_score(
        cls,
        decision: DecisionCard,
        query: str,
    ) -> tuple[int, int] | None:
        query_fragments = cls._chinese_bigrams(query)
        topic_matches = query_fragments & cls._chinese_bigrams(
            cls._normalize(decision.topic)
        )
        text_matches = query_fragments & cls._chinese_bigrams(
            cls._normalize(decision.decision_text)
        )
        total_matches = topic_matches | text_matches
        if not topic_matches or len(total_matches) < 2:
            return None
        return len(topic_matches), len(total_matches)

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
        base_version: int,
        observed_text: str,
        proposed_decision_text: str,
        reason: str,
        evidence_refs: tuple[EvidenceRef, ...],
        active_approval_ref: HumanApprovalRef | None,
    ) -> str:
        payload = {
            "project_id": project_id,
            "decision_id": decision_id,
            "base_version": base_version,
            "observed_text": observed_text.strip(),
            "proposed_decision_text": proposed_decision_text,
            "reason": reason.strip(),
            "source_refs": [item.model_dump(mode="json") for item in evidence_refs],
            "active_approval_ref": active_approval_ref.model_dump(mode="json") if active_approval_ref else None,
        }
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:24]
        return f"conflict-{digest}"

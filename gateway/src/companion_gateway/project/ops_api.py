from __future__ import annotations

from datetime import datetime
from typing import Any

from companion_gateway.project.models import ConflictCandidate, ConflictStatus


def _evidence(candidate: ConflictCandidate) -> dict[str, Any] | None:
    if not candidate.source_refs:
        return None
    source = candidate.source_refs[0]
    return {
        "source_type": source.source_type,
        "source_time": source.source_time,
        "excerpt": (source.excerpt or "")[:150],
    }


def redact_conflict(candidate: ConflictCandidate) -> dict[str, Any]:
    return {
        "candidate_id": candidate.candidate_id,
        "decision_id": candidate.decision_id,
        "active_text": candidate.active_decision_text,
        "proposed_text": candidate.proposed_decision_text,
        "observed_text": candidate.observed_text,
        "reason": candidate.reason,
        "status": candidate.status.value,
        "created_at": candidate.created_at,
        "reviewed_by": candidate.reviewed_by,
        "reviewed_at": candidate.reviewed_at,
        "review_reason": candidate.review_reason,
        "evidence": _evidence(candidate),
    }


def project_summary(
    project_memory: Any,
    project_id: str,
    *,
    now: datetime,
    sync_repository: Any = None,
) -> dict[str, Any]:
    context = project_memory.get_context(project_id)
    conflicts = project_memory.list_conflicts(project_id)
    counts = {status.value: 0 for status in ConflictStatus}
    for candidate in conflicts:
        counts[candidate.status.value] = counts.get(candidate.status.value, 0) + 1

    source_keys = {
        (source.source_type, source.source_id)
        for source in context.source_refs
    }
    source_keys.update(
        (source.source_type, source.source_id)
        for decision in context.active_decisions
        for source in decision.source_refs
    )

    clock_status = "normal"
    last_success_at = context.generated_at
    if sync_repository is not None:
        try:
            if sync_repository.project_requires_clock_resync(project_id):
                clock_status = "resync_required"
        except (AttributeError, RuntimeError, ValueError):
            pass
    return {
        "project_name": context.project_name,
        "source_count": len(source_keys),
        "freshness_seconds": context.freshness_seconds,
        "last_success_at_present": last_success_at is not None,
        "clock_status": clock_status,
        "active_decision_count": len(context.active_decisions),
        "candidate_counts": counts,
        "proposed_candidate_count": counts.get(ConflictStatus.PROPOSED.value, 0),
        "rejected_candidate_count": counts.get(ConflictStatus.REJECTED.value, 0),
        "accepted_candidate_count": counts.get(ConflictStatus.ACCEPTED.value, 0),
        "refreshed_at": now,
    }


def project_conflicts(project_memory: Any, project_id: str) -> tuple[dict[str, Any], ...]:
    return tuple(redact_conflict(item) for item in project_memory.list_conflicts(project_id))

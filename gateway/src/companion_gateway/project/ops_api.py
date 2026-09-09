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
    snapshot_reader: Any = None,
    sync_repository: Any = None,
) -> dict[str, Any]:
    context = project_memory.get_context(project_id)
    conflicts = project_memory.list_conflicts(project_id)
    counts = {status.value: 0 for status in ConflictStatus}
    for candidate in conflicts:
        counts[candidate.status.value] = counts.get(candidate.status.value, 0) + 1

    clock_status, snapshot = _runtime_status(
        project_id,
        snapshot_reader=snapshot_reader,
        sync_repository=sync_repository,
    )
    states = () if snapshot is None else snapshot.source_states
    return {
        "project_name": context.project_name,
        "source_count": len(states),
        "freshness_seconds": context.freshness_seconds,
        "last_success_at_present": any(
            state.last_success_at is not None for state in states
        ),
        "clock_status": clock_status,
        "active_decision_count": len(context.active_decisions),
        "candidate_counts": counts,
        "proposed_candidate_count": counts.get(ConflictStatus.PROPOSED.value, 0),
        "rejected_candidate_count": counts.get(ConflictStatus.REJECTED.value, 0),
        "accepted_candidate_count": counts.get(ConflictStatus.ACCEPTED.value, 0),
        "refreshed_at": now,
}


def _runtime_status(
    project_id: str,
    *,
    snapshot_reader: Any,
    sync_repository: Any,
) -> tuple[str, Any | None]:
    if snapshot_reader is None or sync_repository is None:
        return "unavailable", None
    try:
        clock_state = sync_repository.load_clock_state()
    except Exception:
        return "unavailable", None
    if clock_state.clock_untrusted:
        return "clock_untrusted", _read_snapshot(snapshot_reader, project_id)
    try:
        needs_sync = sync_repository.project_requires_clock_resync(project_id)
    except Exception:
        return "unavailable", None
    snapshot = _read_snapshot(snapshot_reader, project_id)
    if needs_sync:
        return "needs_sync", snapshot
    if snapshot is None:
        return "unavailable", None
    return "normal", snapshot


def _read_snapshot(snapshot_reader: Any, project_id: str) -> Any | None:
    try:
        return snapshot_reader.get(project_id)
    except Exception:
        return None


def project_conflicts(project_memory: Any, project_id: str) -> tuple[dict[str, Any], ...]:
    return tuple(redact_conflict(item) for item in project_memory.list_conflicts(project_id))

from __future__ import annotations

from collections.abc import Iterable

from companion_gateway.project.models import ProjectContextPackage
from companion_gateway.project.sync_models import (
    SourceSnapshot,
    SourceSyncStatus,
    SyncSourceType,
)


def _normalized_text(value: str) -> str:
    return "".join(value.split()).casefold()


def validate_sourced_context(
    context: ProjectContextPackage,
    sources: Iterable[SourceSnapshot],
) -> None:
    reject_external_approvals(context)
    if (
        context.open_actions
        or context.current_risks
        or context.next_meeting is not None
    ):
        raise ValueError("context_fact_unreferenced")

    active_sources = {
        (source.source_type, source.source_id): source
        for source in sources
        if source.status is SourceSyncStatus.ACTIVE
    }
    sourced_facts = (*context.sourced_actions, *context.sourced_risks)
    if context.sourced_next_meeting is not None:
        sourced_facts += (context.sourced_next_meeting,)
    references = (
        *context.source_refs,
        *(
            reference
            for decision in context.active_decisions
            for reference in decision.source_refs
        ),
        *(
            reference
            for fact in sourced_facts
            for reference in fact.source_refs
        ),
    )

    for reference in references:
        try:
            source_type = SyncSourceType(reference.source_type)
        except ValueError:
            raise ValueError("source_ref_mismatch") from None
        source = active_sources.get((source_type, reference.source_id))
        if source is None or (
            reference.source_title != source.source_title
            or reference.source_url != source.source_url
            or reference.source_time != source.source_time
            or reference.permission_scope != source.permission_scope
            or reference.permission_scope != context.permission_scope
        ):
            raise ValueError("source_ref_mismatch")
        source_text = _normalized_text(
            "\n".join(chunk.text for chunk in source.chunks)
        )
        excerpt = _normalized_text(reference.excerpt)
        if not source_text or excerpt not in source_text:
            raise ValueError("source_excerpt_mismatch")


def reject_external_approvals(context: ProjectContextPackage) -> None:
    if any(decision.approval_ref is not None for decision in context.active_decisions):
        raise ValueError("external_approval_forbidden")

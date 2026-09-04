import hashlib
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from threading import Event, Thread

import pytest

from companion_gateway.project.index import (
    EvidenceSource,
    ProjectEvidenceIndex,
    ProjectRuntimeSnapshot,
    ProjectSnapshotRegistry,
    chunk_text,
)
from companion_gateway.project.models import (
    DecisionCard,
    EvidenceRef,
    ProjectContextPackage,
)
from companion_gateway.project.sync_models import (
    EvidenceChunk,
    SourceState,
    SyncSourceType,
)


NOW = datetime(2026, 9, 5, 8, 0, tzinfo=UTC)
LATER = NOW + timedelta(minutes=1)


def source_id_hash(source_id: str) -> str:
    return hashlib.sha256(source_id.encode("utf-8")).hexdigest()


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def evidence_source(
    source_id: str,
    *,
    source_type: SyncSourceType = SyncSourceType.DOCUMENT,
    source_time: datetime = NOW,
    source_version: str = "v1",
) -> EvidenceSource:
    return EvidenceSource(
        source_type=source_type,
        source_id=source_id,
        source_id_hash=source_id_hash(source_id),
        source_title=f"{source_id} 标题",
        source_url=f"https://example.invalid/{source_id}",
        source_version=source_version,
        source_time=source_time,
        permission_hash=digest(f"permission:{source_id}"),
        content_hash=digest(f"content:{source_id}"),
    )


def evidence_chunk(
    source: EvidenceSource,
    text: str,
    *,
    ordinal: int = 0,
    chunk_id: str | None = None,
) -> EvidenceChunk:
    return EvidenceChunk(
        chunk_id=chunk_id or digest(f"{source.source_id}:{ordinal}:{text}"),
        source_id=source.source_id,
        source_version=source.source_version,
        ordinal=ordinal,
        heading_path=("资料",),
        text=text,
        start_offset=ordinal * 100,
        end_offset=ordinal * 100 + len(text),
        content_hash=digest(text),
    )


def source_state(
    source: EvidenceSource,
    *,
    project_id: str = "project-1",
) -> SourceState:
    return SourceState(
        project_id=project_id,
        source_type=source.source_type,
        source_id_hash=source.source_id_hash,
        source_version=source.source_version,
        content_hash=source.content_hash,
        permission_hash=source.permission_hash,
        status="active",
        last_attempt_at=source.source_time,
        last_success_at=source.source_time,
        last_error_type=None,
    )


def context(
    *,
    project_id: str = "project-1",
    decision_sources: tuple[EvidenceSource, ...] = (),
) -> ProjectContextPackage:
    refs = tuple(
        EvidenceRef(
            source_type=source.source_type.value,
            source_id=source.source_id,
            source_title=source.source_title,
            source_url=source.source_url,
            source_time=source.source_time,
            excerpt="已验证来源",
            permission_scope="project:demo",
        )
        for source in decision_sources
    )
    decisions = (
        DecisionCard(
            decision_id="decision-1",
            project_id=project_id,
            topic="发布方案",
            decision_text="采用方案 B",
            rationale="风险更低",
            owner="owner-1",
            decided_at=NOW,
            source_refs=refs,
            status="active",
            confidence=0.9,
        ),
    ) if refs else ()
    return ProjectContextPackage(
        project_id=project_id,
        project_name="会锚项目",
        generated_at=NOW,
        source_refs=refs,
        active_decisions=decisions,
        permission_scope="project:demo",
    )


def evidence_index(
    *,
    sources: tuple[EvidenceSource, ...],
    chunks: tuple[EvidenceChunk, ...],
    decision_sources: tuple[EvidenceSource, ...] = (),
) -> ProjectEvidenceIndex:
    return ProjectEvidenceIndex(
        context=context(decision_sources=decision_sources),
        sources=sources,
        chunks=chunks,
    )


def snapshot(
    *,
    generation: str = "generation-1",
    text: str = "发布方案内容",
) -> ProjectRuntimeSnapshot:
    source = evidence_source("source-1")
    chunk = evidence_chunk(source, text)
    runtime_context = context(decision_sources=(source,))
    index = ProjectEvidenceIndex(
        context=runtime_context,
        sources=(source,),
        chunks=(chunk,),
    )
    return ProjectRuntimeSnapshot(
        project_id="project-1",
        generation_id=generation,
        context=runtime_context,
        source_states=(source_state(source),),
        sources=(source,),
        chunks=(chunk,),
        evidence_index=index,
    )


def test_chunking_is_stable_and_bounded() -> None:
    markdown = "# 方案\n" + "甲" * 2500

    first = chunk_text("doc-1", "v1", markdown)
    second = chunk_text("doc-1", "v1", markdown)

    assert first == second
    assert all(len(item.text) <= 1200 for item in first)
    assert len(first) == 3
    assert all(item.heading_path == ("方案",) for item in first)
    assert all("方案" not in item.text for item in first)
    assert first[1].start_offset == first[0].end_offset - 150
    assert first[2].start_offset == first[1].end_offset - 150


def test_chunking_packs_blocks_with_their_heading_path() -> None:
    chunks = chunk_text(
        "doc-1",
        "v1",
        "# 总览\n首段\n\n- 第一项\n- 第二项\n\n## 风险\n风险内容",
    )

    assert [(item.heading_path, item.text) for item in chunks] == [
        (("总览",), "首段\n\n- 第一项\n- 第二项"),
        (("总览", "风险"), "风险内容"),
    ]


def test_chunking_preserves_original_inter_block_text_and_offsets() -> None:
    markdown = "# 总览\r\n首段\r\n\r\n第二段"

    chunks = chunk_text("doc-1", "v1", markdown)

    assert len(chunks) == 1
    assert chunks[0].text == "首段\r\n\r\n第二段"
    assert chunks[0].start_offset == len("# 总览\r\n")
    assert chunks[0].end_offset == len(markdown)


def test_chunking_does_not_cross_a_repeated_heading_path() -> None:
    chunks = chunk_text("doc-1", "v1", "# 方案\n第一段\n\n# 方案\n第二段")

    assert [(item.heading_path, item.text) for item in chunks] == [
        (("方案",), "第一段"),
        (("方案",), "第二段"),
    ]


def test_chunking_keeps_heading_like_code_fence_lines_as_evidence() -> None:
    chunks = chunk_text(
        "doc-1",
        "v1",
        "# 总览\n```python\n# 这不是标题\nvalue = 1\n```",
    )

    assert [(item.heading_path, item.text) for item in chunks] == [
        (("总览",), "```python\n# 这不是标题\nvalue = 1\n```")
    ]


def test_chunking_does_not_close_a_fence_with_a_non_whitespace_suffix() -> None:
    chunks = chunk_text(
        "doc-1",
        "v1",
        "```text\n```not-close\n# still-code\n```\n\n# 后续标题\n正文",
    )

    assert [(item.heading_path, item.text) for item in chunks] == [
        ((), "```text\n```not-close\n# still-code\n```"),
        (("后续标题",), "正文"),
    ]


def test_chunking_splits_list_like_code_fence_lines_with_overlap() -> None:
    chunks = chunk_text(
        "doc-1",
        "v1",
        "# 总览\n```text\n- "
        + "甲" * 600
        + "\n\n- "
        + "乙" * 600
        + "\n```",
    )

    assert len(chunks) == 2
    assert all(len(item.text) <= 1200 for item in chunks)
    assert all(item.heading_path == ("总览",) for item in chunks)
    assert chunks[1].start_offset == chunks[0].end_offset - 150


def test_chunking_recognizes_indented_atx_headings() -> None:
    chunks = chunk_text("doc-1", "v1", "  # 总览\n首段\n\n  ## 风险\n风险内容")

    assert [(item.heading_path, item.text) for item in chunks] == [
        (("总览",), "首段"),
        (("总览", "风险"), "风险内容"),
    ]


def test_chunking_preserves_non_closing_hashes_in_an_atx_heading() -> None:
    chunks = chunk_text("doc-1", "v1", "# C#\n正文")

    assert [(item.heading_path, item.text) for item in chunks] == [
        (("C#",), "正文")
    ]


def test_chunking_strips_space_prefixed_atx_closing_hashes() -> None:
    chunks = chunk_text("doc-1", "v1", "# 标题 ###\n正文")

    assert [(item.heading_path, item.text) for item in chunks] == [
        (("标题",), "正文")
    ]


def test_chunking_treats_a_bare_atx_marker_as_an_empty_boundary() -> None:
    chunks = chunk_text("doc-1", "v1", "# 前一节\n第一段\n\n#\n第二段")

    assert [(item.heading_path, item.text) for item in chunks] == [
        (("前一节",), "第一段"),
        ((), "第二段"),
    ]


def test_chunking_treats_hash_only_atx_content_as_an_empty_boundary() -> None:
    chunks = chunk_text("doc-1", "v1", "# 前一节\n第一段\n\n# ###\n第二段")

    assert [(item.heading_path, item.text) for item in chunks] == [
        (("前一节",), "第一段"),
        ((), "第二段"),
    ]


def test_chunking_splits_a_loose_list_with_overlap() -> None:
    chunks = chunk_text(
        "doc-1",
        "v1",
        "# 总览\n- " + "甲" * 600 + "\n\n- " + "乙" * 600,
    )

    assert len(chunks) == 2
    assert all(len(item.text) <= 1200 for item in chunks)
    assert all(item.heading_path == ("总览",) for item in chunks)
    assert chunks[1].start_offset == chunks[0].end_offset - 150


def test_chunking_splits_a_fenced_block_with_overlap() -> None:
    chunks = chunk_text(
        "doc-1",
        "v1",
        "# 总览\n```text\n" + "甲" * 600 + "\n\n" + "乙" * 600 + "\n```",
    )

    assert len(chunks) == 2
    assert all(len(item.text) <= 1200 for item in chunks)
    assert all(item.heading_path == ("总览",) for item in chunks)
    assert chunks[1].start_offset == chunks[0].end_offset - 150


def test_chunking_empty_content_returns_no_chunks() -> None:
    assert chunk_text("doc-1", "v1", "# 仅标题\n\n") == ()


def test_search_scores_decision_source_before_newer_text_match() -> None:
    decision_source = evidence_source("decision-source", source_time=NOW)
    newer_source = evidence_source(
        "newer-source",
        source_type=SyncSourceType.MEETING_NOTE,
        source_time=LATER,
    )
    decision_chunk = evidence_chunk(decision_source, "风险与交付说明")
    newer_chunk = evidence_chunk(newer_source, "请确认发布方案已经确认")
    index = evidence_index(
        sources=(decision_source, newer_source),
        chunks=(newer_chunk, decision_chunk),
        decision_sources=(decision_source,),
    )

    hits = index.search("请确认发布方案")

    assert [hit.chunk.chunk_id for hit in hits] == [
        decision_chunk.chunk_id,
        newer_chunk.chunk_id,
    ]
    assert hits[0].score == 1_040
    assert hits[1].score == 635


def test_search_normalizes_text_scores_bigrams_and_uses_source_weight() -> None:
    document = evidence_source("document", source_type=SyncSourceType.DOCUMENT)
    meeting = evidence_source(
        "meeting",
        source_type=SyncSourceType.MEETING_NOTE,
        source_time=LATER,
    )
    document_chunk = evidence_chunk(document, "ALPHA beta 当前状态")
    meeting_chunk = evidence_chunk(meeting, "alpha beta 当前状态")
    index = evidence_index(
        sources=(meeting, document),
        chunks=(meeting_chunk, document_chunk),
    )

    hits = index.search(" Alpha\nBETA ")

    assert [hit.chunk.chunk_id for hit in hits] == [
        document_chunk.chunk_id,
        meeting_chunk.chunk_id,
    ]
    assert [hit.score for hit in hits] == [640, 635]


def test_search_filters_sources_before_scoring_and_rejects_nonmatches() -> None:
    document = evidence_source("document", source_type=SyncSourceType.DOCUMENT)
    meeting = evidence_source("meeting", source_type=SyncSourceType.MEETING_NOTE)
    document_chunk = evidence_chunk(document, "发布方案正文")
    meeting_chunk = evidence_chunk(meeting, "发布方案会议纪要")
    index = evidence_index(
        sources=(document, meeting),
        chunks=(document_chunk, meeting_chunk),
    )

    assert index.search("发布方案", allowed_source_hashes=frozenset()) == ()
    assert index.search(
        "发布方案",
        allowed_source_hashes=frozenset({document.source_id_hash}),
    ) == (
        index.search("发布方案")[0],
    )
    assert index.search(
        "发布方案",
        source_types=frozenset({SyncSourceType.MEETING_NOTE}),
    ) == (index.search("发布方案")[1],)
    assert index.search("完全无关的查询") == ()


def test_search_sorts_equal_scores_by_newest_source_then_chunk_id() -> None:
    oldest = evidence_source("oldest", source_time=NOW)
    newest = evidence_source("newest", source_time=LATER)
    same_time = evidence_source("same-time", source_time=LATER)
    oldest_chunk = evidence_chunk(oldest, "发布方案正文", chunk_id="a" * 64)
    newest_chunk = evidence_chunk(newest, "发布方案正文", chunk_id="f" * 64)
    same_time_chunk = evidence_chunk(same_time, "发布方案正文", chunk_id="b" * 64)
    index = evidence_index(
        sources=(oldest, newest, same_time),
        chunks=(newest_chunk, oldest_chunk, same_time_chunk),
    )

    hits = index.search("发布方案")

    assert [hit.chunk.chunk_id for hit in hits] == [
        same_time_chunk.chunk_id,
        newest_chunk.chunk_id,
        oldest_chunk.chunk_id,
    ]


def test_search_validates_limit_and_returns_empty_for_blank_query() -> None:
    source = evidence_source("source-1")
    index = evidence_index(
        sources=(source,),
        chunks=(evidence_chunk(source, "发布方案正文"),),
    )

    assert index.search(" \n\t ") == ()
    with pytest.raises(ValueError, match="limit"):
        index.search("发布方案", limit=0)
    with pytest.raises(ValueError, match="limit"):
        index.search("发布方案", limit=21)


def test_snapshot_copies_iterables_and_rejects_mutation() -> None:
    source = evidence_source("source-1")
    chunk = evidence_chunk(source, "发布方案正文")
    runtime_context = context()
    index = ProjectEvidenceIndex(
        context=runtime_context,
        sources=(source,),
        chunks=(chunk,),
    )
    supplied_states = [source_state(source)]
    supplied_sources = [source]
    supplied_chunks = [chunk]
    runtime_snapshot = ProjectRuntimeSnapshot(
        project_id="project-1",
        generation_id="generation-1",
        context=runtime_context,
        source_states=supplied_states,
        sources=supplied_sources,
        chunks=supplied_chunks,
        evidence_index=index,
    )
    supplied_states.clear()
    supplied_sources.clear()
    supplied_chunks.clear()

    assert runtime_snapshot.source_states == (source_state(source),)
    assert runtime_snapshot.sources == (source,)
    assert runtime_snapshot.chunks == (chunk,)
    with pytest.raises(FrozenInstanceError):
        runtime_snapshot.generation_id = "generation-2"  # type: ignore[misc]
    with pytest.raises(TypeError):
        runtime_snapshot.evidence_index._sources_by_hash["other"] = source


def test_snapshot_rejects_identity_and_coverage_mismatches() -> None:
    source = evidence_source("source-1")
    chunk = evidence_chunk(source, "发布方案正文")
    runtime_context = context()
    index = ProjectEvidenceIndex(
        context=runtime_context,
        sources=(source,),
        chunks=(chunk,),
    )

    with pytest.raises(ValueError, match="project"):
        ProjectRuntimeSnapshot(
            project_id="other-project",
            generation_id="generation-1",
            context=runtime_context,
            source_states=(source_state(source, project_id="other-project"),),
            sources=(source,),
            chunks=(chunk,),
            evidence_index=index,
        )
    with pytest.raises(ValueError, match="source_state"):
        ProjectRuntimeSnapshot(
            project_id="project-1",
            generation_id="generation-1",
            context=runtime_context,
            source_states=(),
            sources=(source,),
            chunks=(chunk,),
            evidence_index=index,
        )
    with pytest.raises(ValueError, match="chunk"):
        ProjectRuntimeSnapshot(
            project_id="project-1",
            generation_id="generation-1",
            context=runtime_context,
            source_states=(source_state(source),),
            sources=(source,),
            chunks=(
                evidence_chunk(
                    source,
                    "发布方案正文",
                    chunk_id="f" * 64,
                ).model_copy(update={"source_version": "v2"}),
            ),
            evidence_index=index,
        )


def test_registry_never_exposes_a_partial_snapshot() -> None:
    registry = ProjectSnapshotRegistry()
    old = snapshot(generation="g1", text="方案B")
    new = snapshot(generation="g2", text="方案C")
    assert registry.swap("project-1", old) is None

    held = registry.get("project-1")
    assert held is old
    assert registry.swap("project-1", new) is old

    assert held.generation_id == "g1"
    assert registry.get("project-1") is new
    assert registry.remove("project-1") is new
    assert registry.get("project-1") is None


def test_registry_holding_an_old_reference_is_safe_during_swap() -> None:
    registry = ProjectSnapshotRegistry()
    old = snapshot(generation="g1", text="方案B")
    new = snapshot(generation="g2", text="方案C")
    registry.swap("project-1", old)
    held_ready = Event()
    release_reader = Event()
    observed: list[str] = []

    def hold_snapshot() -> None:
        held = registry.get("project-1")
        assert held is old
        held_ready.set()
        assert release_reader.wait(timeout=5)
        observed.append(held.generation_id)

    reader = Thread(target=hold_snapshot)
    reader.start()
    assert held_ready.wait(timeout=5)
    registry.swap("project-1", new)
    release_reader.set()
    reader.join(timeout=5)

    assert not reader.is_alive()
    assert observed == ["g1"]
    assert registry.get("project-1") is new


def test_registry_rejects_mismatched_project_before_swap() -> None:
    registry = ProjectSnapshotRegistry()

    with pytest.raises(ValueError, match="project_id"):
        registry.swap("other-project", snapshot())

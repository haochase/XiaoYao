from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from threading import RLock
from types import MappingProxyType
from typing import Iterable, Mapping

from companion_gateway.project.models import (
    DecisionStatus,
    ProjectContextPackage,
)
from companion_gateway.project.sync_models import (
    EvidenceChunk,
    SourceState,
    SyncSourceType,
)


_HEADING_PATTERN = re.compile(r"^[ \t]{0,3}(#{1,6})[ \t]+(.+?)\s*$")
_FENCE_PATTERN = re.compile(r"^[ \t]*(`{3,}|~{3,})")
_LIST_ITEM_PATTERN = re.compile(r"^[ \t]*(?:[-+*]|\d+[.)])[ \t]+")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_SOURCE_WEIGHTS: Mapping[SyncSourceType, int] = MappingProxyType(
    {
        SyncSourceType.DOCUMENT: 40,
        SyncSourceType.MEETING_NOTE: 35,
        SyncSourceType.TASK: 20,
        SyncSourceType.CALENDAR: 10,
    }
)
_MAX_CHUNK_LENGTH = 1_200
_CHUNK_OVERLAP = 150
_CHUNK_STEP = _MAX_CHUNK_LENGTH - _CHUNK_OVERLAP


def _require_non_blank(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must not be blank")
    return value


def _require_sha256(value: str, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 hash")
    return value


def _require_aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def _source_id_hash(source_id: str) -> str:
    return hashlib.sha256(source_id.encode("utf-8")).hexdigest()


def _normalize(value: str) -> str:
    return "".join(value.split()).casefold()


def _bigrams(value: str) -> frozenset[str]:
    return frozenset(value[index : index + 2] for index in range(len(value) - 1))


@dataclass(frozen=True)
class EvidenceSource:
    source_type: SyncSourceType
    source_id: str
    source_id_hash: str
    source_title: str
    source_url: str
    source_version: str
    source_time: datetime
    permission_hash: str
    content_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.source_type, SyncSourceType):
            raise ValueError("source_type must be a SyncSourceType")
        _require_non_blank(self.source_id, "source_id")
        _require_sha256(self.source_id_hash, "source_id_hash")
        if self.source_id_hash != _source_id_hash(self.source_id):
            raise ValueError("source_id_hash must match source_id")
        _require_non_blank(self.source_title, "source_title")
        _require_non_blank(self.source_url, "source_url")
        _require_non_blank(self.source_version, "source_version")
        _require_aware(self.source_time, "source_time")
        _require_sha256(self.permission_hash, "permission_hash")
        _require_sha256(self.content_hash, "content_hash")


@dataclass(frozen=True)
class EvidenceHit:
    chunk: EvidenceChunk
    source: EvidenceSource
    score: int


@dataclass(frozen=True, init=False)
class ProjectEvidenceIndex:
    context: ProjectContextPackage
    sources: tuple[EvidenceSource, ...]
    chunks: tuple[EvidenceChunk, ...]
    _sources_by_hash: Mapping[str, EvidenceSource] = field(repr=False)
    _sources_by_id: Mapping[str, EvidenceSource] = field(repr=False)
    _sources_by_chunk_id: Mapping[str, EvidenceSource] = field(repr=False)
    _normalized_text_by_chunk_id: Mapping[str, str] = field(repr=False)
    _decision_sources_by_topic: Mapping[str, frozenset[tuple[str, str]]] = field(
        repr=False
    )

    def __init__(
        self,
        context: ProjectContextPackage,
        sources: Iterable[EvidenceSource],
        chunks: Iterable[EvidenceChunk],
    ) -> None:
        sources_tuple = tuple(sources)
        chunks_tuple = tuple(chunks)
        self._validate_sources_and_chunks(sources_tuple, chunks_tuple)
        sources_by_hash = {item.source_id_hash: item for item in sources_tuple}
        sources_by_id = {item.source_id: item for item in sources_tuple}
        sources_by_chunk_id = {
            item.chunk_id: sources_by_id[item.source_id] for item in chunks_tuple
        }
        normalized_text_by_chunk_id = {
            item.chunk_id: _normalize(item.text) for item in chunks_tuple
        }
        decision_sources_by_topic = self._decision_sources_by_topic(context)

        object.__setattr__(self, "context", context)
        object.__setattr__(self, "sources", sources_tuple)
        object.__setattr__(self, "chunks", chunks_tuple)
        object.__setattr__(
            self,
            "_sources_by_hash",
            MappingProxyType(sources_by_hash),
        )
        object.__setattr__(self, "_sources_by_id", MappingProxyType(sources_by_id))
        object.__setattr__(
            self,
            "_sources_by_chunk_id",
            MappingProxyType(sources_by_chunk_id),
        )
        object.__setattr__(
            self,
            "_normalized_text_by_chunk_id",
            MappingProxyType(normalized_text_by_chunk_id),
        )
        object.__setattr__(
            self,
            "_decision_sources_by_topic",
            MappingProxyType(decision_sources_by_topic),
        )

    @staticmethod
    def _validate_sources_and_chunks(
        sources: tuple[EvidenceSource, ...],
        chunks: tuple[EvidenceChunk, ...],
    ) -> None:
        source_hashes = [item.source_id_hash for item in sources]
        if len(source_hashes) != len(set(source_hashes)):
            raise ValueError("source_id_hash values must be unique")
        source_ids = [item.source_id for item in sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("source_id values must be unique")
        chunk_ids = [item.chunk_id for item in chunks]
        if len(chunk_ids) != len(set(chunk_ids)):
            raise ValueError("chunk_id values must be unique")
        sources_by_id = {item.source_id: item for item in sources}
        for chunk in chunks:
            source = sources_by_id.get(chunk.source_id)
            if source is None:
                raise ValueError("chunk source is not indexed")
            if chunk.source_version != source.source_version:
                raise ValueError("chunk source_version does not match source")

    @staticmethod
    def _decision_sources_by_topic(
        context: ProjectContextPackage,
    ) -> dict[str, frozenset[tuple[str, str]]]:
        mutable_sources_by_topic: dict[str, set[tuple[str, str]]] = {}
        for decision in context.active_decisions:
            if decision.status is not DecisionStatus.ACTIVE:
                continue
            topic = _normalize(decision.topic)
            if not topic:
                continue
            source_refs = mutable_sources_by_topic.setdefault(topic, set())
            source_refs.update(
                (item.source_type, item.source_id) for item in decision.source_refs
            )
        return {
            topic: frozenset(source_refs)
            for topic, source_refs in mutable_sources_by_topic.items()
        }

    def search(
        self,
        query: str,
        *,
        limit: int = 5,
        allowed_source_hashes: frozenset[str] | None = None,
        source_types: frozenset[SyncSourceType] | None = None,
    ) -> tuple[EvidenceHit, ...]:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 20
        ):
            raise ValueError("limit must be between 1 and 20")
        if not isinstance(query, str):
            raise TypeError("query must be a string")
        normalized_query = _normalize(query)
        if not normalized_query:
            return ()
        query_bigrams = _bigrams(normalized_query)
        hits: list[EvidenceHit] = []
        for chunk in self.chunks:
            source = self._sources_by_chunk_id[chunk.chunk_id]
            if (
                allowed_source_hashes is not None
                and source.source_id_hash not in allowed_source_hashes
            ):
                continue
            if source_types is not None and source.source_type not in source_types:
                continue
            score = self._score(
                normalized_query,
                query_bigrams,
                chunk,
                source,
            )
            if score > _SOURCE_WEIGHTS[source.source_type]:
                hits.append(EvidenceHit(chunk=chunk, source=source, score=score))

        hits.sort(key=lambda item: item.chunk.chunk_id)
        hits.sort(key=lambda item: item.source.source_time, reverse=True)
        hits.sort(key=lambda item: item.score, reverse=True)
        return tuple(hits[:limit])

    def _score(
        self,
        normalized_query: str,
        query_bigrams: frozenset[str],
        chunk: EvidenceChunk,
        source: EvidenceSource,
    ) -> int:
        normalized_text = self._normalized_text_by_chunk_id[chunk.chunk_id]
        score = _SOURCE_WEIGHTS[source.source_type]
        source_identity = (source.source_type.value, source.source_id)
        if any(
            topic in normalized_query and source_identity in source_refs
            for topic, source_refs in self._decision_sources_by_topic.items()
        ):
            score += 1_000
        if normalized_query in normalized_text:
            score += 400
        if len(normalized_text) >= 2 and normalized_text in normalized_query:
            score += 300
        if query_bigrams:
            score += (200 * len(query_bigrams & _bigrams(normalized_text))) // len(
                query_bigrams
            )
        return score


@dataclass(frozen=True, init=False)
class ProjectRuntimeSnapshot:
    project_id: str
    generation_id: str
    context: ProjectContextPackage
    source_states: tuple[SourceState, ...]
    sources: tuple[EvidenceSource, ...]
    chunks: tuple[EvidenceChunk, ...]
    evidence_index: ProjectEvidenceIndex

    def __init__(
        self,
        project_id: str,
        generation_id: str,
        context: ProjectContextPackage,
        source_states: Iterable[SourceState],
        sources: Iterable[EvidenceSource],
        chunks: Iterable[EvidenceChunk],
        evidence_index: ProjectEvidenceIndex,
    ) -> None:
        source_states_tuple = tuple(source_states)
        sources_tuple = tuple(sources)
        chunks_tuple = tuple(chunks)
        self._validate(
            project_id,
            generation_id,
            context,
            source_states_tuple,
            sources_tuple,
            chunks_tuple,
            evidence_index,
        )
        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "generation_id", generation_id)
        object.__setattr__(self, "context", context)
        object.__setattr__(self, "source_states", source_states_tuple)
        object.__setattr__(self, "sources", sources_tuple)
        object.__setattr__(self, "chunks", chunks_tuple)
        object.__setattr__(self, "evidence_index", evidence_index)

    @staticmethod
    def _validate(
        project_id: str,
        generation_id: str,
        context: ProjectContextPackage,
        source_states: tuple[SourceState, ...],
        sources: tuple[EvidenceSource, ...],
        chunks: tuple[EvidenceChunk, ...],
        evidence_index: ProjectEvidenceIndex,
    ) -> None:
        _require_non_blank(project_id, "project_id")
        _require_non_blank(generation_id, "generation_id")
        if context.project_id != project_id:
            raise ValueError("snapshot project_id must match context.project_id")
        if evidence_index.context != context:
            raise ValueError("evidence_index context does not match snapshot")
        if evidence_index.sources != sources:
            raise ValueError("evidence_index sources do not match snapshot")
        if evidence_index.chunks != chunks:
            raise ValueError("evidence_index chunks do not match snapshot")
        ProjectEvidenceIndex._validate_sources_and_chunks(sources, chunks)

        state_keys = [
            (item.source_type, item.source_id_hash) for item in source_states
        ]
        if len(state_keys) != len(set(state_keys)):
            raise ValueError("source_state identities must be unique")
        states_by_key = {
            (item.source_type, item.source_id_hash): item for item in source_states
        }
        for state in source_states:
            if state.project_id != project_id:
                raise ValueError("source_state project_id does not match snapshot")
        for source in sources:
            state = states_by_key.get((source.source_type, source.source_id_hash))
            if state is None:
                raise ValueError("source_state coverage is missing for source")
            if (
                state.source_version != source.source_version
                or state.permission_hash != source.permission_hash
                or state.content_hash != source.content_hash
            ):
                raise ValueError("source_state does not match source")


class ProjectSnapshotRegistry:
    def __init__(self) -> None:
        self._lock = RLock()
        self._snapshots: dict[str, ProjectRuntimeSnapshot] = {}

    def get(self, project_id: str) -> ProjectRuntimeSnapshot | None:
        with self._lock:
            return self._snapshots.get(project_id)

    def swap(
        self,
        project_id: str,
        snapshot: ProjectRuntimeSnapshot,
    ) -> ProjectRuntimeSnapshot | None:
        if snapshot.project_id != project_id:
            raise ValueError("project_id does not match snapshot.project_id")
        with self._lock:
            previous = self._snapshots.get(project_id)
            self._snapshots[project_id] = snapshot
            return previous

    def remove(self, project_id: str) -> ProjectRuntimeSnapshot | None:
        with self._lock:
            return self._snapshots.pop(project_id, None)


@dataclass(frozen=True)
class _MarkdownBlock:
    heading_path: tuple[str, ...]
    section_index: int
    text: str
    start_offset: int
    end_offset: int


def chunk_text(
    source_id: str,
    version: str,
    markdown: str,
) -> tuple[EvidenceChunk, ...]:
    _require_non_blank(source_id, "source_id")
    _require_non_blank(version, "version")
    if not isinstance(markdown, str):
        raise TypeError("markdown must be a string")
    blocks = _markdown_blocks(markdown)
    if not blocks:
        return ()

    chunks: list[EvidenceChunk] = []
    current_path: tuple[str, ...] | None = None
    current_section: int | None = None
    current_text = ""
    current_start = 0
    current_end = 0

    def emit_current() -> None:
        nonlocal current_path, current_section, current_text, current_start, current_end
        if current_path is not None:
            chunks.append(
                _evidence_chunk(
                    source_id,
                    version,
                    len(chunks),
                    current_path,
                    current_text,
                    current_start,
                    current_end,
                )
            )
        current_path = None
        current_section = None
        current_text = ""
        current_start = 0
        current_end = 0

    for block in blocks:
        if len(block.text) > _MAX_CHUNK_LENGTH:
            emit_current()
            for start in range(0, len(block.text), _CHUNK_STEP):
                text = block.text[start : start + _MAX_CHUNK_LENGTH]
                chunks.append(
                    _evidence_chunk(
                        source_id,
                        version,
                        len(chunks),
                        block.heading_path,
                        text,
                        block.start_offset + start,
                        block.start_offset + start + len(text),
                    )
                )
                if start + _MAX_CHUNK_LENGTH >= len(block.text):
                    break
            continue

        if (
            current_path != block.heading_path
            or current_section != block.section_index
        ):
            emit_current()
        if current_path is None:
            current_path = block.heading_path
            current_section = block.section_index
            current_text = block.text
            current_start = block.start_offset
            current_end = block.end_offset
            continue

        if block.start_offset < current_end:
            raise ValueError("markdown blocks overlap")
        separator = markdown[current_end:block.start_offset]
        candidate_text = current_text + separator + block.text
        if len(candidate_text) > _MAX_CHUNK_LENGTH:
            emit_current()
            current_path = block.heading_path
            current_section = block.section_index
            current_text = block.text
            current_start = block.start_offset
            current_end = block.end_offset
            continue
        current_text = candidate_text
        current_end = block.end_offset

    emit_current()
    return tuple(chunks)


def _markdown_blocks(markdown: str) -> tuple[_MarkdownBlock, ...]:
    blocks: list[_MarkdownBlock] = []
    heading_path: list[str] = []
    section_index = 0
    active_fence: tuple[str, int] | None = None
    block_start: int | None = None
    block_end = 0
    block_kind: str | None = None
    pending_list_gap = False
    offset = 0

    def emit_block() -> None:
        nonlocal block_start, block_end, block_kind, pending_list_gap
        if block_start is not None:
            blocks.append(
                _MarkdownBlock(
                    heading_path=tuple(heading_path),
                    section_index=section_index,
                    text=markdown[block_start:block_end],
                    start_offset=block_start,
                    end_offset=block_end,
                )
            )
        block_start = None
        block_end = 0
        block_kind = None
        pending_list_gap = False

    for line in markdown.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        fence = _FENCE_PATTERN.match(content)
        was_inside_fence = active_fence is not None
        opens_fence = False
        is_fence_line = was_inside_fence
        heading = None
        if active_fence is None:
            if fence is not None:
                marker = fence.group(1)
                active_fence = (marker[0], len(marker))
                opens_fence = True
                is_fence_line = True
            else:
                heading = _HEADING_PATTERN.match(content)
        elif (
            fence is not None
            and fence.group(1)[0] == active_fence[0]
            and len(fence.group(1)) >= active_fence[1]
        ):
            active_fence = None
        if heading is not None:
            emit_block()
            section_index += 1
            level = len(heading.group(1))
            title = heading.group(2).strip().rstrip("#").rstrip()
            if title:
                if level <= len(heading_path):
                    del heading_path[level - 1 :]
                heading_path.append(title)
            offset += len(line)
            continue
        if not content.strip():
            if active_fence is None and block_kind != "list":
                emit_block()
            elif block_kind == "list":
                pending_list_gap = True
            offset += len(line)
            continue
        is_list_item = _LIST_ITEM_PATTERN.match(content) is not None
        if (
            block_kind == "list"
            and pending_list_gap
            and not is_list_item
        ):
            emit_block()
        elif (
            block_kind == "fence"
            and not is_fence_line
        ) or (
            opens_fence
            and block_start is not None
        ) or (
            is_list_item
            and not is_fence_line
            and block_start is not None
            and block_kind != "list"
        ):
            emit_block()
        if block_start is None:
            block_start = offset
            if is_fence_line:
                block_kind = "fence"
            elif is_list_item:
                block_kind = "list"
            else:
                block_kind = "paragraph"
        pending_list_gap = False
        block_end = offset + len(content)
        offset += len(line)
    emit_block()

    if offset < len(markdown):
        content = markdown[offset:]
        if content.strip():
            blocks.append(
                _MarkdownBlock(
                    heading_path=tuple(heading_path),
                    section_index=section_index,
                    text=content,
                    start_offset=offset,
                    end_offset=len(markdown),
                )
            )
    return tuple(blocks)


def _evidence_chunk(
    source_id: str,
    version: str,
    ordinal: int,
    heading_path: tuple[str, ...],
    text: str,
    start_offset: int,
    end_offset: int,
) -> EvidenceChunk:
    payload = {
        "end_offset": end_offset,
        "heading_path": heading_path,
        "ordinal": ordinal,
        "source_id": source_id,
        "start_offset": start_offset,
        "text": text,
        "version": version,
    }
    chunk_id = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return EvidenceChunk(
        chunk_id=chunk_id,
        source_id=source_id,
        source_version=version,
        ordinal=ordinal,
        heading_path=heading_path,
        text=text,
        start_offset=start_offset,
        end_offset=end_offset,
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


__all__ = [
    "EvidenceHit",
    "EvidenceSource",
    "ProjectEvidenceIndex",
    "ProjectRuntimeSnapshot",
    "ProjectSnapshotRegistry",
    "chunk_text",
]

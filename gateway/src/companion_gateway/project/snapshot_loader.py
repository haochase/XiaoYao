from __future__ import annotations

import hashlib
import json

from companion_gateway.project.index import (
    EvidenceSource,
    ProjectEvidenceIndex,
    ProjectRuntimeSnapshot,
)
from companion_gateway.project.protection import ContentProtector
from companion_gateway.project.sync_models import EvidenceChunk, SyncSourceType
from companion_gateway.project.sync_repository import StoredProjectGeneration


class SnapshotHydrationError(RuntimeError):
    pass


class ProjectSnapshotHydrator:
    def __init__(self, protector: ContentProtector) -> None:
        if not callable(getattr(protector, "unprotect", None)):
            raise TypeError("protector must support unprotect")
        self._protector = protector

    def snapshot(
        self,
        generation: StoredProjectGeneration,
    ) -> ProjectRuntimeSnapshot:
        sources, chunks = self.evidence(generation)
        return ProjectRuntimeSnapshot(
            project_id=generation.project_id,
            generation_id=generation.generation_id,
            context=generation.context,
            source_states=generation.source_states,
            sources=sources,
            chunks=chunks,
            evidence_index=ProjectEvidenceIndex(
                generation.context,
                sources,
                chunks,
            ),
        )

    def evidence(
        self,
        generation: StoredProjectGeneration,
        keys: set[tuple[SyncSourceType, str]] | None = None,
    ) -> tuple[tuple[EvidenceSource, ...], tuple[EvidenceChunk, ...]]:
        sources: list[EvidenceSource] = []
        source_ids: dict[tuple[SyncSourceType, str], str] = {}
        try:
            for item in generation.protected_sources:
                key = (item.source_type, item.source_id_hash)
                if keys is not None and key not in keys:
                    continue
                source_id = self._unprotect_text(
                    generation.project_id,
                    item.protected_source_id,
                )
                source_title = self._unprotect_text(
                    generation.project_id,
                    item.protected_title,
                )
                source_url = self._unprotect_text(
                    generation.project_id,
                    item.protected_url,
                )
                if (
                    _source_id_hash(source_id) != item.source_id_hash
                    or item.source_version is None
                    or item.source_time is None
                    or item.content_hash is None
                ):
                    raise ValueError("incomplete protected source")
                sources.append(
                    EvidenceSource(
                        source_type=item.source_type,
                        source_id=source_id,
                        source_id_hash=item.source_id_hash,
                        source_title=source_title,
                        source_url=source_url,
                        source_version=item.source_version,
                        source_time=item.source_time,
                        permission_hash=item.permission_hash,
                        content_hash=item.content_hash,
                    )
                )
                source_ids[key] = source_id

            chunks: list[EvidenceChunk] = []
            for item in generation.protected_chunks:
                key = (item.source_type, item.source_id_hash)
                if keys is not None and key not in keys:
                    continue
                source_id = source_ids.get(key)
                if source_id is None:
                    raise ValueError("protected chunk source missing")
                heading_value = json.loads(
                    self._unprotect_text(
                        generation.project_id,
                        item.protected_heading_path,
                    )
                )
                if not isinstance(heading_value, list) or any(
                    not isinstance(value, str) for value in heading_value
                ):
                    raise ValueError("invalid heading path")
                text = self._unprotect_text(
                    generation.project_id,
                    item.protected_text,
                )
                if hashlib.sha256(text.encode("utf-8")).hexdigest() != (
                    item.content_hash
                ):
                    raise ValueError("invalid text hash")
                chunk = EvidenceChunk(
                    chunk_id=item.chunk_id,
                    source_id=source_id,
                    source_version=item.source_version,
                    ordinal=item.ordinal,
                    heading_path=tuple(heading_value),
                    text=text,
                    start_offset=item.start_offset,
                    end_offset=item.end_offset,
                    content_hash=item.content_hash,
                )
                if _chunk_id(chunk) != chunk.chunk_id:
                    raise ValueError("invalid chunk id")
                chunks.append(chunk)
        except (
            json.JSONDecodeError,
            TypeError,
            UnicodeDecodeError,
            ValueError,
        ):
            raise SnapshotHydrationError("protected_content_invalid") from None

        return (
            tuple(
                sorted(
                    sources,
                    key=lambda item: (
                        item.source_type.value,
                        item.source_id_hash,
                    ),
                )
            ),
            tuple(
                sorted(
                    chunks,
                    key=lambda item: (
                        item.source_id,
                        item.source_version,
                        item.ordinal,
                        item.chunk_id,
                    ),
                )
            ),
        )

    def _unprotect_text(self, project_id: str, protected: bytes) -> str:
        plaintext = self._protector.unprotect(project_id, protected)
        if not plaintext:
            raise ValueError("protected content empty")
        return plaintext.decode("utf-8")


def _source_id_hash(source_id: str) -> str:
    return hashlib.sha256(source_id.encode("utf-8")).hexdigest()


def _chunk_id(chunk: EvidenceChunk) -> str:
    payload = {
        "end_offset": chunk.end_offset,
        "heading_path": chunk.heading_path,
        "ordinal": chunk.ordinal,
        "source_id": chunk.source_id,
        "start_offset": chunk.start_offset,
        "text": chunk.text,
        "version": chunk.source_version,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "ProjectSnapshotHydrator",
    "SnapshotHydrationError",
]

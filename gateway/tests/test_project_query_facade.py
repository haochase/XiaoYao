from datetime import UTC, datetime
from types import SimpleNamespace

from companion_gateway.project.models import ProjectContextPackage
from companion_gateway.project.query_facade import RepositoryBackedProjectQueryFacade


NOW = datetime(2026, 9, 10, 8, 0, tzinfo=UTC)


class _Protector:
    protector_version = "test-v1"

    def unprotect(self, project_id: str, protected: bytes) -> bytes:
        raise AssertionError("the cache-key test replaces the real hydrator")


class _Repository:
    def __init__(self, context: ProjectContextPackage) -> None:
        self.context = context

    def observe_wall_clock(  # type: ignore[no-untyped-def]
        self,
        wall_now,
        *,
        rollback_threshold_seconds,
    ):
        assert wall_now == NOW
        assert rollback_threshold_seconds > 0
        return SimpleNamespace(
            trusted_wall_at=NOW,
            last_observed_wall_at=NOW,
            clock_untrusted=False,
            needs_sync=False,
            reason="normal",
        )

    def protection_descriptor(self):  # type: ignore[no-untyped-def]
        return None

    def has_active_generation(self, project_id: str) -> bool:
        assert project_id == "project-1"
        return False

    def load_active_generation(self, project_id: str):  # type: ignore[no-untyped-def]
        assert project_id == "project-1"
        return SimpleNamespace(
            project_id=project_id,
            generation_id="generation-1",
            source_cursor=1,
            context=self.context,
        )


class _Hydrator:
    def __init__(self) -> None:
        self.calls = 0

    def snapshot(self, active):  # type: ignore[no-untyped-def]
        self.calls += 1
        return SimpleNamespace(
            project_id=active.project_id,
            generation_id=active.generation_id,
            context=active.context,
        )


def _context(project_name: str) -> ProjectContextPackage:
    return ProjectContextPackage(
        project_id="project-1",
        project_name=project_name,
        generated_at=NOW,
        permission_scope="project:demo",
    )


def test_facade_refreshes_when_context_changes_in_same_generation_and_cursor() -> None:
    repository = _Repository(_context("迁移前"))
    facade = RepositoryBackedProjectQueryFacade(
        repository,  # type: ignore[arg-type]
        _Protector(),  # type: ignore[arg-type]
        identity_digest=lambda: "a" * 64,
        source_freshness_seconds=300,
        clock=lambda: NOW,
        monotonic=lambda: 1.0,
        awake_time=lambda: 1.0,
    )
    hydrator = _Hydrator()
    facade._hydrator = hydrator  # type: ignore[assignment]

    first = facade.refresh("project-1")
    repository.context = _context("迁移后")
    second = facade.refresh("project-1")

    assert first.context.project_name == "迁移前"
    assert second.context.project_name == "迁移后"
    assert hydrator.calls == 2

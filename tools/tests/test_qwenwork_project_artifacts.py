import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from companion_gateway.project.models import (
    DecisionCard,
    DecisionStatus,
    EvidenceRef,
    ProjectContextPackage,
    SourcedFact,
)
from tools.qwenwork_project_artifacts import (
    PostMeetingArtifact,
    PreMeetingArtifact,
    ReviewOutcome,
    validate_post_meeting_artifact,
    validate_pre_meeting_artifact,
)


NOW = datetime(2026, 9, 11, 9, 0, tzinfo=UTC)
MODULE = Path(__file__).resolve().parents[1] / "qwenwork_project_artifacts.py"


def test_artifact_models_import_from_outside_repository(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import runpy; runpy.run_path({str(MODULE)!r}, run_name='artifact_probe')",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def source(excerpt: str = "行动项：陈可完成设备验收。") -> EvidenceRef:
    return EvidenceRef(
        source_type="document",
        source_id="source-1",
        source_title="需求与决策记录",
        source_url="https://example.invalid/source-1",
        source_time=NOW,
        excerpt=excerpt,
        permission_scope="project:demo",
    )


def fact(text: str, excerpt: str) -> SourcedFact:
    return SourcedFact(text=text, source_refs=(source(excerpt),))


def context() -> ProjectContextPackage:
    decision_ref = source("已确认采用 ESP32-S3 语音终端方案。")
    return ProjectContextPackage(
        project_id="project-1",
        project_name="小千演示项目",
        generated_at=NOW,
        permission_scope="project:demo",
        freshness_seconds=1800,
        source_refs=(decision_ref,),
        active_decisions=(
            DecisionCard(
                decision_id="decision-1",
                project_id="project-1",
                topic="桌面终端",
                decision_text="采用 ESP32-S3 语音终端",
                rationale="低功耗并复用现有音频链路",
                owner="陈可",
                decided_at=NOW,
                source_refs=(decision_ref,),
                status=DecisionStatus.ACTIVE,
                confidence=1.0,
            ),
        ),
        sourced_actions=(fact("陈可完成设备验收", "行动项：陈可完成设备验收。"),),
        sourced_risks=(fact("设备断线后需验证自动恢复", "风险：设备断线后需验证自动恢复。"),),
    )


def test_pre_meeting_artifact_must_be_bound_to_verified_context() -> None:
    memory = context()
    artifact = PreMeetingArtifact(
        project_id=memory.project_id,
        project_name=memory.project_name,
        generated_at=NOW,
        permission_scope=memory.permission_scope,
        last_decisions=memory.active_decisions,
        unfinished_actions=memory.sourced_actions,
        current_risks=memory.sourced_risks,
        verification_items=memory.sourced_risks,
    )

    assert validate_pre_meeting_artifact(artifact, memory) == artifact

    forged = artifact.model_copy(
        update={
            "verification_items": (
                fact("新增未经来源支持的验收项", "不存在于项目记忆的片段"),
            )
        }
    )
    with pytest.raises(ValueError, match="pre_meeting_context_mismatch"):
        validate_pre_meeting_artifact(forged, memory)


def review(status: str) -> dict[str, object]:
    terminal = status in {"accepted", "rejected"}
    return {
        "candidate_id": f"candidate-{status}",
        "decision_id": "decision-1",
        "active_text": "采用 ESP32-S3 语音终端",
        "proposed_text": "改用另一终端方案",
        "reason": "会议发言与当前决策不一致",
        "status": status,
        "created_at": NOW.isoformat(),
        "reviewed_by": "负责人" if terminal else None,
        "reviewed_at": NOW.isoformat() if terminal else None,
        "review_reason": "审核台确认" if terminal else None,
        "evidence": {
            "source_type": "document",
            "source_time": NOW.isoformat(),
            "excerpt": "已确认采用 ESP32-S3 语音终端方案。",
        },
    }


def test_post_meeting_artifact_must_match_review_snapshot_and_status_buckets() -> None:
    memory = context()
    raw = [review("accepted"), review("rejected"), review("proposed")]
    outcomes = [ReviewOutcome.model_validate(item) for item in raw]
    artifact = PostMeetingArtifact(
        project_id=memory.project_id,
        project_name=memory.project_name,
        generated_at=NOW,
        permission_scope=memory.permission_scope,
        accepted=(outcomes[0],),
        rejected=(outcomes[1],),
        pending=(outcomes[2],),
        action_items=memory.sourced_actions,
    )

    assert validate_post_meeting_artifact(artifact, memory, raw) == artifact

    wrong_bucket = artifact.model_copy(
        update={"accepted": (outcomes[1],), "rejected": (outcomes[0],)}
    )
    with pytest.raises(ValueError, match="post_meeting_status_mismatch"):
        PostMeetingArtifact.model_validate(wrong_bucket.model_dump())


def test_post_meeting_artifact_rejects_missing_or_fabricated_review_records() -> None:
    memory = context()
    raw = [review("rejected")]
    rejected = ReviewOutcome.model_validate(raw[0])
    artifact = PostMeetingArtifact(
        project_id=memory.project_id,
        project_name=memory.project_name,
        generated_at=NOW,
        permission_scope=memory.permission_scope,
        rejected=(rejected,),
        action_items=memory.sourced_actions,
    )

    missing = artifact.model_copy(update={"rejected": ()})
    with pytest.raises(ValueError, match="post_meeting_review_mismatch"):
        validate_post_meeting_artifact(missing, memory, raw)

    forged = rejected.model_copy(update={"reviewed_by": "模型推测的负责人"})
    fabricated = artifact.model_copy(update={"rejected": (forged,)})
    with pytest.raises(ValueError, match="post_meeting_review_mismatch"):
        validate_post_meeting_artifact(fabricated, memory, raw)

    duplicated = artifact.model_copy(update={"rejected": (rejected, rejected)})
    with pytest.raises(ValueError, match="post_meeting_review_mismatch"):
        validate_post_meeting_artifact(duplicated, memory, raw)

    conflicting = review("accepted")
    conflicting["candidate_id"] = rejected.candidate_id
    raw_with_duplicate_id = [raw[0], conflicting]
    accepted = ReviewOutcome.model_validate(conflicting)
    cross_bucket = artifact.model_copy(update={"accepted": (accepted,)})
    with pytest.raises(ValueError, match="post_meeting_review_mismatch"):
        validate_post_meeting_artifact(cross_bucket, memory, raw_with_duplicate_id)

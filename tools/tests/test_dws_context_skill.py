from pathlib import Path
from zipfile import ZipFile

import pytest

from tools.package_dws_context_skill import package_skill


def test_skill_uses_xiaoqian_display_brand_and_keeps_compatibility_id() -> None:
    skill = (
        Path(__file__).resolve().parents[2]
        / "skills"
        / "hui-anchor-dws-project-context-v1"
        / "SKILL.md"
    ).read_text(encoding="utf-8")

    assert "name: hui-anchor-dws-project-context-v1" in skill
    assert "name_en: XiaoQian Project Context" in skill
    assert "name_zh: 小千项目上下文" in skill
    assert "# XiaoQian Project Context" in skill
    assert "Hui Anchor Project Context" not in skill
    assert "会锚项目上下文" not in skill


def test_repository_readme_introduces_xiaoqian_product_layer() -> None:
    readme = (Path(__file__).resolve().parents[2] / "README.md").read_text(
        encoding="utf-8"
    )

    assert "## XiaoQian project memory assistant" in readme
    assert "XiaoQian (小千)" in readme
    assert "hui-anchor-dws-project-context-v1" in readme


def test_package_contains_only_public_self_contained_skill(tmp_path: Path) -> None:
    target = tmp_path / "context-skill.zip"
    package_skill(target)
    with ZipFile(target) as archive:
        assert set(archive.namelist()) == {
            "hui-anchor-dws-project-context-v1/SKILL.md",
            "hui-anchor-dws-project-context-v1/.skill-metadata.yaml",
            "hui-anchor-dws-project-context-v1/contract.md",
            "hui-anchor-dws-project-context-v1/pre-meeting-contract.md",
            "hui-anchor-dws-project-context-v1/post-meeting-contract.md",
        }
        skill = archive.read("hui-anchor-dws-project-context-v1/SKILL.md")
        assert b"name: hui-anchor-dws-project-context-v1" in skill
        assert b"contract.md" in skill


def test_package_is_deterministic_and_refuses_overwrite(tmp_path: Path) -> None:
    first, second = tmp_path / "one.zip", tmp_path / "two.zip"
    package_skill(first)
    package_skill(second)
    assert first.read_bytes() == second.read_bytes()
    with pytest.raises(FileExistsError):
        package_skill(first)


def test_skill_routes_pre_and_post_meeting_modes_to_separate_contracts() -> None:
    root = (
        Path(__file__).resolve().parents[2]
        / "skills"
        / "hui-anchor-dws-project-context-v1"
    )
    skill = (root / "SKILL.md").read_text(encoding="utf-8")
    pre = (root / "pre-meeting-contract.md").read_text(encoding="utf-8")
    post = (root / "post-meeting-contract.md").read_text(encoding="utf-8")

    assert "pre-meeting-contract.md" in skill
    assert "post-meeting-contract.md" in skill
    assert "PreMeetingArtifact.model_validate" in pre
    assert "validate_pre_meeting_artifact" in pre
    assert "PostMeetingArtifact.model_validate" in post
    assert "validate_post_meeting_artifact" in post
    assert "不得推测审批状态" in post
    assert "会后模式需同时提供" in skill


def test_package_rejects_c_drive_output() -> None:
    with pytest.raises(ValueError, match="output_requires_e_drive"):
        package_skill(Path("C:/forbidden-context-skill.zip"))


def read_prompt(name: str) -> str:
    return (
        Path(__file__).resolve().parents[2] / "prompts" / name
    ).read_text(encoding="utf-8")


def test_unattended_prompt_uses_direct_core_without_host_payload() -> None:
    prompt = read_prompt("qwenwork-dws-project-sync.md")
    normalized = " ".join(prompt.replace("`", "").split())

    assert "python tools/dws_sync_runtime.py check-core" in normalized
    assert "python tools/dws_sync_runtime.py collect-direct" in normalized
    for forbidden in (
        "dws doc info",
        "dws doc read",
        "payload_chunks",
        "capture-info",
        "complete-host-import",
        "pending-post-tool-use",
    ):
        assert forbidden not in prompt
    expected_flow = (
        "python tools/dws_sync_runtime.py check",
        "python tools/dws_sync_runtime.py check-core",
        "python tools/dws_sync_runtime.py begin",
        "python tools/dws_sync_runtime.py collect-direct",
        "python tools/dws_sync_runtime.py pending",
        "python tools/dws_sync_runtime.py reuse-artifact --unattended",
        "python tools/dws_sync_runtime.py push",
        "python tools/dws_sync_runtime.py end",
    )
    positions = [normalized.index(item) for item in expected_flow]
    assert positions == sorted(positions)
    assert "hui-anchor-dws-project-context-v1" not in prompt
    assert "python tools/dws_sync_runtime.py artifact" not in normalized
    assert "--dry-run" not in prompt


def test_unattended_prompt_aborts_manual_refresh_and_reruns_same_path() -> None:
    prompt = (
        Path(__file__).resolve().parents[2]
        / "prompts"
        / "qwenwork-dws-project-sync.md"
    ).read_text(encoding="utf-8")
    normalized = " ".join(prompt.replace("`", "").split())

    reuse_at = normalized.index("python tools/dws_sync_runtime.py reuse-artifact --unattended")
    manual_at = normalized.index("manual_refresh_required", reuse_at)
    abort_at = normalized.index("python tools/dws_sync_runtime.py abort", manual_at)
    return_at = normalized.index("return", abort_at)
    push_at = normalized.index("python tools/dws_sync_runtime.py push", return_at)
    assert reuse_at < manual_at < abort_at < return_at < push_at

    rerun_at = normalized.index("返回 rerun 时")
    rerun_flow = (
        "collect-direct",
        "pending",
        "reuse-artifact --unattended",
        "push",
        "end",
    )
    rerun_positions = []
    cursor = rerun_at
    for item in rerun_flow:
        cursor = normalized.index(item, cursor)
        rerun_positions.append(cursor)
    assert rerun_positions == sorted(rerun_positions)


def test_manual_prompt_generates_new_artifact_for_changed_source() -> None:
    prompt = read_prompt("qwenwork-dws-project-manual-refresh.md")
    normalized = " ".join(prompt.replace("`", "").split())

    assert "hui-anchor-dws-project-context-v1" in prompt
    assert "QwenProjectContextArtifact.model_validate" in prompt
    assert "python tools/dws_sync_runtime.py artifact" in prompt
    assert "python tools/dws_sync_runtime.py push --dry-run" in prompt
    assert "decision_change_requires_review" in prompt
    assert "reuse-artifact --unattended" not in prompt
    assert "dws doc read" not in prompt
    for fixed_value in (
        "generated_at = collected_at",
        "freshness_seconds = 1800",
        "open_actions = []",
        "current_risks = []",
        "next_meeting = null",
        "completed_retrieval_request_ids = []",
    ):
        assert fixed_value in prompt
    assert "单个 active 来源" in normalized
    assert "连续非标题正文 excerpt" in normalized
    assert "最长 150 字" in normalized
    assert "不得拼接" in normalized


def test_t4_prompt_keeps_three_outputs_in_one_qwenwork_session() -> None:
    prompt = read_prompt("qwenwork-t4-project-artifacts.md")
    normalized = " ".join(prompt.replace("`", "").split())

    assert "只在当前对话" in prompt
    assert "不得新建、分叉或并行创建其他对话" in prompt
    assert normalized.index("项目记忆") < normalized.index("会前要点")
    assert normalized.index("会前要点") < normalized.index("会后审核报告")
    assert "PreMeetingArtifact.model_validate" in prompt
    assert "validate_pre_meeting_artifact" in prompt
    assert "PostMeetingArtifact.model_validate" in prompt
    assert "validate_post_meeting_artifact" in prompt
    assert "Session ID" in prompt
    assert "不可获得就报告 session_id_unavailable" in normalized
    assert "不得输出凭据" in prompt
    assert "只继承其门禁、生命周期步骤与失败处理" in prompt
    assert "end=completed 后继续" in normalized
    assert "用户可见的最终输出延迟到三个阶段全部结束" in prompt
    assert r"E:\haochase\xiaoqian\小千项目文档\submission\t4" in prompt
    assert "必须位于 Git 工作树之外" in prompt

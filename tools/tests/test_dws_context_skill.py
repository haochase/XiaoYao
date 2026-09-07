from pathlib import Path
import re
from zipfile import ZipFile

import pytest

from tools.package_dws_context_skill import package_skill


def test_package_contains_only_public_self_contained_skill(tmp_path: Path) -> None:
    target = tmp_path / "context-skill.zip"
    package_skill(target)
    with ZipFile(target) as archive:
        assert set(archive.namelist()) == {
            "hui-anchor-dws-project-context-v1/SKILL.md",
            "hui-anchor-dws-project-context-v1/.skill-metadata.yaml",
            "hui-anchor-dws-project-context-v1/contract.md",
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


def test_package_rejects_c_drive_output() -> None:
    with pytest.raises(ValueError, match="output_requires_e_drive"):
        package_skill(Path("C:/forbidden-context-skill.zip"))


def test_unattended_prompt_uses_two_phase_host_envelopes_without_model_artifact() -> None:
    prompt = (
        Path(__file__).resolve().parents[2]
        / "prompts"
        / "qwenwork-dws-project-sync.md"
    ).read_text(encoding="utf-8")
    normalized = " ".join(prompt.replace("`", "").split())
    bash_blocks = re.findall(r"```bash\s*(.*?)\s*```", prompt, re.DOTALL)

    assert len(bash_blocks) == 2
    assert 'operation:"doc_info"' in bash_blocks[0]
    assert 'operation:"doc_read"' in bash_blocks[1]
    for block in bash_blocks:
        assert "encoding:\"base64-json\"" in block
        assert "byte_count:($raw|utf8bytelength)" in block
        assert "payload:($raw|@base64)" in block
    expected_flow = (
        "python tools/dws_sync_runtime.py begin",
        "dws doc info",
        "python tools/dws_sync_runtime.py capture-info",
        "dws doc read",
        "python tools/dws_sync_runtime.py complete-host-import",
        "python tools/dws_sync_runtime.py pending",
        "python tools/dws_sync_runtime.py reuse-artifact --unattended",
        "python tools/dws_sync_runtime.py push",
        "python tools/dws_sync_runtime.py end",
    )
    positions = [normalized.index(item) for item in expected_flow]
    assert positions == sorted(positions)
    assert "host-import-construction" not in prompt
    assert "outer =" not in prompt
    assert '"results"' not in prompt
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
        "dws doc info",
        "capture-info",
        "dws doc read",
        "complete-host-import",
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


def test_unattended_prompt_authorizes_only_cli_managed_host_capture_files() -> None:
    prompt = (
        Path(__file__).resolve().parents[2]
        / "prompts"
        / "qwenwork-dws-project-sync.md"
    ).read_text(encoding="utf-8")
    normalized = " ".join(prompt.replace("`", "").split())

    capture_command_at = normalized.index(
        "python tools/dws_sync_runtime.py capture-info"
    )
    capture_root_at = normalized.index(".private/dws-host-captures")
    assert capture_command_at < capture_root_at
    assert "只有受信 CLI 可以管理" in normalized[capture_root_at - 40 :]
    assert "digest 命名的 capture 文件及相关锁" in normalized
    for operation in ("读取", "复制", "修改", "另存", "删除"):
        assert f"Agent 不得直接{operation}" in normalized
    assert "complete 事务失败会先恢复 capture" in normalized
    assert "finally 使用同一 token abort" in normalized
    assert "abort 随后按设计清理 capture" in normalized
    assert "不承诺 abort 后保留诊断文件" in normalized
    assert "保留 capture 以供独立人工刷新诊断" not in normalized

import re
from pathlib import Path


WORKTREE_ROOT = Path(__file__).resolve().parents[2]


def test_minicpm_o_example_contains_only_a_placeholder_for_the_api_key() -> None:
    example = (WORKTREE_ROOT / "gateway" / ".env.example").read_text(
        encoding="utf-8"
    )

    token_lines = re.findall(
        r"^COMPANION_MINICPM_O_AUTH_TOKEN=(.*)$",
        example,
        flags=re.MULTILINE,
    )
    assert token_lines == [""]
    assert "tp-" not in example
    assert (
        "COMPANION_MINICPM_O_COMPATIBLE_BASE_URL=http://127.0.0.1:9000/v1"
        in example
    )


def test_local_env_files_are_ignored() -> None:
    gitignore = (WORKTREE_ROOT / ".gitignore").read_text(encoding="utf-8")

    assert ".env" in gitignore
    assert "!.env.example" in gitignore


def test_feishu_chat_example_is_disabled_and_contains_no_credentials() -> None:
    example = (WORKTREE_ROOT / "gateway" / ".env.example").read_text(
        encoding="utf-8"
    )
    pyproject = (WORKTREE_ROOT / "gateway" / "pyproject.toml").read_text(
        encoding="utf-8"
    )

    assert "COMPANION_FEISHU_CHAT_ENABLED=false" in example
    assert "COMPANION_FEISHU_CHAT_HISTORY_TURNS=6" in example
    assert "lark-channel-sdk>=1.2,<2" in pyproject
    assert "cli_test_app" not in example
    assert "secret_test_value" not in example


def test_ascend_runbook_is_public_safe_and_covers_d1_tools() -> None:
    runbook = (WORKTREE_ROOT / "deploy" / "ascend" / "README.md").read_text(
        encoding="utf-8"
    )

    assert "ascend_runtime_probe.py" in runbook
    assert "minicpm_o_endpoint_check.py" in runbook
    for forbidden in ("192.168.", "COM7", "COMPANION_MINICPM_O_AUTH_TOKEN="):
        assert forbidden not in runbook


def test_ascend_runbook_endpoint_checks_prepare_pythonpath_before_tool() -> None:
    runbook = (WORKTREE_ROOT / "deploy" / "ascend" / "README.md").read_text(
        encoding="utf-8"
    )

    endpoint_check_blocks = [
        (language, block)
        for language, block in re.findall(
            r"```([A-Za-z0-9_-]+)\r?\n(.*?)```", runbook, flags=re.DOTALL
        )
        if "minicpm_o_endpoint_check.py" in block
    ]

    local_blocks = [
        block for language, block in endpoint_check_blocks if language == "powershell"
    ]
    hidelab_blocks = [
        block for language, block in endpoint_check_blocks if language == "bash"
    ]

    assert len(endpoint_check_blocks) == 3
    assert len(local_blocks) == 2
    assert len(hidelab_blocks) == 1
    assert len(local_blocks) + len(hidelab_blocks) == len(endpoint_check_blocks)

    for block in local_blocks:
        pythonpath_setup = r"$env:PYTHONPATH='gateway\src'"
        tool_call = r"tools\minicpm_o_endpoint_check.py"
        assert pythonpath_setup in block
        assert block.index(pythonpath_setup) < block.index(tool_call)

    for block in hidelab_blocks:
        pythonpath_setup = "export PYTHONPATH='gateway/src'"
        tool_call = "tools/minicpm_o_endpoint_check.py"
        assert pythonpath_setup in block
        assert block.index(pythonpath_setup) < block.index(tool_call)


def test_ascend_runbook_separates_local_and_hidelab_probe_contracts() -> None:
    runbook = (WORKTREE_ROOT / "deploy" / "ascend" / "README.md").read_text(
        encoding="utf-8"
    )

    local_heading = "## 1. Local D1 workspace probe"
    hidelab_heading = "## 2. HiDevLab required-NPU probe"
    local_section = runbook.partition(local_heading)[2].partition(hidelab_heading)[0]
    hidelab_section = runbook.partition(hidelab_heading)[2].partition(
        "## 3. Local MiniCPM-o Mock"
    )[0]

    assert local_section
    workspace_creation = (
        "New-Item -ItemType Directory -Force $probeWorkspace | Out-Null"
    )
    assert workspace_creation in local_section
    assert local_section.index(workspace_creation) < local_section.index(
        r"tools\ascend_runtime_probe.py"
    )
    assert "--workspace $probeWorkspace" in local_section
    assert "--require-npu" not in local_section
    assert "Exit code `0` means `status=ready`" in local_section

    assert hidelab_section
    assert "```bash" in hidelab_section
    assert r"C:\Users\chase\miniconda3\python.exe" not in hidelab_section
    workspace_creation = 'mkdir -p "$probe_workspace"'
    assert workspace_creation in hidelab_section
    assert hidelab_section.index(workspace_creation) < hidelab_section.index(
        "python3 tools/ascend_runtime_probe.py"
    )
    assert "python3 tools/ascend_runtime_probe.py" in hidelab_section
    assert '--workspace "$probe_workspace"' in hidelab_section
    assert "--require-npu --json" in hidelab_section
    assert "Exit code `1` means `status=blocked`" in hidelab_section

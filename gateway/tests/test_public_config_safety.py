import re
from pathlib import Path


WORKTREE_ROOT = Path(__file__).resolve().parents[2]


def test_mimo_example_contains_only_a_placeholder_for_the_api_key() -> None:
    example = (WORKTREE_ROOT / "gateway" / ".env.example").read_text(
        encoding="utf-8"
    )

    assert "COMPANION_MIMO_API_KEY=" in example
    assert "tp-" not in example
    assert (
        "COMPANION_MIMO_OPENAI_BASE_URL=https://token-plan-cn.xiaomimimo.com/v1"
        in example
    )
    assert (
        "COMPANION_MIMO_ANTHROPIC_BASE_URL="
        "https://token-plan-cn.xiaomimimo.com/anthropic"
    ) in example


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


def test_meeting_assistant_example_is_disabled_and_has_no_target_identifier() -> None:
    example = (WORKTREE_ROOT / "gateway" / ".env.example").read_text(
        encoding="utf-8"
    )

    assert "COMPANION_MEETING_ASSISTANT_ENABLED=false" in example
    assert "COMPANION_MEETING_TARGET_DEVICE_ID=" in example
    assert "COMPANION_MEETING_POLL_INTERVAL_SECONDS=30" in example
    assert "COMPANION_MEETING_LOOKAHEAD_HOURS=24" in example
    assert "COMPANION_MEETING_REMINDER_LEAD_SECONDS=600" in example
    assert "COMPANION_MEETING_CONTEXT_TTL_SECONDS=300" in example
    assert "desk-device" not in example


def test_meeting_assistant_runbook_covers_public_safe_operator_contract() -> None:
    readme = (WORKTREE_ROOT / "gateway" / "README.md").read_text(
        encoding="utf-8"
    )

    for required in (
        "calendar:calendar:readonly",
        "/open-apis/calendar/v4/calendars/primarys?user_id_type=open_id",
        "tools\\feishu_calendar_check.py --hours 24",
        "tools\\feishu_calendar_check.py --hours 24 --dry-run",
        "mode: \"dry_run\"",
        "device: \"offline\"",
        "COMPANION_MEETING_ASSISTANT_ENABLED",
        "COMPANION_MEETING_TARGET_DEVICE_ID",
        "COMPANION_MEETING_POLL_INTERVAL_SECONDS",
        "COMPANION_MEETING_LOOKAHEAD_HOURS",
        "COMPANION_MEETING_REMINDER_LEAD_SECONDS",
        "COMPANION_MEETING_CONTEXT_TTL_SECONDS",
        "exactly 10 minutes",
        "deterministic fallback",
        "device-offline fallback",
        "raw event IDs",
        "Real Feishu owner authentication, token rotation, and calendar read:",
        "`PASS` on 2026-09-02 through two read-only dry-runs",
        "Real eligible meeting reminder candidate: `PASS` on 2026-09-02",
        "Real MiMo briefing leaf: `PASS` on 2026-09-02",
        "Real Feishu bot message leaf: `PASS` on 2026-09-02",
        "recipient confirmed receipt in Feishu",
        "Real ESP32 meeting TTS: `PASS` on 2026-09-02",
        "Real state-driven RGB meeting cue: `PASS` on 2026-09-02",
        "Standalone custom RGB color or blink command: `NOT_IMPLEMENTED`",
        "Real grounded `next_meeting` voice query: `PASS` on 2026-09-02",
        "Real calendar-to-MiMo-to-Feishu offline fallback: provider-level `PASS`",
        "Recipient confirmation for that end-to-end fallback message: `PASS`",
        "Real bounded gateway scheduler: `PASS` on 2026-09-02",
        "Persistent local gateway activation: `PASS` on 2026-09-02",
        "Real Feishu private text channel: `PASS` on 2026-09-02",
        "Real medication reminder: `PASS` on 2026-09-02",
        "Full competition rehearsal: `PASS` on 2026-09-02",
    ):
        assert required in readme

    assert "COMPANION_MEETING_INDICATOR" not in readme


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

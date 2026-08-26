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


def test_ascend_runbook_is_public_safe_and_covers_d1_tools() -> None:
    runbook = (WORKTREE_ROOT / "deploy" / "ascend" / "README.md").read_text(
        encoding="utf-8"
    )

    assert "ascend_runtime_probe.py" in runbook
    assert "minicpm_o_endpoint_check.py" in runbook
    for forbidden in ("192.168.", "COM7", "COMPANION_MINICPM_O_AUTH_TOKEN="):
        assert forbidden not in runbook

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

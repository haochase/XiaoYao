from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[2]


def test_gateway_dependencies_pin_the_tested_fastapi_line_and_windows_tzdata() -> None:
    with (ROOT / "gateway" / "pyproject.toml").open("rb") as file:
        dependencies = tomllib.load(file)["project"]["dependencies"]

    assert "fastapi>=0.115,<0.137" in dependencies
    assert 'tzdata>=2026.3; sys_platform == "win32"' in dependencies

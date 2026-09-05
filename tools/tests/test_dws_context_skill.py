from pathlib import Path
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

from __future__ import annotations

import json

from tools.camera_probe import probe_source


def test_camera_probe_reports_required_board_camera_and_psram_capabilities(tmp_path) -> None:
    source = tmp_path / "xiaozhi"
    board = source / "main" / "boards" / "waveshare" / "esp32-s3-audio-board"
    board.mkdir(parents=True)
    (board / "camera_board_config.h").write_text(
        "#define CAMERA_SENSOR OV2640\nCONFIG_SPIRAM=y\n",
        encoding="utf-8",
    )
    (source / "sdkconfig.defaults").write_text(
        "CONFIG_CAMERA_OV2640=y\nCONFIG_SPIRAM=y\n",
        encoding="utf-8",
    )

    result = probe_source(source)

    assert result == {
        "status": "ok",
        "source_present": True,
        "board_camera_definition": True,
        "ov2640_configured": True,
        "psram_configured": True,
    }
    assert json.dumps(result) == json.dumps(result, ensure_ascii=False)


def test_camera_probe_is_safe_and_deterministic_when_source_is_missing(tmp_path) -> None:
    result = probe_source(tmp_path / "missing-source")

    assert result == {
        "status": "error",
        "source_present": False,
        "board_camera_definition": False,
        "ov2640_configured": False,
        "psram_configured": False,
    }
    assert str(tmp_path) not in json.dumps(result)

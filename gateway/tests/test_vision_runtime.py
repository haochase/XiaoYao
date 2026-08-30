from __future__ import annotations

from companion_gateway.device.camera import CameraFrameRegistry
from companion_gateway.vision.runtime import (
    VisionRuntime,
    VisionRuntimeError,
    VisionTurnCoordinator,
)
from companion_gateway.vision.service import VisionObservationService


class RecordingVisionRuntime:
    def __init__(self, response: str = "前方是一张测试图片") -> None:
        self.response = response
        self.calls: list[tuple[bytes, str]] = []

    def describe(self, *, image: bytes, prompt: str) -> str:
        self.calls.append((image, prompt))
        return self.response


def build_coordinator(tmp_path, runtime: VisionRuntime):
    repository = __import__(
        "companion_gateway.storage.sqlite",
        fromlist=["SQLiteTaskRepository"],
    ).SQLiteTaskRepository(tmp_path / "vision-runtime.db")
    repository.initialize()
    return VisionTurnCoordinator(
        vision_service=VisionObservationService(
            repository=repository,
            storage_path=tmp_path / "images",
            enabled=True,
            max_upload_bytes=2_097_152,
        ),
        frame_registry=CameraFrameRegistry(),
        runtime=runtime,
        subject_id="voice-user",
    )


def test_vision_coordinator_describes_one_uploaded_frame_and_consumes_it(tmp_path) -> None:
    runtime = RecordingVisionRuntime()
    coordinator = build_coordinator(tmp_path, runtime)
    payload = b"\xff\xd8\xffjpeg\xff\xd9"
    coordinator._frame_registry.put(
        session_id="ses-1",
        turn_id="turn-1",
        payload=payload,
    )

    result = coordinator.describe(
        session_id="ses-1",
        turn_id="turn-1",
        prompt="看看前面是什么",
    )

    assert result == "前方是一张测试图片"
    assert runtime.calls == [(payload, "看看前面是什么")]
    assert coordinator._frame_registry.get(
        session_id="ses-1",
        turn_id="turn-1",
    ) is None


def test_vision_coordinator_requires_uploaded_frame_and_nonempty_model_text(tmp_path) -> None:
    coordinator = build_coordinator(tmp_path, RecordingVisionRuntime(response=""))

    try:
        coordinator.describe(
            session_id="ses-missing",
            turn_id="turn-missing",
            prompt="识别",
        )
    except VisionRuntimeError as exc:
        assert "frame" in str(exc)
    else:
        raise AssertionError("missing frame should fail")


def test_vision_coordinator_speaks_result_once(tmp_path) -> None:
    runtime = RecordingVisionRuntime()
    coordinator = build_coordinator(tmp_path, runtime)
    coordinator._frame_registry.put(
        session_id="ses-2",
        turn_id="turn-2",
        payload=b"\xff\xd8\xffjpeg\xff\xd9",
    )
    spoken: list[str] = []

    result = coordinator.describe_and_speak(
        session_id="ses-2",
        turn_id="turn-2",
        prompt="识别",
        speak=spoken.append,
    )

    assert result == "前方是一张测试图片"
    assert spoken == ["前方是一张测试图片"]

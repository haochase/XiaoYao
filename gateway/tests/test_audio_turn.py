import companion_gateway.audio.turn as audio_turn
from companion_gateway.audio.turn import (
    AutoTurnEndpointDetector,
    ConsecutiveSilenceGate,
)


def test_consecutive_silence_gate_resets_after_audible_frame() -> None:
    gate = ConsecutiveSilenceGate(
        rms_threshold=35.0,
        consecutive_silent_frames=2,
    )

    assert gate.observe(rms_amplitude=10.0) is False
    assert gate.observe(rms_amplitude=80.0) is False
    assert gate.silent_frames == 0
    assert gate.observe(rms_amplitude=10.0) is False
    assert gate.observe(rms_amplitude=10.0) is True


def test_endpoint_detector_finishes_after_configured_silent_frames() -> None:
    detector = AutoTurnEndpointDetector(
        rms_threshold=35.0,
        consecutive_silent_frames=3,
    )

    assert detector.observe(rms_amplitude=120.0) is False
    assert detector.observe(rms_amplitude=20.0) is False
    assert detector.observe(rms_amplitude=10.0) is False
    assert detector.observe(rms_amplitude=18.0) is True


def test_endpoint_detector_resets_silence_after_audible_frame() -> None:
    detector = AutoTurnEndpointDetector(
        rms_threshold=35.0,
        consecutive_silent_frames=3,
    )

    assert detector.observe(rms_amplitude=20.0) is False
    assert detector.observe(rms_amplitude=15.0) is False
    assert detector.observe(rms_amplitude=60.0) is False
    assert detector.silent_frames == 0
    assert detector.observe(rms_amplitude=20.0) is False
    assert detector.observe(rms_amplitude=20.0) is False
    assert detector.observe(rms_amplitude=20.0) is True


def test_endpoint_detector_does_not_finish_before_any_audible_speech() -> None:
    detector = AutoTurnEndpointDetector(
        rms_threshold=35.0,
        consecutive_silent_frames=3,
    )

    assert detector.observe(rms_amplitude=20.0) is False
    assert detector.observe(rms_amplitude=15.0) is False
    assert detector.observe(rms_amplitude=18.0) is False
    assert detector.has_heard_speech is False


def test_endpoint_detector_requires_consecutive_speech_frames_before_finishing() -> None:
    detector = AutoTurnEndpointDetector(
        rms_threshold=35.0,
        consecutive_silent_frames=2,
        minimum_speech_frames=2,
    )

    assert detector.observe(rms_amplitude=120.0) is False
    assert detector.observe(rms_amplitude=10.0) is False
    assert detector.observe(rms_amplitude=10.0) is False
    assert detector.observe(rms_amplitude=120.0) is False
    assert detector.observe(rms_amplitude=10.0) is False
    assert detector.observe(rms_amplitude=10.0) is False

    assert detector.has_heard_speech is False
    assert detector.observe(rms_amplitude=120.0) is False
    assert detector.observe(rms_amplitude=120.0) is False
    assert detector.has_heard_speech is True
    assert detector.observe(rms_amplitude=10.0) is False
    assert detector.observe(rms_amplitude=10.0) is True


def test_vad_endpoint_requires_a_complete_minimum_length_speech_segment() -> None:
    detector = audio_turn.VadTurnEndpointDetector(minimum_speech_frames=2)

    assert detector.stop() is False
    detector.start()
    detector.observe_audio()
    detector.start()
    assert detector.stop() is False

    detector.start()
    detector.observe_audio()
    detector.observe_audio()

    assert detector.has_confirmed_speech is True
    assert detector.stop() is True
    assert detector.stop() is False


def test_vad_endpoint_tracks_rms_metrics_for_the_current_segment() -> None:
    detector = audio_turn.VadTurnEndpointDetector(minimum_speech_frames=2)

    detector.start()
    detector.observe_audio(rms_amplitude=12.0)
    detector.observe_audio(rms_amplitude=48.0)

    assert detector.audio_frames == 2
    assert detector.rms_min == 12.0
    assert detector.rms_max == 48.0
    assert detector.average_rms == 30.0
    assert detector.stop() is True

    detector.start()
    assert detector.audio_frames == 0
    assert detector.rms_min is None
    assert detector.rms_max is None
    assert detector.average_rms is None

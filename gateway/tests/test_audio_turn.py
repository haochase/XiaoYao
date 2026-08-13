from companion_gateway.audio.turn import AutoTurnEndpointDetector


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

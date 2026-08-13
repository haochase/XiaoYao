from dataclasses import dataclass


@dataclass
class AutoTurnEndpointDetector:
    rms_threshold: float
    consecutive_silent_frames: int
    minimum_speech_frames: int = 1
    silent_frames: int = 0
    speech_frames: int = 0
    has_heard_speech: bool = False

    def __post_init__(self) -> None:
        if self.rms_threshold < 0:
            raise ValueError("rms_threshold must not be negative")
        if self.consecutive_silent_frames < 1:
            raise ValueError("consecutive_silent_frames must be positive")
        if self.minimum_speech_frames < 1:
            raise ValueError("minimum_speech_frames must be positive")

    def observe(self, *, rms_amplitude: float) -> bool:
        if rms_amplitude > self.rms_threshold:
            self.has_heard_speech = True
            self.speech_frames += 1
            self.silent_frames = 0
            return False
        self.silent_frames += 1
        return (
            self.has_heard_speech
            and self.speech_frames >= self.minimum_speech_frames
            and self.silent_frames >= self.consecutive_silent_frames
        )

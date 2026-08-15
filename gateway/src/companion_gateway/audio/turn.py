from dataclasses import dataclass


@dataclass
class ConsecutiveSilenceGate:
    rms_threshold: float
    consecutive_silent_frames: int
    silent_frames: int = 0

    def __post_init__(self) -> None:
        if self.rms_threshold < 0:
            raise ValueError("rms_threshold must not be negative")
        if self.consecutive_silent_frames < 1:
            raise ValueError("consecutive_silent_frames must be positive")

    def observe(self, *, rms_amplitude: float) -> bool:
        if rms_amplitude > self.rms_threshold:
            self.silent_frames = 0
            return False
        self.silent_frames += 1
        return self.silent_frames >= self.consecutive_silent_frames


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
            self.speech_frames += 1
            if self.speech_frames >= self.minimum_speech_frames:
                self.has_heard_speech = True
            self.silent_frames = 0
            return False
        if not self.has_heard_speech:
            self.speech_frames = 0
            self.silent_frames = 0
            return False
        self.silent_frames += 1
        return (
            self.has_heard_speech
            and self.silent_frames >= self.consecutive_silent_frames
        )

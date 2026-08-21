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


@dataclass
class VadTurnEndpointDetector:
    minimum_speech_frames: int = 1
    speech_frames: int = 0
    speech_active: bool = False
    audio_frames: int = 0
    rms_min: float | None = None
    rms_max: float | None = None
    rms_sum: float = 0.0

    def __post_init__(self) -> None:
        if self.minimum_speech_frames < 1:
            raise ValueError("minimum_speech_frames must be positive")

    @property
    def has_confirmed_speech(self) -> bool:
        return self.speech_frames >= self.minimum_speech_frames

    def start(self) -> None:
        if self.speech_active:
            return
        self.speech_active = True
        self.speech_frames = 0
        self.audio_frames = 0
        self.rms_min = None
        self.rms_max = None
        self.rms_sum = 0.0

    @property
    def average_rms(self) -> float | None:
        if not self.audio_frames:
            return None
        return self.rms_sum / self.audio_frames

    def meets_rms_threshold(self, minimum_average_rms: float | None) -> bool:
        if minimum_average_rms is None:
            return True
        if minimum_average_rms < 0:
            raise ValueError("minimum_average_rms must not be negative")
        return (
            self.average_rms is not None
            and self.average_rms >= minimum_average_rms
        )

    def observe_audio(self, *, rms_amplitude: float | None = None) -> None:
        if self.speech_active:
            self.speech_frames += 1
            if rms_amplitude is None:
                return
            if rms_amplitude < 0:
                raise ValueError("rms_amplitude must not be negative")
            self.audio_frames += 1
            self.rms_min = (
                rms_amplitude
                if self.rms_min is None
                else min(self.rms_min, rms_amplitude)
            )
            self.rms_max = (
                rms_amplitude
                if self.rms_max is None
                else max(self.rms_max, rms_amplitude)
            )
            self.rms_sum += rms_amplitude

    def stop(self) -> bool:
        if not self.speech_active:
            return False
        confirmed = self.has_confirmed_speech
        self.speech_active = False
        self.speech_frames = 0
        return confirmed

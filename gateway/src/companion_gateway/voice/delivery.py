from __future__ import annotations

from typing import Protocol

from companion_gateway.device.transport import MAX_TTS_FRAMES
from companion_gateway.voice.service import VoiceTurn, VoiceTurnService


class TtsTransport(Protocol):
    def send_tts_stream(
        self,
        session_id: str,
        opus_frames: tuple[bytes, ...],
    ) -> None: ...


class DeviceVoiceDeliveryService:
    def __init__(
        self,
        *,
        voice_turn_service: VoiceTurnService,
        device_transport: TtsTransport,
    ) -> None:
        self._voice_turn_service = voice_turn_service
        self._device_transport = device_transport

    def process_and_send(self, *, session_id: str) -> VoiceTurn | None:
        turn = self._voice_turn_service.process_next_input()
        if turn is None:
            return None
        for offset in range(0, len(turn.device_opus_frames), MAX_TTS_FRAMES):
            self._device_transport.send_tts_stream(
                session_id,
                turn.device_opus_frames[offset : offset + MAX_TTS_FRAMES],
            )
        return turn

    def accept_and_send(
        self,
        *,
        session_id: str,
        opus_frame: bytes,
    ) -> VoiceTurn | None:
        self._voice_turn_service.accept_opus_uplink(opus_frame)
        return self.process_and_send(session_id=session_id)

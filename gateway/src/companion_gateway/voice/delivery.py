from __future__ import annotations

import asyncio
from typing import Protocol

from companion_gateway.device.transport import MAX_TTS_FRAMES
from companion_gateway.domain.executor import TaskExecutor
from companion_gateway.medication.service import MedicationReminderService
from companion_gateway.memory.service import MemoryService
from companion_gateway.service import TaskService
from companion_gateway.voice.service import (
    AgentContextProvider,
    VoiceTurn,
    VoiceTurnService,
)


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

    def process_and_send(
        self,
        *,
        session_id: str,
        target_device_id: str | None = None,
    ) -> VoiceTurn | None:
        turn = self._voice_turn_service.process_pending_turn(
            session_id=session_id,
            target_device_id=target_device_id,
        )
        if turn is None:
            # Retain the direct AudioBridge test/integration entry point while
            # production WebSocket frames use the session-specific buffer.
            turn = self._voice_turn_service.process_pending_turn(
                target_device_id=target_device_id,
            )
        if turn is None:
            return None
        self._send_tts_frames(
            session_id=session_id,
            opus_frames=turn.device_opus_frames,
        )
        return turn

    def synthesize_and_send(self, *, session_id: str, text: str) -> None:
        self._send_tts_frames(
            session_id=session_id,
            opus_frames=self._voice_turn_service.synthesize_text(text),
        )

    def can_synthesize(self, text: str) -> bool:
        """Run a bounded synthesis canary without sending audio to a device."""
        try:
            return bool(self._voice_turn_service.synthesize_text(text))
        except Exception:
            return False

    async def process_and_send_async(
        self,
        *,
        session_id: str,
        target_device_id: str | None = None,
    ) -> VoiceTurn | None:
        return await asyncio.to_thread(
            self.process_and_send,
            session_id=session_id,
            target_device_id=target_device_id,
        )

    def accept_and_send(
        self,
        *,
        session_id: str,
        opus_frame: bytes,
    ) -> Pcm16Mono:
        return self._voice_turn_service.accept_opus_uplink(
            opus_frame,
            session_id=session_id,
        )

    def clear_pending_input(self, *, session_id: str | None = None) -> None:
        self._voice_turn_service.clear_pending_input(session_id=session_id)

    def set_task_executor(self, task_executor: TaskExecutor) -> None:
        self._voice_turn_service.set_task_executor(task_executor)

    def set_medication_service(
        self,
        medication_service: MedicationReminderService,
    ) -> None:
        self._voice_turn_service.set_medication_service(medication_service)

    def set_memory_service(self, memory_service: MemoryService) -> None:
        self._voice_turn_service.set_memory_service(memory_service)

    def set_task_service(self, task_service: TaskService) -> None:
        self._voice_turn_service.set_task_service(task_service)

    def set_agent_context_provider(
        self,
        provider: AgentContextProvider | None,
    ) -> None:
        self._voice_turn_service.set_agent_context_provider(provider)

    def _send_tts_frames(
        self,
        *,
        session_id: str,
        opus_frames: tuple[bytes, ...],
    ) -> None:
        for offset in range(0, len(opus_frames), MAX_TTS_FRAMES):
            self._device_transport.send_tts_stream(
                session_id,
                opus_frames[offset : offset + MAX_TTS_FRAMES],
            )

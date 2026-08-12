from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import RLock
from uuid import uuid4

from companion_gateway.audio.bridge import (
    AudioBridge,
    AudioMetrics,
    AudioQueueFull,
    Pcm16Mono,
)
from companion_gateway.domain.executor import TaskExecutor
from companion_gateway.domain.models import TaskRecord
from companion_gateway.medication.service import MedicationReminderService
from companion_gateway.memory.service import MemoryService
from companion_gateway.voice.runtime import ModelRuntime, VoiceAction


@dataclass(frozen=True)
class VoiceTurn:
    input_metrics: AudioMetrics
    response_text: str
    response_metrics: AudioMetrics
    device_opus_frames: tuple[bytes, ...]
    task: TaskRecord | None = None
    memory_proposal_ids: tuple[str, ...] = ()

    @property
    def device_opus_frame(self) -> bytes:
        if len(self.device_opus_frames) != 1:
            raise ValueError("voice turn contains more than one Opus frame")
        return self.device_opus_frames[0]


class VoiceTurnService:
    def __init__(
        self,
        *,
        audio_bridge: AudioBridge,
        model_runtime: ModelRuntime,
        task_executor: TaskExecutor | None = None,
        medication_service: MedicationReminderService | None = None,
        memory_service: MemoryService | None = None,
        actor_id: str = "voice-user",
    ) -> None:
        self._audio_bridge = audio_bridge
        self._model_runtime = model_runtime
        self._task_executor = task_executor
        self._medication_service = medication_service
        self._memory_service = memory_service
        self._actor_id = actor_id
        self._session_uplink: dict[str, deque[Pcm16Mono]] = {}
        self._session_uplink_lock = RLock()

    def process_next_input(
        self,
        *,
        session_id: str | None = None,
        target_device_id: str | None = None,
    ) -> VoiceTurn | None:
        input_pcm = (
            self._audio_bridge.pop_uplink()
            if session_id is None
            else self._pop_session_uplink(session_id)
        )
        if input_pcm is None:
            return None

        return self._process_input(input_pcm, target_device_id=target_device_id)

    def process_pending_turn(
        self,
        *,
        session_id: str | None = None,
        target_device_id: str | None = None,
    ) -> VoiceTurn | None:
        input_frames = (
            self._audio_bridge.drain_uplink()
            if session_id is None
            else self._drain_session_uplink(session_id)
        )
        if not input_frames:
            return None

        first = input_frames[0]
        input_pcm = Pcm16Mono(
            sample_rate=first.sample_rate,
            payload=b"".join(frame.payload for frame in input_frames),
        )
        return self._process_input(input_pcm, target_device_id=target_device_id)

    def clear_pending_input(self, *, session_id: str | None = None) -> None:
        if session_id is None:
            self._audio_bridge.drain_uplink()
            return
        self._drain_session_uplink(session_id)

    def set_task_executor(self, task_executor: TaskExecutor) -> None:
        self._task_executor = task_executor

    def set_medication_service(
        self,
        medication_service: MedicationReminderService,
    ) -> None:
        self._medication_service = medication_service

    def set_memory_service(self, memory_service: MemoryService) -> None:
        self._memory_service = memory_service

    def synthesize_text(self, text: str) -> tuple[bytes, ...]:
        synthesize = getattr(self._model_runtime, "synthesize", None)
        if synthesize is None:
            raise RuntimeError("voice runtime does not support text synthesis")
        response_pcm = synthesize(text)
        return tuple(
            self._audio_bridge.encode_downlink(frame)
            for frame in self._split_response_pcm(response_pcm)
        )

    def _process_input(
        self,
        input_pcm: Pcm16Mono,
        *,
        target_device_id: str | None,
    ) -> VoiceTurn:

        self._prepare_model_context(target_device_id=target_device_id)
        response = self._model_runtime.respond(input_pcm)
        memory_proposal_ids: tuple[str, ...] = ()
        if self._memory_service is not None and response.memory_proposals:
            try:
                proposals = self._memory_service.propose(
                    subject_id=self._actor_id,
                    candidates=response.memory_proposals,
                    source=f"trc_voice_memory_{uuid4().hex}",
                )
                memory_proposal_ids = tuple(
                    proposal.proposal_id for proposal in proposals
                )
            except Exception:
                logging.getLogger(__name__).warning(
                    "memory_proposal_persist_failed",
                )
        self._apply_medication_action(
            response.action,
            target_device_id=target_device_id,
        )
        task = None
        if response.task is not None:
            if self._task_executor is None:
                raise RuntimeError("task executor is required for model task output")
            task, _ = self._task_executor.create_and_schedule(
                response.task,
                trace_id=f"trc_voice_{uuid4().hex}",
            )
        device_opus_frames = tuple(
            self._audio_bridge.encode_downlink(frame)
            for frame in self._split_response_pcm(response.pcm)
        )
        return VoiceTurn(
            input_metrics=input_pcm.metrics,
            response_text=response.text,
            response_metrics=response.pcm.metrics,
            device_opus_frames=device_opus_frames,
            task=task,
            memory_proposal_ids=memory_proposal_ids,
        )

    def _apply_medication_action(
        self,
        action: VoiceAction | None,
        *,
        target_device_id: str | None,
    ) -> None:
        if action is None or self._medication_service is None or target_device_id is None:
            return
        try:
            if action.type == "acknowledge_medication_occurrence":
                if action.occurrence_id is None:
                    return
                self._medication_service.acknowledge_occurrence(
                    action.occurrence_id,
                    actor_id=self._actor_id,
                    target_device_id=target_device_id,
                    occurred_at=datetime.now(UTC),
                    trace_id=f"trc_voice_medication_{uuid4().hex}",
                )
                return
            if action.plan_id is None:
                return
            self._medication_service.disable_plan(
                action.plan_id,
                actor_id=self._actor_id,
                target_device_id=target_device_id,
                occurred_at=datetime.now(UTC),
            )
        except (KeyError, PermissionError, ValueError):
            # A stale or cross-device model action must not mutate state or
            # interrupt the spoken response for the current turn.
            return

    def _prepare_model_context(self, *, target_device_id: str | None) -> None:
        set_memory_context = getattr(self._model_runtime, "set_memory_context", None)
        if set_memory_context is not None:
            memory_context = ""
            if self._memory_service is not None:
                try:
                    memory_context = self._memory_service.build_context(
                        subject_id=self._actor_id,
                    )
                except Exception:
                    logging.getLogger(__name__).warning(
                        "memory_context_build_failed",
                    )
            set_memory_context(memory_context)
        if self._medication_service is None or target_device_id is None:
            return
        set_context = getattr(self._model_runtime, "set_action_context", None)
        if set_context is None:
            return
        context = self._medication_service.voice_context(
            actor_id=self._actor_id,
            target_device_id=target_device_id,
        )
        set_context(
            occurrence_ids=context["occurrence_ids"],
            plan_ids=context["plan_ids"],
        )

    def accept_opus_uplink(
        self,
        payload: bytes,
        *,
        session_id: str | None = None,
    ) -> None:
        if session_id is None:
            self._audio_bridge.decode_uplink(payload)
            return

        model_pcm = self._audio_bridge.decode_uplink_frame(payload)
        with self._session_uplink_lock:
            pending = self._session_uplink.setdefault(session_id, deque())
            if len(pending) >= self._audio_bridge.queue_capacity:
                raise AudioQueueFull("decoded audio queue is full")
            pending.append(model_pcm)

    def _pop_session_uplink(self, session_id: str) -> Pcm16Mono | None:
        with self._session_uplink_lock:
            pending = self._session_uplink.get(session_id)
            if not pending:
                return None
            frame = pending.popleft()
            if not pending:
                del self._session_uplink[session_id]
            return frame

    def _drain_session_uplink(self, session_id: str) -> tuple[Pcm16Mono, ...]:
        with self._session_uplink_lock:
            pending = self._session_uplink.pop(session_id, None)
            return tuple(pending) if pending is not None else ()

    @staticmethod
    def _split_response_pcm(pcm: Pcm16Mono) -> tuple[Pcm16Mono, ...]:
        frame_samples = pcm.sample_rate * 60 // 1_000
        if frame_samples < 1:
            raise ValueError("model PCM sample rate cannot represent a 60 ms frame")
        frame_bytes = frame_samples * 2
        frames: list[Pcm16Mono] = []
        for offset in range(0, len(pcm.payload), frame_bytes):
            payload = pcm.payload[offset : offset + frame_bytes]
            if len(payload) < frame_bytes:
                payload += b"\x00" * (frame_bytes - len(payload))
            frames.append(Pcm16Mono(sample_rate=pcm.sample_rate, payload=payload))
        return tuple(frames)

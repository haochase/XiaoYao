import asyncio
import json
import logging
import time
from email import policy
from email.parser import BytesParser
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError
from starlette.websockets import WebSocketDisconnect

from companion_gateway.agent.compiler import (
    AGENT_COMPILER_SYSTEM_PROMPT,
    AgentSpecCompileError,
    MimoAgentSpecCompiler,
)
from companion_gateway.agent.registry import AgentRegistry
from companion_gateway.agent.router import AgentCommandRouter
from companion_gateway.agent.runtime import AgentRuntime
from companion_gateway.agent.scheduler import DynamicAgentScheduler
from companion_gateway.agent.service import (
    AgentToolNotAllowed,
    AgentToolRequest,
    AgentToolService,
    AgentToolTimeout,
)
from companion_gateway.agent.templates.summary import (
    DailySummaryBuilder,
    DailySummaryFacts,
)
from companion_gateway.agent.tools.weather import WeatherTool
from companion_gateway.audio.bridge import AudioFrameRejected, AudioQueueFull
from companion_gateway.audio.turn import (
    AutoTurnEndpointDetector,
    ConsecutiveSilenceGate,
    VadTurnEndpointDetector,
)
from companion_gateway.channels.feishu_chat import create_feishu_chat_listener
from companion_gateway.chat.mimo import MimoTextChatRuntime
from companion_gateway.chat.service import TextChatRuntime
from companion_gateway.context.service import ConversationContextService
from companion_gateway.device.events import (
    BoundedDeviceEventSink,
    DeviceBackpressure,
    DiscardingDeviceEventSink,
)
from companion_gateway.device.camera import (
    CameraCaptureMetadata,
    CameraFrameRegistry,
    CameraUploadError,
    CameraUploadState,
    build_vision_capability_message,
    derive_vision_explain_url,
)
from companion_gateway.device.idle import ConversationIdleController
from companion_gateway.device.models import (
    AbortControl,
    DeviceHello,
    ListenControl,
    VadControl,
    server_hello,
)
from companion_gateway.device.session import (
    DeviceAuthenticator,
    DeviceSession,
    DeviceSessionRegistry,
    InvalidDevicePhase,
    redact_device_id,
)
from companion_gateway.device.transport import (
    DeviceNotConnected,
    DeviceOutboundBackpressure,
    DeviceTransport,
    OutboundControl,
    OutboundTask,
)
from companion_gateway.domain.executor import TaskDeliveryAttempt, TaskExecutor
from companion_gateway.domain.agents import AgentDraft, AgentSpec
from companion_gateway.domain.medication import MedicationPlanCreate
from companion_gateway.domain.memory import MemoryCandidate, MemoryCategory, utc_now
from companion_gateway.domain.models import (
    ContentText,
    Identifier,
    TaskCreate,
    TaskRecord,
)
from companion_gateway.domain.scheduler import TaskScheduler
from companion_gateway.domain.tasks import InvalidTaskTransition, TaskEventType
from companion_gateway.medication.scheduler import MedicationScheduler
from companion_gateway.medication.service import (
    MedicationNotifier,
    MedicationReminderService,
    UnconfiguredMedicationNotifier,
)
from companion_gateway.memory.service import (
    MemoryConsentRequired,
    MemoryFeatureDisabled,
    MemoryNotFound,
    MemoryOwnershipError,
    MemoryQuotaExceeded,
    MemoryService,
)
from companion_gateway.memory.scheduler import MemoryScheduler
from companion_gateway.notifications.feishu import FeishuNotifier
from companion_gateway.service import TaskService
from companion_gateway.settings import Settings, load_environment_file
from companion_gateway.storage.sqlite import SQLiteTaskRepository
from companion_gateway.vision.scheduler import VisionScheduler
from companion_gateway.vision.runtime import VisionRuntime, VisionRuntimeError, VisionTurnCoordinator
from companion_gateway.vision.service import (
    VisionConsentRequired,
    VisionDuplicateTurn,
    VisionFeatureDisabled,
    VisionObservationService,
    VisionQuotaExceeded,
    VisionTooLarge,
    VisionUnsupportedType,
)
from companion_gateway.voice.delivery import DeviceVoiceDeliveryService
from companion_gateway.voice.fixture import (
    create_fixture_voice_delivery,
    create_voice_delivery,
)
from companion_gateway.voice.minicpm_o import (
    MinicpmOHttpRuntime,
    MinicpmORealtimeRuntime,
    ModelRuntimeError,
)
from companion_gateway.voice.mimo_v25 import MimoV25Runtime


Reason = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=256),
]


async def _sleep_between_tts_frames(seconds: float) -> None:
    await asyncio.sleep(seconds)


class EventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Reason


class MedicationOwnershipRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor_id: str
    target_device_id: str


class MemoryConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_id: Identifier | None = None
    subject_id: Identifier
    category: MemoryCategory
    value: ContentText
    confirmed: bool


class MemoryProposalConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_id: Identifier


class AgentDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_text: ContentText
    source_message_id: Identifier


class VisionDescribeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    turn_id: Identifier
    prompt: ContentText


class UnsupportedDeviceControl(ValueError):
    pass


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
if not logger.handlers:
    logger.addHandler(logging.StreamHandler())
logger.propagate = False
LOCAL_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
_LOCAL_ADMIN_HOSTS = frozenset({"127.0.0.1", "::1", "testclient"})
_PUBLIC_AGENT_CONFIG_FIELDS = frozenset(
    {"message", "city", "level", "scenario", "input_mode"}
)


def _request_trace_id(request: Request) -> str:
    return request.state.trace_id


def _parse_camera_explain_multipart(
    content_type: str,
    body: bytes,
) -> tuple[str, bytes]:
    if not content_type.lower().startswith("multipart/form-data"):
        raise ValueError("camera explain requires multipart form data")
    message = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: "
        + content_type.encode("ascii", errors="strict")
        + b"\r\nMIME-Version: 1.0\r\n\r\n"
        + body
    )
    question: str | None = None
    image: bytes | None = None
    for part in message.iter_parts():
        disposition = part.get("Content-Disposition", "")
        name = part.get_param("name", header="content-disposition")
        if name == "question" and "filename" not in disposition:
            value = part.get_content()
            if isinstance(value, str):
                question = value.strip()
        elif name == "file":
            image = part.get_payload(decode=True)
    if not question:
        raise ValueError("camera explain question is required")
    if not image:
        raise ValueError("camera explain JPEG file is required")
    return question, image


def _require_local_dynamic_admin(request: Request) -> None:
    client_host = request.client.host if request.client is not None else None
    if client_host not in _LOCAL_ADMIN_HOSTS:
        raise HTTPException(
            status_code=403,
            detail="dynamic Agent management is local-only",
        )


def _public_agent(agent: AgentSpec) -> dict[str, object]:
    safe_config = {
        key: value
        for key, value in agent.config.items()
        if key in _PUBLIC_AGENT_CONFIG_FIELDS
    }
    return {
        "agent_id": agent.agent_id,
        "name": agent.name,
        "kind": agent.kind.value,
        "enabled": agent.enabled,
        "trigger": jsonable_encoder(agent.trigger),
        "channels": [channel.value for channel in agent.channels],
        "memory_policy": agent.memory_policy.value,
        "max_turns": agent.max_turns,
        "config": jsonable_encoder(safe_config),
    }


def _public_agent_draft(draft: AgentDraft) -> dict[str, object]:
    return {
        "draft_id": draft.draft_id,
        "created_at": jsonable_encoder(draft.created_at),
        "agent": _public_agent(draft.spec),
    }


def create_app(
    settings: Settings,
    *,
    device_event_sink: (
        BoundedDeviceEventSink | DiscardingDeviceEventSink | None
    ) = None,
    device_transport: DeviceTransport | None = None,
    voice_delivery_service: DeviceVoiceDeliveryService | None = None,
    medication_notifier: MedicationNotifier | None = None,
    feishu_chat_listener=None,
    feishu_chat_listener_factory: Callable[[AgentCommandRouter | None], object]
    | None = None,
    agent_text_runtime: TextChatRuntime | None = None,
    vision_runtime: VisionRuntime | None = None,
    memory_clock: Callable[[], datetime] = utc_now,
    vision_clock: Callable[[], datetime] = utc_now,
    agent_clock: Callable[[], datetime] = utc_now,
) -> FastAPI:
    repository = SQLiteTaskRepository(settings.database_path)
    repository.initialize()
    recent_context_service = ConversationContextService(
        repository,
        subject_id=settings.subject_id,
        enabled=settings.recent_context_enabled,
        retention_days=settings.recent_context_retention_days,
        max_messages=settings.recent_context_max_messages,
        max_bytes=settings.recent_context_max_bytes,
    )
    service = TaskService(repository)
    task_executor = TaskExecutor(service)
    device_sessions = DeviceSessionRegistry()
    camera_frames = CameraFrameRegistry()
    device_authenticator = DeviceAuthenticator(settings.device_token_hashes)
    transport = device_transport or DeviceTransport()
    sink = device_event_sink or DiscardingDeviceEventSink()

    def send_feishu_fallback(task: TaskRecord) -> TaskDeliveryAttempt:
        if medication_notifier is None:
            return TaskDeliveryAttempt.failed("feishu_fallback_unavailable")
        try:
            result = medication_notifier.send_text(
                text=task.payload.text,
                trace_id=f"task-fallback-{task.task_id}",
            )
        except Exception:
            logger.exception(
                "task_delivery_fallback_failed device=%s task=%s",
                redact_device_id(task.target_device_id),
                task.task_id,
            )
            return TaskDeliveryAttempt.failed("feishu_fallback_failed")
        if not result.success:
            return TaskDeliveryAttempt.failed("feishu_fallback_failed")
        logger.info(
            "task_delivery_fallback_succeeded device=%s task=%s",
            redact_device_id(task.target_device_id),
            task.task_id,
        )
        return TaskDeliveryAttempt.succeeded()

    def deliver_task(task: TaskRecord) -> TaskDeliveryAttempt:
        session = device_sessions.get(task.target_device_id)
        if session is None:
            logger.info(
                "task_delivery_failed device=%s task=%s reason=device_offline",
                redact_device_id(task.target_device_id),
                task.task_id,
            )
            return send_feishu_fallback(task)
        try:
            if voice_delivery_service is None:
                logger.info(
                    "task_delivery_failed device=%s task=%s "
                    "reason=voice_synthesis_unavailable",
                    redact_device_id(task.target_device_id),
                    task.task_id,
                )
                return send_feishu_fallback(task)
            voice_delivery_service.synthesize_notification_and_send(
                session_id=session.session_id,
                text=task.payload.text,
            )
            logger.info(
                "task_voice_enqueued device=%s task=%s",
                redact_device_id(task.target_device_id),
                task.task_id,
            )
        except DeviceNotConnected:
            logger.info(
                "task_delivery_failed device=%s task=%s reason=device_offline",
                redact_device_id(task.target_device_id),
                task.task_id,
            )
            return send_feishu_fallback(task)
        except DeviceOutboundBackpressure:
            logger.info(
                "task_delivery_failed device=%s task=%s "
                "reason=outbound_backpressure",
                redact_device_id(task.target_device_id),
                task.task_id,
            )
            return TaskDeliveryAttempt.failed("outbound_backpressure")
        except (ModelRuntimeError, RuntimeError, ValueError):
            logger.info(
                "task_delivery_failed device=%s task=%s reason=voice_synthesis_failed",
                redact_device_id(task.target_device_id),
                task.task_id,
            )
            return send_feishu_fallback(task)
        logger.info(
            "task_delivery_succeeded device=%s task=%s",
            redact_device_id(task.target_device_id),
            task.task_id,
        )
        return TaskDeliveryAttempt.succeeded()

    task_scheduler = TaskScheduler(
        executor=task_executor,
        deliver=deliver_task,
        interval_seconds=settings.task_scheduler_interval_seconds,
    )
    if medication_notifier is None and settings.feishu_configured:
        if (
            settings.feishu_app_id is None
            or settings.feishu_app_secret is None
            or settings.feishu_receiver_open_id is None
        ):
            raise ValueError("Feishu settings are incomplete")
        medication_notifier = FeishuNotifier(
            app_id=settings.feishu_app_id,
            app_secret=settings.feishu_app_secret,
            receiver_open_id=settings.feishu_receiver_open_id,
            base_url=settings.feishu_base_url,
            timeout_seconds=settings.feishu_timeout_seconds,
            max_retries=settings.feishu_max_retries,
            retry_backoff_seconds=settings.feishu_retry_backoff_seconds,
        )
    medication_service = MedicationReminderService(
        repository=repository,
        task_service=service,
        task_executor=task_executor,
        notifier=medication_notifier or UnconfiguredMedicationNotifier(),
    )
    memory_service = MemoryService(
        repository,
        enabled=settings.memory_enabled,
        retention_days=settings.memory_retention_days,
        quota_bytes=settings.memory_quota_bytes,
        proposal_ttl_seconds=settings.memory_proposal_ttl_seconds,
        clock=memory_clock,
    )
    memory_scheduler = MemoryScheduler(
        service=memory_service,
        interval_seconds=settings.memory_cleanup_interval_seconds,
        clock=memory_clock,
    )
    vision_service = VisionObservationService(
        repository=repository,
        storage_path=settings.vision_storage_path,
        enabled=settings.vision_enabled,
        max_upload_bytes=settings.vision_max_upload_bytes,
        retention_days=settings.vision_retention_days,
        quota_bytes=settings.vision_quota_bytes,
        clock=vision_clock,
    )
    vision_scheduler = VisionScheduler(
        service=vision_service,
        interval_seconds=settings.vision_cleanup_interval_seconds,
        clock=vision_clock,
    )
    vision_coordinator = (
        VisionTurnCoordinator(
            vision_service=vision_service,
            frame_registry=camera_frames,
            runtime=vision_runtime,
            subject_id=settings.subject_id,
        )
        if vision_runtime is not None and settings.vision_enabled
        else None
    )
    agent_tool_service = AgentToolService(
        task_service=service,
        task_executor=task_executor,
        clock=agent_clock,
    )
    medication_scheduler = MedicationScheduler(
        service=medication_service,
        interval_seconds=settings.task_scheduler_interval_seconds,
    )
    agent_registry = None
    agent_runtime = None
    agent_command_router = None
    dynamic_agent_scheduler = None
    if settings.dynamic_agents_enabled:
        owner_id = settings.dynamic_agent_owner_id
        target_device_id = settings.dynamic_agent_target_device_id
        if owner_id is None or target_device_id is None:
            raise ValueError("dynamic Agent settings are incomplete")
        if agent_text_runtime is None:
            if settings.mimo_api_key is None:
                raise ValueError("dynamic Agents require a MiMo API key")
            agent_text_runtime = MimoTextChatRuntime(
                openai_base_url=settings.mimo_openai_base_url,
                api_key=settings.mimo_api_key,
                model=settings.mimo_model,
                timeout_seconds=settings.mimo_timeout_seconds,
                max_retries=settings.mimo_max_retries,
                retry_backoff_seconds=settings.mimo_retry_backoff_seconds,
                system_prompt=AGENT_COMPILER_SYSTEM_PROMPT,
            )
        agent_registry = AgentRegistry(
            repository=repository,
            compiler=MimoAgentSpecCompiler(runtime=agent_text_runtime),
        )

        def send_dynamic_feishu(text: str) -> bool:
            if medication_notifier is None:
                return False
            result = medication_notifier.send_text(
                text=text,
                trace_id=f"dynamic-agent-{uuid4().hex}",
            )
            return result.success

        def speak_dynamic_esp32(text: str) -> bool:
            if voice_delivery_service is None:
                return False
            session = device_sessions.get(target_device_id)
            if session is None:
                return False
            try:
                voice_delivery_service.synthesize_and_send(
                    session_id=session.session_id,
                    text=text,
                )
            except (ModelRuntimeError, RuntimeError, ValueError):
                return False
            return True

        def daily_summary_facts(
            summary_owner_id: str,
            now: datetime,
        ) -> DailySummaryFacts:
            entries: list[str] = []
            for agent in repository.list_agents(owner_id=summary_owner_id):
                for execution in repository.list_executions(
                    agent.agent_id,
                    owner_id=summary_owner_id,
                ):
                    if execution.started_at.date() == now.date():
                        entries.append(
                            f"{agent.name}: {execution.status.value}"
                        )
            return DailySummaryFacts(agent_executions=tuple(entries[-20:]))

        agent_runtime = AgentRuntime(
            repository=repository,
            weather_tool=WeatherTool(),
            send_feishu=send_dynamic_feishu,
            speak_esp32=speak_dynamic_esp32,
            summary_builder=DailySummaryBuilder(
                facts_provider=daily_summary_facts,
            ),
        )
        agent_command_router = AgentCommandRouter(
            registry=agent_registry,
            runtime=agent_runtime,
            clock=agent_clock,
            reminder_tool=agent_tool_service,
            target_device_id=target_device_id,
        )
        dynamic_agent_scheduler = DynamicAgentScheduler(
            repository=repository,
            runtime=agent_runtime,
            owner_id=owner_id,
            interval_seconds=(
                settings.dynamic_agent_scheduler_interval_seconds
            ),
            clock=agent_clock,
        )
    if feishu_chat_listener is None and feishu_chat_listener_factory is not None:
        feishu_chat_listener = feishu_chat_listener_factory(agent_command_router)
    if feishu_chat_listener is not None:
        set_recent_context = getattr(feishu_chat_listener, "set_recent_context", None)
        if callable(set_recent_context):
            set_recent_context(recent_context_service)
    app = FastAPI(title="XiaoYao Voice Gateway", version="0.1.0")
    app.state.repository = repository
    app.state.recent_context_service = recent_context_service
    app.state.service = service
    app.state.task_executor = task_executor
    app.state.device_sessions = device_sessions
    app.state.camera_frames = camera_frames
    app.state.device_event_sink = sink
    app.state.device_transport = transport
    app.state.voice_delivery_service = voice_delivery_service
    app.state.task_scheduler = task_scheduler
    app.state.medication_service = medication_service
    app.state.medication_scheduler = medication_scheduler
    app.state.medication_notifier = medication_notifier
    app.state.feishu_chat_listener = feishu_chat_listener
    app.state.memory_service = memory_service
    app.state.memory_scheduler = memory_scheduler
    app.state.vision_service = vision_service
    app.state.vision_scheduler = vision_scheduler
    app.state.vision_coordinator = vision_coordinator
    app.state.agent_tool_service = agent_tool_service
    app.state.agent_registry = agent_registry
    app.state.agent_runtime = agent_runtime
    app.state.agent_command_router = agent_command_router
    app.state.dynamic_agent_scheduler = dynamic_agent_scheduler
    if voice_delivery_service is not None:
        voice_delivery_service.set_task_executor(task_executor)
        voice_delivery_service.set_task_service(service)
        voice_delivery_service.set_medication_service(medication_service)
        voice_delivery_service.set_memory_service(memory_service)
        set_recent_context_provider = getattr(
            voice_delivery_service,
            "set_recent_context_provider",
            None,
        )
        if callable(set_recent_context_provider):
            set_recent_context_provider(
                lambda _actor_id, _target_device_id: (
                    recent_context_service.build_context()
                )
            )
        if (
            agent_command_router is not None
            and settings.dynamic_agent_owner_id is not None
            and settings.dynamic_agent_target_device_id is not None
        ):
            dynamic_owner_id = settings.dynamic_agent_owner_id
            dynamic_target_device_id = settings.dynamic_agent_target_device_id

            def active_voice_agent_context(
                _actor_id: str,
                target_device_id: str | None,
            ) -> str:
                if target_device_id != dynamic_target_device_id:
                    return ""
                return agent_command_router.active_context_for_owner(
                    owner_id=dynamic_owner_id,
                )

            voice_delivery_service.set_agent_context_provider(
                active_voice_agent_context
            )
    if settings.task_scheduler_enabled:
        app.add_event_handler("startup", task_scheduler.start)
        app.add_event_handler("shutdown", task_scheduler.stop)
        app.add_event_handler("startup", medication_scheduler.start)
        app.add_event_handler("shutdown", medication_scheduler.stop)
    if settings.memory_enabled:
        app.add_event_handler("startup", memory_scheduler.start)
        app.add_event_handler("shutdown", memory_scheduler.stop)
    if settings.vision_enabled:
        app.add_event_handler("startup", vision_scheduler.start)
        app.add_event_handler("shutdown", vision_scheduler.stop)
    if dynamic_agent_scheduler is not None:
        app.add_event_handler("startup", dynamic_agent_scheduler.start)
        app.add_event_handler("shutdown", dynamic_agent_scheduler.stop)
    if feishu_chat_listener is not None:
        app.add_event_handler("startup", feishu_chat_listener.start)
        app.add_event_handler("shutdown", feishu_chat_listener.stop)

    @app.middleware("http")
    async def attach_trace_id(request: Request, call_next):
        supplied = request.headers.get("X-Trace-Id", "").strip()
        request.state.trace_id = supplied or f"trc_{uuid4().hex}"
        response = await call_next(request)
        response.headers["X-Trace-Id"] = request.state.trace_id
        return response

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    def ready() -> JSONResponse:
        database_ready = repository.check()
        content = {
            "status": "ready" if database_ready else "not_ready",
            "checks": {"database": "ok" if database_ready else "error"},
        }
        return JSONResponse(
            status_code=200 if database_ready else 503,
            content=content,
        )

    @app.get(
        "/v1/demo/status",
        dependencies=[Depends(_require_local_dynamic_admin)],
    )
    def demo_status() -> dict[str, bool | int]:
        target_device_id = settings.dynamic_agent_target_device_id
        recent_context_count = 0
        if recent_context_service.enabled:
            try:
                recent_context_count = len(
                    repository.list_recent_messages(
                        subject_id=recent_context_service.subject_id,
                        now=utc_now(),
                        limit=settings.recent_context_max_messages,
                        max_bytes=settings.recent_context_max_bytes,
                    )
                )
            except Exception:
                recent_context_count = 0
        recent_image_count = 0
        if settings.vision_enabled:
            try:
                recent_image_count = len(
                    vision_service.list(subject_id=settings.subject_id)
                )
            except Exception:
                recent_image_count = 0
        mimo_canary_ok = False
        if agent_text_runtime is not None:
            try:
                mimo_canary_ok = bool(
                    agent_text_runtime.respond(
                        'Return exactly this JSON object: {"status":"ok"}',
                        history=(),
                    ).strip()
                )
            except Exception:
                mimo_canary_ok = False
        tts_canary_ok = bool(
            voice_delivery_service is not None
            and voice_delivery_service.can_synthesize("小瑶在线检查")
        )
        return {
            "mimo_configured": settings.mimo_api_key is not None,
            "mimo_canary_ok": mimo_canary_ok,
            "tts_configured": voice_delivery_service is not None,
            "tts_canary_ok": tts_canary_ok,
            "feishu_available": bool(
                feishu_chat_listener is not None
                and getattr(feishu_chat_listener, "is_available", False)
            ),
            "device_online": bool(
                target_device_id is not None
                and device_sessions.get(target_device_id) is not None
            ),
            "dynamic_agents_enabled": agent_registry is not None,
            "dynamic_agent_count": (
                len(agent_registry.list(owner_id=settings.dynamic_agent_owner_id))
                if agent_registry is not None
                and settings.dynamic_agent_owner_id is not None
                else 0
            ),
            "recent_context_enabled": recent_context_service.enabled,
            "recent_context_count": recent_context_count,
            "camera_enabled": settings.camera_enabled,
            "camera_capable_device_online": (
                settings.camera_enabled
                and device_sessions.has_camera_capable_session()
            ),
            "recent_image_count": recent_image_count,
            "conversation_idle_timeout_seconds": (
                settings.device_conversation_idle_timeout_seconds
            ),
        }

    @app.post(
        "/v1/context/clear",
        dependencies=[Depends(_require_local_dynamic_admin)],
    )
    def clear_recent_context() -> dict[str, int]:
        try:
            deleted = recent_context_service.clear()
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail="recent context clear failed",
            ) from exc
        return {"deleted": deleted}

    @app.get("/v1/channels/feishu/status")
    def feishu_chat_status() -> dict[str, bool | int]:
        return {
            "configured": feishu_chat_listener is not None,
            "available": bool(
                feishu_chat_listener is not None
                and getattr(feishu_chat_listener, "is_available", False)
            ),
            "received_messages": int(
                getattr(feishu_chat_listener, "received_messages", 0)
            ),
            "replied_messages": int(
                getattr(feishu_chat_listener, "replied_messages", 0)
            ),
        }

    if agent_registry is not None and agent_runtime is not None:
        dynamic_owner_id = settings.dynamic_agent_owner_id
        if dynamic_owner_id is None:
            raise ValueError("dynamic Agent owner is not configured")

        @app.post(
            "/v1/agents/drafts",
            status_code=201,
            dependencies=[Depends(_require_local_dynamic_admin)],
        )
        def propose_dynamic_agent(
            body: AgentDraftRequest,
        ) -> dict[str, object]:
            try:
                draft = agent_registry.propose(
                    body.request_text,
                    owner_id=dynamic_owner_id,
                    source_message_id=body.source_message_id,
                )
            except (AgentSpecCompileError, ValueError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return {"draft": _public_agent_draft(draft)}

        @app.post(
            "/v1/agents/drafts/{draft_id}/confirm",
            status_code=201,
            dependencies=[Depends(_require_local_dynamic_admin)],
        )
        def confirm_dynamic_agent(draft_id: Identifier) -> dict[str, object]:
            try:
                agent = agent_registry.confirm(
                    draft_id,
                    owner_id=dynamic_owner_id,
                )
            except KeyError as exc:
                raise HTTPException(
                    status_code=404,
                    detail="agent draft not found",
                ) from exc
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return {"agent": _public_agent(agent)}

        @app.get(
            "/v1/agents",
            dependencies=[Depends(_require_local_dynamic_admin)],
        )
        def list_dynamic_agents() -> dict[str, object]:
            return {
                "agents": [
                    _public_agent(agent)
                    for agent in agent_registry.list(owner_id=dynamic_owner_id)
                ]
            }

        @app.get(
            "/v1/agents/{agent_id}",
            dependencies=[Depends(_require_local_dynamic_admin)],
        )
        def get_dynamic_agent(agent_id: Identifier) -> dict[str, object]:
            agent = agent_registry.get(agent_id, owner_id=dynamic_owner_id)
            if agent is None:
                raise HTTPException(status_code=404, detail="agent not found")
            return {"agent": _public_agent(agent)}

        @app.post(
            "/v1/agents/{agent_id}/run",
            dependencies=[Depends(_require_local_dynamic_admin)],
        )
        def run_dynamic_agent(
            agent_id: Identifier,
            request: Request,
        ) -> dict[str, object]:
            try:
                execution = agent_runtime.run(
                    agent_id,
                    owner_id=dynamic_owner_id,
                    trigger_id=f"api-{_request_trace_id(request)}",
                    now=agent_clock(),
                )
            except KeyError as exc:
                raise HTTPException(
                    status_code=404,
                    detail="agent not found",
                ) from exc
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return {"execution": jsonable_encoder(execution)}

        @app.post(
            "/v1/agents/{agent_id}/pause",
            dependencies=[Depends(_require_local_dynamic_admin)],
        )
        def pause_dynamic_agent(agent_id: Identifier) -> dict[str, object]:
            try:
                agent = agent_registry.pause(
                    agent_id,
                    owner_id=dynamic_owner_id,
                )
            except KeyError as exc:
                raise HTTPException(
                    status_code=404,
                    detail="agent not found",
                ) from exc
            return {"agent": _public_agent(agent)}

        @app.post(
            "/v1/agents/{agent_id}/resume",
            dependencies=[Depends(_require_local_dynamic_admin)],
        )
        def resume_dynamic_agent(agent_id: Identifier) -> dict[str, object]:
            try:
                agent = agent_registry.resume(
                    agent_id,
                    owner_id=dynamic_owner_id,
                )
            except KeyError as exc:
                raise HTTPException(
                    status_code=404,
                    detail="agent not found",
                ) from exc
            return {"agent": _public_agent(agent)}

        @app.delete(
            "/v1/agents/{agent_id}",
            dependencies=[Depends(_require_local_dynamic_admin)],
        )
        def delete_dynamic_agent(agent_id: Identifier) -> dict[str, bool]:
            deleted = agent_registry.delete(
                agent_id,
                owner_id=dynamic_owner_id,
            )
            if not deleted:
                raise HTTPException(status_code=404, detail="agent not found")
            return {"deleted": True}

        @app.get(
            "/v1/agents/{agent_id}/executions",
            dependencies=[Depends(_require_local_dynamic_admin)],
        )
        def list_dynamic_agent_executions(
            agent_id: Identifier,
        ) -> dict[str, object]:
            if agent_registry.get(agent_id, owner_id=dynamic_owner_id) is None:
                raise HTTPException(status_code=404, detail="agent not found")
            executions = repository.list_executions(
                agent_id,
                owner_id=dynamic_owner_id,
            )
            return {"executions": jsonable_encoder(executions)}

    @app.get("/v1/devices/{device_id}/status")
    def device_status(device_id: Identifier) -> dict[str, object]:
        return {"device": jsonable_encoder(device_sessions.status(device_id))}

    @app.post(
        "/v1/devices/{device_id}/camera/capture",
        dependencies=[Depends(_require_local_dynamic_admin)],
    )
    def request_camera_capture(
        device_id: Identifier,
        request: Request,
    ) -> dict[str, object]:
        if not settings.camera_enabled:
            raise HTTPException(status_code=503, detail="camera feature is disabled")
        session = device_sessions.get(device_id)
        if session is None:
            raise HTTPException(status_code=409, detail="device is offline")
        if not session.hello.features.camera_jpeg:
            raise HTTPException(status_code=409, detail="device camera is unavailable")
        turn_id = request.headers.get("X-Turn-Id", "").strip() or _request_trace_id(request)
        max_bytes = min(
            settings.camera_max_bytes,
            session.hello.features.camera_max_bytes or settings.camera_max_bytes,
        )
        try:
            transport.send_control(
                session.session_id,
                {
                    "type": "camera",
                    "state": "capture",
                    "session_id": session.session_id,
                    "turn_id": turn_id,
                    "format": "jpeg",
                    "max_bytes": max_bytes,
                },
            )
        except (DeviceNotConnected, DeviceOutboundBackpressure) as exc:
            raise HTTPException(status_code=409, detail="camera request unavailable") from exc
        return {"requested": True, "turn_id": turn_id, "max_bytes": max_bytes}

    @app.post(
        "/v1/vision/sessions/{session_id}/describe",
        dependencies=[Depends(_require_local_dynamic_admin)],
    )
    def describe_camera_frame(
        session_id: Identifier,
        body: VisionDescribeRequest,
    ) -> dict[str, str]:
        if vision_coordinator is None:
            raise HTTPException(status_code=503, detail="vision runtime is unavailable")
        try:
            text = vision_coordinator.describe(
                session_id=session_id,
                turn_id=body.turn_id,
                prompt=body.prompt,
            )
        except VisionRuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except (VisionFeatureDisabled, VisionUnsupportedType, VisionTooLarge, VisionDuplicateTurn) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"text": text}

    @app.post("/v1/vision/explain")
    async def explain_camera_multipart(request: Request) -> JSONResponse:
        device_id = request.headers.get("Device-Id", "").strip()
        client_id = request.headers.get("Client-Id", "").strip()
        session = device_sessions.get_by_identity(
            device_id=device_id,
            client_id=client_id,
        )
        if session is None:
            return JSONResponse(
                status_code=401,
                content={
                    "success": False,
                    "message": "camera client unauthorized",
                },
            )
        if not settings.camera_enabled:
            return JSONResponse(
                status_code=503,
                content={
                    "success": False,
                    "message": "camera feature disabled",
                },
            )
        if vision_coordinator is None:
            return JSONResponse(
                status_code=503,
                content={
                    "success": False,
                    "message": "vision runtime unavailable",
                },
            )
        content_length = request.headers.get("Content-Length")
        if content_length is not None:
            try:
                if int(content_length) > settings.camera_max_bytes + 16_384:
                    return JSONResponse(
                        status_code=413,
                        content={
                            "success": False,
                            "message": "camera image too large",
                        },
                    )
            except ValueError:
                return JSONResponse(
                    status_code=400,
                    content={"success": False, "message": "invalid content length"},
                )
        try:
            question, image = _parse_camera_explain_multipart(
                request.headers.get("Content-Type", ""),
                await request.body(),
            )
            if len(image) > settings.camera_max_bytes:
                raise VisionTooLarge("camera image too large")
            turn_id = f"camera-http-{uuid4().hex}"
            camera_frames.put(
                session_id=session.session_id,
                turn_id=turn_id,
                payload=image,
            )
            result = vision_coordinator.describe(
                session_id=session.session_id,
                turn_id=turn_id,
                prompt=question,
            )
        except (ValueError, VisionRuntimeError, VisionFeatureDisabled) as exc:
            return JSONResponse(
                status_code=503 if isinstance(exc, VisionRuntimeError) else 400,
                content={
                    "success": False,
                    "message": "camera explain failed",
                },
            )
        return JSONResponse(
            status_code=200,
            content={"success": True, "result": result},
        )

    @app.post("/v1/vision/observations")
    async def upload_vision_observation(request: Request) -> JSONResponse:
        subject_id = request.headers.get("X-Subject-Id", "").strip()
        turn_id = request.headers.get("X-Turn-Id", "").strip()
        content_type = request.headers.get("Content-Type", "")
        consent = request.headers.get("X-Vision-Consent", "").strip().lower() == "true"
        if not subject_id or not turn_id:
            raise HTTPException(status_code=400, detail="subject and turn headers required")
        content_length = request.headers.get("Content-Length")
        if content_length is not None:
            try:
                if int(content_length) > settings.vision_max_upload_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail="vision image exceeds the upload limit",
                    )
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail="invalid Content-Length",
                ) from exc
        try:
            observation = vision_service.upload(
                subject_id=subject_id,
                turn_id=turn_id,
                content_type=content_type,
                payload=await request.body(),
                consent=consent,
            )
        except VisionFeatureDisabled as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except VisionConsentRequired as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except VisionUnsupportedType as exc:
            raise HTTPException(status_code=415, detail=str(exc)) from exc
        except VisionTooLarge as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except VisionDuplicateTurn as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except VisionQuotaExceeded as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        return JSONResponse(
            status_code=201,
            content={"observation": jsonable_encoder(observation)},
        )

    @app.get("/v1/vision/observations")
    def list_vision_observations(subject_id: Identifier) -> dict[str, object]:
        try:
            observations = vision_service.list(subject_id=subject_id)
        except VisionFeatureDisabled as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"observations": jsonable_encoder(observations)}

    @app.delete("/v1/vision/observations/{observation_id}")
    def delete_vision_observation(
        observation_id: Identifier,
        subject_id: Identifier,
    ) -> dict[str, bool]:
        try:
            deleted = vision_service.delete(
                subject_id=subject_id,
                observation_id=observation_id,
            )
        except VisionFeatureDisabled as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if not deleted:
            raise HTTPException(status_code=404, detail="vision observation not found")
        return {"deleted": True}

    @app.post("/v1/agent/tools/{tool_name}")
    def execute_agent_tool(
        tool_name: str,
        body: AgentToolRequest,
        request: Request,
    ) -> dict[str, object]:
        if tool_name not in AgentToolService.policies():
            raise HTTPException(status_code=404, detail="agent tool not found")
        try:
            result = agent_tool_service.execute(
                tool_name,
                actor_id=body.actor_id,
                target_device_id=body.target_device_id,
                arguments=body.arguments,
                trace_id=_request_trace_id(request),
            )
        except AgentToolNotAllowed as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except AgentToolTimeout as exc:
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "result": {
                "tool": result.tool,
                **jsonable_encoder(result.result),
                "auto_executed": result.auto_executed,
            }
        }

    @app.post("/v1/ota")
    def ota_bootstrap(request: Request) -> JSONResponse:
        device_id = request.headers.get("Device-Id", "").strip()
        if not device_id:
            raise HTTPException(
                status_code=400,
                detail="Device-Id header required",
                headers={"Cache-Control": "no-store"},
            )
        if not settings.public_websocket_url or not settings.ota_device_tokens:
            raise HTTPException(
                status_code=404,
                detail="OTA bootstrap disabled",
                headers={"Cache-Control": "no-store"},
            )
        token = settings.ota_device_tokens.get(device_id)
        if token is None:
            raise HTTPException(
                status_code=403,
                detail="device is not enrolled",
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(
            content={
                "websocket": {
                    "url": settings.public_websocket_url,
                    "token": token,
                    "version": 1,
                }
            },
            headers={"Cache-Control": "no-store"},
        )

    async def reject_websocket(websocket: WebSocket, code: int) -> None:
        await websocket.close(code=code)

    def bearer_token(websocket: WebSocket) -> str | None:
        authorization = websocket.headers.get("Authorization", "")
        scheme, separator, token = authorization.partition(" ")
        if separator != " " or scheme.lower() != "bearer" or not token:
            return None
        return token

    async def send_device_error(
        websocket: WebSocket,
        *,
        code: str,
        trace_id: str,
        retryable: bool,
    ) -> None:
        await websocket.send_json(
            {
                "type": "error",
                "code": code,
                "retryable": retryable,
                "trace_id": trace_id,
            }
        )

    @app.websocket("/v1/devices/ws")
    async def device_websocket(websocket: WebSocket) -> None:
        if websocket.headers.get("Protocol-Version") != "1":
            await reject_websocket(websocket, 1002)
            return

        device_id = websocket.headers.get("Device-Id", "").strip()
        client_id = websocket.headers.get("Client-Id", "").strip()
        token = bearer_token(websocket)
        if (
            not device_id
            or not client_id
            or token is None
            or not device_authenticator.verify(device_id, token)
        ):
            await reject_websocket(websocket, 1008)
            return

        await websocket.accept()
        session: DeviceSession | None = None
        camera_upload: CameraUploadState | None = None
        outbound_sender: asyncio.Task[None] | None = None
        keepalive_sender: asyncio.Task[None] | None = None
        auto_stop_task: asyncio.Task[None] | None = None
        idle_controller: ConversationIdleController | None = None
        pending_session_close_reason: str | None = None
        tts_active = False
        close_code: int | None = None
        close_reason_length = 0
        wake_word_frames_ignored = 0
        auto_turn_tail_frames_ignored = 0
        auto_turn_audio_frames = 0
        audio_rms_min: float | None = None
        audio_rms_max: float | None = None
        audio_rms_last: float | None = None
        endpoint_detector: AutoTurnEndpointDetector | None = None
        vad_endpoint_detector: VadTurnEndpointDetector | None = None
        vad_preroll_frames: deque[bytes] = deque(maxlen=5)
        vad_turn_discarded = False
        vad_blocked_segment = False
        post_tts_silence_gate: ConsecutiveSilenceGate | None = None
        post_tts_frames_ignored = 0
        tts_interrupted = asyncio.Event()
        trace_id = f"trc_{uuid4().hex}"
        logger.info("device_ws_accepted device=%s", redact_device_id(device_id))
        try:
            raw_hello = await asyncio.wait_for(
                websocket.receive_json(),
                timeout=settings.device_hello_timeout_seconds,
            )
            hello = DeviceHello.model_validate(raw_hello)
            if settings.camera_enabled and hello.features.camera_jpeg:
                camera_upload = CameraUploadState(
                    max_bytes=min(
                        settings.camera_max_bytes,
                        hello.features.camera_max_bytes
                        or settings.camera_max_bytes,
                    )
                )
            session = DeviceSession.create(
                device_id=device_id,
                client_id=client_id,
                hello=hello,
            )
            logger.info(
                "device_ws_hello device=%s session=%s audio=%s/%s/%s/%sms "
                "vad_events=%s",
                redact_device_id(device_id),
                session.session_id,
                hello.audio_params.format,
                hello.audio_params.sample_rate,
                hello.audio_params.channels,
                hello.audio_params.frame_duration,
                hello.features.vad_events,
            )
            previous = device_sessions.connect(session)
            if previous is not None:
                previous.close()
                transport.unregister(previous.session_id)
            response_hello = server_hello(session.session_id)
            logger.info(
                "device_ws_server_hello device=%s session=%s version=%s transport=%s "
                "audio=%s/%s/%s/%sms",
                redact_device_id(device_id),
                session.session_id,
                response_hello["version"],
                response_hello["transport"],
                response_hello["audio_params"]["format"],
                response_hello["audio_params"]["sample_rate"],
                response_hello["audio_params"]["channels"],
                response_hello["audio_params"]["frame_duration"],
            )
            await websocket.send_json(response_hello)
            transport.register(session.session_id)

            async def close_for_idle(generation: int) -> None:
                if session is None or tts_active:
                    return
                if not session.is_conversation_idle_current(generation):
                    return
                session.cancel_conversation_idle()
                await websocket.close(code=1000, reason="conversation_idle")

            idle_controller = ConversationIdleController(
                timeout_seconds=settings.device_conversation_idle_timeout_seconds,
                on_timeout=close_for_idle,
            )

            async def forward_outbound() -> None:
                nonlocal post_tts_frames_ignored, post_tts_silence_gate
                nonlocal tts_active, pending_session_close_reason
                nonlocal close_code, close_reason_length
                frame_duration_ms = response_hello["audio_params"]["frame_duration"]
                outbound_kind = "unknown"
                frames_sent = 0
                total_frames = 0
                try:
                    while True:
                        message = await transport.next_outbound(session.session_id)
                        if isinstance(message, OutboundControl):
                            await websocket.send_json(message.payload)
                            continue
                        if isinstance(message, OutboundTask):
                            outbound_kind = "task"
                            frames_sent = 0
                            total_frames = 0
                            await websocket.send_json(
                                {
                                    "type": "task",
                                    "state": "notify",
                                    "session_id": message.session_id,
                                    "task": jsonable_encoder(message.task),
                                }
                            )
                            continue
                        outbound_kind = "tts"
                        if idle_controller is not None:
                            idle_controller.cancel()
                        session.cancel_conversation_idle()
                        tts_active = True
                        frames_sent = 0
                        total_frames = len(message.opus_frames)
                        tts_interrupted.clear()
                        await websocket.send_json(
                            {
                                "type": "tts",
                                "state": "start",
                                "purpose": message.purpose,
                                "session_id": message.session_id,
                            }
                        )
                        started_at = time.perf_counter()
                        logger.info(
                            "device_ws_tts_stream_started device=%s session=%s "
                            "frames=%s frame_duration_ms=%s",
                            redact_device_id(device_id),
                            session.session_id,
                            len(message.opus_frames),
                            frame_duration_ms,
                        )
                        for frame_index, opus_frame in enumerate(message.opus_frames):
                            if tts_interrupted.is_set():
                                break
                            if frame_index:
                                await _sleep_between_tts_frames(
                                    frame_duration_ms / 1_000
                                )
                            if tts_interrupted.is_set():
                                break
                            await websocket.send_bytes(opus_frame)
                            frames_sent += 1
                        if tts_interrupted.is_set():
                            logger.info(
                                "device_ws_tts_stream_interrupted device=%s session=%s "
                                "frames_sent=%s total_frames=%s",
                                redact_device_id(device_id),
                                session.session_id,
                                frames_sent,
                                len(message.opus_frames),
                            )
                            continue
                        post_tts_rms_threshold = (
                            settings.device_vad_post_tts_rms_threshold
                            if hello.features.vad_events
                            else settings.device_auto_turn_rms_threshold
                        )
                        if post_tts_rms_threshold is not None:
                            post_tts_frames_ignored = 0
                            post_tts_silence_gate = ConsecutiveSilenceGate(
                                rms_threshold=post_tts_rms_threshold,
                                consecutive_silent_frames=(
                                    settings.device_post_tts_silence_frames
                                ),
                            )
                        await websocket.send_json(
                            {
                                "type": "tts",
                                "state": "stop",
                                "session_id": message.session_id,
                            }
                        )
                        logger.info(
                            "device_ws_tts_stream_finished device=%s session=%s "
                            "frames=%s duration_ms=%s",
                            redact_device_id(device_id),
                            session.session_id,
                            len(message.opus_frames),
                            round((time.perf_counter() - started_at) * 1_000),
                        )
                        tts_active = False
                        if pending_session_close_reason is not None:
                            reason = pending_session_close_reason
                            pending_session_close_reason = None
                            close_code = 1000
                            close_reason_length = len(reason)
                            await websocket.close(code=1000, reason=reason)
                            return
                        if message.purpose == "notification":
                            post_tts_silence_gate = None
                            continue
                        if (
                            settings.device_continuous_conversation_enabled
                            and idle_controller is not None
                        ):
                            generation = session.arm_conversation_idle()
                            idle_controller.arm(generation)
                        else:
                            close_code = 1000
                            close_reason = "conversation_turn_complete"
                            close_reason_length = len(close_reason)
                            await websocket.close(code=1000, reason=close_reason)
                            return
                except (RuntimeError, WebSocketDisconnect) as exc:
                    logger.info(
                        "device_ws_outbound_stopped device=%s session=%s kind=%s "
                        "error_type=%s error=%r frames_sent=%s total_frames=%s",
                        redact_device_id(device_id),
                        session.session_id,
                        outbound_kind,
                        type(exc).__name__,
                        str(exc),
                        frames_sent,
                        total_frames,
                    )

            outbound_sender = asyncio.create_task(forward_outbound())

            async def send_keepalives() -> None:
                while True:
                    await asyncio.sleep(settings.device_control_keepalive_seconds)
                    try:
                        transport.send_control(
                            session.session_id,
                            {
                                "type": "keepalive",
                                "session_id": session.session_id,
                            },
                        )
                    except (DeviceNotConnected, DeviceOutboundBackpressure):
                        return

            keepalive_sender = asyncio.create_task(send_keepalives())
            if settings.camera_enabled and vision_coordinator is not None:
                if settings.public_websocket_url:
                    transport.send_control(
                        session.session_id,
                        build_vision_capability_message(
                            derive_vision_explain_url(
                                settings.public_websocket_url
                            ),
                            session_id=session.session_id,
                        ),
                    )

            async def process_voice_turn(*, trigger: str) -> None:
                nonlocal pending_session_close_reason
                if voice_delivery_service is None:
                    return
                try:
                    turn = await voice_delivery_service.process_and_send_async(
                        session_id=session.session_id,
                        target_device_id=session.device_id,
                    )
                    if turn is not None and turn.end_conversation:
                        pending_session_close_reason = "user_exit"
                    logger.info(
                        "device_ws_voice_turn_processed device=%s session=%s "
                        "trigger=%s delivered=%s",
                        redact_device_id(device_id),
                        session.session_id,
                        trigger,
                        turn is not None,
                    )
                except ModelRuntimeError:
                    voice_delivery_service.clear_pending_input(
                        session_id=session.session_id,
                    )
                    await send_device_error(
                        websocket,
                        code="model_unavailable",
                        trace_id=trace_id,
                        retryable=True,
                    )
                    await websocket.close(code=1011)
                except (DeviceNotConnected, DeviceOutboundBackpressure):
                    voice_delivery_service.clear_pending_input(
                        session_id=session.session_id,
                    )
                    logger.info(
                        "device_ws_voice_turn_delivery_skipped device=%s session=%s "
                        "trigger=%s",
                        redact_device_id(device_id),
                        session.session_id,
                        trigger,
                    )

            async def reject_auto_turn(*, reason: str) -> None:
                if voice_delivery_service is None:
                    return
                voice_delivery_service.clear_pending_input(
                    session_id=session.session_id,
                )
                logger.info(
                    "device_ws_auto_turn_rejected device=%s session=%s "
                    "reason=%s frames=%s",
                    redact_device_id(device_id),
                    session.session_id,
                    reason,
                    auto_turn_audio_frames,
                )
                await websocket.close(code=1000, reason="no_speech")

            async def finish_auto_turn_after_silence(expected_frames: int) -> None:
                await asyncio.sleep(settings.device_auto_stop_idle_seconds)
                if session.audio_frames_received != expected_frames:
                    return
                if not session.finish_auto_listening():
                    return
                logger.info(
                    "device_ws_auto_listen_finished device=%s session=%s frames=%s "
                    "idle_seconds=%s",
                    redact_device_id(device_id),
                    session.session_id,
                    expected_frames,
                    settings.device_auto_stop_idle_seconds,
                )
                await process_voice_turn(trigger="auto_silence")

            async def finish_auto_turn_after_pcm_silence() -> None:
                if not session.finish_auto_listening():
                    return
                logger.info(
                    "device_ws_auto_pcm_endpoint device=%s session=%s frames=%s "
                    "rms_threshold=%s silent_frames=%s min_speech_frames=%s",
                    redact_device_id(device_id),
                    session.session_id,
                    auto_turn_audio_frames,
                    settings.device_auto_turn_rms_threshold,
                    settings.device_auto_turn_silence_frames,
                    settings.device_auto_turn_min_speech_frames,
                )
                await process_voice_turn(trigger="pcm_silence")

            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    close_code = message.get("code")
                    close_reason_length = len(message.get("reason") or "")
                    logger.info(
                        "device_ws_peer_closed device=%s session=%s code=%s "
                        "reason_length=%s phase=%s frames=%s mode=%s",
                        redact_device_id(device_id),
                        session.session_id,
                        close_code,
                        close_reason_length,
                        session.phase.value,
                        session.audio_frames_received,
                        session.listening_mode,
                    )
                    break
                session.touch()

                text = message.get("text")
                if text is not None:
                    try:
                        payload = json.loads(text)
                        message_type = (
                            payload.get("type")
                            if isinstance(payload, dict)
                            else None
                        )
                        if message_type == "camera":
                            if camera_upload is None:
                                await send_device_error(
                                    websocket,
                                    code="camera_unavailable",
                                    trace_id=trace_id,
                                    retryable=False,
                                )
                                continue
                            metadata = CameraCaptureMetadata.model_validate(payload)
                            if metadata.session_id != session.session_id:
                                raise InvalidDevicePhase(
                                    "camera message has the wrong session_id"
                                )
                            camera_upload.start(metadata)
                            continue
                        if message_type == "listen":
                            control = ListenControl.model_validate(payload)
                            session.apply_listen(control)
                            if control.state == "start":
                                if idle_controller is not None:
                                    idle_controller.cancel()
                                session.cancel_conversation_idle()
                            if control.state == "detect":
                                post_tts_silence_gate = None
                            if control.state == "start":
                                auto_turn_audio_frames = 0
                                vad_preroll_frames.clear()
                                vad_turn_discarded = False
                                vad_blocked_segment = False
                                vad_endpoint_detector = (
                                    VadTurnEndpointDetector(
                                        minimum_speech_frames=(
                                            settings.device_auto_turn_min_speech_frames
                                        )
                                    )
                                    if hello.features.vad_events
                                    else None
                                )
                                endpoint_detector = (
                                    AutoTurnEndpointDetector(
                                        rms_threshold=(
                                            settings.device_auto_turn_rms_threshold
                                        ),
                                        consecutive_silent_frames=(
                                            settings.device_auto_turn_silence_frames
                                        ),
                                        minimum_speech_frames=(
                                            settings.device_auto_turn_min_speech_frames
                                        ),
                                    )
                                    if not hello.features.vad_events
                                    and settings.device_auto_turn_rms_threshold is not None
                                    else None
                                )
                        elif message_type == "vad":
                            control = VadControl.model_validate(payload)
                            if session.should_ignore_auto_turn_tail_audio():
                                logger.info(
                                    "device_ws_auto_tail_vad_ignored device=%s "
                                    "session=%s state=%s",
                                    redact_device_id(device_id),
                                    session.session_id,
                                    control.state,
                                )
                                continue
                            session.apply_vad(control)
                        elif message_type == "abort":
                            control = AbortControl.model_validate(payload)
                            session.apply_abort(control)
                            tts_interrupted.set()
                        else:
                            raise UnsupportedDeviceControl(
                                "unsupported control message"
                            )
                        logger.info(
                            "device_ws_control device=%s session=%s type=%s state=%s "
                            "mode=%s",
                            redact_device_id(device_id),
                            session.session_id,
                            message_type,
                            getattr(control, "state", None),
                            getattr(control, "mode", None),
                        )
                        sink.on_control(session, control)
                        if message_type == "vad":
                            if vad_endpoint_detector is None:
                                raise InvalidDevicePhase(
                                    "VAD endpoint is unavailable before listen.start"
                                )
                            if control.state == "start":
                                if post_tts_silence_gate is not None:
                                    vad_blocked_segment = True
                                    vad_preroll_frames.clear()
                                    auto_turn_audio_frames = 0
                                    if voice_delivery_service is not None:
                                        voice_delivery_service.clear_pending_input(
                                            session_id=session.session_id,
                                        )
                                    logger.info(
                                        "device_ws_vad_blocked_by_post_tts_gate "
                                        "device=%s session=%s",
                                        redact_device_id(device_id),
                                        session.session_id,
                                    )
                                elif not (
                                    vad_blocked_segment or vad_turn_discarded
                                ) and not vad_endpoint_detector.speech_active:
                                    if idle_controller is not None:
                                        idle_controller.cancel()
                                    session.cancel_conversation_idle()
                                    vad_endpoint_detector.start()
                                    if voice_delivery_service is not None:
                                        voice_delivery_service.clear_pending_input(
                                            session_id=session.session_id,
                                        )
                                        while vad_preroll_frames:
                                            if (
                                                auto_turn_audio_frames
                                                >= settings.device_auto_turn_max_frames
                                            ):
                                                vad_turn_discarded = True
                                                break
                                            voice_delivery_service.accept_and_send(
                                                session_id=session.session_id,
                                                opus_frame=vad_preroll_frames.popleft(),
                                            )
                                            auto_turn_audio_frames += 1
                                        if vad_turn_discarded:
                                            voice_delivery_service.clear_pending_input(
                                                session_id=session.session_id,
                                            )
                                            vad_preroll_frames.clear()
                                            logger.info(
                                                "device_ws_vad_frame_limit_discarded "
                                                "device=%s session=%s frames=%s "
                                                "source=preroll",
                                                redact_device_id(device_id),
                                                session.session_id,
                                                auto_turn_audio_frames,
                                            )
                                    else:
                                        vad_preroll_frames.clear()
                            else:
                                speech_frames = vad_endpoint_detector.speech_frames
                                if vad_blocked_segment or vad_turn_discarded:
                                    discard_reason = (
                                        "post_tts_gate"
                                        if vad_blocked_segment
                                        else "frame_limit"
                                    )
                                    vad_blocked_segment = False
                                    vad_turn_discarded = False
                                    vad_endpoint_detector.stop()
                                    if voice_delivery_service is not None:
                                        voice_delivery_service.clear_pending_input(
                                            session_id=session.session_id,
                                        )
                                    vad_preroll_frames.clear()
                                    auto_turn_audio_frames = 0
                                    logger.info(
                                        "device_ws_vad_segment_discarded device=%s "
                                        "session=%s reason=%s",
                                        redact_device_id(device_id),
                                        session.session_id,
                                        discard_reason,
                                    )
                                elif vad_endpoint_detector.stop():
                                    if voice_delivery_service is None:
                                        vad_preroll_frames.clear()
                                        auto_turn_audio_frames = 0
                                        continue
                                    if not vad_endpoint_detector.meets_rms_threshold(
                                        settings.device_vad_turn_rms_threshold
                                    ):
                                        voice_delivery_service.clear_pending_input(
                                            session_id=session.session_id,
                                        )
                                        vad_preroll_frames.clear()
                                        auto_turn_audio_frames = 0
                                        logger.info(
                                            "device_ws_vad_rms_rejected device=%s "
                                            "session=%s speech_frames=%s "
                                            "rms_avg=%s threshold=%s",
                                            redact_device_id(device_id),
                                            session.session_id,
                                            speech_frames,
                                            vad_endpoint_detector.average_rms,
                                            settings.device_vad_turn_rms_threshold,
                                        )
                                        continue
                                    if not session.finish_auto_listening():
                                        raise InvalidDevicePhase(
                                            "VAD endpoint requires auto listening"
                                        )
                                    logger.info(
                                        "device_ws_vad_endpoint device=%s session=%s "
                                        "speech_frames=%s audio_frames=%s "
                                        "rms_min=%s rms_max=%s rms_avg=%s",
                                        redact_device_id(device_id),
                                        session.session_id,
                                        speech_frames,
                                        auto_turn_audio_frames,
                                        vad_endpoint_detector.rms_min,
                                        vad_endpoint_detector.rms_max,
                                        vad_endpoint_detector.average_rms,
                                    )
                                    await process_voice_turn(trigger="device_vad")
                                else:
                                    if voice_delivery_service is not None:
                                        voice_delivery_service.clear_pending_input(
                                            session_id=session.session_id,
                                        )
                                    vad_preroll_frames.clear()
                                    auto_turn_audio_frames = 0
                                    logger.info(
                                        "device_ws_vad_rejected device=%s session=%s "
                                        "speech_frames=%s",
                                        redact_device_id(device_id),
                                        session.session_id,
                                        speech_frames,
                                    )
                        elif (
                            message_type == "listen"
                            and control.state == "stop"
                            and not session.auto_turn_finished
                        ):
                            if auto_stop_task is not None:
                                auto_stop_task.cancel()
                                auto_stop_task = None
                            if hello.features.vad_events:
                                if vad_endpoint_detector is not None:
                                    vad_endpoint_detector.stop()
                                if voice_delivery_service is not None:
                                    voice_delivery_service.clear_pending_input(
                                        session_id=session.session_id,
                                    )
                                vad_preroll_frames.clear()
                                auto_turn_audio_frames = 0
                                vad_turn_discarded = False
                                vad_blocked_segment = False
                                logger.info(
                                    "device_ws_vad_listen_stop_discarded device=%s "
                                    "session=%s",
                                    redact_device_id(device_id),
                                    session.session_id,
                                )
                            elif (
                                voice_delivery_service is not None
                                and session.listening_mode == "auto"
                                and endpoint_detector is not None
                                and not endpoint_detector.has_heard_speech
                            ):
                                await reject_auto_turn(reason="unconfirmed_speech")
                                break
                            elif voice_delivery_service is not None:
                                await process_voice_turn(trigger="listen_stop")
                        elif message_type == "abort":
                            if auto_stop_task is not None:
                                auto_stop_task.cancel()
                                auto_stop_task = None
                            if voice_delivery_service is not None:
                                voice_delivery_service.clear_pending_input(
                                    session_id=session.session_id,
                                )
                            if idle_controller is not None:
                                idle_controller.cancel()
                            session.cancel_conversation_idle()
                            if control.reason in {
                                "user_exit",
                                "conversation_idle",
                                "stop",
                                "end_conversation",
                            }:
                                close_code = 1000
                                close_reason_length = len("device_abort")
                                await websocket.close(
                                    code=1000,
                                    reason="device_abort",
                                )
                                break
                    except (
                        json.JSONDecodeError,
                        ValidationError,
                        UnsupportedDeviceControl,
                    ):
                        await send_device_error(
                            websocket,
                            code="invalid_control",
                            trace_id=trace_id,
                            retryable=False,
                        )
                        await websocket.close(code=1003)
                        break
                    except InvalidDevicePhase:
                        await send_device_error(
                            websocket,
                            code="invalid_device_phase",
                            trace_id=trace_id,
                            retryable=False,
                        )
                        await websocket.close(code=1008)
                        break
                    except AudioFrameRejected as exc:
                        logger.warning(
                            "device_ws_audio_rejected device=%s session=%s "
                            "source=vad_preroll reason=%s",
                            redact_device_id(device_id),
                            session.session_id,
                            exc,
                        )
                        await send_device_error(
                            websocket,
                            code="audio_decode_failed",
                            trace_id=trace_id,
                            retryable=False,
                        )
                        await websocket.close(code=1003)
                        break
                    except AudioQueueFull:
                        await send_device_error(
                            websocket,
                            code="audio_pipeline_backpressure",
                            trace_id=trace_id,
                            retryable=True,
                        )
                        await websocket.close(code=1013)
                        break
                    except DeviceBackpressure:
                        await send_device_error(
                            websocket,
                            code="device_backpressure",
                            trace_id=trace_id,
                            retryable=True,
                        )
                        await websocket.close(code=1013)
                        break
                    continue

                audio_frame = message.get("bytes")
                if audio_frame is None:
                    continue
                if camera_upload is not None and camera_upload.active:
                    metadata = camera_upload.metadata
                    try:
                        camera_upload.accept_chunk(audio_frame)
                        if camera_upload.complete and metadata is not None:
                            camera_frames.put(
                                session_id=session.session_id,
                                turn_id=metadata.turn_id,
                                payload=camera_upload.finish(),
                            )
                    except CameraUploadError:
                        await send_device_error(
                            websocket,
                            code="camera_invalid",
                            trace_id=trace_id,
                            retryable=False,
                        )
                    continue
                if not audio_frame:
                    await send_device_error(
                        websocket,
                        code="audio_frame_empty",
                        trace_id=trace_id,
                        retryable=False,
                    )
                    await websocket.close(code=1003)
                    break
                if len(audio_frame) > settings.device_audio_frame_max_bytes:
                    await send_device_error(
                        websocket,
                        code="audio_frame_too_large",
                        trace_id=trace_id,
                        retryable=False,
                    )
                    await websocket.close(code=1009)
                    break
                try:
                    if session.should_ignore_wake_word_audio():
                        wake_word_frames_ignored += 1
                        if wake_word_frames_ignored in (1, 10) or (
                            wake_word_frames_ignored % 50 == 0
                        ):
                            logger.info(
                                "device_ws_wake_audio_ignored device=%s session=%s frames=%s",
                                redact_device_id(device_id),
                                session.session_id,
                                wake_word_frames_ignored,
                            )
                        continue
                    if session.should_ignore_auto_turn_tail_audio():
                        auto_turn_tail_frames_ignored += 1
                        if auto_turn_tail_frames_ignored == 1:
                            logger.info(
                                "device_ws_auto_tail_audio_ignored device=%s session=%s",
                                redact_device_id(device_id),
                                session.session_id,
                            )
                        continue
                    session.accept_audio_frame()
                    if session.audio_frames_received in (1, 10) or (
                        session.audio_frames_received % 50 == 0
                    ):
                        logger.info(
                            "device_ws_audio device=%s session=%s frames=%s",
                            redact_device_id(device_id),
                            session.session_id,
                            session.audio_frames_received,
                        )
                    sink.on_audio(session, audio_frame)
                    if (
                        hello.features.vad_events
                        and vad_endpoint_detector is not None
                    ):
                        if (
                            voice_delivery_service is not None
                            and post_tts_silence_gate is not None
                        ):
                            pcm_frame = voice_delivery_service.accept_and_send(
                                session_id=session.session_id,
                                opus_frame=audio_frame,
                            )
                            audio_rms_last = pcm_frame.metrics.rms_amplitude
                            audio_rms_min = (
                                audio_rms_last
                                if audio_rms_min is None
                                else min(audio_rms_min, audio_rms_last)
                            )
                            audio_rms_max = (
                                audio_rms_last
                                if audio_rms_max is None
                                else max(audio_rms_max, audio_rms_last)
                            )
                            voice_delivery_service.clear_pending_input(
                                session_id=session.session_id,
                            )
                            post_tts_frames_ignored += 1
                            if post_tts_silence_gate.observe(
                                rms_amplitude=audio_rms_last
                            ):
                                logger.info(
                                    "device_ws_post_tts_gate_opened device=%s "
                                    "session=%s ignored_frames=%s "
                                    "silence_frames=%s",
                                    redact_device_id(device_id),
                                    session.session_id,
                                    post_tts_frames_ignored,
                                    settings.device_post_tts_silence_frames,
                                )
                                post_tts_silence_gate = None
                            continue
                        if vad_blocked_segment or vad_turn_discarded:
                            continue
                        if not vad_endpoint_detector.speech_active:
                            if voice_delivery_service is not None:
                                vad_preroll_frames.append(bytes(audio_frame))
                            continue
                        if (
                            auto_turn_audio_frames
                            >= settings.device_auto_turn_max_frames
                        ):
                            if voice_delivery_service is not None:
                                voice_delivery_service.clear_pending_input(
                                    session_id=session.session_id,
                                )
                            vad_preroll_frames.clear()
                            vad_turn_discarded = True
                            logger.info(
                                "device_ws_vad_frame_limit_discarded device=%s "
                                "session=%s frames=%s source=active",
                                redact_device_id(device_id),
                                session.session_id,
                                auto_turn_audio_frames,
                            )
                            continue
                        auto_turn_audio_frames += 1
                        if voice_delivery_service is not None:
                            pcm_frame = voice_delivery_service.accept_and_send(
                                session_id=session.session_id,
                                opus_frame=audio_frame,
                            )
                            audio_rms_last = pcm_frame.metrics.rms_amplitude
                            audio_rms_min = (
                                audio_rms_last
                                if audio_rms_min is None
                                else min(audio_rms_min, audio_rms_last)
                            )
                            audio_rms_max = (
                                audio_rms_last
                                if audio_rms_max is None
                                else max(audio_rms_max, audio_rms_last)
                            )
                            vad_endpoint_detector.observe_audio(
                                rms_amplitude=audio_rms_last,
                            )
                        else:
                            vad_endpoint_detector.observe_audio()
                        continue
                    if voice_delivery_service is not None:
                        pcm_frame = voice_delivery_service.accept_and_send(
                            session_id=session.session_id,
                            opus_frame=audio_frame,
                        )
                        audio_rms_last = pcm_frame.metrics.rms_amplitude
                        audio_rms_min = (
                            audio_rms_last
                            if audio_rms_min is None
                            else min(audio_rms_min, audio_rms_last)
                        )
                        audio_rms_max = (
                            audio_rms_last
                            if audio_rms_max is None
                            else max(audio_rms_max, audio_rms_last)
                        )
                        if post_tts_silence_gate is not None:
                            voice_delivery_service.clear_pending_input(
                                session_id=session.session_id,
                            )
                            post_tts_frames_ignored += 1
                            if post_tts_silence_gate.observe(
                                rms_amplitude=audio_rms_last
                            ):
                                logger.info(
                                    "device_ws_post_tts_gate_opened device=%s "
                                    "session=%s ignored_frames=%s "
                                    "silence_frames=%s",
                                    redact_device_id(device_id),
                                    session.session_id,
                                    post_tts_frames_ignored,
                                    settings.device_post_tts_silence_frames,
                                )
                                post_tts_silence_gate = None
                            continue
                        if session.listening_mode == "auto":
                            auto_turn_audio_frames += 1
                        reached_pcm_endpoint = (
                            session.listening_mode == "auto"
                            and endpoint_detector is not None
                            and endpoint_detector.observe(
                                rms_amplitude=audio_rms_last
                            )
                        )
                        if reached_pcm_endpoint:
                            if auto_stop_task is not None:
                                auto_stop_task.cancel()
                                auto_stop_task = None
                            await finish_auto_turn_after_pcm_silence()
                            continue
                        if (
                            session.listening_mode == "auto"
                            and vad_endpoint_detector is None
                            and auto_turn_audio_frames
                            >= settings.device_auto_turn_max_frames
                        ):
                            if auto_stop_task is not None:
                                auto_stop_task.cancel()
                                auto_stop_task = None
                            if not session.finish_auto_listening():
                                continue
                            if (
                                endpoint_detector is not None
                                and not endpoint_detector.has_heard_speech
                            ):
                                await reject_auto_turn(reason="frame_limit_no_speech")
                                break
                            else:
                                logger.info(
                                    "device_ws_auto_frame_limit device=%s "
                                    "session=%s frames=%s",
                                    redact_device_id(device_id),
                                    session.session_id,
                                    auto_turn_audio_frames,
                                )
                                await process_voice_turn(trigger="frame_limit")
                            continue
                        if (
                            session.listening_mode == "auto"
                            and endpoint_detector is None
                        ):
                            if auto_stop_task is not None:
                                auto_stop_task.cancel()
                            auto_stop_task = asyncio.create_task(
                                finish_auto_turn_after_silence(
                                    session.audio_frames_received
                                )
                            )
                except InvalidDevicePhase:
                    await send_device_error(
                        websocket,
                        code="audio_not_allowed",
                        trace_id=trace_id,
                        retryable=False,
                    )
                    await websocket.close(code=1008)
                    break
                except AudioFrameRejected as exc:
                    logger.warning(
                        "device_ws_audio_rejected device=%s session=%s frame_bytes=%s "
                        "reason=%s",
                        redact_device_id(device_id),
                        session.session_id,
                        len(audio_frame),
                        exc,
                    )
                    await send_device_error(
                        websocket,
                        code="audio_decode_failed",
                        trace_id=trace_id,
                        retryable=False,
                    )
                    await websocket.close(code=1003)
                    break
                except (AudioQueueFull, DeviceOutboundBackpressure):
                    await send_device_error(
                        websocket,
                        code="audio_pipeline_backpressure",
                        trace_id=trace_id,
                        retryable=True,
                    )
                    await websocket.close(code=1013)
                    break
                except DeviceNotConnected:
                    await send_device_error(
                        websocket,
                        code="device_session_unavailable",
                        trace_id=trace_id,
                        retryable=True,
                    )
                    await websocket.close(code=1011)
                    break
                except DeviceBackpressure:
                    await send_device_error(
                        websocket,
                        code="device_backpressure",
                        trace_id=trace_id,
                        retryable=True,
                    )
                    await websocket.close(code=1013)
                    break
        except (asyncio.TimeoutError, json.JSONDecodeError, ValidationError) as exc:
            logger.warning(
                "device_ws_protocol_error device=%s error=%s",
                redact_device_id(device_id),
                type(exc).__name__,
            )
            await reject_websocket(websocket, 1003)
        except WebSocketDisconnect as exc:
            close_code = exc.code
            logger.info(
                "device_ws_disconnect device=%s code=%s",
                redact_device_id(device_id),
                exc.code,
            )
        finally:
            phase_before_close = session.phase.value if session is not None else "none"
            duration_ms = (
                max(
                    0,
                    int((datetime.now(UTC) - session.connected_at).total_seconds() * 1000),
                )
                if session is not None
                else 0
            )
            if voice_delivery_service is not None and session is not None:
                voice_delivery_service.clear_pending_input(
                    session_id=session.session_id,
                )
            if auto_stop_task is not None:
                auto_stop_task.cancel()
                with suppress(asyncio.CancelledError):
                    await auto_stop_task
            if idle_controller is not None:
                idle_controller.cancel()
            if audio_rms_last is not None and session is not None:
                logger.info(
                    "device_ws_audio_metrics device=%s session=%s frames=%s "
                    "rms_min=%.1f rms_max=%.1f rms_last=%.1f",
                    redact_device_id(device_id),
                    session.session_id,
                    session.audio_frames_received,
                    audio_rms_min,
                    audio_rms_max,
                    audio_rms_last,
                )
            if outbound_sender is not None:
                outbound_sender.cancel()
                with suppress(asyncio.CancelledError):
                    await outbound_sender
            if keepalive_sender is not None:
                keepalive_sender.cancel()
                with suppress(asyncio.CancelledError):
                    await keepalive_sender
            if session is not None:
                transport.unregister(session.session_id)
                device_sessions.disconnect(session)
            logger.info(
                "device_ws_closed device=%s session=%s frames=%s "
                "phase_before_close=%s close_code=%s reason_length=%s "
                "duration_ms=%s mode=%s",
                redact_device_id(device_id),
                session.session_id if session is not None else "none",
                session.audio_frames_received if session is not None else 0,
                phase_before_close,
                close_code,
                close_reason_length,
                duration_ms,
                session.listening_mode if session is not None else None,
            )

    @app.post("/v1/tasks")
    def create_task(command: TaskCreate, request: Request) -> JSONResponse:
        task, created = service.create_task(
            command,
            trace_id=_request_trace_id(request),
        )
        return JSONResponse(
            status_code=201 if created else 200,
            content=jsonable_encoder({"created": created, "task": task}),
        )

    @app.get("/v1/tasks/{task_id}")
    def get_task(task_id: str) -> dict[str, object]:
        task = service.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        return {"task": task, "events": service.get_events(task_id)}

    def apply_event(
        task_id: str,
        event_type: TaskEventType,
        body: EventRequest,
        request: Request,
    ) -> dict[str, object]:
        try:
            task, event = service.record_event(
                task_id,
                event_type,
                reason=body.reason,
                trace_id=_request_trace_id(request),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="task not found") from exc
        except InvalidTaskTransition as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"task": task, "event": event}

    @app.post("/v1/tasks/{task_id}/ack")
    def acknowledge_task(
        task_id: str,
        body: EventRequest,
        request: Request,
    ) -> dict[str, object]:
        return apply_event(
            task_id,
            TaskEventType.ACKNOWLEDGED,
            body,
            request,
        )

    @app.post("/v1/tasks/{task_id}/confirm")
    def confirm_task(
        task_id: str,
        body: EventRequest,
        request: Request,
    ) -> dict[str, object]:
        return apply_event(
            task_id,
            TaskEventType.CONFIRMED,
            body,
            request,
        )

    @app.post("/v1/tasks/{task_id}/reject")
    def reject_task(
        task_id: str,
        body: EventRequest,
        request: Request,
    ) -> dict[str, object]:
        return apply_event(
            task_id,
            TaskEventType.REJECTED,
            body,
            request,
        )

    @app.post("/v1/tasks/{task_id}/cancel")
    def cancel_task(
        task_id: str,
        body: EventRequest,
        request: Request,
    ) -> dict[str, object]:
        return apply_event(
            task_id,
            TaskEventType.CANCELLED,
            body,
            request,
        )

    @app.post("/v1/medication/plans")
    def create_medication_plan(
        plan: MedicationPlanCreate,
        request: Request,
    ) -> JSONResponse:
        if not settings.feishu_configured:
            raise HTTPException(
                status_code=503,
                detail="Feishu fallback is not configured",
            )
        created_plan, created = service.create_medication_plan(
            plan,
            trace_id=_request_trace_id(request),
        )
        return JSONResponse(
            status_code=201 if created else 200,
            content=jsonable_encoder({"created": created, "plan": created_plan}),
        )

    @app.get("/v1/medication/plans")
    def list_medication_plans() -> dict[str, object]:
        return {"plans": service.list_medication_plans()}

    @app.post("/v1/medication/plans/{plan_id}/disable")
    def disable_medication_plan(
        plan_id: str,
        body: MedicationOwnershipRequest,
        request: Request,
    ) -> dict[str, object]:
        try:
            plan = medication_service.disable_plan(
                plan_id,
                actor_id=body.actor_id,
                target_device_id=body.target_device_id,
                occurred_at=datetime.now(UTC),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="medication plan not found") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="medication plan ownership mismatch") from exc
        return {"plan": plan, "trace_id": _request_trace_id(request)}

    @app.get("/v1/medication/occurrences")
    def list_medication_occurrences() -> dict[str, object]:
        return {"occurrences": service.list_medication_occurrences()}

    @app.post("/v1/medication/occurrences/{occurrence_id}/ack")
    def acknowledge_medication_occurrence(
        occurrence_id: str,
        body: MedicationOwnershipRequest,
        request: Request,
    ) -> dict[str, object]:
        try:
            occurrence = medication_service.acknowledge_occurrence(
                occurrence_id,
                actor_id=body.actor_id,
                target_device_id=body.target_device_id,
                occurred_at=datetime.now(UTC),
                trace_id=_request_trace_id(request),
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail="medication occurrence not found",
            ) from exc
        except PermissionError as exc:
            raise HTTPException(
                status_code=403,
                detail="medication occurrence ownership mismatch",
            ) from exc
        except (ValueError, InvalidTaskTransition) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"occurrence": occurrence}

    @app.post("/v1/memory/confirm")
    def confirm_memory(
        body: MemoryConfirmRequest,
        request: Request,
    ) -> JSONResponse:
        try:
            memory = memory_service.confirm(
                MemoryCandidate(
                    memory_id=body.memory_id,
                    subject_id=body.subject_id,
                    category=body.category,
                    value=body.value,
                    confirmed=body.confirmed,
                ),
                source=_request_trace_id(request),
            )
        except MemoryFeatureDisabled as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except MemoryConsentRequired as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except MemoryOwnershipError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except MemoryQuotaExceeded as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return JSONResponse(
            status_code=200,
            content=jsonable_encoder({"memory": memory}),
        )

    @app.get("/v1/memory")
    def list_memory(
        subject_id: Identifier,
        query: str | None = Query(default=None, max_length=2000),
        limit: int = Query(default=20, ge=1, le=50),
    ) -> dict[str, object]:
        try:
            memories = memory_service.list(
                subject_id=subject_id,
                query=query,
                limit=limit,
            )
        except MemoryFeatureDisabled as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"memories": memories}

    @app.get("/v1/memory/export")
    def export_memory(subject_id: Identifier) -> dict[str, object]:
        try:
            memories = memory_service.export(subject_id=subject_id)
        except MemoryFeatureDisabled as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"memories": memories}

    @app.get("/v1/memory/proposals")
    def list_memory_proposals(subject_id: Identifier) -> dict[str, object]:
        try:
            proposals = memory_service.list_proposals(subject_id=subject_id)
        except MemoryFeatureDisabled as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"proposals": proposals}

    @app.post("/v1/memory/proposals/{proposal_id}/confirm")
    def confirm_memory_proposal(
        proposal_id: Identifier,
        body: MemoryProposalConfirmRequest,
        request: Request,
    ) -> JSONResponse:
        try:
            memory = memory_service.confirm_proposal(
                subject_id=body.subject_id,
                proposal_id=proposal_id,
                source=_request_trace_id(request),
            )
        except MemoryFeatureDisabled as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except MemoryNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except MemoryQuotaExceeded as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return JSONResponse(
            status_code=200,
            content=jsonable_encoder({"memory": memory}),
        )

    @app.delete("/v1/memory/proposals/{proposal_id}")
    def reject_memory_proposal(
        proposal_id: Identifier,
        subject_id: Identifier,
    ) -> dict[str, bool]:
        try:
            deleted = memory_service.reject_proposal(
                subject_id=subject_id,
                proposal_id=proposal_id,
            )
        except MemoryFeatureDisabled as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if not deleted:
            raise HTTPException(status_code=404, detail="memory proposal not found")
        return {"deleted": True}

    @app.delete("/v1/memory/{memory_id}")
    def delete_memory(memory_id: Identifier, subject_id: Identifier) -> dict[str, bool]:
        try:
            deleted = memory_service.delete(
                subject_id=subject_id,
                memory_id=memory_id,
            )
        except MemoryFeatureDisabled as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if not deleted:
            raise HTTPException(status_code=404, detail="memory not found")
        return {"deleted": True}

    return app


def create_default_app() -> FastAPI:
    load_environment_file(LOCAL_ENV_PATH)
    settings = Settings.from_environment()
    transport = DeviceTransport()
    voice_delivery_service = None
    agent_text_runtime = None
    feishu_text_runtime = None
    feishu_chat_listener_factory = None
    if settings.voice_runtime == "fixture":
        if settings.fake_voice_fixture_path is None:
            raise ValueError("fixture voice runtime requires a fixture path")
        voice_delivery_service = create_fixture_voice_delivery(
            fixture_path=settings.fake_voice_fixture_path,
            device_transport=transport,
            queue_capacity=settings.audio_queue_capacity,
        )
    elif settings.voice_runtime == "http":
        if settings.minicpm_o_endpoint is None:
            raise ValueError("http voice runtime requires a MiniCPM-o endpoint")
        voice_delivery_service = create_voice_delivery(
            model_runtime=MinicpmOHttpRuntime(
                endpoint=settings.minicpm_o_endpoint,
                auth_token=settings.minicpm_o_auth_token,
                timeout_seconds=settings.minicpm_o_timeout_seconds,
                max_retries=settings.minicpm_o_max_retries,
                retry_backoff_seconds=settings.minicpm_o_retry_backoff_seconds,
            ),
            device_transport=transport,
            model_sample_rate=16_000,
            response_sample_rate=24_000,
            queue_capacity=settings.audio_queue_capacity,
        )
    elif settings.voice_runtime == "realtime":
        if settings.minicpm_o_endpoint is None:
            raise ValueError(
                "realtime voice runtime requires a MiniCPM-o endpoint"
            )
        voice_delivery_service = create_voice_delivery(
            model_runtime=MinicpmORealtimeRuntime(
                endpoint=settings.minicpm_o_endpoint,
                auth_token=settings.minicpm_o_auth_token,
                timeout_seconds=settings.minicpm_o_timeout_seconds,
            ),
            device_transport=transport,
            model_sample_rate=16_000,
            response_sample_rate=24_000,
            queue_capacity=settings.audio_queue_capacity,
        )
    elif settings.voice_runtime == "mimo":
        if settings.mimo_api_key is None:
            raise ValueError("mimo voice runtime requires COMPANION_MIMO_API_KEY")
        voice_delivery_service = create_voice_delivery(
            model_runtime=MimoV25Runtime(
                openai_base_url=settings.mimo_openai_base_url,
                api_key=settings.mimo_api_key,
                model=settings.mimo_model,
                tts_model=settings.mimo_tts_model,
                tts_voice=settings.mimo_tts_voice,
                timeout_seconds=settings.mimo_timeout_seconds,
                max_retries=settings.mimo_max_retries,
                retry_backoff_seconds=settings.mimo_retry_backoff_seconds,
                memory_proposals_enabled=settings.memory_enabled,
            ),
            device_transport=transport,
            model_sample_rate=16_000,
            response_sample_rate=24_000,
            queue_capacity=settings.audio_queue_capacity,
        )
    if settings.dynamic_agents_enabled:
        if settings.mimo_api_key is None:
            raise ValueError("text Agent runtime requires COMPANION_MIMO_API_KEY")
        agent_text_runtime = MimoTextChatRuntime(
            openai_base_url=settings.mimo_openai_base_url,
            api_key=settings.mimo_api_key,
            model=settings.mimo_model,
            timeout_seconds=settings.mimo_timeout_seconds,
            max_retries=settings.mimo_max_retries,
            retry_backoff_seconds=settings.mimo_retry_backoff_seconds,
            system_prompt=AGENT_COMPILER_SYSTEM_PROMPT,
        )
    if settings.feishu_chat_enabled:
        if (
            settings.feishu_app_id is None
            or settings.feishu_app_secret is None
            or settings.feishu_receiver_open_id is None
            or settings.mimo_api_key is None
        ):
            raise ValueError("Feishu chat settings are incomplete")
        feishu_text_runtime = MimoTextChatRuntime(
            openai_base_url=settings.mimo_openai_base_url,
            api_key=settings.mimo_api_key,
            model=settings.mimo_model,
            timeout_seconds=settings.mimo_timeout_seconds,
            max_retries=settings.mimo_max_retries,
            retry_backoff_seconds=settings.mimo_retry_backoff_seconds,
        )

        def feishu_chat_listener_factory(agent_router):
            return create_feishu_chat_listener(
                app_id=settings.feishu_app_id,
                app_secret=settings.feishu_app_secret,
                owner_open_id=settings.feishu_receiver_open_id,
                runtime=feishu_text_runtime,
                history_turns=settings.feishu_chat_history_turns,
                startup_timeout_seconds=(
                    settings.feishu_chat_startup_timeout_seconds
                ),
                base_url=settings.feishu_base_url,
                agent_router=agent_router,
            )

    return create_app(
        settings,
        device_transport=transport,
        voice_delivery_service=voice_delivery_service,
        feishu_chat_listener_factory=feishu_chat_listener_factory,
        agent_text_runtime=agent_text_runtime,
    )

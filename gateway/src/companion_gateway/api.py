import asyncio
import json
import logging
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError
from starlette.websockets import WebSocketDisconnect

from companion_gateway.audio.bridge import AudioFrameRejected, AudioQueueFull
from companion_gateway.agent.service import (
    AgentToolNotAllowed,
    AgentToolRequest,
    AgentToolService,
    AgentToolTimeout,
)
from companion_gateway.device.events import (
    BoundedDeviceEventSink,
    DeviceBackpressure,
)
from companion_gateway.device.models import (
    AbortControl,
    DeviceHello,
    ListenControl,
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
    OutboundTask,
)
from companion_gateway.domain.memory import MemoryCandidate, MemoryCategory, utc_now
from companion_gateway.domain.executor import TaskDeliveryAttempt, TaskExecutor
from companion_gateway.domain.medication import MedicationPlanCreate
from companion_gateway.domain.models import (
    ContentText,
    Identifier,
    TaskCreate,
    TaskRecord,
)
from companion_gateway.domain.scheduler import TaskScheduler
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
from companion_gateway.vision.scheduler import VisionScheduler
from companion_gateway.vision.service import (
    VisionConsentRequired,
    VisionDuplicateTurn,
    VisionFeatureDisabled,
    VisionObservationService,
    VisionQuotaExceeded,
    VisionTooLarge,
    VisionUnsupportedType,
)
from companion_gateway.notifications.feishu import FeishuNotifier
from companion_gateway.domain.tasks import InvalidTaskTransition, TaskEventType
from companion_gateway.service import TaskService
from companion_gateway.settings import Settings, load_environment_file
from companion_gateway.storage.sqlite import SQLiteTaskRepository
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


class UnsupportedDeviceControl(ValueError):
    pass


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
if not logger.handlers:
    logger.addHandler(logging.StreamHandler())
logger.propagate = False
LOCAL_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"


def _request_trace_id(request: Request) -> str:
    return request.state.trace_id


def create_app(
    settings: Settings,
    *,
    device_event_sink: BoundedDeviceEventSink | None = None,
    device_transport: DeviceTransport | None = None,
    voice_delivery_service: DeviceVoiceDeliveryService | None = None,
    medication_notifier: MedicationNotifier | None = None,
    memory_clock: Callable[[], datetime] = utc_now,
    vision_clock: Callable[[], datetime] = utc_now,
    agent_clock: Callable[[], datetime] = utc_now,
) -> FastAPI:
    repository = SQLiteTaskRepository(settings.database_path)
    repository.initialize()
    service = TaskService(repository)
    task_executor = TaskExecutor(service)
    device_sessions = DeviceSessionRegistry()
    device_authenticator = DeviceAuthenticator(settings.device_token_hashes)
    transport = device_transport or DeviceTransport()
    sink = device_event_sink or BoundedDeviceEventSink()

    def deliver_task(task: TaskRecord) -> TaskDeliveryAttempt:
        session = device_sessions.get(task.target_device_id)
        if session is None:
            logger.info(
                "task_delivery_failed device=%s task=%s reason=device_offline",
                redact_device_id(task.target_device_id),
                task.task_id,
            )
            return TaskDeliveryAttempt.failed("device_offline")
        try:
            if medication_service.is_medication_task(task.task_id):
                if voice_delivery_service is None:
                    logger.info(
                        "task_delivery_failed device=%s task=%s "
                        "reason=voice_synthesis_unavailable",
                        redact_device_id(task.target_device_id),
                        task.task_id,
                    )
                    return TaskDeliveryAttempt.failed("voice_synthesis_unavailable")
                voice_delivery_service.synthesize_and_send(
                    session_id=session.session_id,
                    text=task.payload.text,
                )
                logger.info(
                    "medication_voice_enqueued device=%s task=%s",
                    redact_device_id(task.target_device_id),
                    task.task_id,
                )
            else:
                transport.send_task(session.session_id, task)
        except DeviceNotConnected:
            logger.info(
                "task_delivery_failed device=%s task=%s reason=device_offline",
                redact_device_id(task.target_device_id),
                task.task_id,
            )
            return TaskDeliveryAttempt.failed("device_offline")
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
            return TaskDeliveryAttempt.failed("voice_synthesis_failed")
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
    agent_tool_service = AgentToolService(
        task_service=service,
        task_executor=task_executor,
        clock=agent_clock,
    )
    medication_scheduler = MedicationScheduler(
        service=medication_service,
        interval_seconds=settings.task_scheduler_interval_seconds,
    )
    app = FastAPI(title="XiaoYao Voice Gateway", version="0.1.0")
    app.state.repository = repository
    app.state.service = service
    app.state.task_executor = task_executor
    app.state.device_sessions = device_sessions
    app.state.device_event_sink = sink
    app.state.device_transport = transport
    app.state.voice_delivery_service = voice_delivery_service
    app.state.task_scheduler = task_scheduler
    app.state.medication_service = medication_service
    app.state.medication_scheduler = medication_scheduler
    app.state.medication_notifier = medication_notifier
    app.state.memory_service = memory_service
    app.state.memory_scheduler = memory_scheduler
    app.state.vision_service = vision_service
    app.state.vision_scheduler = vision_scheduler
    app.state.agent_tool_service = agent_tool_service
    if voice_delivery_service is not None:
        voice_delivery_service.set_task_executor(task_executor)
        voice_delivery_service.set_medication_service(medication_service)
        voice_delivery_service.set_memory_service(memory_service)
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

    @app.get("/v1/devices/{device_id}/status")
    def device_status(device_id: Identifier) -> dict[str, object]:
        return {"device": jsonable_encoder(device_sessions.status(device_id))}

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
        outbound_sender: asyncio.Task[None] | None = None
        close_code: int | None = None
        close_reason_length = 0
        trace_id = f"trc_{uuid4().hex}"
        logger.info("device_ws_accepted device=%s", redact_device_id(device_id))
        try:
            raw_hello = await asyncio.wait_for(
                websocket.receive_json(),
                timeout=settings.device_hello_timeout_seconds,
            )
            hello = DeviceHello.model_validate(raw_hello)
            session = DeviceSession.create(
                device_id=device_id,
                client_id=client_id,
                hello=hello,
            )
            logger.info(
                "device_ws_hello device=%s session=%s audio=%s/%s/%s/%sms",
                redact_device_id(device_id),
                session.session_id,
                hello.audio_params.format,
                hello.audio_params.sample_rate,
                hello.audio_params.channels,
                hello.audio_params.frame_duration,
            )
            previous = device_sessions.connect(session)
            if previous is not None:
                previous.close()
                transport.unregister(previous.session_id)
            await websocket.send_json(server_hello(session.session_id))
            transport.register(session.session_id)

            async def forward_outbound() -> None:
                while True:
                    message = await transport.next_outbound(session.session_id)
                    if isinstance(message, OutboundTask):
                        await websocket.send_json(
                            {
                                "type": "task",
                                "state": "notify",
                                "session_id": message.session_id,
                                "task": jsonable_encoder(message.task),
                            }
                        )
                        continue
                    await websocket.send_json(
                        {
                            "type": "tts",
                            "state": "start",
                            "session_id": message.session_id,
                        }
                    )
                    for opus_frame in message.opus_frames:
                        await websocket.send_bytes(opus_frame)
                    await websocket.send_json(
                        {
                            "type": "tts",
                            "state": "stop",
                            "session_id": message.session_id,
                        }
                    )

            outbound_sender = asyncio.create_task(forward_outbound())

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
                        if message_type == "listen":
                            control = ListenControl.model_validate(payload)
                            session.apply_listen(control)
                        elif message_type == "abort":
                            control = AbortControl.model_validate(payload)
                            session.apply_abort(control)
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
                        if (
                            message_type == "listen"
                            and control.state == "stop"
                            and voice_delivery_service is not None
                        ):
                            try:
                                await voice_delivery_service.process_and_send_async(
                                    session_id=session.session_id,
                                    target_device_id=session.device_id,
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
                                break
                        elif (
                            message_type == "abort"
                            and voice_delivery_service is not None
                        ):
                            voice_delivery_service.clear_pending_input(
                                session_id=session.session_id,
                            )
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
                    if voice_delivery_service is not None:
                        voice_delivery_service.accept_and_send(
                            session_id=session.session_id,
                            opus_frame=audio_frame,
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
                except AudioFrameRejected:
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
            if outbound_sender is not None:
                outbound_sender.cancel()
                with suppress(asyncio.CancelledError):
                    await outbound_sender
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
    return create_app(
        settings,
        device_transport=transport,
        voice_delivery_service=voice_delivery_service,
    )

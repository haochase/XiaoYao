import asyncio
import json
from contextlib import suppress
from typing import Annotated
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError
from starlette.websockets import WebSocketDisconnect

from companion_gateway.audio.bridge import AudioFrameRejected, AudioQueueFull
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
)
from companion_gateway.device.transport import (
    DeviceNotConnected,
    DeviceOutboundBackpressure,
    DeviceTransport,
    OutboundTask,
)
from companion_gateway.domain.executor import TaskExecutor
from companion_gateway.domain.models import TaskCreate, TaskRecord
from companion_gateway.domain.scheduler import TaskScheduler
from companion_gateway.domain.tasks import InvalidTaskTransition, TaskEventType
from companion_gateway.service import TaskService
from companion_gateway.settings import Settings
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


Reason = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=256),
]


class EventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Reason


class UnsupportedDeviceControl(ValueError):
    pass


def _request_trace_id(request: Request) -> str:
    return request.state.trace_id


def create_app(
    settings: Settings,
    *,
    device_event_sink: BoundedDeviceEventSink | None = None,
    device_transport: DeviceTransport | None = None,
    voice_delivery_service: DeviceVoiceDeliveryService | None = None,
) -> FastAPI:
    repository = SQLiteTaskRepository(settings.database_path)
    repository.initialize()
    service = TaskService(repository)
    task_executor = TaskExecutor(service)
    device_sessions = DeviceSessionRegistry()
    device_authenticator = DeviceAuthenticator(settings.device_token_hashes)
    transport = device_transport or DeviceTransport()
    sink = device_event_sink or BoundedDeviceEventSink()

    def deliver_task(task: TaskRecord) -> bool:
        session = device_sessions.get(task.target_device_id)
        if session is None:
            return False
        try:
            transport.send_task(session.session_id, task)
        except (DeviceNotConnected, DeviceOutboundBackpressure):
            return False
        return True

    task_scheduler = TaskScheduler(
        executor=task_executor,
        deliver=deliver_task,
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
    if voice_delivery_service is not None:
        voice_delivery_service.set_task_executor(task_executor)
    if settings.task_scheduler_enabled:
        app.add_event_handler("startup", task_scheduler.start)
        app.add_event_handler("shutdown", task_scheduler.stop)

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
        trace_id = f"trc_{uuid4().hex}"
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
                        sink.on_control(session, control)
                        if (
                            message_type == "listen"
                            and control.state == "stop"
                            and voice_delivery_service is not None
                        ):
                            try:
                                await voice_delivery_service.process_and_send_async(
                                    session_id=session.session_id,
                                )
                            except ModelRuntimeError:
                                voice_delivery_service.clear_pending_input()
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
                            voice_delivery_service.clear_pending_input()
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
        except (asyncio.TimeoutError, json.JSONDecodeError, ValidationError):
            await reject_websocket(websocket, 1003)
        except WebSocketDisconnect:
            pass
        finally:
            if voice_delivery_service is not None:
                voice_delivery_service.clear_pending_input()
            if outbound_sender is not None:
                outbound_sender.cancel()
                with suppress(asyncio.CancelledError):
                    await outbound_sender
            if session is not None:
                transport.unregister(session.session_id)
                device_sessions.disconnect(session)

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

    return app


def create_default_app() -> FastAPI:
    settings = Settings.from_environment()
    transport = DeviceTransport()
    voice_delivery_service = None
    if settings.voice_runtime == "fixture":
        if settings.fake_voice_fixture_path is None:
            raise ValueError("fixture voice runtime requires a fixture path")
        voice_delivery_service = create_fixture_voice_delivery(
            fixture_path=settings.fake_voice_fixture_path,
            device_transport=transport,
        )
    elif settings.voice_runtime == "http":
        if settings.minicpm_o_endpoint is None:
            raise ValueError("http voice runtime requires a MiniCPM-o endpoint")
        voice_delivery_service = create_voice_delivery(
            model_runtime=MinicpmOHttpRuntime(
                endpoint=settings.minicpm_o_endpoint,
                auth_token=settings.minicpm_o_auth_token,
                timeout_seconds=settings.minicpm_o_timeout_seconds,
            ),
            device_transport=transport,
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
        )
    return create_app(
        settings,
        device_transport=transport,
        voice_delivery_service=voice_delivery_service,
    )

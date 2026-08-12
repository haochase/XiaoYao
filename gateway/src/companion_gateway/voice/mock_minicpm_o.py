"""Development-only MiniCPM-o service mock for gateway contract tests."""

import base64
import binascii
import struct
from typing import Literal

from fastapi import FastAPI, HTTPException, WebSocket
from pydantic import BaseModel, ConfigDict


_RESPONSE_TEXT = "I am here."
_HTTP_PCM = b"\x02\x00" * 1_440
_REALTIME_PCM = struct.pack("<1440f", *([0.0] * 1_440))


class HttpTurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sample_rate: Literal[16_000]
    channels: Literal[1]
    format: Literal["pcm_s16le"]
    audio_base64: str


app = FastAPI(title="MiniCPM-o Development Mock")


def _require_base64(value: str, *, field: str) -> None:
    try:
        if not base64.b64decode(value, validate=True):
            raise ValueError("empty")
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail=f"{field} must contain non-empty base64 audio",
        ) from exc


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/infer")
def infer(request: HttpTurnRequest) -> dict[str, object]:
    _require_base64(request.audio_base64, field="audio_base64")
    return {
        "text": _RESPONSE_TEXT,
        "sample_rate": 24_000,
        "channels": 1,
        "format": "pcm_s16le",
        "audio_base64": base64.b64encode(_HTTP_PCM).decode("ascii"),
    }


@app.websocket("/v1/realtime")
async def realtime(websocket: WebSocket) -> None:
    await websocket.accept()
    await websocket.send_json({"type": "session.queue_done"})
    init = await websocket.receive_json()
    if not isinstance(init, dict) or init.get("type") != "session.init":
        await websocket.close(code=1003)
        return

    await websocket.send_json(
        {"type": "session.created", "session_id": "mock-session"}
    )
    turn = await websocket.receive_json()
    if not isinstance(turn, dict) or not isinstance(turn.get("input"), dict):
        await websocket.close(code=1003)
        return
    audio = turn["input"].get("audio")
    if turn.get("type") != "input.append" or not isinstance(audio, str):
        await websocket.close(code=1003)
        return
    try:
        if not base64.b64decode(audio, validate=True):
            raise ValueError("empty")
    except (binascii.Error, ValueError):
        await websocket.close(code=1003)
        return

    await websocket.send_json(
        {
            "type": "response.output.delta",
            "kind": "text",
            "text": _RESPONSE_TEXT,
        }
    )
    await websocket.send_json(
        {
            "type": "response.output.delta",
            "kind": "audio",
            "audio": base64.b64encode(_REALTIME_PCM).decode("ascii"),
        }
    )
    await websocket.send_json({"type": "session.closed"})
    await websocket.receive_json()
    await websocket.close()

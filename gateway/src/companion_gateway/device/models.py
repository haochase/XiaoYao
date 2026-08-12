from typing import Literal

from pydantic import BaseModel, ConfigDict


class AudioParameters(BaseModel):
    model_config = ConfigDict(extra="allow")

    format: Literal["opus"]
    sample_rate: Literal[16000]
    channels: Literal[1]
    frame_duration: Literal[60]


class DeviceHello(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: Literal["hello"]
    version: Literal[1]
    transport: Literal["websocket"]
    audio_params: AudioParameters


class ListenControl(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: Literal["listen"]
    state: Literal["start", "stop", "detect"]
    mode: Literal["auto", "manual", "realtime"] | None = None
    session_id: str | None = None
    text: str | None = None


class AbortControl(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: Literal["abort"]
    session_id: str | None = None
    reason: str | None = None


DeviceControl = ListenControl | AbortControl


def server_hello(session_id: str) -> dict[str, object]:
    return {
        "type": "hello",
        "version": 1,
        "transport": "websocket",
        "session_id": session_id,
        "audio_params": {
            "format": "opus",
            "sample_rate": 24000,
            "channels": 1,
            "frame_duration": 60,
        },
    }

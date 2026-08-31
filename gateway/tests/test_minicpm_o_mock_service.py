import base64
import struct

from fastapi.testclient import TestClient

from companion_gateway.voice.mock_minicpm_o import app


def test_mock_minicpm_o_http_contract_returns_24khz_pcm() -> None:
    client = TestClient(app)
    response = client.post(
        "/v1/infer",
        json={
            "sample_rate": 16_000,
            "channels": 1,
            "format": "pcm_s16le",
            "audio_base64": base64.b64encode(b"\x01\x00" * 960).decode(),
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["text"] == "I am here."
    assert body["sample_rate"] == 24_000
    assert body["channels"] == 1
    assert body["format"] == "pcm_s16le"
    assert len(base64.b64decode(body["audio_base64"])) == 1_440 * 2


def test_mock_minicpm_o_realtime_contract_emits_audio_turn() -> None:
    client = TestClient(app)
    with client.websocket_connect("/v1/realtime?mode=audio") as websocket:
        assert websocket.receive_json() == {"type": "session.queue_done"}
        websocket.send_json({"type": "session.init", "payload": {}})
        assert websocket.receive_json() == {
            "type": "session.created",
            "session_id": "mock-session",
        }
        websocket.send_json(
            {
                "type": "input.append",
                "input": {
                    "audio": base64.b64encode(
                        struct.pack("<3f", 0.1, 0.0, -0.1)
                    ).decode(),
                    "force_listen": False,
                },
            }
        )

        text = websocket.receive_json()
        audio = websocket.receive_json()
        closed = websocket.receive_json()
        websocket.send_json({"type": "session.close", "reason": "user_stop"})

    assert text == {
        "type": "response.output.delta",
        "kind": "text",
        "text": "I am here.",
    }
    assert audio["type"] == "response.output.delta"
    assert audio["kind"] == "audio"
    assert len(base64.b64decode(audio["audio"])) == 1_440 * 4
    assert closed == {"type": "session.closed"}

import pytest

import app as luna_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(luna_app, "DB_PATH", tmp_path / "luna_chat.db")
    monkeypatch.setattr(luna_app, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(luna_app, "AUDIO_CACHE_DIR", tmp_path / "data" / "audio_cache")
    monkeypatch.setattr(luna_app, "EXPORT_DIR", tmp_path / "data" / "exports")
    luna_app.init_db()
    luna_app.app.config.update(TESTING=True)
    return luna_app.app.test_client()


@pytest.mark.parametrize("value", ["fast", None, True, float("nan"), float("inf")])
def test_chat_rejects_invalid_response_length(client, value):
    response = client.post("/chat", json={"message": "hello", "response_length": value})

    assert response.status_code == 400
    assert response.is_json
    assert "response_length" in response.json["error"]


@pytest.mark.parametrize("value", [0, 6, 2.5])
def test_chat_rejects_out_of_range_or_fractional_response_length(client, value):
    response = client.post("/chat", json={"message": "hello", "response_length": value})

    assert response.status_code == 400
    assert response.is_json


@pytest.mark.parametrize("query", ["rate=fast", "rate=nan", "rate=0.5", "rate=2"])
def test_audio_export_rejects_invalid_rate_before_message_lookup(client, query):
    response = client.get(f"/api/messages/999/audio?{query}")

    assert response.status_code == 400
    assert response.is_json
    assert "rate" in response.json["error"]


@pytest.mark.parametrize(
    "payload, parameter",
    [
        ({"pacing_cps": "fast"}, "pacing_cps"),
        ({"pacing_cps": 17}, "pacing_cps"),
        ({"pacing_cps": 76}, "pacing_cps"),
        ({"pause_seconds": "long"}, "pause_seconds"),
        ({"pause_seconds": 0}, "pause_seconds"),
        ({"pause_seconds": 2}, "pause_seconds"),
    ],
)
def test_podcast_export_rejects_invalid_numeric_parameters(client, payload, parameter):
    response = client.post("/api/chats/999/podcast", json=payload)

    assert response.status_code == 400
    assert response.is_json
    assert parameter in response.json["error"]


def test_speak_rejects_fractional_message_id(client):
    response = client.post("/speak", json={"message_id": 1.5})

    assert response.status_code == 400
    assert response.is_json
    assert "message_id" in response.json["error"]

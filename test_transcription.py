from io import BytesIO
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import app as luna


class _FakeTranscriptions:
    def __init__(self, text="hello from the microphone"):
        self.text = text
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(text=self.text)


class _FakeClient:
    def __init__(self, text="hello from the microphone"):
        self.audio = SimpleNamespace(transcriptions=_FakeTranscriptions(text))


class TranscriptionEndpointTests(TestCase):
    def setUp(self):
        self.client = luna.app.test_client()

    def test_requires_audio_part(self):
        response = self.client.post("/api/transcribe", data={})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "No audio file received.")

    def test_rejects_empty_audio(self):
        response = self.client.post(
            "/api/transcribe",
            data={"audio": (BytesIO(b""), "recording.webm")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "Empty audio upload.")

    def test_rejects_unsupported_format(self):
        response = self.client.post(
            "/api/transcribe",
            data={"audio": (BytesIO(b"not audio"), "recording.txt")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 415)

    def test_rejects_recording_over_limit_without_reading_unbounded_data(self):
        with patch.object(luna, "MAX_TRANSCRIBE_FILE_SIZE", 3):
            response = self.client.post(
                "/api/transcribe",
                data={"audio": (BytesIO(b"1234"), "recording.webm")},
                content_type="multipart/form-data",
            )
        self.assertEqual(response.status_code, 413)

    def test_returns_sdk_text_and_does_not_force_brittle_response_format(self):
        fake = _FakeClient("  hello Luna  ")
        with patch.object(luna, "get_client", return_value=fake):
            response = self.client.post(
                "/api/transcribe",
                data={"audio": (BytesIO(b"fake-webm"), "recording.webm")},
                content_type="multipart/form-data",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"text": "hello Luna"})
        self.assertEqual(len(fake.audio.transcriptions.calls), 1)
        call = fake.audio.transcriptions.calls[0]
        self.assertEqual(call["model"], luna.TRANSCRIBE_MODEL)
        self.assertNotIn("response_format", call)

    def test_hides_upstream_exception_details(self):
        class BrokenTranscriptions:
            def create(self, **kwargs):
                raise RuntimeError("secret provider detail")

        fake = SimpleNamespace(audio=SimpleNamespace(transcriptions=BrokenTranscriptions()))
        with patch.object(luna, "get_client", return_value=fake):
            response = self.client.post(
                "/api/transcribe",
                data={"audio": (BytesIO(b"fake-webm"), "recording.webm")},
                content_type="multipart/form-data",
            )

        self.assertEqual(response.status_code, 502)
        self.assertNotIn("secret provider detail", response.get_data(as_text=True))

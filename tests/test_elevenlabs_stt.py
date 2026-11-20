"""Tests for ElevenLabs Speech-to-Text (STT) integration, settings persistence, and fallback engine."""
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Setup isolated environment
temp_data = tempfile.mkdtemp(prefix="peakcut-stt-test-")
os.environ["PEAKCUT_DATA"] = temp_data
os.environ["PEAKCUT_ACCOUNTS"] = "0"
os.environ["DATABASE_URL"] = f"sqlite:///{(Path(temp_data) / 'peakcut.db').as_posix()}"

from studio import config, ctx, db, server, stt, transcribe  # noqa: E402

db.init()

fails = []


def check(name, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + name + (f"   {extra}" if extra else ""))
    if not ok:
        fails.append(name)


def run_tests():
    with ctx.as_user("u_admin"):
        # 1. Defaults
        cfg = stt.get_settings()
        check("default stt provider is local", cfg["provider"] == "local")
        pub = stt.public_settings()
        check("public settings masks key", "api_key" not in pub)
        check("public models lists scribe_v1", "scribe_v1" in pub["models"])

        # 2. Save ElevenLabs settings
        saved = stt.save_settings(
            provider="elevenlabs",
            api_key="xi-test-dummy-key-12345",
            model="scribe_v1",
            language="en",
            fallback_to_local=True,
        )
        check("saved provider is elevenlabs", saved["provider"] == "elevenlabs")
        check("saved has_key is true", saved["has_key"] is True)
        check("saved key_hint ends in 2345", saved["key_hint"] == "2345")
        check("saved model is scribe_v1", saved["model"] == "scribe_v1")
        check("saved language is en", saved["language"] == "en")

        # Verify SQLite persistence
        db_cfg = stt._db_get()
        check("persisted in sqlite provider is elevenlabs", db_cfg.get("provider") == "elevenlabs")
        check("persisted in sqlite api_key matches", db_cfg.get("api_key") == "xi-test-dummy-key-12345")

        # Verify JSON file mirror
        f_cfg = json.loads((Path(temp_data) / "stt.json").read_text(encoding="utf-8"))
        check("mirrored to stt.json provider is elevenlabs", f_cfg.get("provider") == "elevenlabs")

        # 3. Key updating without overwriting existing key
        updated = stt.save_settings(
            provider="elevenlabs",
            api_key="",  # empty means keep current key
            model="scribe_v2",
            language="hi",
        )
        check("empty key preserves existing key", updated["has_key"] is True and updated["key_hint"] == "2345")
        check("updated model is scribe_v2", updated["model"] == "scribe_v2")
        check("updated language is hi", updated["language"] == "hi")

        # 4. Audio to WAV conversion
        silence_i16 = np.zeros(16000, dtype=np.int16)
        wav_bytes = stt._audio_to_wav_bytes(silence_i16)
        check("wav bytes header riff", wav_bytes.startswith(b"RIFF"))
        check("wav bytes size matches 1s audio", len(wav_bytes) == 32044)

        # 5. Silence & short audio handling
        check("transcribe returns empty for sub-100ms audio", stt.transcribe_elevenlabs(np.zeros(500, dtype=np.int16)) == [])
        check("transcribe returns empty for pure silence", stt.transcribe_elevenlabs(silence_i16) == [])

        # 6. Test Key Validation Mocking
        # Test 401
        with patch("httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 401
            mock_get.return_value = mock_resp
            try:
                stt.test_elevenlabs_key("bad_key_12345")
                check("test_key raises on 401", False)
            except ValueError as e:
                check("test_key raises on 401", "rejected this key" in str(e))

        # Test 200 Valid
        with patch("httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "subscription": {
                    "tier": "creator",
                    "status": "active",
                    "character_count": 500,
                    "character_limit": 100000,
                }
            }
            mock_get.return_value = mock_resp
            res = stt.test_elevenlabs_key("valid_key_12345")
            check("test_key succeeds on 200", res.get("ok") is True)
            check("test_key returns tier", res.get("tier") == "creator")

        # 7. Mock ElevenLabs Scribe Response Parsing
        sample_audio = (np.sin(np.linspace(0, 500, 32000)) * 10000).astype(np.int16)
        with patch("httpx.post") as mock_post:
            mock_post_resp = MagicMock()
            mock_post_resp.status_code = 200
            mock_post_resp.json.return_value = {
                "text": "Hello world welcome to PeakCut",
                "words": [
                    {"text": "Hello", "start": 0.1, "end": 0.5, "type": "word"},
                    {"text": " ", "start": 0.5, "end": 0.55, "type": "spacing"},
                    {"text": "(applause)", "start": 0.55, "end": 0.9, "type": "audio_event"},
                    {"text": "world,", "start": 0.95, "end": 1.4, "type": "word"},
                    {"text": "welcome", "start": 1.5, "end": 1.9, "type": "word"},
                    {"text": "to", "start": 1.95, "end": 2.1, "type": "word"},
                    {"text": "PeakCut!", "start": 2.15, "end": 2.7, "type": "word"},
                ],
            }
            mock_post.return_value = mock_post_resp
            words = stt.transcribe_elevenlabs(sample_audio, key_override="test_key_12345")
            check("parsed words count excludes spacing & audio_event", len(words) == 5)
            check("first word text is Hello", words[0]["w"] == "Hello")
            check("first word start is 0.1", words[0]["s"] == 0.1)
            check("last word text is PeakCut!", words[-1]["w"] == "PeakCut!")

        # 8. Transcribe Dispatch & Fallback to Local Parakeet
        # Case A: ElevenLabs succeeds
        with patch("studio.stt.transcribe_elevenlabs") as mock_el:
            mock_el.return_value = [{"w": "mocked", "s": 0.0, "e": 0.5}]
            res_words = transcribe.transcribe(sample_audio)
            check("transcribe.transcribe dispatches to elevenlabs when active", res_words == [{"w": "mocked", "s": 0.0, "e": 0.5}])

        # Case B: ElevenLabs throws error, fallback_to_local=True falls back to local Parakeet
        stt.save_settings(provider="elevenlabs", fallback_to_local=True)
        with patch("studio.stt.transcribe_elevenlabs", side_effect=RuntimeError("ElevenLabs quota exceeded")), \
             patch("studio.transcribe._take_now", return_value=MagicMock()), \
             patch("studio.transcribe.decode_pieces", return_value=[(["hello"], [0.2])]):
            res_fallback = transcribe.transcribe(sample_audio)
            check("transcribe.transcribe falls back gracefully on error", isinstance(res_fallback, list))

        # 9. Server API Endpoints
        from fastapi.testclient import TestClient
        client = TestClient(server.app, base_url="http://127.0.0.1:8765")
        origin_hdr = {"Origin": "http://127.0.0.1:8765"}

        # GET /api/stt
        r_get = client.get("/api/stt", headers=origin_hdr)
        check("GET /api/stt returns 200", r_get.status_code == 200, f"status: {r_get.status_code}, body: {r_get.text}")
        data = r_get.json()
        check("GET /api/stt returns provider elevenlabs", data.get("provider") == "elevenlabs")

        # PUT /api/stt
        r_put = client.put("/api/stt", json={
            "provider": "local",
            "model": "scribe_v1",
        }, headers=origin_hdr)
        check("PUT /api/stt returns 200", r_put.status_code == 200)
        check("PUT /api/stt switches provider to local", r_put.json().get("provider") == "local")

        # POST /api/stt/test
        with patch("studio.stt.test_elevenlabs_key", return_value={"ok": True, "tier": "free", "message": "Connected"}):
            r_test = client.post("/api/stt/test", json={"api_key": "some_test_key"}, headers=origin_hdr)
            check("POST /api/stt/test returns 200", r_test.status_code == 200)
            check("POST /api/stt/test returns ok", r_test.json().get("ok") is True)

    print("\n" + ("ALL PASSED" if not fails else f"FAILED: {', '.join(fails)}"))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(run_tests())

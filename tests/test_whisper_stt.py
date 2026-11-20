"""Tests for OpenAI Whisper STT integration, hardware auto-detection, and English/Hindi handling."""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

temp_data = tempfile.mkdtemp(prefix="peakcut-whisper-test-")
os.environ["PEAKCUT_DATA"] = temp_data
os.environ["PEAKCUT_ACCOUNTS"] = "0"
os.environ["PEAKCUT_PUBLIC_URL"] = "http://127.0.0.1:8765"
os.environ["DATABASE_URL"] = f"sqlite:///{(Path(temp_data) / 'peakcut.db').as_posix()}"

from fastapi.testclient import TestClient
from studio import auth, config, ctx, db, server, stt, transcribe

config.ACCOUNTS = False
auth._local = None
db.init()
fails = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"   {detail}" if detail and not ok else ""))
    if not ok:
        fails.append(name)


def run_tests():
    print("Testing Whisper STT Hardware Auto-detection & Transcription...")

    # 1. Hardware auto-detection
    hw = stt.detect_hardware()
    check("Hardware detection returns RAM", hw.get("ram_gb", 0) > 0)
    check("Hardware detection returns CPU cores", hw.get("cpu_cores", 0) > 0)
    check("Hardware detection returns summary", len(hw.get("summary", "")) > 0)
    check("Recommended model is valid", hw.get("recommended_model") in ("tiny", "base", "small", "large-v3-turbo", "large-v3"))
    check("Recommended device is valid", hw.get("recommended_device") in ("cpu", "cuda"))
    check("Recommended compute is valid", hw.get("recommended_compute") in ("int8", "float16", "int8_float16"))

    # 2. Parameter resolution
    m_auto, d_auto, c_auto = stt._resolve_whisper_params("auto", "auto", "auto")
    check("Parameter resolution 'auto' maps to recommended model", m_auto == hw["recommended_model"])
    check("Parameter resolution 'auto' maps to recommended device", d_auto == hw["recommended_device"])

    m_man, d_man, c_man = stt._resolve_whisper_params("large-v3-turbo", "cpu", "int8")
    check("Manual model override is preserved", m_man == "large-v3-turbo")
    check("Manual device override is preserved", d_man == "cpu")
    check("Manual compute override is preserved", c_man == "int8")

    # 3. Model lists & languages
    check("WHISPER_MODELS includes auto, small, large-v3-turbo", any(m["id"] == "auto" for m in stt.WHISPER_MODELS) and any(m["id"] == "small" for m in stt.WHISPER_MODELS) and any(m["id"] == "large-v3-turbo" for m in stt.WHISPER_MODELS))
    check("WHISPER_LANGUAGES includes auto, en, hi", any(l["id"] == "auto" for l in stt.WHISPER_LANGUAGES) and any(l["id"] == "en" for l in stt.WHISPER_LANGUAGES) and any(l["id"] == "hi" for l in stt.WHISPER_LANGUAGES))

    # 4. Settings save & persistence
    res = stt.save_settings(
        provider="whisper",
        model="small",
        language="hi",
        fallback_to_local=True,
        extra_config={
            "whisper_device": "cpu",
            "whisper_compute_type": "int8",
            "hf_token": "hf_testtoken12345",
        },
    )
    check("Saved provider is whisper", res.get("provider") == "whisper")
    check("Saved model is small", res.get("model") == "small")
    check("Saved language is hi", res.get("language") == "hi")
    check("Hardware info included in public settings", "hardware" in res)
    check("Whisper models list included in public settings", len(res.get("whisper_models", [])) >= 5)
    check("Saved hf_token reported in public settings", res.get("has_hf_token") is True)
    check("Saved hf_token_hint is correct", res.get("hf_token_hint") == "hf_t...2345")
    check("get_hf_token returns saved token", stt.get_hf_token() == "hf_testtoken12345")

    # Verify retrieval
    cur = stt.get_settings()
    check("Retrieved provider is whisper", cur.get("provider") == "whisper")
    check("Retrieved model is small", cur.get("model") == "small")
    check("Retrieved language is hi", cur.get("language") == "hi")
    check("Retrieved extra_config contains whisper_device", cur.get("extra_config", {}).get("whisper_device") == "cpu")
    check("Retrieved extra_config contains hf_token", cur.get("extra_config", {}).get("hf_token") == "hf_testtoken12345")

    # 5. REST API verification
    client = TestClient(
        server.app,
        base_url="http://127.0.0.1:8765",
        headers={"Origin": "http://127.0.0.1:8765", "Host": "127.0.0.1:8765"},
    )
    get_res = client.get("/api/stt")
    check("GET /api/stt returns 200", get_res.status_code == 200)
    data = get_res.json()
    check("GET /api/stt returns provider=whisper", data.get("provider") == "whisper")
    check("GET /api/stt returns hardware info", "hardware" in data)
    check("GET /api/stt returns has_hf_token=True", data.get("has_hf_token") is True)

    # Test PUT /api/stt
    put_res = client.put(
        "/api/stt",
        json={
            "provider": "whisper",
            "model": "auto",
            "language": "auto",
            "fallback_to_local": True,
            "extra_config": {"whisper_device": "auto", "whisper_compute_type": "auto", "hf_token": "hf_updatedtoken9876"},
        },
    )
    check("PUT /api/stt returns 200", put_res.status_code == 200)
    put_data = put_res.json()
    check("Updated model is auto", put_data.get("model") == "auto")
    check("Updated language is auto", put_data.get("language") == "auto")
    check("Updated hf_token reported", put_data.get("has_hf_token") is True)

    # Test POST /api/stt/hf/test with mock response
    mock_hf_resp = MagicMock()
    mock_hf_resp.status_code = 200
    mock_hf_resp.json.return_value = {"name": "peakcut_creator", "type": "user"}
    with patch("httpx.Client.get", return_value=mock_hf_resp):
        hf_test_res = client.post("/api/stt/hf/test", json={"hf_token": "hf_valid_token_12345"})
        check("POST /api/stt/hf/test returns 200", hf_test_res.status_code == 200)
        hf_body = hf_test_res.json()
        check("Hugging Face user authenticated", hf_body.get("username") == "peakcut_creator")

    # 6. Whisper model download tracking & on-disk status
    m_status = stt.get_whisper_models_status()
    check("get_whisper_models_status returns list", isinstance(m_status, list) and len(m_status) >= 5)
    tiny_st = next((m for m in m_status if m["id"] == "tiny"), None)
    check("tiny model status is present", tiny_st is not None and "downloaded" in tiny_st)

    dl_stat = stt.get_download_status()
    check("get_download_status returns active field", "active" in dl_stat)
    check("get_download_status returns percent field", "percent" in dl_stat)
    check("get_download_status returns models list", "models" in dl_stat and len(dl_stat["models"]) >= 5)

    # Test GET /api/stt/whisper/download endpoint
    dl_get_res = client.get("/api/stt/whisper/download")
    check("GET /api/stt/whisper/download returns 200", dl_get_res.status_code == 200)
    dl_get_data = dl_get_res.json()
    check("GET /api/stt/whisper/download has status key", "status" in dl_get_data)
    check("GET /api/stt/whisper/download has models list", "models" in dl_get_data)

    # Test POST /api/stt/whisper/download with mock download
    with patch("studio.stt.download_whisper_model", return_value="dummy_path"):
        dl_post_res = client.post("/api/stt/whisper/download", json={"model": "tiny"})
        check("POST /api/stt/whisper/download returns 200", dl_post_res.status_code == 200)
        dl_post_data = dl_post_res.json()
        check("POST /api/stt/whisper/download reports ok=True", dl_post_data.get("ok") is True)
        check("POST /api/stt/whisper/download includes download_status", "download_status" in dl_post_data)

    # Test download progress tracker directly
    with stt._download_lock:
        stt._download_state.update({
            "active": True,
            "model": "small",
            "file": "model.bin",
            "downloaded_bytes": 240000000,
            "total_bytes": 480000000,
            "downloaded_mb": 228.8,
            "total_mb": 457.7,
            "percent": 50.0,
            "speed_mbps": 12.5,
            "eta_seconds": 18,
            "status": "downloading",
            "message": "Downloading model.bin: 50.0% (228.8/457.7 MB) at 12.5 MB/s, ETA 18s",
        })
    dl_active_stat = stt.get_download_status()
    check("get_download_status reflects active download", dl_active_stat["active"] is True)
    check("get_download_status reflects percent", dl_active_stat["percent"] == 50.0)
    check("get_download_status reflects speed_mbps", dl_active_stat["speed_mbps"] == 12.5)
    check("get_download_status reflects eta_seconds", dl_active_stat["eta_seconds"] == 18)

    # Reset download state
    with stt._download_lock:
        stt._download_state["active"] = False
        stt._download_state["status"] = "idle"

    # 7. Whisper transcription mock test with Hindi & English words
    class MockSegment:
        def __init__(self, words):
            self.words = words

    class MockWord:
        def __init__(self, word, start, end):
            self.word = word
            self.start = start
            self.end = end

    class MockInfo:
        def __init__(self, lang, prob):
            self.language = lang
            self.language_probability = prob

    mock_words = [
        MockWord("नमस्ते", 0.5, 1.2),
        MockWord("welcome", 1.3, 1.9),
        MockWord("to", 2.0, 2.2),
        MockWord("PeakCut", 2.3, 3.0),
    ]
    mock_segs = [MockSegment(mock_words)]
    mock_info = MockInfo("hi", 0.94)

    mock_model = MagicMock()
    mock_model.transcribe.return_value = (mock_segs, mock_info)

    with patch("studio.stt.get_whisper_model", return_value=(mock_model, "small", "cpu", "int8")):
        audio_dummy = np.ones(32000, dtype=np.int16) * 1000
        words = stt.transcribe_whisper(audio_dummy, model_name="small", language="auto")
        check("Mock transcription returned 4 words", len(words) == 4)
        check("First word is Hindi", words[0]["w"] == "नमस्ते")
        check("Timestamps preserved", words[0]["s"] == 0.5 and words[0]["e"] >= 1.2)
        check("English words transcribed", words[1]["w"] == "welcome")

    # 7. Pipeline dispatch and fallback
    with patch("studio.stt.transcribe_whisper", side_effect=RuntimeError("Out of Memory")):
        with patch("studio.transcribe._take_now", return_value=MagicMock()):
            with patch("studio.transcribe.decode_pieces", return_value=[("fallback", [0.1, 0.5])]):
                # Test with fallback enabled: should catch Whisper error and fall back to Parakeet
                stt.save_settings(provider="whisper", fallback_to_local=True)
                audio_sample = np.ones(32000, dtype=np.int16) * 1000
                res_words = transcribe.transcribe(audio_sample)
                check("Falls back to local model on Whisper error", isinstance(res_words, list))

    # Clean up
    shutil.rmtree(temp_data, ignore_errors=True)

    if fails:
        print(f"FAILED ({len(fails)} checks): " + ", ".join(fails))
        sys.exit(1)
    print("ALL PASSED\n")


if __name__ == "__main__":
    run_tests()

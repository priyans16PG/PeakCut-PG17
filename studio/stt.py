"""Speech-to-text (STT) provider management: local Parakeet offline model,
OpenAI Whisper (faster-whisper) with hardware auto-detection & English/Hindi support,
or ElevenLabs Scribe API."""
import io
import json
import logging
import os
import shutil
import subprocess
import threading
import time
import wave
from pathlib import Path

import httpx
import numpy as np
import sqlalchemy as sa
import tqdm

from . import config, ctx, db

log = logging.getLogger("peakcut.stt")

ELEVENLABS_MODELS = [
    {"id": "scribe_v1", "name": "Scribe v1 (Fast & Accurate)", "recommended": True},
    {"id": "scribe_v2", "name": "Scribe v2 (Latest)", "recommended": False},
    {"id": "scribe_v1_base", "name": "Scribe v1 Base (Compact)", "recommended": False},
]

WHISPER_MODELS = [
    {"id": "auto", "name": "Auto-Detect Hardware (Small on CPU / Large on GPU)", "desc": "Chooses optimal model based on system RAM & GPU VRAM", "recommended": True},
    {"id": "small", "name": "Small (~480 MB)", "desc": "High accuracy for English & Hindi on CPU (Recommended for CPU)", "recommended": False},
    {"id": "large-v3-turbo", "name": "Large v3 Turbo (~1.6 GB)", "desc": "Fast & top accuracy, ideal for 6GB+ VRAM GPU", "recommended": False},
    {"id": "large-v3", "name": "Large v3 (~3.1 GB)", "desc": "Maximum accuracy across all 99 languages", "recommended": False},
    {"id": "medium", "name": "Medium (~1.5 GB)", "desc": "High multi-lingual accuracy", "recommended": False},
    {"id": "base", "name": "Base (~145 MB)", "desc": "Fast, low memory footprint", "recommended": False},
    {"id": "tiny", "name": "Tiny (~75 MB)", "desc": "Ultra-fast & lightweight (~1GB RAM)", "recommended": False},
]

WHISPER_LANGUAGES = [
    {"id": "auto", "name": "Auto-Detect (English, Hindi & 90+ languages)"},
    {"id": "en", "name": "English"},
    {"id": "hi", "name": "Hindi (हिन्दी)"},
    {"id": "ur", "name": "Urdu (اردو)"},
    {"id": "es", "name": "Spanish"},
    {"id": "fr", "name": "French"},
    {"id": "de", "name": "German"},
]

_lock = threading.Lock()
_saved = {"at": None, "data": {}}

_whisper_cache = {"key": None, "model": None}
_whisper_lock = threading.Lock()

_download_state = {
    "active": False,
    "model": "",
    "file": "",
    "downloaded_bytes": 0,
    "total_bytes": 0,
    "downloaded_mb": 0.0,
    "total_mb": 0.0,
    "percent": 0.0,
    "speed_mbps": 0.0,
    "eta_seconds": 0,
    "status": "idle",
    "message": "",
    "error": "",
    "started_at": 0.0,
}
_download_lock = threading.Lock()


# ================================================================ Hardware Auto-detection
def _setup_cuda_dll_paths():
    """On Windows, adds site-packages/nvidia/*/bin directories to DLL search path
    so that CTranslate2 can load cublas64_12.dll and cudnn DLLs."""
    if os.name != "nt":
        return
    import sys
    for p in sys.path:
        for pkg in ("cublas", "cudnn", "cuda_runtime", "cuda_nvrtc"):
            bin_dir = os.path.join(p, "nvidia", pkg, "bin")
            if os.path.isdir(bin_dir):
                try:
                    os.add_dll_directory(bin_dir)
                except Exception:
                    pass
                if bin_dir not in os.environ.get("PATH", ""):
                    os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")


_setup_cuda_dll_paths()


def _detect_system_ram_gb() -> float:
    """Detects physical system RAM in GB using native OS calls."""
    if os.name == "nt":
        try:
            import ctypes
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]
            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)) and stat.ullTotalPhys > 0:
                return round(stat.ullTotalPhys / (1024 ** 3), 1)
        except Exception:
            pass

    try:
        import psutil
        return round(psutil.virtual_memory().total / (1024 ** 3), 1)
    except Exception:
        pass

    try:
        if hasattr(os, "sysconf") and "SC_PAGE_SIZE" in os.sysconf_names:
            bytes_mem = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
            if bytes_mem > 0:
                return round(bytes_mem / (1024 ** 3), 1)
    except Exception:
        pass

    return 8.0


def _detect_gpu_info() -> tuple:
    """Detects whether a CUDA-capable GPU is available for faster-whisper (CTranslate2).
    Returns: (cuda_available, device_name, vram_gb)
    """
    cuda_available = False
    gpu_name = ""
    vram_gb = 0.0

    # 1. Check CTranslate2 (the engine faster-whisper uses for execution)
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            cuda_available = True
    except Exception:
        pass

    # 2. Check PyTorch CUDA (if installed with CUDA support)
    if not cuda_available:
        try:
            import torch
            if torch.cuda.is_available() and torch.cuda.device_count() > 0:
                cuda_available = True
        except Exception:
            pass

    # 3. Query GPU name and total VRAM via nvidia-smi
    smi_paths = ["nvidia-smi"]
    if os.name == "nt":
        smi_paths.extend([
            r"C:\Windows\System32\nvidia-smi.exe",
            r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
        ])
    for smi in smi_paths:
        if shutil.which(smi) or os.path.exists(smi):
            try:
                out = subprocess.check_output(
                    [smi, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                    text=True, stderr=subprocess.DEVNULL, timeout=3
                ).strip()
                if out:
                    lines = [l.strip() for l in out.splitlines() if l.strip()]
                    if lines:
                        parts = [p.strip() for p in lines[0].split(",")]
                        gpu_name = parts[0]
                        vram_mb = float(parts[1])
                        vram_gb = round(vram_mb / 1024.0, 1)
                        cuda_available = True
                        break
            except Exception:
                pass

    # 4. If not found via nvidia-smi, try PyTorch
    if (not gpu_name or vram_gb <= 0.0) and cuda_available:
        try:
            import torch
            if torch.cuda.is_available() and torch.cuda.device_count() > 0:
                gpu_name = torch.cuda.get_device_name(0)
                vram_bytes = torch.cuda.get_device_properties(0).total_memory
                vram_gb = round(vram_bytes / (1024 ** 3), 1)
        except Exception:
            pass

    if cuda_available:
        device_name = gpu_name or "NVIDIA CUDA GPU"
        if vram_gb <= 0.0:
            vram_gb = 4.0
    else:
        device_name = "CPU"
        vram_gb = 0.0

    return cuda_available, device_name, vram_gb


def detect_hardware() -> dict:
    """Auto-detects CPU vs GPU hardware and available memory to determine
    the optimal Whisper model size and compute type."""
    cuda_available, device_name, vram_gb = _detect_gpu_info()
    ram_gb = _detect_system_ram_gb()
    cpu_cores = os.cpu_count() or 4

    # Decision Matrix: Hardware -> Model Recommendation
    if cuda_available and vram_gb >= 5.5:
        recommended_model = "large-v3-turbo"
        recommended_device = "cuda"
        recommended_compute = "float16"
        summary = f"{device_name} ({vram_gb} GB VRAM, {ram_gb} GB RAM) • Auto: Large v3 Turbo (CUDA float16)"
    elif cuda_available and vram_gb >= 3.5:
        recommended_model = "small"
        recommended_device = "cuda"
        recommended_compute = "int8_float16"
        summary = f"{device_name} ({vram_gb} GB VRAM, {ram_gb} GB RAM) • Auto: Small (CUDA int8)"
    elif cuda_available:
        recommended_model = "base"
        recommended_device = "cuda"
        recommended_compute = "int8_float16"
        summary = f"{device_name} ({vram_gb} GB VRAM, {ram_gb} GB RAM) • Auto: Base (CUDA int8)"
    elif ram_gb >= 12.0:
        recommended_model = "small"
        recommended_device = "cpu"
        recommended_compute = "int8"
        summary = f"CPU ({ram_gb} GB RAM, {cpu_cores} cores) • Auto: Small (CPU int8)"
    elif ram_gb >= 6.0:
        recommended_model = "base"
        recommended_device = "cpu"
        recommended_compute = "int8"
        summary = f"CPU ({ram_gb} GB RAM, {cpu_cores} cores) • Auto: Base (CPU int8)"
    else:
        recommended_model = "tiny"
        recommended_device = "cpu"
        recommended_compute = "int8"
        summary = f"CPU ({ram_gb} GB RAM, {cpu_cores} cores) • Auto: Tiny (CPU int8)"

    return {
        "cuda_available": cuda_available,
        "device_name": device_name,
        "vram_gb": vram_gb,
        "ram_gb": ram_gb,
        "cpu_cores": cpu_cores,
        "recommended_model": recommended_model,
        "recommended_device": recommended_device,
        "recommended_compute": recommended_compute,
        "summary": summary,
    }


# ================================================================ Settings Persistence
def _file() -> Path:
    return config.DATA_DIR / "stt.json"


def _from_file() -> dict:
    f = _file()
    try:
        at = f.stat().st_mtime_ns
    except OSError:
        at = 0
    if _saved["at"] != at:
        try:
            data = json.loads(f.read_text(encoding="utf-8")) if at else {}
        except (OSError, ValueError):
            data = {}
        _saved.update(at=at, data=data if isinstance(data, dict) else {})
    return _saved["data"]


def _db_get() -> dict:
    try:
        with db.engine.connect() as conn:
            row = conn.execute(
                sa.select(db.stt_settings).where(db.stt_settings.c.is_active.is_(True))
            ).fetchone()
            if row:
                m = row._mapping
                return {
                    "provider": m["provider"],
                    "api_key": m["api_key"],
                    "model": m["model"],
                    "language": m["language"],
                    "fallback_to_local": bool(m["fallback_to_local"]),
                    "extra_config": json.loads(m["extra_config"] or "{}"),
                    "updated_at": float(m["updated_at"] or 0),
                }
    except Exception:
        pass
    return {}


def get_hf_token() -> str:
    """Returns the effective Hugging Face token (DB/file settings > config env)."""
    s = get_settings()
    extra = s.get("extra_config") or {}
    token = extra.get("hf_token") or config.hf_token()
    tok_str = str(token).strip() if token else ""
    if tok_str:
        os.environ["HF_TOKEN"] = tok_str
        os.environ["HUGGING_FACE_HUB_TOKEN"] = tok_str
    return tok_str


def clear_whisper_cache():
    with _whisper_lock:
        _whisper_cache["key"] = None
        _whisper_cache["model"] = None


def is_whisper_installed() -> bool:
    try:
        import faster_whisper
        return True
    except ImportError:
        return False


def test_hf_token(token: str = None) -> dict:
    """Validates a Hugging Face API / User Access Token against https://huggingface.co/api/whoami-v2."""
    raw_token = (token or get_hf_token() or "").strip()
    if not raw_token:
        return {"ok": False, "message": "No Hugging Face token provided."}

    url = "https://huggingface.co/api/whoami-v2"
    headers = {"Authorization": f"Bearer {raw_token}"}
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                username = data.get("name") or data.get("fullname") or data.get("email") or "User"
                os.environ["HF_TOKEN"] = raw_token
                os.environ["HUGGING_FACE_HUB_TOKEN"] = raw_token
                clear_whisper_cache()
                return {
                    "ok": True,
                    "username": username,
                    "message": f"Valid Hugging Face token! Authenticated as @{username}.",
                }
            elif resp.status_code == 401:
                return {"ok": False, "message": "Invalid Hugging Face token. Please check and try again."}
            else:
                return {"ok": False, "message": f"Hugging Face returned status {resp.status_code}: {resp.text[:120]}"}
    except Exception as e:
        return {"ok": False, "message": f"Could not connect to Hugging Face API: {e}"}


def get_settings() -> dict:
    """Returns the effective STT settings (DB > stt.json > config env)."""
    with _lock:
        db_cfg = _db_get()
        if db_cfg:
            return db_cfg
        f_cfg = _from_file()
        if f_cfg:
            return f_cfg

        # Default from config / env
        env_key = config.elevenlabs_key()
        provider = config.STT_PROVIDER
        if not provider or provider not in ("local", "whisper", "elevenlabs"):
            provider = "elevenlabs" if env_key else "local"

        default_model = (
            config.WHISPER_MODEL
            if provider == "whisper"
            else (config.ELEVENLABS_MODEL if provider == "elevenlabs" else "parakeet")
        )
        default_lang = (
            config.WHISPER_LANGUAGE
            if provider == "whisper"
            else (config.ELEVENLABS_LANGUAGE if provider == "elevenlabs" else "")
        )

        hf_tok = config.hf_token()
        extra_cfg = {
            "whisper_device": config.WHISPER_DEVICE,
            "whisper_compute_type": config.WHISPER_COMPUTE_TYPE,
        }
        if hf_tok:
            extra_cfg["hf_token"] = hf_tok

        return {
            "provider": provider,
            "api_key": env_key,
            "model": default_model or ("auto" if provider == "whisper" else "scribe_v1"),
            "language": default_lang or ("auto" if provider == "whisper" else ""),
            "fallback_to_local": True,
            "extra_config": extra_cfg,
            "updated_at": 0.0,
        }


def save_settings(
    provider: str,
    api_key: str = None,
    model: str = None,
    language: str = None,
    fallback_to_local: bool = True,
    extra_config: dict = None,
) -> dict:
    provider = "whisper" if provider == "whisper" else ("elevenlabs" if provider == "elevenlabs" else "local")
    if provider == "whisper":
        model = (model or "auto").strip()
        language = (language or "auto").strip()
    elif provider == "elevenlabs":
        model = (model or "scribe_v1").strip()
        language = (language or "").strip()
    else:
        model = "parakeet"
        language = ""

    current = get_settings()
    cur_key = current.get("api_key", "")

    # If key is provided and not empty, update it; otherwise preserve existing key
    if api_key is not None and api_key.strip() != "":
        saved_key = api_key.strip()
    else:
        saved_key = cur_key or config.elevenlabs_key()

    extra = dict(current.get("extra_config") or {})
    if isinstance(extra_config, dict):
        for k, v in extra_config.items():
            if k == "hf_token":
                if v is not None and str(v).strip():
                    val = str(v).strip()
                    extra["hf_token"] = val
                    os.environ["HF_TOKEN"] = val
                    os.environ["HUGGING_FACE_HUB_TOKEN"] = val
                elif v == "":
                    extra.pop("hf_token", None)
                    os.environ.pop("HF_TOKEN", None)
                    os.environ.pop("HUGGING_FACE_HUB_TOKEN", None)
            else:
                extra[k] = v

    now = time.time()
    payload = {
        "provider": provider,
        "api_key": saved_key,
        "model": model,
        "language": language,
        "fallback_to_local": bool(fallback_to_local),
        "extra_config": extra,
        "updated_at": now,
    }

    # Save to SQLite
    try:
        with db.engine.begin() as conn:
            # Set all to inactive
            conn.execute(sa.update(db.stt_settings).values(is_active=False))
            existing = conn.execute(
                sa.select(db.stt_settings).where(db.stt_settings.c.provider == provider)
            ).fetchone()
            if existing:
                conn.execute(
                    sa.update(db.stt_settings)
                    .where(db.stt_settings.c.provider == provider)
                    .values(
                        api_key=saved_key,
                        model=model,
                        language=language,
                        fallback_to_local=bool(fallback_to_local),
                        is_active=True,
                        extra_config=json.dumps(extra),
                        updated_at=now,
                    )
                )
            else:
                conn.execute(
                    sa.insert(db.stt_settings).values(
                        provider=provider,
                        api_key=saved_key,
                        model=model,
                        language=language,
                        fallback_to_local=bool(fallback_to_local),
                        is_active=True,
                        extra_config=json.dumps(extra),
                        updated_at=now,
                    )
                )
    except Exception as e:
        log.warning(f"Could not save STT settings to SQLite: {e}")

    # Mirror to data/stt.json
    try:
        f = _file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        _saved.update(at=f.stat().st_mtime_ns, data=payload)
    except Exception as e:
        log.warning(f"Could not mirror STT settings to file: {e}")

    ctx.bump()
    return public_settings()


def public_settings() -> dict:
    from . import transcribe
    s = get_settings()
    raw_key = s.get("api_key", "") or config.elevenlabs_key()
    has_key = bool(raw_key and len(raw_key) >= 8)
    key_hint = raw_key[-4:] if (raw_key and len(raw_key) >= 8) else ""
    provider = s.get("provider", "local")
    local_ready = transcribe.model_ready()

    hw = detect_hardware()
    whisper_ready = is_whisper_installed()

    hf_tok = get_hf_token()
    has_hf_token = bool(hf_tok and len(hf_tok) >= 8)
    hf_token_hint = (hf_tok[:4] + "..." + hf_tok[-4:]) if (hf_tok and len(hf_tok) >= 8) else ""

    ready = (
        (provider == "local" and local_ready)
        or (provider == "whisper" and whisper_ready)
        or (provider == "elevenlabs" and has_key)
    )

    models_list = (
        [m["id"] for m in WHISPER_MODELS]
        if provider == "whisper"
        else [m["id"] for m in ELEVENLABS_MODELS]
    )

    return {
        "provider": provider,
        "has_key": has_key,
        "key_hint": key_hint,
        "has_hf_token": has_hf_token,
        "hf_token_hint": hf_token_hint,
        "model": s.get("model", "auto" if provider == "whisper" else "scribe_v1"),
        "models": models_list,
        "model_options": ELEVENLABS_MODELS,
        "whisper_models": WHISPER_MODELS,
        "whisper_languages": WHISPER_LANGUAGES,
        "models_status": get_whisper_models_status(),
        "download_status": get_download_status(),
        "hardware": hw,
        "language": s.get("language", "auto" if provider == "whisper" else ""),
        "extra_config": s.get("extra_config") or {},
        "fallback_to_local": s.get("fallback_to_local", True),
        "local_model_ready": local_ready,
        "whisper_ready": whisper_ready,
        "ready": ready,
    }


# ================================================================ ElevenLabs Scribe API
def test_elevenlabs_key(key: str = None) -> dict:
    """Validates an ElevenLabs API key against the ElevenLabs API."""
    raw_key = key or get_settings().get("api_key") or config.elevenlabs_key()
    if not raw_key:
        raise ValueError("No ElevenLabs API key provided to test.")

    url = "https://api.elevenlabs.io/v1/user"
    headers = {"xi-api-key": raw_key}

    try:
        resp = httpx.get(url, headers=headers, timeout=12.0)
    except Exception as e:
        raise RuntimeError(f"Connection to ElevenLabs failed: {e}")

    if resp.status_code == 401:
        raise ValueError("ElevenLabs rejected this key (HTTP 401 Invalid API Key). Please verify your key at elevenlabs.io.")

    if resp.status_code != 200:
        err_msg = resp.text[:200]
        try:
            j = resp.json()
            if isinstance(j.get("detail"), dict):
                err_msg = j["detail"].get("message", err_msg)
            elif isinstance(j.get("detail"), str):
                err_msg = j["detail"]
        except Exception:
            pass
        raise RuntimeError(f"ElevenLabs error ({resp.status_code}): {err_msg}")

    data = resp.json()
    sub = data.get("subscription", {})
    tier = sub.get("tier", "unknown")
    status = sub.get("status", "active")
    chars_used = sub.get("character_count", 0)
    chars_limit = sub.get("character_limit", 0)

    return {
        "ok": True,
        "tier": tier,
        "status": status,
        "character_count": chars_used,
        "character_limit": chars_limit,
        "message": f"Connected to ElevenLabs ({tier.capitalize()} tier, status: {status})",
    }


def _audio_to_wav_bytes(audio: np.ndarray) -> bytes:
    """Converts a numpy audio array (16kHz mono) to in-memory WAV bytes."""
    if audio.dtype != np.int16:
        if audio.dtype in (np.float32, np.float64):
            audio_i16 = np.clip(audio * 32768.0, -32768, 32767).astype(np.int16)
        else:
            audio_i16 = audio.astype(np.int16)
    else:
        audio_i16 = audio

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(audio_i16.tobytes())
    return buf.getvalue()


def transcribe_elevenlabs(
    audio: np.ndarray,
    key_override: str = None,
    model: str = "scribe_v1",
    language: str = "",
    on_progress=None
) -> list:
    """Transcribes audio using ElevenLabs Scribe API.
    Returns: [{'w': word, 's': start_sec, 'e': end_sec}, ...]
    """
    if len(audio) < 1600:
        return []

    # Silence check
    if float(np.sqrt((audio.astype(np.float32) ** 2).mean())) < 1e-4:
        return []

    raw_key = key_override or get_settings().get("api_key") or config.elevenlabs_key()
    if not raw_key:
        raise ValueError("Missing ElevenLabs API key.")

    wav_bytes = _audio_to_wav_bytes(audio)

    data = {
        "model_id": model or "scribe_v1",
        "tag_audio_events": "false",
        "timestamps_granularity": "word",
    }
    if language:
        data["language_code"] = language

    url = "https://api.elevenlabs.io/v1/speech-to-text"
    headers = {"xi-api-key": raw_key}
    files = {"file": ("audio.wav", wav_bytes, "audio/wav")}

    audio_dur = len(audio) / 16000.0
    timeout_secs = max(60.0, min(600.0, audio_dur * 0.5 + 30.0))

    mask_hint = (raw_key[:4] + "..." + raw_key[-4:]) if len(raw_key) > 8 else "***"
    print(f"[STT] Sending {audio_dur:.1f}s audio to ElevenLabs Scribe (model: {model or 'scribe_v1'}, key: {mask_hint})...", flush=True)

    if on_progress:
        on_progress(0.1)

    t0 = time.time()
    resp = httpx.post(url, headers=headers, data=data, files=files, timeout=httpx.Timeout(timeout_secs, connect=10.0))
    t_el = time.time() - t0

    if on_progress:
        on_progress(0.8)

    if resp.status_code != 200:
        err_msg = resp.text[:300]
        try:
            j = resp.json()
            if isinstance(j.get("detail"), dict):
                err_msg = j["detail"].get("message", err_msg)
            elif isinstance(j.get("detail"), str):
                err_msg = j["detail"]
        except Exception:
            pass
        print(f"[STT] ElevenLabs returned HTTP {resp.status_code}: {err_msg}", flush=True)
        raise RuntimeError(f"ElevenLabs transcription error ({resp.status_code}): {err_msg}")

    result = resp.json()
    raw_words = result.get("words", [])
    print(f"[STT] ElevenLabs transcription finished in {t_el:.2f}s ({len(raw_words)} raw tokens)", flush=True)

    words = []
    for item in raw_words:
        wtype = item.get("type", "word")
        if wtype in ("spacing", "audio_event"):
            continue
        txt = str(item.get("text", "")).strip()
        if not txt:
            continue
        s = round(float(item.get("start", 0.0)), 2)
        e = round(float(item.get("end", s + 0.12)), 2)
        if e <= s:
            e = round(s + 0.12, 2)
        words.append({"w": txt, "s": s, "e": e})

    for i, w in enumerate(words):
        nxt = words[i + 1]["s"] if i + 1 < len(words) else w["e"] + 0.4
        w["e"] = round(max(w["s"] + 0.08, min(nxt, max(w["e"], w["s"] + 0.6))), 2)

    if on_progress:
        on_progress(1.0)

    return words


# ================================================================ OpenAI Whisper (faster-whisper)
def _whisper_download_dir() -> Path:
    d = config.DATA_DIR / "models" / "whisper"
    d.mkdir(parents=True, exist_ok=True)
    return d


def is_whisper_model_downloaded(model_name: str) -> tuple:
    """Checks if model files for model_name exist in the whisper cache.
    Returns: (is_downloaded: bool, size_in_mb: float)
    """
    import faster_whisper.utils
    resolved = _resolve_whisper_params(model_name)[0] if model_name == "auto" else model_name
    download_root = str(_whisper_download_dir())
    try:
        p = Path(faster_whisper.utils.download_model(resolved, cache_dir=download_root, local_files_only=True))
        bin_file = p / "model.bin"
        if bin_file.exists():
            real_file = bin_file.resolve()
            if real_file.exists() and real_file.stat().st_size > 1024 * 1024:
                sz = sum(f.resolve().stat().st_size for f in p.glob("*") if f.exists())
                return True, round(sz / (1024 * 1024), 1)
    except Exception:
        pass
    return False, 0.0


def get_whisper_models_status() -> list:
    """Returns a list of all Whisper models and their current on-disk download status."""
    res = []
    for m in WHISPER_MODELS:
        mid = m["id"]
        if mid == "auto":
            continue
        dl, sz = is_whisper_model_downloaded(mid)
        res.append({
            "id": mid,
            "name": m["name"],
            "desc": m.get("desc", ""),
            "downloaded": dl,
            "size_mb": sz,
        })
    return res


class _ModelDownloadTracker(tqdm.tqdm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.filename = self.desc or "model file"
        with _download_lock:
            _download_state["file"] = self.filename
            if self.total and self.total > 0:
                _download_state["total_bytes"] = self.total
                _download_state["total_mb"] = round(self.total / (1024 * 1024), 1)

    def update(self, n=1):
        super().update(n)
        now = time.time()
        with _download_lock:
            _download_state["downloaded_bytes"] = self.n
            tot = max(1, self.total or _download_state.get("total_bytes") or 1)
            pct = round((self.n / tot) * 100, 1)
            _download_state["percent"] = min(100.0, pct)

            elapsed = max(0.01, now - _download_state.get("started_at", now))
            speed = (self.n / (1024 * 1024)) / elapsed
            _download_state["speed_mbps"] = round(speed, 1)

            remaining_bytes = max(0, tot - self.n)
            eta = int(remaining_bytes / max(1.0, speed * 1024 * 1024))
            _download_state["eta_seconds"] = eta

            curr_mb = round(self.n / (1024 * 1024), 1)
            tot_mb = round(tot / (1024 * 1024), 1)
            _download_state["downloaded_mb"] = curr_mb
            _download_state["total_mb"] = tot_mb

            _download_state["message"] = (
                f"Downloading {self.filename} ({curr_mb} MB / {tot_mb} MB - {pct}% at {speed:.1f} MB/s)"
            )


def download_whisper_model(model_name: str, on_progress=None) -> str:
    """Downloads a Whisper model from Hugging Face with live progress tracking.
    Returns the local directory path where the model is stored.
    """
    from huggingface_hub import snapshot_download
    import faster_whisper.utils

    resolved = _resolve_whisper_params(model_name)[0] if model_name == "auto" else model_name
    repo_id = faster_whisper.utils._MODELS.get(resolved, resolved)

    download_root = str(_whisper_download_dir())
    hf_tok = get_hf_token()
    if hf_tok:
        os.environ["HF_TOKEN"] = hf_tok
        os.environ["HUGGING_FACE_HUB_TOKEN"] = hf_tok

    with _download_lock:
        _download_state.update({
            "active": True,
            "model": resolved,
            "file": "connecting...",
            "downloaded_bytes": 0,
            "total_bytes": 0,
            "downloaded_mb": 0.0,
            "total_mb": 0.0,
            "percent": 0.0,
            "speed_mbps": 0.0,
            "eta_seconds": 0,
            "status": "downloading",
            "message": f"Connecting to Hugging Face for '{resolved}'...",
            "error": "",
            "started_at": time.time(),
        })

    allow_patterns = [
        "config.json",
        "preprocessor_config.json",
        "model.bin",
        "tokenizer.json",
        "vocabulary.*",
    ]

    try:
        path = snapshot_download(
            repo_id,
            cache_dir=download_root,
            allow_patterns=allow_patterns,
            token=hf_tok or None,
            tqdm_class=_ModelDownloadTracker,
        )
        with _download_lock:
            dl, sz = is_whisper_model_downloaded(resolved)
            _download_state.update({
                "active": False,
                "percent": 100.0,
                "status": "done",
                "downloaded_mb": sz,
                "total_mb": sz,
                "message": f"Whisper model '{resolved}' downloaded and verified successfully ({sz} MB)!",
            })
        clear_whisper_cache()
        if on_progress:
            on_progress(get_download_status())
        return path
    except Exception as e:
        with _download_lock:
            _download_state.update({
                "active": False,
                "status": "error",
                "error": str(e),
                "message": f"Download failed: {e}",
            })
        if on_progress:
            on_progress(get_download_status())
        raise


def start_whisper_download_async(model_name: str = "auto") -> dict:
    """Starts asynchronous background download of a Whisper model with live progress."""
    resolved = _resolve_whisper_params(model_name)[0] if model_name == "auto" else model_name
    is_dl, sz = is_whisper_model_downloaded(resolved)
    if is_dl:
        return {
            "ok": True,
            "already_downloaded": True,
            "model": resolved,
            "size_mb": sz,
            "message": f"Model '{resolved}' is already downloaded on disk ({sz} MB).",
            "download_status": get_download_status(),
        }

    with _download_lock:
        if _download_state.get("active"):
            return {
                "ok": True,
                "already_downloading": True,
                "model": _download_state.get("model"),
                "message": f"Model '{_download_state.get('model')}' is currently downloading.",
                "download_status": get_download_status(),
            }

    def _bg():
        try:
            download_whisper_model(resolved)
        except Exception as e:
            log.error(f"Failed to download Whisper model '{resolved}': {e}")

    t = threading.Thread(target=_bg, daemon=True)
    t.start()
    return {
        "ok": True,
        "status": "started",
        "model": resolved,
        "message": f"Started downloading Whisper model '{resolved}'...",
        "download_status": get_download_status(),
    }


def get_download_status() -> dict:
    with _download_lock:
        st = dict(_download_state)
    st["models"] = get_whisper_models_status()
    return st


def _resolve_whisper_params(model_name: str = "auto", device: str = "auto", compute_type: str = "auto") -> tuple:
    hw = detect_hardware()
    # Resolve model
    resolved_model = model_name or "auto"
    if resolved_model == "auto":
        resolved_model = hw["recommended_model"]

    # Resolve device
    resolved_device = device or "auto"
    if resolved_device == "auto":
        resolved_device = hw["recommended_device"]

    # Resolve compute_type
    resolved_compute = compute_type or "auto"
    if resolved_compute == "auto":
        if resolved_device == "cuda":
            resolved_compute = "float16" if hw["vram_gb"] >= 6.0 else "int8_float16"
        else:
            resolved_compute = "int8"

    return resolved_model, resolved_device, resolved_compute


def get_whisper_model(model_name: str = "auto", device: str = "auto", compute_type: str = "auto"):
    from faster_whisper import WhisperModel
    resolved_model, resolved_device, resolved_compute = _resolve_whisper_params(model_name, device, compute_type)
    cache_key = (resolved_model, resolved_device, resolved_compute)

    with _whisper_lock:
        if _whisper_cache["key"] == cache_key and _whisper_cache["model"] is not None:
            return _whisper_cache["model"], resolved_model, resolved_device, resolved_compute

        hf_tok = get_hf_token()
        if hf_tok:
            os.environ["HF_TOKEN"] = hf_tok
            os.environ["HUGGING_FACE_HUB_TOKEN"] = hf_tok
        download_root = str(_whisper_download_dir())
        cpu_threads = min(4, os.cpu_count() or 4)

        # Ensure model is downloaded with real-time progress tracking
        is_dl, _ = is_whisper_model_downloaded(resolved_model)
        if not is_dl:
            print(f"[STT] Whisper model '{resolved_model}' not found on disk. Downloading from Hugging Face...", flush=True)
            download_whisper_model(resolved_model)

        print(f"[STT] Loading Whisper model '{resolved_model}' on {resolved_device} ({resolved_compute}) (HF authenticated: {bool(hf_tok)})...", flush=True)
        try:
            model = WhisperModel(
                resolved_model,
                device=resolved_device,
                compute_type=resolved_compute,
                download_root=download_root,
                cpu_threads=cpu_threads,
                use_auth_token=hf_tok or None,
            )
        except Exception as e:
            if resolved_device == "cuda":
                print(f"[STT] Warning: Failed to load on CUDA ({e}). Falling back to CPU (int8)...", flush=True)
                resolved_device = "cpu"
                resolved_compute = "int8"
                model = WhisperModel(
                    resolved_model,
                    device="cpu",
                    compute_type="int8",
                    download_root=download_root,
                    cpu_threads=cpu_threads,
                    use_auth_token=hf_tok or None,
                )
            else:
                raise
        _whisper_cache["key"] = cache_key
        _whisper_cache["model"] = model
        print(f"[STT] Whisper model '{resolved_model}' initialized and cached successfully.", flush=True)
        return model, resolved_model, resolved_device, resolved_compute


def transcribe_whisper(
    audio: np.ndarray,
    model_name: str = "auto",
    language: str = "auto",
    device: str = "auto",
    compute_type: str = "auto",
    on_progress=None,
) -> list:
    """Transcribes audio using OpenAI Whisper (faster-whisper) with word-level timestamps.
    Automatically detects language (English, Hindi, etc.) when language is 'auto' or empty."""
    if len(audio) < 1600:
        return []

    # Silence check
    if float(np.sqrt((audio.astype(np.float32) ** 2).mean())) < 1e-4:
        return []

    # Convert audio to 16kHz float32 mono
    if audio.dtype != np.float32:
        if audio.dtype in (np.int16, np.int32):
            audio_f32 = audio.astype(np.float32) / 32768.0
        else:
            audio_f32 = audio.astype(np.float32)
    else:
        audio_f32 = audio

    audio_dur = len(audio_f32) / 16000.0

    model, resolved_model, resolved_device, resolved_compute = get_whisper_model(model_name, device, compute_type)

    lang_param = None if (not language or language == "auto") else language.strip().lower()
    lang_label = lang_param or "auto-detect (en/hi)"

    print(
        f"[STT] Whisper transcribing {audio_dur:.1f}s audio (model: {resolved_model}, device: {resolved_device}, "
        f"compute: {resolved_compute}, language: {lang_label})...",
        flush=True,
    )

    if on_progress:
        on_progress(0.1)

    t0 = time.time()
    try:
        segments, info = model.transcribe(
            audio_f32,
            word_timestamps=True,
            language=lang_param,
            beam_size=5,
        )
        seg_list = list(segments)
    except Exception as e:
        if resolved_device == "cuda":
            print(f"[STT] CUDA transcription failed ({e}). Retrying with CPU (int8)...", flush=True)
            model, resolved_model, resolved_device, resolved_compute = get_whisper_model(resolved_model, "cpu", "int8")
            segments, info = model.transcribe(
                audio_f32,
                word_timestamps=True,
                language=lang_param,
                beam_size=5,
            )
            seg_list = list(segments)
        else:
            raise

    if on_progress:
        on_progress(0.5)

    words = []
    for segment in seg_list:
        for w in (segment.words or []):
            txt = str(w.word or "").strip()
            if not txt:
                continue
            s = round(float(w.start), 2)
            e = round(float(w.end), 2)
            if e <= s:
                e = round(s + 0.12, 2)
            words.append({"w": txt, "s": s, "e": e})

    # Adjust end timestamps to prevent overlapping words
    for i, w in enumerate(words):
        nxt = words[i + 1]["s"] if i + 1 < len(words) else w["e"] + 0.4
        w["e"] = round(max(w["s"] + 0.08, min(nxt, max(w["e"], w["s"] + 0.6))), 2)

    t_el = time.time() - t0
    detected_lang = getattr(info, "language", "unknown")
    lang_prob = getattr(info, "language_probability", 1.0)

    print(
        f"[STT] Whisper finished in {t_el:.2f}s: detected language '{detected_lang}' (confidence: {lang_prob:.1%}), "
        f"{len(words)} words transcribed.",
        flush=True,
    )

    if on_progress:
        on_progress(1.0)

    return words


def test_whisper_model(model_name: str = "auto", device: str = "auto", compute_type: str = "auto") -> dict:
    """Preloads and tests Whisper model with a 0.5s audio pulse."""
    _setup_cuda_dll_paths()
    t0 = time.time()
    model, resolved_model, resolved_device, resolved_compute = get_whisper_model(model_name, device, compute_type)
    # Test with 0.5s silence
    test_audio = np.zeros(8000, dtype=np.float32)
    try:
        segments, info = model.transcribe(test_audio, word_timestamps=True)
        list(segments)
    except Exception as e:
        if resolved_device == "cuda":
            print(f"[STT] CUDA test failed ({e}), falling back to CPU (int8)...", flush=True)
            model, resolved_model, resolved_device, resolved_compute = get_whisper_model(resolved_model, "cpu", "int8")
            segments, info = model.transcribe(test_audio, word_timestamps=True)
            list(segments)
        else:
            raise
    t_el = time.time() - t0
    hw = detect_hardware()
    dl, sz = is_whisper_model_downloaded(resolved_model)
    return {
        "ok": True,
        "model": resolved_model,
        "device": resolved_device,
        "compute_type": resolved_compute,
        "downloaded": dl,
        "size_mb": sz,
        "hardware_summary": hw["summary"],
        "took": round(t_el, 2),
        "message": f"Whisper '{resolved_model}' is ready on {resolved_device} ({resolved_compute}) in {t_el:.2f}s.",
    }

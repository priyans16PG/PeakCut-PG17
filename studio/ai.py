"""Talking to the AI service. You bring your own key, and choose the service and model in the dashboard
(Settings, AI) or in .env. Provider configurations are kept in SQLite (ai_providers) and synced to ai.json.

    PEAKCUT_AI_PROVIDER   gemini | openai | xai | groq | openrouter | anthropic | ollama | custom
    PEAKCUT_AI_MODELS     one or more model ids, comma separated, tried in this order
    PEAKCUT_AI_KEY        your key (or PEAKCUT_AI_KEY_FILE, a file holding it)
    PEAKCUT_AI_BASE_URL   only for "custom" (any service that speaks the OpenAI chat format), or to override

Every service is asked for the same thing: JSON that follows a small schema. Gemini takes the schema directly.
The others are told the schema in the prompt and asked for a JSON object, which works with any chat model."""
import json
import os
import re
import threading
import time
import uuid

import httpx
import sqlalchemy as sa

from . import config, db

# Strict Gemini 3.x model rotation pool
GEMINI_3_MODELS = [
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.1-pro-preview",
    "gemini-3-flash-preview",
]

# name -> (label shown on the pages, address, how it is spoken to, where to get a key, an example model id)
PROVIDERS = {
    "gemini": ("Gemini", "https://generativelanguage.googleapis.com/v1beta/models", "gemini", "https://aistudio.google.com/apikey", "gemini-3.8-flash"),
    "openai": ("OpenAI", "https://api.openai.com/v1", "openai", "https://platform.openai.com/api-keys", "gpt-4o-mini"),
    "xai": ("Grok", "https://api.x.ai/v1", "openai", "https://console.x.ai", "grok-4"),
    "groq": ("Groq", "https://api.groq.com/openai/v1", "openai", "https://console.groq.com/keys", "llama-3.3-70b-versatile"),
    "openrouter": ("OpenRouter", "https://openrouter.ai/api/v1", "openai", "https://openrouter.ai/keys", "google/gemini-3-flash-preview"),
    "anthropic": ("Claude", "https://api.anthropic.com/v1", "anthropic", "https://console.anthropic.com", ""),
    "ollama": ("Ollama (on this machine)", "http://127.0.0.1:11434/v1", "openai", "https://ollama.com", "llama3.1"),
    "custom": ("Custom (OpenAI-compatible)", "", "openai", "", ""),
}
ALIASES = {"google": "gemini", "grok": "xai", "claude": "anthropic", "local": "ollama"}
NO_KEY = {"ollama", "custom"}       # a model running on your own machine needs no key

# ---------------------------------------------------------------- errors & global quota state
class AIError(RuntimeError):
    pass


class GeminiQuotaExhaustedError(AIError):
    code = "ALL_GEMINI_QUOTA_REACHED"
    message = "All Gemini API keys and Gemini 3 models have reached their quota. Add another key, switch providers, or wait for quota reset."

    def __init__(self, message=None):
        super().__init__(message or self.message)


_cooldowns: dict = {}               # (key_id, model_id) -> float(cooldown_until)
_cooldown_lock = threading.Lock()
_gemini_all_quota_reached: bool = False


def is_gemini_all_quota_reached() -> bool:
    global _gemini_all_quota_reached
    return _gemini_all_quota_reached


def set_gemini_all_quota_reached(val: bool):
    global _gemini_all_quota_reached
    _gemini_all_quota_reached = bool(val)


# ---------------------------------------------------------------- the settings in use
_lock = threading.Lock()
_saved = {"at": None, "data": {}}


def _file():
    return config.DATA_DIR / "ai.json"


def _from_dashboard() -> dict:
    """What was saved from the dashboard, read again only when the file changes."""
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


def _db_get_all() -> dict:
    """Load all provider settings from the SQLite table. Seeds if empty."""
    out = {}
    try:
        with db.engine.connect() as conn:
            rows = conn.execute(sa.select(db.ai_providers)).fetchall()
            for r in rows:
                p_dict = {
                    "provider": r._mapping["provider"],
                    "api_keys": json.loads(r._mapping["api_keys"] or "[]"),
                    "models": json.loads(r._mapping["models"] or "[]"),
                    "is_active": bool(r._mapping["is_active"]),
                    "extra_config": json.loads(r._mapping["extra_config"] or "{}"),
                    "updated_at": float(r._mapping["updated_at"] or 0),
                }
                out[p_dict["provider"]] = p_dict
    except Exception:
        return {}

    if not out:
        f_data = _from_dashboard()
        cur_p = str(f_data.get("provider") or config.AI_PROVIDER or "gemini").strip().lower()
        cur_p = ALIASES.get(cur_p, cur_p)
        cur_k = str(f_data.get("key") or config.ai_key() or "").strip()
        cur_m = list(f_data.get("models") or config.AI_MODELS or [])
        cur_b = str(f_data.get("base_url") or config.AI_BASE_URL or "").strip()
        now = time.time()

        for pid in PROVIDERS:
            is_act = (pid == cur_p)
            if pid == "gemini":
                keys = [{"id": "key_1", "key": cur_k, "label": "Primary Key", "status": "active", "cooldown_until": 0.0, "created_at": now}] if (is_act and cur_k) else []
                clean_m = [m for m in cur_m if not re.match(r"^gemini-[12](\.|$)", m.lower())]
                mods = clean_m if (is_act and clean_m) else list(GEMINI_3_MODELS)
            else:
                keys = [{"id": f"key_{pid}_1", "key": cur_k, "label": "Primary Key", "status": "active", "cooldown_until": 0.0, "created_at": now}] if (is_act and cur_k) else []
                mods = cur_m if (is_act and cur_m) else ([PROVIDERS[pid][4]] if PROVIDERS[pid][4] else [])
            extra = {"base_url": cur_b} if (is_act and cur_b) else {}
            item = {"provider": pid, "api_keys": keys, "models": mods, "is_active": is_act, "extra_config": extra, "updated_at": now}
            out[pid] = item
            try:
                with db.engine.begin() as conn:
                    conn.execute(db.ai_providers.insert().values(
                        provider=pid,
                        api_keys=json.dumps(keys),
                        models=json.dumps(mods),
                        is_active=is_act,
                        extra_config=json.dumps(extra),
                        updated_at=now,
                    ))
            except Exception:
                pass
    return out


def _db_save_provider(pid: str, api_keys: list, models: list, is_active: bool, extra_config: dict):
    """Save or update one provider row. When is_active is True, marks others inactive and syncs ai.json."""
    now = time.time()
    try:
        with db.engine.begin() as conn:
            if is_active:
                conn.execute(db.ai_providers.update().values(is_active=False))
            exists = conn.execute(sa.select(db.ai_providers.c.provider).where(db.ai_providers.c.provider == pid)).first()
            if exists:
                conn.execute(db.ai_providers.update().where(db.ai_providers.c.provider == pid).values(
                    api_keys=json.dumps(api_keys),
                    models=json.dumps(models),
                    is_active=is_active,
                    extra_config=json.dumps(extra_config),
                    updated_at=now,
                ))
            else:
                conn.execute(db.ai_providers.insert().values(
                    provider=pid,
                    api_keys=json.dumps(api_keys),
                    models=json.dumps(models),
                    is_active=is_active,
                    extra_config=json.dumps(extra_config),
                    updated_at=now,
                ))
    except Exception:
        pass

    if is_active:
        with _lock:
            f = _file()
            tmp = f.with_suffix(".tmp")
            first_key = api_keys[0]["key"] if api_keys and isinstance(api_keys[0], dict) else (api_keys[0] if api_keys else "")
            tmp.write_text(json.dumps({
                "provider": pid,
                "models": models,
                "base_url": extra_config.get("base_url", ""),
                "key": first_key,
            }), encoding="utf-8")
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            tmp.replace(f)
            _saved["at"] = None


def current() -> dict:
    """Provider, models, address and key: what the dashboard saved, or else what .env says."""
    d = _from_dashboard()
    if d.get("provider"):
        p = str(d.get("provider")).strip().lower()
        p = ALIASES.get(p, p)
        all_p = _db_get_all()
        row = all_p.get(p, {})
        keys = row.get("api_keys", [])
        k = keys[0]["key"] if keys and isinstance(keys[0], dict) else (d.get("key") or config.ai_key())
        ms = list(d.get("models") or row.get("models") or [])
        b = (str(d.get("base_url") or row.get("extra_config", {}).get("base_url") or "")).rstrip("/")
        if p == "gemini" and not ms:
            ms = list(GEMINI_3_MODELS)
        return {"provider": p, "models": ms, "base_url": b, "key": k}

    p = str(config.AI_PROVIDER or "").strip().lower()
    p = ALIASES.get(p, p)
    mods = list(config.AI_MODELS)
    if p == "gemini" and not mods:
        mods = list(GEMINI_3_MODELS)
    return {
        "provider": p,
        "models": mods,
        "base_url": config.AI_BASE_URL.rstrip("/"),
        "key": config.ai_key(),
    }


def provider() -> str:
    return current()["provider"]


def models() -> list:
    return current()["models"]


def key() -> str:
    return current()["key"]


def label() -> str:
    return PROVIDERS.get(provider(), ("",))[0]


def base_url() -> str:
    c = current()
    return c["base_url"] or PROVIDERS.get(c["provider"], ("", ""))[1]


def ready() -> bool:
    c = current()
    return bool(c["provider"] in PROVIDERS and c["models"] and base_url() and (c["key"] or c["provider"] in NO_KEY))


def public() -> dict:
    """What the settings page may see. The key itself never leaves the server."""
    c = current()
    all_p = _db_get_all()
    gem_keys = get_gemini_keys(mask=True)

    saved_providers = {}
    for pid, pdata in all_p.items():
        keys = pdata.get("api_keys", [])
        has_k = bool(keys)
        khint = ""
        if keys:
            raw = keys[0].get("key", "") if isinstance(keys[0], dict) else str(keys[0])
            if len(raw) >= 12:
                khint = raw[-4:]
        saved_providers[pid] = {
            "provider": pid,
            "models": pdata.get("models", []),
            "base_url": pdata.get("extra_config", {}).get("base_url", ""),
            "has_key": has_k,
            "key_hint": khint,
            "is_active": pdata.get("is_active", False),
        }

    return {
        "provider": c["provider"],
        "models": c["models"],
        "base_url": c["base_url"],
        "has_key": bool(c["key"]),
        "key_hint": c["key"][-4:] if len(c["key"]) >= 12 else "",
        "ready": ready(),
        "label": label(),
        "saved_here": bool(_from_dashboard().get("provider") or all_p),
        "providers": [
            {"id": k, "label": v[0], "address": v[1], "needs_key": k not in NO_KEY, "key_url": v[3], "example": v[4]}
            for k, v in PROVIDERS.items()
        ],
        "gemini_keys": gem_keys,
        "gemini_all_quota_reached": is_gemini_all_quota_reached(),
        "gemini_3_models": GEMINI_3_MODELS,
        "saved_providers": saved_providers,
    }


def save(provider_id: str, model_ids, address: str = "", new_key=None) -> dict:
    """Save the choice made in the dashboard. Other providers' settings in SQLite are NEVER wiped."""
    p = ALIASES.get(str(provider_id or "").strip().lower(), str(provider_id or "").strip().lower())
    if p not in PROVIDERS:
        raise ValueError("Choose one of the AI services in the list.")
    if isinstance(model_ids, str):
        model_ids = model_ids.split(",")
    ms = [str(m).strip()[:120] for m in (model_ids or []) if str(m).strip()][:6]
    if not ms:
        raise ValueError("Enter the model to use.")
    if any(not re.fullmatch(r"[\w.\-:/@]+", m) for m in ms):
        raise ValueError("A model id has letters, digits and . - : / only.")

    # Strict Gemini 3.x validation: reject Gemini 1.x and 2.x models
    if p == "gemini":
        for m in ms:
            if re.match(r"^gemini-[12](\.|$)", m.lower()):
                raise ValueError(f"Gemini 1.x and 2.x models ({m}) are not supported. Please use Gemini 3.x models.")

    address = str(address or "").strip().rstrip("/")[:300]
    if address and not re.fullmatch(r"https?://[^\s]+", address):
        raise ValueError("The address has to start with http:// or https://")
    if p == "custom" and not address:
        raise ValueError("A custom service needs its address, for example http://127.0.0.1:1234/v1")

    all_p = _db_get_all()
    existing_row = all_p.get(p, {})
    keys = list(existing_row.get("api_keys", []))

    if new_key is not None and str(new_key).strip():
        # A new key was typed
        k_str = str(new_key).strip()[:400]
        if p == "gemini":
            # Add or replace primary key
            if keys:
                keys[0]["key"] = k_str
                keys[0]["status"] = "active"
                keys[0]["cooldown_until"] = 0.0
            else:
                keys.append({"id": "key_1", "key": k_str, "label": "Primary Key", "status": "active", "cooldown_until": 0.0, "created_at": time.time()})
        else:
            if keys and isinstance(keys[0], dict):
                keys[0]["key"] = k_str
                keys[0]["status"] = "active"
            else:
                keys = [{"id": f"key_{p}_1", "key": k_str, "label": "Primary Key", "status": "active", "cooldown_until": 0.0, "created_at": time.time()}]
    elif new_key is not None and not str(new_key).strip() and not keys:
        if p not in NO_KEY:
            raise ValueError("Paste the key for this service.")
    else:
        # new_key is None or empty: keep existing keys if available
        if not keys and p not in NO_KEY:
            # Check env fallback
            if config.ai_key() and current()["provider"] == p:
                keys = [{"id": f"key_{p}_1", "key": config.ai_key(), "label": "Primary Key", "status": "active", "cooldown_until": 0.0, "created_at": time.time()}]
            else:
                raise ValueError("Paste the key for this service.")

    extra = dict(existing_row.get("extra_config", {}))
    extra["base_url"] = address

    _db_save_provider(p, keys, ms, is_active=True, extra_config=extra)
    return public()


def list_models(provider_id=None, address=None, with_key=None) -> list:
    """The model ids a service offers, so the settings page can show a list to pick from."""
    c = current()
    p = ALIASES.get(str(provider_id or c["provider"]).lower(), str(provider_id or c["provider"]).lower())
    if p not in PROVIDERS:
        raise ValueError("Choose one of the AI services in the list.")
    k = str(with_key or "").strip() or (c["key"] if c["provider"] == p else "")
    base = str(address or "").strip().rstrip("/") or (c["base_url"] if c["provider"] == p else "") or PROVIDERS[p][1]
    if not base:
        raise ValueError("Enter the service's address first.")
    how = PROVIDERS[p][2]
    if how == "gemini":
        r = httpx.get(base, params={"pageSize": 200}, headers={"x-goog-api-key": k}, timeout=20)
    elif how == "anthropic":
        r = httpx.get(f"{base}/models", params={"limit": 200}, headers={"x-api-key": k, "anthropic-version": "2023-06-01"}, timeout=20)
    else:
        r = httpx.get(f"{base}/models", headers={"Authorization": f"Bearer {k}"} if k else {}, timeout=20)
    if r.status_code != 200:
        raise AIError(f"HTTP {r.status_code} {r.text[:200].replace(k or chr(0), '<key>')}")
    data = r.json()
    if how == "gemini":
        found = [m.get("name", "").split("/")[-1] for m in data.get("models", [])
                 if "generateContent" in (m.get("supportedGenerationMethods") or [])]
        # Remove ALL Gemini 1.x and 2.x models!
        found = [m for m in found if not re.match(r"^gemini-[12](\.|$)", m.lower())]
    else:
        found = [m.get("id", "") for m in data.get("data", []) if isinstance(m, dict)]
    return sorted({m for m in found if m})[:400]


# ---------------------------------------------------------------- Gemini Multi-Key Management
def get_gemini_keys(mask: bool = False) -> list:
    """List of all configured Gemini API keys."""
    all_p = _db_get_all()
    gem = all_p.get("gemini", {})
    keys = list(gem.get("api_keys", []))
    if not keys and config.ai_key():
        keys = [{
            "id": "key_env",
            "key": config.ai_key(),
            "label": "Default (from environment)",
            "status": "active",
            "cooldown_until": 0.0,
            "created_at": time.time(),
        }]
    now = time.time()
    out = []
    for k in keys:
        item = dict(k)
        if item.get("status") == "quota_exhausted" and now >= item.get("cooldown_until", 0):
            item["status"] = "active"
            item["cooldown_until"] = 0.0
        if mask:
            raw = item.get("key", "")
            item["key_hint"] = raw[-4:] if len(raw) >= 12 else "****"
            item["masked_key"] = (raw[:4] + "••••" + raw[-4:]) if len(raw) >= 12 else "••••"
            item.pop("key", None)
        out.append(item)
    return out


def add_gemini_key(raw_key: str, label: str = "") -> dict:
    """Store a new Gemini API key."""
    raw_key = str(raw_key or "").strip()
    if not raw_key or len(raw_key) < 8 or not re.fullmatch(r"\S{8,}", raw_key):
        raise ValueError("Please provide a valid Gemini API key (at least 8 characters).")
    label = str(label or "").strip()[:80] or "Key"

    all_p = _db_get_all()
    gem = all_p.get("gemini", {})
    keys = list(gem.get("api_keys", []))

    for existing in keys:
        if existing.get("key") == raw_key:
            if label and label != existing.get("label"):
                existing["label"] = label
            existing["status"] = "active"
            existing["cooldown_until"] = 0.0
            _db_save_provider("gemini", keys, gem.get("models") or GEMINI_3_MODELS, gem.get("is_active", False), gem.get("extra_config", {}))
            set_gemini_all_quota_reached(False)
            return existing

    new_id = "key_" + uuid.uuid4().hex[:8]
    new_obj = {
        "id": new_id,
        "key": raw_key,
        "label": label,
        "status": "active",
        "cooldown_until": 0.0,
        "created_at": time.time(),
    }
    keys.append(new_obj)
    models = gem.get("models") or list(GEMINI_3_MODELS)
    _db_save_provider("gemini", keys, models, gem.get("is_active", False), gem.get("extra_config", {}))
    set_gemini_all_quota_reached(False)

    return {
        "id": new_id,
        "label": label,
        "status": "active",
        "cooldown_until": 0.0,
        "key_hint": raw_key[-4:] if len(raw_key) >= 12 else "****",
        "masked_key": (raw_key[:4] + "••••" + raw_key[-4:]) if len(raw_key) >= 12 else "••••",
        "created_at": new_obj["created_at"],
    }


def update_gemini_key_label(key_id: str, label: str) -> dict:
    """Label or rename a specific Gemini key."""
    all_p = _db_get_all()
    gem = all_p.get("gemini", {})
    keys = list(gem.get("api_keys", []))
    target = next((k for k in keys if k["id"] == key_id), None)
    if not target:
        raise ValueError("No such Gemini key found.")
    target["label"] = str(label or "").strip()[:80] or "Key"
    _db_save_provider("gemini", keys, gem.get("models") or GEMINI_3_MODELS, gem.get("is_active", False), gem.get("extra_config", {}))
    return {
        "id": target["id"],
        "label": target["label"],
        "status": target["status"],
        "cooldown_until": target.get("cooldown_until", 0.0),
        "key_hint": target["key"][-4:] if len(target.get("key", "")) >= 12 else "****",
    }


def test_gemini_key(key_id: str) -> dict:
    """Live lightweight validation call for a specific Gemini key."""
    all_p = _db_get_all()
    gem = all_p.get("gemini", {})
    keys = list(gem.get("api_keys", []))
    target = next((k for k in keys if k["id"] == key_id), None)
    if not target:
        if key_id == "key_env" and config.ai_key():
            raw_key = config.ai_key()
            target = {"id": "key_env", "key": raw_key, "label": "Default", "status": "active", "cooldown_until": 0.0}
        else:
            raise ValueError("No such Gemini key found.")
    else:
        raw_key = target["key"]

    base = (gem.get("extra_config", {}).get("base_url") or "").rstrip("/") or PROVIDERS["gemini"][1]
    models_to_test = gem.get("models") or list(GEMINI_3_MODELS)
    test_models = []
    for pref in ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-3.6-flash"]:
        if pref in models_to_test and pref not in test_models:
            test_models.append(pref)
    for m in models_to_test:
        if m not in test_models:
            test_models.append(m)

    test_body = {
        "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
        "generationConfig": {"maxOutputTokens": 1},
    }

    last_status = None
    last_msg = ""
    for tm in test_models:
        try:
            r = httpx.post(f"{base}/{tm}:generateContent", json=test_body, headers={"x-goog-api-key": raw_key}, timeout=12)
            if r.status_code == 200:
                target["status"] = "active"
                target["cooldown_until"] = 0.0
                if target.get("id") != "key_env":
                    _db_save_provider("gemini", keys, gem.get("models") or GEMINI_3_MODELS, gem.get("is_active", False), gem.get("extra_config", {}))
                set_gemini_all_quota_reached(False)
                return {"ok": True, "status": "active", "message": f"Key is active and responsive ({tm})."}
            if r.status_code == 429:
                target["status"] = "quota_exhausted"
                target["cooldown_until"] = time.time() + 60.0
                if target.get("id") != "key_env":
                    _db_save_provider("gemini", keys, gem.get("models") or GEMINI_3_MODELS, gem.get("is_active", False), gem.get("extra_config", {}))
                return {"ok": False, "status": "quota_exhausted", "message": "Quota / rate limit exceeded (HTTP 429). 60s cooldown applied."}
            if r.status_code in (400, 403):
                err_data = {}
                try:
                    err_data = r.json().get("error", {})
                except Exception:
                    pass
                msg = err_data.get("message") or r.text[:120]
                target["status"] = "invalid"
                if target.get("id") != "key_env":
                    _db_save_provider("gemini", keys, gem.get("models") or GEMINI_3_MODELS, gem.get("is_active", False), gem.get("extra_config", {}))
                if "free tier is not available" in msg.lower() or "billing" in msg.lower():
                    clean_msg = "Gemini free tier not available for this project (billing required)."
                else:
                    clean_msg = f"Key rejected (HTTP {r.status_code}): {msg[:100]}"
                return {"ok": False, "status": "invalid", "message": clean_msg}
            if r.status_code in (500, 502, 503, 504):
                last_status = "active"
                last_msg = f"Model {tm} temporarily busy (HTTP {r.status_code}), testing alternatives..."
                continue
            last_status = "invalid"
            last_msg = f"HTTP {r.status_code}: {r.text[:100]}"
        except httpx.TimeoutException:
            last_status = target.get("status", "active")
            last_msg = f"Timeout connecting to {tm}."
            continue
        except Exception as e:
            return {"ok": False, "status": target.get("status", "active"), "message": f"Validation failed: {str(e)[:160]}"}

    if last_status == "active":
        return {"ok": True, "status": "active", "message": "Key recognized, but models are currently experiencing high demand."}
    return {"ok": False, "status": last_status or "invalid", "message": last_msg or "Validation failed."}


def reorder_gemini_keys(order: list) -> list:
    """Reorder the priority of Gemini keys."""
    all_p = _db_get_all()
    gem = all_p.get("gemini", {})
    keys = list(gem.get("api_keys", []))
    key_map = {k["id"]: k for k in keys}
    reordered = []
    for kid in order:
        if kid in key_map:
            reordered.append(key_map.pop(kid))
    for remaining in keys:
        if remaining["id"] in key_map:
            reordered.append(remaining)
    _db_save_provider("gemini", reordered, gem.get("models") or GEMINI_3_MODELS, gem.get("is_active", False), gem.get("extra_config", {}))
    return get_gemini_keys(mask=True)


def delete_gemini_key(key_id: str) -> list:
    """Delete a Gemini key."""
    all_p = _db_get_all()
    gem = all_p.get("gemini", {})
    keys = [k for k in gem.get("api_keys", []) if k["id"] != key_id]
    _db_save_provider("gemini", keys, gem.get("models") or GEMINI_3_MODELS, gem.get("is_active", False), gem.get("extra_config", {}))
    return get_gemini_keys(mask=True)


def reset_gemini_cooldowns(key_id: str = None) -> list:
    """Reset quota cooldowns for a specific key or all keys."""
    all_p = _db_get_all()
    gem = all_p.get("gemini", {})
    keys = list(gem.get("api_keys", []))
    with _cooldown_lock:
        if key_id:
            for k in keys:
                if k["id"] == key_id:
                    k["status"] = "active"
                    k["cooldown_until"] = 0.0
            to_del = [pair for pair in _cooldowns if pair[0] == key_id]
            for pair in to_del:
                _cooldowns.pop(pair, None)
        else:
            for k in keys:
                k["status"] = "active"
                k["cooldown_until"] = 0.0
            _cooldowns.clear()
    _db_save_provider("gemini", keys, gem.get("models") or GEMINI_3_MODELS, gem.get("is_active", False), gem.get("extra_config", {}))
    set_gemini_all_quota_reached(False)
    return get_gemini_keys(mask=True)


# ---------------------------------------------------------------- Gemini 3.x Rotation Engine
def call_gemini_rotation(prompt: str, schema: dict):
    """Executes a Gemini call with multi-key and Gemini 3 model rotation.
    On HTTP 429, marks (key_id, model_id) cooldown for 60s, tries next Gemini 3 model on that key,
    then moves to next key. If all combinations are exhausted, raises GeminiQuotaExhaustedError."""
    all_p = _db_get_all()
    gem = all_p.get("gemini", {})
    keys = [k for k in gem.get("api_keys", []) if k.get("key")]
    if not keys and config.ai_key():
        keys = [{"id": "key_env", "key": config.ai_key(), "label": "Default", "status": "active", "cooldown_until": 0.0}]

    if not keys:
        raise ValueError("No Gemini API keys are configured.")

    configured_models = [m for m in (gem.get("models") or models()) if not re.match(r"^gemini-[12](\.|$)", m.lower())]
    models_to_try = []
    for m in configured_models:
        if m not in models_to_try:
            models_to_try.append(m)
    for m in GEMINI_3_MODELS:
        if m not in models_to_try:
            models_to_try.append(m)

    base = (gem.get("extra_config", {}).get("base_url") or "").rstrip("/") or PROVIDERS["gemini"][1]
    now = time.time()

    keys_changed = False
    for k in keys:
        if k.get("status") == "quota_exhausted" and now >= k.get("cooldown_until", 0):
            k["status"] = "active"
            k["cooldown_until"] = 0.0
            keys_changed = True
    if keys_changed:
        _db_save_provider("gemini", keys, gem.get("models") or GEMINI_3_MODELS, gem.get("is_active", True), gem.get("extra_config", {}))
        set_gemini_all_quota_reached(False)

    active_keys = [k for k in keys if k.get("status") != "invalid"]
    if not active_keys:
        set_gemini_all_quota_reached(True)
        raise GeminiQuotaExhaustedError("All Gemini keys are marked invalid.")

    last_err = None

    for k in active_keys:
        key_id = k["id"]
        raw_key = k["key"]

        if k.get("status") == "quota_exhausted" and time.time() < k.get("cooldown_until", 0):
            continue

        key_models_available = False
        for model in models_to_try:
            with _cooldown_lock:
                cd = _cooldowns.get((key_id, model), 0)
            if time.time() < cd:
                continue

            key_models_available = True
            body = {
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.4,
                    "responseMimeType": "application/json",
                    "responseSchema": schema,
                },
            }
            try:
                r = httpx.post(f"{base}/{model}:generateContent", json=body, headers={"x-goog-api-key": raw_key}, timeout=120)
                if r.status_code == 200:
                    data = r.json()
                    text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
                    u = data.get("usageMetadata", {})
                    sent = int(u.get("promptTokenCount") or 0)
                    wrote = max(0, int(u.get("totalTokenCount") or 0) - sent)
                    if k.get("status") == "quota_exhausted":
                        k["status"] = "active"
                        _db_save_provider("gemini", keys, gem.get("models") or GEMINI_3_MODELS, gem.get("is_active", True), gem.get("extra_config", {}))
                    set_gemini_all_quota_reached(False)
                    return json.loads(text), model, sent, wrote

                if r.status_code == 429:
                    with _cooldown_lock:
                        _cooldowns[(key_id, model)] = time.time() + 60.0
                    continue

                if r.status_code in (500, 502, 503, 504):
                    # Model temporarily overloaded / spikes in demand: cooldown model for 30s and try next
                    with _cooldown_lock:
                        _cooldowns[(key_id, model)] = time.time() + 30.0
                    continue

                if r.status_code == 404:
                    with _cooldown_lock:
                        _cooldowns[(key_id, model)] = time.time() + 3600.0
                    continue

                if r.status_code in (400, 403):
                    # Key invalid, project billing required / free tier disabled, or permission denied:
                    # Mark key invalid and rotate immediately to next key
                    k["status"] = "invalid"
                    _db_save_provider("gemini", keys, gem.get("models") or GEMINI_3_MODELS, gem.get("is_active", True), gem.get("extra_config", {}))
                    break

                last_err = AIError(f"HTTP {r.status_code} {r.text[:200]}")
            except httpx.TimeoutException:
                with _cooldown_lock:
                    _cooldowns[(key_id, model)] = time.time() + 30.0
                continue
            except Exception as e:
                err_text = str(e)
                last_err = e
                if "HTTP 429" in err_text or "RESOURCE_EXHAUSTED" in err_text:
                    with _cooldown_lock:
                        _cooldowns[(key_id, model)] = time.time() + 60.0
                    continue
                if any(x in err_text for x in ("503", "500", "502", "504", "timeout", "timed out")):
                    with _cooldown_lock:
                        _cooldowns[(key_id, model)] = time.time() + 30.0
                    continue
                continue

        # If all models for this key failed with 429/temporary failure
        if k.get("status") != "invalid":
            if not key_models_available or all(time.time() < _cooldowns.get((key_id, m), 0) for m in models_to_try):
                k["status"] = "quota_exhausted"
                k["cooldown_until"] = time.time() + 60.0
                _db_save_provider("gemini", keys, gem.get("models") or GEMINI_3_MODELS, gem.get("is_active", True), gem.get("extra_config", {}))

    set_gemini_all_quota_reached(True)
    raise GeminiQuotaExhaustedError()


def test() -> dict:
    """One tiny request with the settings in use, to prove the key, the address and the model all work."""
    ms = models()
    if not ready():
        raise ValueError("Save a service, a model and a key first.")
    out, sent, wrote = call(ms[0], 'Answer with a JSON array that holds the single string "ok".', {"type": "ARRAY", "items": {"type": "STRING"}})
    return {"ok": True, "model": ms[0], "tokens": sent + wrote, "answer": str(out)[:80]}


def _fail(r, model):
    text = r.text[:300].replace(key() or "\0", "<key>")
    raise AIError(f"HTTP {r.status_code} {text}")


# ---------------------------------------------------------------- the schema, said in words for chat models
def _json_schema(s):
    """Gemini's schema spelling (ARRAY, OBJECT...) as ordinary JSON Schema."""
    if isinstance(s, dict):
        return {k: (v.lower() if k == "type" and isinstance(v, str) else _json_schema(v)) for k, v in s.items()}
    if isinstance(s, list):
        return [_json_schema(x) for x in s]
    return s


def _wrapped(prompt, schema):
    return (prompt + "\n\nAnswer with JSON only, no other text and no code fences: one object with a single key "
            "\"items\", whose value follows this JSON Schema:\n" + json.dumps(_json_schema(schema)))


def _parse(text):
    """The JSON in an answer, even if the model wrapped it in a code fence or a sentence."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        out = json.loads(text)
    except ValueError:
        m = re.search(r"[\[{].*[\]}]", text, re.S)
        if not m:
            raise AIError("The AI did not answer with JSON.")
        out = json.loads(m.group(0))
    if isinstance(out, dict) and "items" in out:
        out = out["items"]
    return out


# ---------------------------------------------------------------- one call
def call(model: str, prompt: str, schema: dict):
    """Ask one model. Returns (the parsed JSON answer, tokens sent, tokens written back)."""
    how = PROVIDERS[provider()][2]
    return {"gemini": _gemini, "openai": _openai, "anthropic": _anthropic}[how](model, prompt, schema)


def _gemini(model, prompt, schema, key_override=None):
    k = key_override or key()
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.4, "responseMimeType": "application/json", "responseSchema": schema}}
    r = httpx.post(f"{base_url()}/{model}:generateContent", json=body, headers={"x-goog-api-key": k}, timeout=120)
    if r.status_code != 200:
        _fail(r, model)
    data = r.json()
    text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
    u = data.get("usageMetadata", {})
    sent = int(u.get("promptTokenCount") or 0)
    return json.loads(text), sent, max(0, int(u.get("totalTokenCount") or 0) - sent)


def _openai(model, prompt, schema):
    headers = {"Authorization": f"Bearer {key()}"} if key() else {}
    body = {"model": model, "messages": [{"role": "user", "content": _wrapped(prompt, schema)}]}
    extras = [{"temperature": 0.4, "response_format": {"type": "json_object"}}, {"response_format": {"type": "json_object"}}, {}]
    r = None
    for extra in extras:
        r = httpx.post(f"{base_url()}/chat/completions", json={**body, **extra}, headers=headers, timeout=180)
        if r.status_code not in (400, 422):
            break
    if r.status_code != 200:
        _fail(r, model)
    data = r.json()
    u = data.get("usage") or {}
    return _parse(data["choices"][0]["message"].get("content")), int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)


def _anthropic(model, prompt, schema):
    body = {"model": model, "max_tokens": 8192, "messages": [{"role": "user", "content": _wrapped(prompt, schema)}]}
    r = httpx.post(f"{base_url()}/messages", json=body, timeout=180,
                   headers={"x-api-key": key(), "anthropic-version": "2023-06-01"})
    if r.status_code != 200:
        _fail(r, model)
    data = r.json()
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    u = data.get("usage") or {}
    return _parse(text), int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0)

"""Test Multi-Provider Storage, Multi-Key Gemini Management, and Gemini 3.x Rotation Engine."""
import json
import os
import sys
import tempfile
import time

os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-gemini-test-")
os.environ.update(
    DATABASE_URL="",
    PEAKCUT_ACCOUNTS="0",
    PEAKCUT_AI_PROVIDER="gemini",
    PEAKCUT_AI_MODELS="",
    PEAKCUT_AI_KEY="",
    PEAKCUT_AI_KEY_FILE="",
    PEAKCUT_AI_BASE_URL="",
    PEAKCUT_PUBLIC_URL="http://127.0.0.1:8765",
)
sys.path.insert(0, ".")
from fastapi.testclient import TestClient

from studio import ai, config, db, server

db.init()
c = TestClient(server.app, base_url="http://127.0.0.1:8765", follow_redirects=False)
ORIGIN = {"Origin": "http://127.0.0.1:8765"}
fails = []


def check(name, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   got {got!r}"))
    if not ok:
        fails.append(name)


# 1. Multi-key storage endpoints
k1 = "AIzaSyTestKeyNumberOne1111111"
k2 = "AIzaSyTestKeyNumberTwo2222222"
k3 = "AIzaSyTestKeyNumberThree33333"

# Add Key 1
r = c.post("/api/ai/gemini/keys", json={"key": k1, "label": "Key One (Primary)"}, headers=ORIGIN)
check("add key 1 status", r.status_code, 200)
keys = r.json()["keys"]
check("key 1 in list with label", len(keys) == 1 and keys[0]["label"] == "Key One (Primary)")
check("key 1 masked", "1111" in keys[0]["key_hint"] and "AIza" not in keys[0].get("key", ""))
key1_id = keys[0]["id"]

# Add Key 2 & 3
r = c.post("/api/ai/gemini/keys", json={"key": k2, "label": "Key Two"}, headers=ORIGIN)
key2_id = r.json()["added"]["id"]
r = c.post("/api/ai/gemini/keys", json={"key": k3, "label": "Key Three"}, headers=ORIGIN)
key3_id = r.json()["added"]["id"]

keys = c.get("/api/ai/gemini/keys").json()["keys"]
check("all 3 keys stored", len(keys), 3)

# Label update
r = c.put(f"/api/ai/gemini/keys/{key2_id}", json={"label": "Key Two (Backup)"}, headers=ORIGIN)
check("update label status", r.status_code, 200)
keys = c.get("/api/ai/gemini/keys").json()["keys"]
check("label updated", next(k for k in keys if k["id"] == key2_id)["label"], "Key Two (Backup)")

# Reorder keys
r = c.post("/api/ai/gemini/keys/reorder", json={"order": [key3_id, key1_id, key2_id]}, headers=ORIGIN)
check("reorder status", r.status_code, 200)
keys = r.json()["keys"]
check("reordered keys order", [k["id"] for k in keys], [key3_id, key1_id, key2_id])

# 2. Strict Gemini 3.x validation
check("reject gemini-2.5-flash", c.put("/api/ai", json={"provider": "gemini", "models": "gemini-2.5-flash"}, headers=ORIGIN).status_code, 400)
check("reject gemini-1.5-pro", c.put("/api/ai", json={"provider": "gemini", "models": "gemini-1.5-pro"}, headers=ORIGIN).status_code, 400)
check("reject gemini-2.0-flash", c.put("/api/ai", json={"provider": "gemini", "models": "gemini-2.0-flash"}, headers=ORIGIN).status_code, 400)

# Save with Gemini 3.x models
r = c.put("/api/ai", json={"provider": "gemini", "models": "gemini-3.8-flash, gemini-3.6-flash"}, headers=ORIGIN)
check("save gemini 3 models status", r.status_code, 200)
check("active provider is gemini", r.json()["provider"], "gemini")

# 3. Switching providers must NEVER wipe other providers' settings
r = c.put("/api/ai", json={"provider": "groq", "models": "llama-3.3-70b-versatile", "key": "gsk_testgroqkey123456789"}, headers=ORIGIN)
check("save groq status", r.status_code, 200)
check("active provider is now groq", r.json()["provider"], "groq")

# Verify gemini keys are STILL in sqlite and public api
ai_info = c.get("/api/ai").json()
check("gemini keys preserved when groq active", len(ai_info["gemini_keys"]), 3)
check("groq saved in saved_providers", "groq" in ai_info["saved_providers"] and ai_info["saved_providers"]["groq"]["has_key"])
check("gemini saved in saved_providers", "gemini" in ai_info["saved_providers"] and ai_info["saved_providers"]["gemini"]["has_key"])

# Switch back to Gemini without sending a key - previous keys and models are preserved!
r = c.put("/api/ai", json={"provider": "gemini", "models": "gemini-3.8-flash"}, headers=ORIGIN)
check("switch back to gemini status", r.status_code, 200)
check("gemini has_key is True", r.json()["has_key"], True)
check("groq settings still preserved in db", c.get("/api/ai").json()["saved_providers"]["groq"]["has_key"], True)

# 4. Rotation Engine: 429 cooldown & fallback to next model & next key
class FakeResponse:
    def __init__(self, status_code, data):
        self.status_code = status_code
        self._data = data
        self.text = json.dumps(data)

    def json(self):
        return self._data

history = []

def fake_rotation_post(url, json=None, headers=None, timeout=None):
    key_used = headers.get("x-goog-api-key")
    model_used = url.split("/")[-1].split(":")[0]
    history.append((key_used, model_used))

    # Let Key 3 fail on all models with 429
    if key_used == k3:
        return FakeResponse(429, {"error": {"code": 429, "message": "RESOURCE_EXHAUSTED", "status": "RESOURCE_EXHAUSTED"}})
    # Let Key 1 fail on gemini-3.8-flash with 429, but succeed on gemini-3.6-flash!
    if key_used == k1 and model_used == "gemini-3.8-flash":
        return FakeResponse(429, {"error": {"code": 429, "message": "RESOURCE_EXHAUSTED"}})
    if key_used == k1 and model_used == "gemini-3.6-flash":
        return FakeResponse(200, {
            "candidates": [{"content": {"parts": [{"text": json_dumps([{"start": 1, "end": 10, "score": 90, "hook": "H", "title": "T", "description": "D", "hashtags": [], "reason": "R"}])}]}}],
            "usageMetadata": {"promptTokenCount": 50, "totalTokenCount": 80}
        })
    return FakeResponse(200, {
        "candidates": [{"content": {"parts": [{"text": json_dumps([{"start": 1, "end": 10, "score": 90, "hook": "H", "title": "T", "description": "D", "hashtags": [], "reason": "R"}])}]}}],
        "usageMetadata": {"promptTokenCount": 50, "totalTokenCount": 80}
    })

def json_dumps(x):
    return json.dumps(x)

ai.httpx.post = fake_rotation_post

# Call gemini rotation:
# Key order is: [key3_id, key1_id, key2_id]
# Models to try: ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.1-pro-preview", "gemini-3-flash-preview"]
# Key 3 should exhaust its models (429), then rotation moves to Key 1!
# On Key 1, gemini-3.8-flash gets 429, so it automatically retries with gemini-3.6-flash, which succeeds!
history.clear()
ai.reset_gemini_cooldowns(None)
res, model, sent, wrote = ai.call_gemini_rotation("test prompt", {"type": "ARRAY", "items": {"type": "OBJECT"}})
check("rotation succeeded with model gemini-3.6-flash", model, "gemini-3.6-flash")
check("rotation tried k3 first", any(h[0] == k3 for h in history))
check("rotation then used k1", any(h[0] == k1 and h[1] == "gemini-3.6-flash" for h in history))
check("gemini_all_quota_reached is False", ai.is_gemini_all_quota_reached(), False)

# 5. When ALL keys and models return 429:
def fake_all_exhausted_post(url, json=None, headers=None, timeout=None):
    return FakeResponse(429, {"error": {"code": 429, "message": "RESOURCE_EXHAUSTED", "status": "RESOURCE_EXHAUSTED"}})

ai.httpx.post = fake_all_exhausted_post
history.clear()
ai.reset_gemini_cooldowns(None)

try:
    ai.call_gemini_rotation("test prompt", {"type": "ARRAY", "items": {"type": "OBJECT"}})
    check("all exhausted raised error", False)
except ai.GeminiQuotaExhaustedError as e:
    check("raised GeminiQuotaExhaustedError", True)
    check("error code is ALL_GEMINI_QUOTA_REACHED", getattr(e, "code", ""), "ALL_GEMINI_QUOTA_REACHED")
    check("error message matches specification", str(e), "All Gemini API keys and Gemini 3 models have reached their quota. Add another key, switch providers, or wait for quota reset.")

check("global gemini_all_quota_reached is True", ai.is_gemini_all_quota_reached(), True)

# Check /api/status broadcasts error
st = c.get("/api/status").json()
check("api/status reports gemini_all_quota_reached", st["ai"].get("gemini_all_quota_reached"), True)
check("api/status reports quota_error code", st["ai"].get("quota_error", {}).get("code"), "ALL_GEMINI_QUOTA_REACHED")

# Reset cooldowns clears the state
c.post("/api/ai/gemini/reset-cooldowns", headers=ORIGIN)
check("reset-cooldowns clears gemini_all_quota_reached", ai.is_gemini_all_quota_reached(), False)

# Delete key
r = c.delete(f"/api/ai/gemini/keys/{key3_id}", headers=ORIGIN)
check("delete key status", r.status_code, 200)
check("key deleted", len(r.json()["keys"]), 2)

# 6. Test 403 Billing / Project Error and 503 High Demand rotation
def fake_billing_and_503_post(url, json=None, headers=None, timeout=None):
    key_used = headers.get("x-goog-api-key")
    model_used = url.split("/")[-1].split(":")[0]

    # If Key 1: returns 403 billing required
    if key_used == k1:
        return FakeResponse(403, {"error": {"code": 403, "message": "The Gemini API free tier is not available for this project. To use the Gemini API, please set up billing."}})

    # If Key 2: model gemini-3.8-flash returns 503 (high demand), but gemini-3.6-flash succeeds!
    if key_used == k2 and model_used == "gemini-3.8-flash":
        return FakeResponse(503, {"error": {"code": 503, "message": "This model is currently experiencing high demand."}})
    if key_used == k2 and model_used == "gemini-3.6-flash":
        return FakeResponse(200, {
            "candidates": [{"content": {"parts": [{"text": json_dumps([{"start": 1, "end": 10, "score": 90, "hook": "H", "title": "T", "description": "D", "hashtags": [], "reason": "R"}])}]}}],
            "usageMetadata": {"promptTokenCount": 50, "totalTokenCount": 80}
        })
    return FakeResponse(200, {
        "candidates": [{"content": {"parts": [{"text": json_dumps([{"start": 1, "end": 10, "score": 90, "hook": "H", "title": "T", "description": "D", "hashtags": [], "reason": "R"}])}]}}],
        "usageMetadata": {"promptTokenCount": 50, "totalTokenCount": 80}
    })

ai.httpx.post = fake_billing_and_503_post
ai.reset_gemini_cooldowns(None)

# Rotation should see Key 1 return 403, mark Key 1 invalid, skip to Key 2,
# see gemini-3.8-flash return 503, retry next model gemini-3.6-flash on Key 2, and SUCCEED!
res, model, sent, wrote = ai.call_gemini_rotation("test prompt", {"type": "ARRAY", "items": {"type": "OBJECT"}})
check("403 and 503 recovered and succeeded with gemini-3.6-flash", model, "gemini-3.6-flash")

# Verify Key 1 was marked invalid in SQLite
updated_keys = c.get("/api/ai/gemini/keys").json()["keys"]
k1_entry = next(k for k in updated_keys if k["id"] == key1_id)
check("k1 marked invalid after 403 billing error", k1_entry["status"], "invalid")

# Test test_gemini_key identifies 403 billing failure
test_res = c.post(f"/api/ai/gemini/keys/{key1_id}/test", headers=ORIGIN).json()
check("test_gemini_key marks billing error as invalid", test_res["status"], "invalid")
check("test_gemini_key mentions billing / free tier", "billing" in test_res["message"].lower() or "free tier" in test_res["message"].lower())

print("\n" + ("ALL PASSED" if not fails else f"{len(fails)} FAILED"))
sys.exit(1 if fails else 0)

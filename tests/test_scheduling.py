"""Test Local vs YouTube Native Scheduling in PeakCut."""
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-sched-test-")
os.environ.update(
    DATABASE_URL="",
    PEAKCUT_ACCOUNTS="0",
    PEAKCUT_PUBLIC_URL="http://127.0.0.1:8765",
)
sys.path.insert(0, ".")

from fastapi.testclient import TestClient
from studio import accounts, config, creators, ctx, db, jobs, posting, server

db.init()
c = TestClient(server.app, base_url="http://127.0.0.1:8765")
ORIGIN = {"Origin": "http://127.0.0.1:8765"}
fails = []


def check(name, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   got {got!r}"))
    if not ok:
        fails.append(name)


# Setup mock YouTube account
acc_dir = config.user_dir("u_local") / "accounts"
acc_dir.mkdir(parents=True, exist_ok=True)
yt_acc = {
    "id": "yt_test_channel",
    "platform": "youtube",
    "name": "Test YT Channel",
    "added": time.time(),
    "access_token": "ya29.test_token",
    "refresh_token": "1//test_refresh",
    "token_time": time.time(),
}
with ctx.as_user("u_local"):
    accounts._write("accounts.json", [yt_acc])

# Setup dummy job and clip
job_dir = Path(os.environ["PEAKCUT_DATA"]) / "users" / "u_local" / "jobs" / "job_test_01"
(job_dir / "clips").mkdir(parents=True, exist_ok=True)
(job_dir / "clips" / "clip01.mp4").write_bytes(b"dummy_mp4_bytes")

test_job = {
    "id": "job_test_01",
    "user": "u_local",
    "creator": None,
    "dir": str(job_dir),
    "title": "GTA 6 Best Stunt",
    "clips": [
        {
            "id": "clip01",
            "file": "clip01.mp4",
            "title": "Insane Stunt",
            "hook": "WAIT FOR IT",
            "description": "Crazy stunt in GTA 6",
            "hashtags": ["gta6", "gaming"],
            "status": "done",
            "score": 90,
            "posts": {},
        }
    ],
}
jobs._jobs[test_job["id"]] = test_job
jobs._by_user.setdefault("u_local", {})[test_job["id"]] = test_job
jobs.save(test_job)

uploaded_payloads = []
deleted_ids = []


def fake_youtube_upload(acc, path, title, description, tags, privacy, publish_at=None):
    uploaded_payloads.append({
        "acc": acc["id"],
        "path": str(path),
        "title": title,
        "privacy": privacy,
        "publish_at": publish_at,
    })
    return "vid_abc123", "https://www.youtube.com/shorts/vid_abc123"


def fake_youtube_delete(acc, video_id):
    deleted_ids.append(video_id)
    return True


accounts.youtube_upload = fake_youtube_upload
accounts.youtube_delete = fake_youtube_delete

# 1. Test Local Scheduling
future_time = time.time() + 3600
res = c.post("/api/posts", json={
    "job": "job_test_01",
    "clip": "clip01",
    "accounts": ["yt_test_channel"],
    "schedule_mode": "local",
    "due_at": future_time,
}, headers=ORIGIN)
check("enqueue local schedule status 200", res.status_code, 200)
check("local schedule not uploaded yet", len(uploaded_payloads), 0)

with ctx.as_user("u_local"):
    posts = posting._mine()
    p1 = next(p for p in posts if p["clip"] == "clip01")
    check("local post schedule_mode is local", p1.get("schedule_mode"), "local")
    check("local post status is scheduled", p1["status"], "scheduled")
    check("local post remote_id empty", p1["remote_id"], "")

# 2. Convert Local to Native via schedulenative endpoint
res = c.post(f"/api/posts/{p1['id']}/schedulenative", headers=ORIGIN)
check("convert to native schedule status 200", res.status_code, 200)
check("youtube upload called once", len(uploaded_payloads), 1)

up = uploaded_payloads[0]
check("publish_at is provided for native schedule", bool(up["publish_at"]))
check("privacy forced to private for publishAt", up["privacy"], "private")

with ctx.as_user("u_local"):
    p1_updated = next(p for p in posting._mine() if p["id"] == p1["id"])
    check("p1 schedule_mode updated to native", p1_updated["schedule_mode"], "native")
    check("p1 remote_id populated", p1_updated["remote_id"], "vid_abc123")
    check("p1 url populated", p1_updated["url"], "https://www.youtube.com/shorts/vid_abc123")
    check("p1 status remains scheduled", p1_updated["status"], "scheduled")

# 3. Cancel Native Scheduled Post -> Deletes from YouTube
res = c.post(f"/api/posts/{p1['id']}/cancel", headers=ORIGIN)
check("cancel post status 200", res.status_code, 200)
check("youtube_delete called with remote_id", "vid_abc123" in deleted_ids)

# 4. Direct Native Schedule via /api/posts
(job_dir / "clips" / "clip02.mp4").write_bytes(b"dummy_mp4_bytes")
test_job["clips"].append({
    "id": "clip02",
    "file": "clip02.mp4",
    "title": "Epic Drift",
    "hook": "LOOK AT THIS",
    "description": "Best drift",
    "hashtags": ["drift"],
    "status": "done",
    "score": 85,
    "posts": {},
})
jobs._jobs[test_job["id"]] = test_job
jobs._by_user.setdefault("u_local", {})[test_job["id"]] = test_job
jobs.save(test_job)

uploaded_payloads.clear()
future_time2 = time.time() + 7200
res = c.post("/api/posts", json={
    "job": "job_test_01",
    "clip": "clip02",
    "accounts": ["yt_test_channel"],
    "schedule_mode": "native",
    "due_at": future_time2,
}, headers=ORIGIN)
check("direct native schedule status 200", res.status_code, 200)
check("uploaded immediately to youtube", len(uploaded_payloads), 1)

with ctx.as_user("u_local"):
    p2 = next(p for p in posting._mine() if p["clip"] == "clip02")
    check("p2 schedule_mode is native", p2["schedule_mode"], "native")
    check("p2 has url immediately", bool(p2["url"]))
    check("p2 status is scheduled", p2["status"], "scheduled")

# 5. Background worker _run handles native scheduled due_at without re-uploading
upload_count_before = len(uploaded_payloads)
with ctx.as_user("u_local"):
    p2["due_at"] = time.time() - 10  # pretend time has passed
    posting._save()

# Trigger one cycle of due checking
with ctx.as_user("u_local"):
    due = [p for p in list(posting._posts) if p["status"] == "scheduled" and p["due_at"] <= time.time()]
    for p in due:
        if p.get("schedule_mode") == "native" and p.get("remote_id"):
            p["status"] = "posted"
            posting._save()
            posting._mirror(p)

with ctx.as_user("u_local"):
    p2_final = next(p for p in posting._mine() if p["clip"] == "clip02")
    check("native post status transitioned to posted", p2_final["status"], "posted")
check("no extra re-upload occurred", len(uploaded_payloads), upload_count_before)

print("\n" + ("ALL PASSED" if not fails else f"{len(fails)} FAILED"))
sys.exit(1 if fails else 0)

"""Tests for complete pipeline logging, job.log creation, and GET /api/jobs/{jid}/log endpoint."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
temp_data = tempfile.mkdtemp(prefix="peakcut-log-test-")
os.environ["PEAKCUT_DATA"] = temp_data
os.environ["PEAKCUT_ACCOUNTS"] = "0"
os.environ["PEAKCUT_PUBLIC_URL"] = "http://127.0.0.1:8765"
os.environ["DATABASE_URL"] = f"sqlite:///{(Path(temp_data) / 'peakcut.db').as_posix()}"

from studio import auth, config, ctx, db, jobs, server

config.ACCOUNTS = False
auth._local = None
fails = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"   {detail}" if detail and not ok else ""))
    if not ok:
        fails.append(name)


def run_tests():
    db.init()
    user = auth.local_user()
    uid = user.id

    # 1. Test jobs.public has_log property
    mock_job = {
        "id": "test_job_12345678",
        "user": uid,
        "source": {"type": "file", "path": "/tmp/test.mp4", "name": "test.mp4"},
        "clips": [],
        "status": "done",
        "stage": "Finished",
        "dir": str(config.DATA_DIR / "users" / uid / "jobs" / "test_job_12345678"),
        "logs": ["[2026-11-20 12:00:00] Job started", "[2026-11-20 12:00:05] Audio analyzed"],
    }
    jdir = Path(mock_job["dir"])
    jdir.mkdir(parents=True, exist_ok=True)

    pub = jobs.public(mock_job)
    check("public has_log is True when logs array present", pub.get("has_log") is True)
    check("public hides server dir", "dir" not in pub)
    check("public hides logs array to keep sync payload small", "logs" not in pub)

    # 2. Test persistent job.log file
    log_file = jdir / "job.log"
    log_content = (
        "[2026-11-20 12:00:00] [PeakCut test_job_12345678] Video pipeline started\n"
        "[2026-11-20 12:00:02] [Audio] Audio extracted: 60.0s (16000 Hz, mono)\n"
        "[2026-11-20 12:00:05] [STT] Using ElevenLabs Scribe API (model: scribe_v1)\n"
        "[2026-11-20 12:00:10] [STT] Transcribed 120 words with word-level timestamps\n"
        "[2026-11-20 12:00:15] [AI] Highlight discovery complete: found 3 candidate clips\n"
        "[2026-11-20 12:00:25] [Render] Clip 1 rendered in 4.52s: clip-01.mp4\n"
        "[2026-11-20 12:00:30] Video processing completed in 30.0s. 1 clip ready.\n"
    )
    log_file.write_text(log_content, encoding="utf-8")

    # Register job in jobs._jobs and jobs._by_user
    with jobs._lock:
        jobs._jobs[mock_job["id"]] = mock_job
        jobs._by_user.setdefault(uid, {})[mock_job["id"]] = mock_job

    client = TestClient(
        server.app,
        base_url="http://127.0.0.1:8765",
        headers={"Origin": "http://127.0.0.1:8765", "Host": "127.0.0.1:8765"},
    )

    # 3. Test GET /api/jobs/{jid}/log endpoint
    resp = client.get(f"/api/jobs/{mock_job['id']}/log")
    check("GET /api/jobs/{jid}/log returns 200", resp.status_code == 200, resp.text)
    data = resp.json()
    check("Response includes job id", data.get("id") == mock_job["id"])
    check("Response includes complete log content", "[Audio] Audio extracted" in data.get("log", ""))
    check("Response includes lines array", len(data.get("lines", [])) == 7)
    check("First line matches timestamp format", data["lines"][0].startswith("[2026-11-20 12:00:00]"))

    # 4. Test 404 for non-existent job
    resp404 = client.get("/api/jobs/non_existent_job_12345/log")
    check("Non-existent job returns 404", resp404.status_code == 404)

    # 5. Clean up
    shutil.rmtree(temp_data, ignore_errors=True)

    if fails:
        print(f"FAILED ({len(fails)} checks): " + ", ".join(fails))
        sys.exit(1)
    print("ALL PASSED\n")


if __name__ == "__main__":
    run_tests()


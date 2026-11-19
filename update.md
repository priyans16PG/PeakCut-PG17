# PeakCut Updates & Changelog

## v1.2 — Multi-Provider API Storage & Multi-Key Gemini 3.x Rotation Engine

### Overview
PeakCut v1.2 introduces native multi-provider storage backed by SQLite, multi-key rotation for Google Gemini API keys, strict Gemini 3.x model pool enforcement, and an automatic rate-limit failover engine to eliminate pipeline downtime caused by free-tier RPM/RPD limits.

---

### Key Features & Updates

#### 1. Safe SQLite Migration & Multi-Provider Persistence
- **`ai_providers` Database Table**: Stored in SQLite (`data/peakcut.db`) with fields:
  - `provider`: Primary key (`gemini`, `groq`, `openai`, `xai`, `anthropic`, `openrouter`, `ollama`, `custom`).
  - `api_keys`: Structured JSON array storing multiple keys, labels, statuses, and cooldown timestamps.
  - `models`: Configured model list for the provider.
  - `is_active`: Identifies the currently active AI provider.
  - `extra_config`: Structured JSON for provider-specific settings (e.g., custom `base_url`).
  - `updated_at`: Last modified timestamp.
- **Cross-Provider Isolation**: Switching the active provider in the UI never overwrites, wipes, or resets the keys, models, or configurations of other providers.
- **Backward Compatibility**: Automatically migrates existing configurations from `ai.json` or environment variables without data loss. Active settings are continuously mirrored to `data/ai.json` for file-based background processes and test harnesses.

#### 2. Multi-Key Gemini Storage & Key Management
- **Key Object Schema**:
  - `id`: Unique identifier (`key_<hex>`).
  - `key`: Full API key (secured on server; never exposed to browser).
  - `label`: Custom label for tracking accounts (e.g., "Personal Free Tier", "Studio Backup").
  - `status`: Live key health state (`active`, `quota_exhausted`, `invalid`).
  - `cooldown_until`: Timestamp until which the key is temporarily sidelined.
  - `created_at`: Creation timestamp.
- **Management API Endpoints**:
  - `GET /api/ai/gemini/keys`: Returns all configured keys with masked key previews (`AIza••••XXXX`).
  - `POST /api/ai/gemini/keys`: Adds a new Gemini API key with custom label and format validation.
  - `PUT /api/ai/gemini/keys/{key_id}`: Renames/updates key label.
  - `POST /api/ai/gemini/keys/{key_id}/test`: Performs a lightweight live validation ping (`pageSize=1`) without burning generation quota; automatically flags `active`, `quota_exhausted`, or `invalid`.
  - `POST /api/ai/gemini/keys/reorder`: Updates key priority order for rotation.
  - `DELETE /api/ai/gemini/keys/{key_id}`: Removes an individual key.
  - `POST /api/ai/gemini/keys/{key_id}/reset`: Clears quota cooldown and restores key to `active`.
  - `POST /api/ai/gemini/reset-cooldowns`: Clears all cooldowns across all Gemini keys and models.

#### 3. Strict Gemini 3.x-Only Model Pool
- **Legacy Models Removed**: Purged all Gemini 1.x and 2.x models (`gemini-2.5-*`, `gemini-2.0-*`, `gemini-1.5-*`) from presets, defaults, dropdowns, and examples.
- **Strict Default Rotation Order**:
  1. `gemini-3.8-flash`
  2. `gemini-3.6-flash`
  3. `gemini-3.5-flash-lite`
  4. `gemini-3.1-flash-lite`
  5. `gemini-3.1-pro-preview`
  6. `gemini-3-flash-preview`
- **Validation Gate**: Backend rejects any attempt to save models matching `gemini-[12].*` with a clean error message.

#### 4. Automatic 429 Failover & Quota Exhaustion Engine
- **Per-Model Cooldown on HTTP 429 (`RESOURCE_EXHAUSTED`)**:
  - When a rate limit or quota exhaustion occurs, the `(key_id, model_id)` pair enters an automatic 60-second cooldown.
  - The rotation engine immediately retries using the next Gemini 3 model on that key.
  - If all Gemini 3 models on that key hit quota limits, the key transitions to `quota_exhausted` and the engine rotates to the next available key across its Gemini 3 models.
- **Graceful Full Quota Exhaustion**:
  - If all `(key, gemini-3-model)` combinations are exhausted:
    - Global state `gemini_all_quota_reached` is set to `True`.
    - Returns error code `ALL_GEMINI_QUOTA_REACHED` with message: `"All Gemini API keys and Gemini 3 models have reached their quota. Add another key, switch providers, or wait for quota reset."`
    - Broadcasted in real-time through `/api/status` and `/api/ai`.
    - Processing pipelines in `studio/brain.py` and `studio/jobs.py` catch the error gracefully and fall back to loudness/speech peak clipping without crashing worker threads or losing video progress.

#### 5. Settings -> AI Frontend Overhaul (`web/index.html`)
- **UI & Styling Compliance**:
  - Styled with `#F97316` primary buttons (`--brand: #F97316`), rounded cards, and subtle borders.
  - Preserves dark sidebar navigation and clean light canvas.
- **Dynamic Provider Switching**: Selecting a service from the dropdown populates that service's saved models, key hint, and endpoint address without clearing other providers' saved state.
- **Dedicated Gemini Cards**:
  - **Strict Gemini 3.x Model Pool**: Interactive pills showing the 6 Gemini 3 models in rotation order with a one-click "Use Strict Pool" preset.
  - **Gemini API Keys Table**: Reorder buttons (Move Up / Move Down), editable labels, masked key views, status pills (`Active`, `Quota Exhausted`, `Invalid`), live test button, cooldown reset, and delete action.
  - **Add Key Form**: Password input with label support.
  - **Quota Exhaustion Alert Banner**: Displays `ALL_GEMINI_QUOTA_REACHED` alert box with a one-click "Reset Quota Cooldowns" button when quotas are reached.

#### 6. Test Suite & Verification
- Added `tests/test_gemini_rotation.py` covering:
  - Multi-key CRUD and reordering.
  - Legacy model rejection (`gemini-2.5-flash`, `gemini-1.5-pro`, `gemini-2.0-flash`).
  - Provider switching and persistence without cross-provider data loss.
  - Live 429 simulation verifying multi-model and multi-key failover.
  - Full quota exhaustion simulation verifying `ALL_GEMINI_QUOTA_REACHED` error code and `/api/status` broadcast.
- Verified 100% pass rate across existing test suites (`tests/test_ai.py`, `tests/test_local.py`, `tests/test_roman.py`, `tests/test_delete.py`).

---

## v1.2.1 — Rotation Resilience & Project Billing Detection Patch

### Fixes & Enhancements
1. **Accurate Key Generation Validation (`test_gemini_key`)**:
   - Replaced shallow model listing (`GET /models?pageSize=1`) with live lightweight content generation (`maxOutputTokens: 1`).
   - Accurately detects and distinguishes between:
     - `Active`: Key is recognized and permitted to generate content.
     - `Quota Exhausted`: Key has temporarily exceeded rate limits (HTTP 429).
     - `Invalid / Billing Required`: Detects Google Cloud projects where the Gemini API free tier is not enabled or requires billing setup (`HTTP 403`), displaying exact guidance instead of false-positive "Active" pills.
2. **Resilient 403 & 503 Failover in `call_gemini_rotation`**:
   - **HTTP 403 (Billing / Project Disabled / Permission Denied)**: Automatically marks that key as `invalid` and advances immediately to the next available active key, preventing the video pipeline from aborting into loudness-only fallback.
   - **HTTP 503 / 500 / 502 / 504 / Timeouts (Spikes in High Demand)**: Automatically places that specific model on a 30-second cooldown and seamlessly retries the next Gemini 3 model in the rotation pool on that same key (e.g., falling back to `gemini-3.1-flash-lite` if `gemini-3.8-flash` experiences demand spikes).
3. **Fixed Local Variable Shadowing in `studio/jobs.py`**:
   - Resolved an `AttributeError` caused by local tuple unpacking (`found, ai = brain.find_clips(...)`) shadowing the `studio.ai` module when checking `ai.is_gemini_all_quota_reached()`.

---

## v1.3 — Dual Scheduling Engine: Local Queue vs Native YouTube Shorts Scheduling

### Overview
PeakCut v1.3 introduces native YouTube Shorts scheduling alongside local queue scheduling. Users can now choose whether clips are held locally until publication time or uploaded immediately to YouTube with YouTube's official `publishAt` parameter, making them appear instantly in YouTube Studio with a "Scheduled" visibility status.

---

### Key Features & Enhancements

#### 1. Dual Scheduling Modes
- **Local Queue (`local`)**:
  - Video remains on local disk in PeakCut's queue until the due time.
  - When the scheduled time arrives, PeakCut uploads and publishes the video.
  - Ideal for preserving daily YouTube API upload quota or working offline.
- **YouTube Native Scheduling (`native`)**:
  - Video is rendered and uploaded immediately to YouTube via the YouTube Data API v3 `videos.insert` endpoint.
  - Sent with `status.privacyStatus = "private"` and `status.publishAt = "<ISO 8601 UTC timestamp>"` (RFC 3339).
  - Appears immediately in **YouTube Studio** under **Content -> Shorts** with visibility marked as **Scheduled**.
  - YouTube's servers handle the public release automatically at the exact scheduled second, even if PeakCut or your computer is turned off.

#### 2. Clip Editor (`#clipdlg`) Enhancements
- **Schedule Mode Dropdown**: Choose between "Local queue" and "YouTube Native".
- **Dynamic Mode Hint**: Explains whether the video will be uploaded now or kept on disk.
- **Specific Time Picker (`datetime-local`)**: Pick an exact future date and time for posting, bypassing default hourly cadence when desired.
- **Interactive "Schedule post" Button**: Clearly reflects when a post is being scheduled rather than queued locally.

#### 3. Post Queue Table & 1-Click Conversions (`Overview`)
- **Distinct Status Pills**:
  - **`YT Scheduled (time)`** (green pill): Includes a direct clickable link to `https://www.youtube.com/shorts/{id}` so you can preview the scheduled Short immediately in YouTube Studio or Shorts player.
  - **`Local (time)`** (amber pill): Indicates the clip is waiting on local disk.
- **One-Click Actions on Waiting Posts**:
  - **`Send to YT Schedule`**: Instantly converts any locally waiting post into a YouTube Native scheduled video and uploads it right away.
  - **`Post now`**: Instantly publishes a waiting post ahead of schedule.
  - **`Cancel` with Remote Cleanup**: Cancelling a natively scheduled post automatically invokes `youtube_delete` via YouTube API to delete the scheduled video from your YouTube channel.

#### 4. Creator-Level Defaults
- Added `yt_schedule_mode` (`local` | `native`) to Creator profile settings in Creator setup/edit modal.
- Automated clipping pipelines respect the creator's default schedule mode when enqueuing new clips.

#### 5. Robust YouTube API Compliance & Automated Tests
- **Mandatory Privacy Status Handling**: YouTube Data API v3 rejects requests if `privacyStatus` is not `"private"` when `publishAt` is specified (`HTTP 400`). PeakCut strictly enforces `privacyStatus: "private"` during native scheduling.
- **Background Sync**: The background posting runner transitions native scheduled posts to `posted` at their due time without duplicate uploads.
- **Comprehensive Test Suite (`tests/test_scheduling.py`)**: 20 automated checks integrated into `tests/run.py` verifying local queue, native upload payloads, `publishAt` ISO formatting, conversion via `/schedulenative`, and remote deletion.

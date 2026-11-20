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

---

## v1.4 — Custom Speech-to-Text: ElevenLabs Scribe API Integration & Local Fallback Engine

### Overview
PeakCut v1.4 allows creators and users to connect their own **ElevenLabs Speech-to-Text (Scribe)** API key. This delivers cloud-grade, multi-language speech recognition with word-level timestamps and punctuation, backed by an automatic fallback engine that seamlessly switches to the local Parakeet model if ElevenLabs credits run out or the service is temporarily unreachable.

---

### Key Features & Updates

#### 1. Dual Speech-to-Text Engines
- **Local Offline Engine (`local`)**:
  - Uses the built-in Parakeet 0.6b model via `sherpa-onnx` on your CPU or GPU.
  - 100% offline, free, and completely private (no internet or keys required).
- **ElevenLabs Scribe Engine (`elevenlabs`)**:
  - Uses ElevenLabs official `POST /v1/speech-to-text` Scribe API endpoint.
  - Generates precise word-level timestamps, capitalization, and punctuation.
  - Supports multi-language transcription and automatic language detection.
  - Accepts in-memory 16kHz mono audio streams with zero temporary file pollution.

#### 2. Settings -> Speech to text UI (`web/index.html`)
- **Dedicated Settings Tab**: Accessible under **Settings -> Speech to text** with an active status badge.
- **Engine Selector**: Switch between Local offline model and ElevenLabs Scribe API in 1 click.
- **Secure Key Management**: Masked key preview (`••••XXXX`), password input with direct links to `https://elevenlabs.io`.
- **Live Key Testing**: One-click **Test Key** button validates API key against ElevenLabs and displays subscription tier and usage status without consuming transcription quota.
- **Model & Language Controls**:
  - Model selection dropdown: `Scribe v1` (Recommended), `Scribe v2` (Latest), `Scribe v1 Base` (Compact).
  - Optional ISO 639-1 language code input (e.g. `en`, `hi`, `es`, `fr`), or leave empty for auto-detection.
- **Automatic Local Fallback Checkbox**: Option to automatically fall back to the local Parakeet model if ElevenLabs returns an error or quota limit, guaranteeing zero video pipeline halts.

#### 3. Database Persistence & Env Configuration
- **SQLite Table (`stt_settings`)**: Stored in `data/peakcut.db` with structured columns for provider, api_key, model, language, fallback_to_local, and is_active.
- **JSON Mirroring (`data/stt.json`)**: Real-time sync for file-based background workers and test environments.
- **Environment Variable Fallback**: Configure via `ELEVENLABS_API_KEY` or `PEAKCUT_STT_PROVIDER=elevenlabs` in `.env`.

#### 4. Automated Test Suite (`tests/test_elevenlabs_stt.py`)
- 32 automated checks verifying:
  - Settings saving and SQLite/JSON persistence without cross-provider data loss.
  - ElevenLabs key testing and HTTP 401 error rejection.
  - In-memory 16kHz mono WAV encoding.
  - Word-level JSON response parsing with spacing and audio event filtering.
  - Pipeline dispatch and graceful fallback to the local Parakeet model on HTTP errors.
  - REST API endpoints: `GET /api/stt`, `PUT /api/stt`, and `POST /api/stt/test`.

---

## v1.5 — Comprehensive Pipeline Logging & In-App Log Viewer

### Overview
PeakCut v1.5 introduces an end-to-end, real-time logging infrastructure that records every single step of the video processing lifecycle. From audio extraction and loudness analysis to Speech-to-Text provider selection (YouTube transcript vs ElevenLabs Scribe vs local Parakeet), AI multi-key rotation and token usage, candidate clip discovery, exact speech alignments, and FFmpeg video rendering times—every event is logged and accessible directly inside the UI.

---

### Key Features & Updates

#### 1. Real-Time Terminal & Console Output
- **Prefix Standard**: All console logs are labeled with `[PeakCut <jid>]` and sub-module tags:
  - `[Audio]`: Audio extraction, length in seconds, sample rate, channels, and loudness peak dB thresholds.
  - `[STT]`: Detailed transcription logs showing the exact provider chosen (YouTube official transcript, ElevenLabs Scribe cloud API, or local Parakeet offline model), key hints, language code, audio duration, request elapsed time, and token/word counts.
  - `[AI]`: AI highlight discovery calls, key rotation events, model selections (e.g. `gemini-3.8-flash`), token consumption (input/output), candidate clip counts, scores, and reasonings.
  - `[Render]`: Video cutting, layout composite (e.g. `blur`, `stacked`), caption style, font rendering, speech alignment timing, and FFmpeg execution duration in seconds.
- **Flushed Stdout**: All terminal logs use `flush=True` so logs appear instantly without buffering.

#### 2. Persistent `job.log` Storage
- **File Location**: Saved directly inside the video directory at `data/users/<uid>/jobs/<jid>/job.log`.
- **Timestamped Entries**: Every entry is formatted with `[%Y-%m-%d %H:%M:%S]`.
- **Permanent Availability**: Persists across server reboots, container restarts, and database migrations.
- **Fast In-Memory Buffer**: Simultaneously stored in `job["logs"]` for immediate memory access.

#### 3. New API Endpoint: `GET /api/jobs/{jid}/log`
- Returns a structured JSON payload:
  - `id`: Job identifier.
  - `title`: Video title or source file name.
  - `status`: Current status (`queued`, `running`, `done`, `error`).
  - `stage`: Current human-readable stage description.
  - `log`: Full plain-text log string.
  - `lines`: Array of clean log lines ready for terminal display.

#### 4. In-App Pipeline Log Modal (`web/index.html`)
- **"View Log" Receipt Button (`ic("receipt")`)**: Added to every video card header and progress bar in the library.
- **Accessible During & After Processing**:
  - Click while a job is running to view live progress with auto-refresh every 3 seconds.
  - Click on completed or failed jobs to inspect why a specific STT engine, AI key, or loudness fallback was chosen.
- **Monospace Terminal Viewer**:
  - Dark terminal theme (`#17110e`) with auto-scrolling to the latest entry.
  - One-click **Copy** button to copy the entire log to clipboard for sharing or troubleshooting.
  - One-click **Refresh** button and clean escape/close handlers.

#### 5. Automated Test Suite (`tests/test_job_logging.py`)
- Integrated into `tests/run.py` with 9 automated tests verifying:
  - `jobs.public(job)` safely hides server directories and array payloads while exposing `has_log: True`.
  - Timestamp formatting `[%Y-%m-%d %H:%M:%S]` in `job.log`.
  - `GET /api/jobs/{jid}/log` returns 200 with complete contents and structured lines.
  - Returns clean 404 for non-existent video jobs.

---

## v1.6 — OpenAI Whisper STT, Hardware Auto-Detection & Hindi/English Transcription

### Overview
PeakCut v1.6 introduces native **OpenAI Whisper** transcription powered by `faster-whisper` (CTranslate2). It features real-time **hardware auto-detection** to automatically pick the fastest model (`small` on CPU vs `large-v3-turbo`/`large-v3` on GPU) based on available system RAM and GPU VRAM, while offering full manual override controls for model size, device, compute type, and language lock (English, Hindi, Hinglish, and 90+ languages).

---

### Key Features & Updates

#### 1. Hardware Auto-Detection Engine
- **Resource Profiling**:
  - Detects CPU cores, total system RAM (GB), NVIDIA CUDA availability, GPU device name, and dedicated VRAM (GB).
- **Intelligent Decision Matrix**:
  - **CUDA GPU with >= 6 GB VRAM**: Recommends `large-v3-turbo` with `float16` for maximum speed and accuracy.
  - **CUDA GPU with >= 3.5 GB VRAM**: Recommends `small` with `int8_float16`.
  - **CPU with >= 12 GB RAM**: Recommends `small` with `int8` (fast, lightweight, highly accurate for English & Hindi).
  - **CPU with >= 6 GB RAM**: Recommends `base` with `int8`.
  - **Low-resource systems (< 6 GB RAM)**: Recommends `tiny` with `int8`.
- **Live Hardware Badge**: Displays detected hardware and automatic recommendation directly in the dashboard under **Settings -> Speech to text**.

#### 2. English & Hindi Auto-Detection + Multi-Lingual Support
- **Automatic Language Recognition**:
  - Automatically identifies whether audio is English, Hindi, Hinglish code-switching, or other languages.
  - Logs detected language and confidence probability (e.g. `[STT] Whisper detected language 'hi' (confidence: 94.0%)`).
- **Manual Language Locking**:
  - Dedicated dropdown allowing creators to lock transcription to English (`en`), Hindi (`hi`), Urdu (`ur`), or keep `auto-detect`.

#### 3. Manual Model Size & Compute Overrides
- **Supported Whisper Models**:
  - `auto`: Hardware auto-detected recommendation (default).
  - `small` (~480 MB): Balanced speed and accuracy; recommended for CPU systems.
  - `large-v3-turbo` (~1.6 GB): Optimized 8-layer decoder architecture; ideal for GPU systems.
  - `large-v3` (~3.1 GB): Maximum accuracy across complex accents and dialect variations.
  - `medium` (~1.5 GB), `base` (~145 MB), `tiny` (~75 MB).
- **Compute Overrides**:
  - Device: `auto`, `cpu`, `cuda`.
  - Quantization: `auto`, `int8`, `float16`, `int8_float16`, `float32`.

#### 4. Thread-Safe Model Caching & High Performance
- Models are cached in memory via `_whisper_cache` so subsequent blocks of long videos reuse the model with zero re-loading overhead.
- Centralized model storage at `data/models/whisper/`.

#### 5. Graceful Local Parakeet Fallback
- If Whisper runs into an out-of-memory condition or missing dependency, the pipeline catches the error and seamlessly falls back to the built-in Parakeet offline model so video clipping never fails.

#### 6. Settings -> Speech to text UI Overhaul (`web/index.html`)
- Added OpenAI Whisper as a first-class transcription engine alongside Parakeet and ElevenLabs Scribe.
- Hardware auto-detection banner with dynamic GPU/CPU icons and accurate NVIDIA GPU (RTX 3050, 6GB VRAM) and system RAM detection.
- **Hugging Face API Access Token (`HF_TOKEN`) Support**:
  - In-app token input field with masked hint and direct link to get a free token on Hugging Face.
  - One-click **Test Token** button (`POST /api/stt/hf/test`) to validate credentials against Hugging Face API (`@username`).
  - Authenticated model downloads with higher bandwidth and elimination of unauthenticated rate limit warnings.
- One-click **Test & Preload Model** button (`#test_whisper_btn`) with instant status toast.

#### 7. Automated Test Suite (`tests/test_whisper_stt.py`)
- Added 41 automated tests verifying hardware detection, parameter mapping, Hugging Face token persistence, SQLite/file settings persistence, Hindi/English word-level timestamp generation, REST endpoints (`GET /api/stt`, `PUT /api/stt`, `POST /api/stt/hf/test`), and pipeline fallback.

---

## v1.7 — Live Whisper Model Download Progress Tracking & Model Library

### Overview
PeakCut v1.7 introduces a real-time model download manager and live progress feedback system for OpenAI Whisper speech-to-text models. Instead of silent terminal freezes when downloading large models (`small` ~480 MB, `large-v3-turbo` ~1.6 GB, `large-v3` ~3.1 GB), PeakCut provides fine-grained byte-level download tracking across the UI, background video pipeline, and REST API.

---

### Key Features & Updates

#### 1. Real-Time Byte-Level Download Tracker (`_ModelDownloadTracker`)
- **Hugging Face Stream Interception**:
  - Subclasses `tqdm.tqdm` to intercept Hugging Face `snapshot_download` chunks.
  - Dynamically computes downloaded MB, total file/snapshot MB, completion percentage, transfer speed in MB/s, and accurate ETA in seconds.
  - Thread-safe state tracking in `studio/stt.py` (`_download_state` protected by `_download_lock`).

#### 2. Live On-Disk Model Detection & Status (`get_whisper_models_status`)
- **Accurate Size & Ready Status**:
  - Checks `data/models/whisper/` and Hugging Face snapshot symlink structures to report which models are downloaded (`✓ Ready`) vs missing (`Needs Download`).
  - Calculates accurate disk usage in MB/GB for installed models (e.g. `tiny` 74.6 MB, `large-v3-turbo` 1546.5 MB).

#### 3. Real-Time Download Box & Library Manager in Settings (`web/index.html`)
- **Visual Progress Bar (`#whisper_dl_box`)**:
  - Styled with PeakCut's clean orange accent (`var(--brand, #F97316)`) and rounded container.
  - Displays spinning indicator, model name, active file (e.g. `model.bin`), live percentage (`45%`), downloaded/total MB (`215.4 MB / 461.0 MB`), real-time speed (`18.2 MB/s`), and ETA countdown (`14s`).
- **Installed Whisper Model Library Tray**:
  - Grid of model cards showing on-disk availability:
    - Installed models show a green `✓ Ready (size MB)` pill.
    - Missing models show estimated size and a one-click `⬇️ Get` button to start background downloading immediately.
- **Model Dropdown Indicators**:
  - Whisper model dropdown items show `[✓ Ready]` or `[Needs Download]` badges.
  - Selecting a model provides a contextual dynamic note and a quick download button if not yet on disk.
- **Continuous Polling Engine (`pollWhisperDownload`)**:
  - Polls `GET /api/stt/whisper/download` every 500ms during active downloads and automatically updates the progress bar.
  - On completion, turns into a success confirmation, sends a toast notification, and automatically refreshes settings badges without requiring a page reload.

#### 4. Video Pipeline Integration (`studio/jobs.py`)
- **Live Job Stage Updates**:
  - When a video is queued for clipping with Whisper and the required model is not yet downloaded, the video job updates its stage live: `Downloading Whisper model (small): 42%`.
  - Automatically logs progress every 10% to the job log file (`job.log`) and server terminal.

#### 5. Asynchronous Download Endpoints (`studio/server.py`)
- `GET /api/stt/whisper/download`: Returns current download state (`active`, `model`, `file`, `downloaded_mb`, `total_mb`, `percent`, `speed_mbps`, `eta_seconds`, `status`, `message`, `models`).
- `POST /api/stt/whisper/download`: Initiates background downloading in a daemon thread, returning immediate status and preventing HTTP request timeout.

#### 6. Verification & Test Suite
- Enhanced `tests/test_whisper_stt.py` with 11 new tests covering:
  - `stt.get_whisper_models_status()` and `stt.is_whisper_model_downloaded()`.
  - `GET /api/stt/whisper/download` and `POST /api/stt/whisper/download` endpoints.
  - Progress tracker calculations (downloaded MB, percentage, speed, ETA).
  - Background asynchronous task startup and thread safety.




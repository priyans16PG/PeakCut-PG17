# PeakCut (Local Edition)

Turn long videos and live streams into captioned vertical clips, ready to post. Free, open source, and runs **100% locally on your computer with no Docker needed**.

Paste a YouTube, Twitch or Kick link (or upload a local video file) and PeakCut finds the moments worth clipping, cuts them to 9:16 vertical format, adds word-by-word animated captions and hooks, and prepares them for posting directly to **YouTube Shorts** and **Instagram Reels**.

---

## Highlights

- **100% Local & No Docker**: Runs straight on your device with Python and FFmpeg. No containers, no database setup (uses built-in SQLite).
- **One-Click Launch**: Double-click `run.bat` on Windows.
- **Offline Speech-to-Text**: Transcribes video audio completely on your device using `sherpa-onnx` and the Parakeet model. Audio never leaves your computer.
- **Direct YouTube & Instagram Posting**: Connect your YouTube channel right from the web dashboard to upload rendered clips directly as YouTube Shorts.
- **No Sign-In Hassle**: Opens directly into your dashboard at `http://127.0.0.1:8765`.
- **Bring Your Own AI**: Connect Google Gemini, Groq, OpenAI, Claude, OpenRouter, or a 100% offline local model with **Ollama**. (Without an AI key, PeakCut clips automatically using audio loudness and speech peaks).
- **Reliable Long Video Processing**: Built-in download timeout safeguards and recovery for multi-hour videos and live streams.

---

## Quick Start (Windows)

### 1. Requirements
- **Python 3.10+** (installed and added to PATH)
- **FFmpeg** (installed and added to PATH)

### 2. Launch
Simply double-click **`run.bat`** (or in PowerShell run `.\start.ps1`).

That's it! The launcher automatically:
1. Sets up the Python virtual environment (`.venv`) and installs required packages if needed.
2. Checks that FFmpeg is available.
3. Downloads the offline speech model once (if missing).
4. Launches the local server and opens **`http://127.0.0.1:8765`** in your default web browser.

---

## Connecting YouTube (for auto-posting Shorts)

You can link your YouTube channel directly from the web UI to post clips with one click:

1. Open **Settings** (gear icon) → **Connected apps**.
2. Under **YouTube**, click **Set up**.
3. Enter your Google Cloud OAuth **Client ID** and **Client Secret** (from the [Google Cloud Console](https://console.cloud.google.com/apis/credentials)) and click **Save credentials**.
4. Click **Connect** and authorize your channel in the Google consent screen.
5. Once linked, open any completed clip in PeakCut, tick your channel under **Post it**, and click **Post now** (or **Add to queue**).

> **Tip for Google Cloud setup:** If your Google Cloud app is in "Testing" mode, add your Gmail under **OAuth consent screen → Test users** before connecting.

---

## AI Setup (Optional)

PeakCut works without any AI key by finding clips based on audio peaks. 

To enable AI-powered highlight selection, hooks, and titles:
1. Open the dashboard at `http://127.0.0.1:8765`.
2. Go to **Settings → AI**.
3. Choose your provider, enter your key, and click **Save and test**.

You can also set your keys in [`.env`](.env):

### Recommended Providers:
| Provider | Model ID | Notes |
|---|---|---|
| **Google Gemini** | `gemini-3.8-flash` | Free tier available at [Google AI Studio](https://aistudio.google.com/apikey) |
| **Groq** | `qwen/qwen3.8-27b`, `openai/gpt-oss-120b` | Ultra-fast inference at [Groq Console](https://console.groq.com/keys) |
| **Ollama** | `llama3.1` | 100% offline, free local LLM: [ollama.com](https://ollama.com) |

---

## Folder Structure

| Folder / File | What it is |
|---|---|
| `run.bat` | One-click Windows starter script |
| `start.ps1` | PowerShell starter script |
| `studio/` | PeakCut core server, video clipping pipeline, captions, and AI callers |
| `web/` | Web dashboard (pure HTML/CSS/JS, no node/npm build needed) |
| `data/` | Your local database (`peakcut.db`), offline speech models, credentials, and rendered clips |
| `.env` | Local settings and API keys (never shared or committed) |
| `tests/` | Automated test suite |

---

## Running Tests

To verify your local installation at any time:

```bash
.\.venv\Scripts\python.exe tests/run.py
```

This runs against an isolated temporary directory and tests all local functionality without touching your real videos or database.

---

## License

[MIT](LICENSE).

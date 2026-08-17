# AI Podcast Clip Cutter

A Windows desktop app that downloads a YouTube video once and cuts it into
multiple clips based on Start Time / End Time pairs pasted from any AI tool
(ChatGPT, Claude, Gemini, DeepSeek, Perplexity, etc).

## Run from source

```
pip install -r requirements.txt
python main.py
```

Requires FFmpeg (ffmpeg + ffprobe) available on your system PATH, or set an
explicit path via `AppConfig.ffmpeg_path_override` / `ffprobe_path_override`
(persisted in the app's settings.json).

## Build a Windows .exe

1. `pip install pyinstaller`
2. Place `ffmpeg.exe` and `ffprobe.exe` inside an `ffmpeg\` folder next to
   `build.bat` (optional — bundles FFmpeg so end users don't need it on PATH).
3. Run `build.bat`.
4. Find the executable in `dist\AIPodcastClipCutter.exe`.

## Fixing the 360p cap

If every download comes out at 360p regardless of the resolution you pick,
this is not a bug in this app's resolution setting — it's one of two
separate YouTube-side requirements for its `web` client (the one that can
actually serve 720p/1080p+). **Both** are needed; having only one still
leaves you capped at 360p:

1. **A JS runtime, to solve YouTube's "n challenge".** Without this,
   `web` can't decode real video/audio URLs at all — only thumbnail
   images remain "downloadable" — so extraction falls through to
   `tv`/`ios`/`android`, which (as of 2026, due to a separate YouTube
   SABR-only rollout) only expose the old 360p stream (itag 18) as a
   fallback.
2. **A PO Token** (Proof-of-Origin), a per-video token `web` has required
   since early 2026. Without it, `web` gets a 403 even with a JS runtime
   present, and the same fallback-to-360p happens.

### Step 1: Install a JS runtime

Install **[Node.js](https://nodejs.org/)** (any current LTS, version 20+)
or **[Deno](https://docs.deno.com/runtime/getting_started/installation/)**
— either works; this app is already configured to use whichever one it
finds (see `app/core/downloader.py`, the `js_runtimes` option). No
further app configuration is needed once one of them is installed and on
your system `PATH`.

Then install/upgrade dependencies with the `[default]` extra, which
bundles the actual "n challenge" solver scripts (`yt-dlp-ejs`) — a plain
`pip install yt-dlp` does **not** include these:

```
pip install -U "yt-dlp[default]"
pip install -r requirements.txt
```

If your network can't reach PyPI for that package, this app also enables
`--remote-components ejs:github`-equivalent behavior automatically as a
fallback, so it can self-heal by fetching the scripts from GitHub
instead — but the `pip install` route above is more reliable and doesn't
depend on GitHub being reachable at run time.

### Step 2: Set up the PO Token provider

`requirements.txt` already includes `bgutil-ytdlp-pot-provider`, which
teaches yt-dlp how to fetch that token, but it needs a one-time local
setup because it runs a small helper script via Node.js (if you installed
Node.js in Step 1, you already have what this needs):

1. `pip install -r requirements.txt` (installs the plugin if you haven't already).
2. In your home folder, run:
   ```
   git clone --branch 1.3.1 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git
   cd bgutil-ytdlp-pot-provider/server
   npm ci
   npx tsc
   ```
   (Windows: run these from PowerShell or Command Prompt; the folder must
   stay at `%USERPROFILE%\bgutil-ytdlp-pot-provider` — that's the default
   location yt-dlp's plugin looks for.)
3. Restart the app. No further code changes are needed on the app's side
   — once a JS runtime is installed (Step 1) and yt-dlp can source a
   token (Step 2), its normal `web`-client request in `downloader.py`
   succeeds on its own and 1080p/720p become available again.

You'll know it's working when the Activity Log's "Độ phân giải thực tế"
line matches what you picked instead of always reading 360p. If it's
still stuck, check the on-disk log file (not the in-app Activity Log,
which is deliberately kept quiet — see `_SilentYtDlpLogger` in
`downloader.py`) for the exact yt-dlp warning text, which will say
either "No supported JavaScript runtime" (→ redo Step 1) or mention "n
challenge solving failed" / a 403 on the `web` client (→ redo Step 2).
Using logged-in cookies (the Cookies picker in this app) still helps on
top of both steps — YouTube serves some formats only to authenticated
sessions — but cookies alone won't fix the 360p cap.

## Usage

1. Paste a YouTube URL.
2. Paste AI-generated Clip Data (Start Time / End Time pairs) — the
   Detected Clips table fills in automatically as you paste.
3. Click **Cut Clips**. Progress and logs stream live; per-clip status
   updates in the table as each one finishes.
4. When done, a **Summary** table shows filename / duration / size / status
   for every clip, and **Open Output Folder** opens the result.

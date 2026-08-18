# AI Podcast Clip Cutter

A Windows/macOS desktop app that turns a single long-form YouTube video
(podcast, interview, livestream) into a batch of ready-to-post short
clips — each one auto-translated to Vietnamese, subtitled, and
optionally AI-dubbed with a distinct voice per speaker — all from one
video download.

> 🎬 *Screenshots / demo GIF coming soon.*

---

## Why this project

Most "video clipper" tools stop at cutting a timestamp range. The
interesting engineering problems here start *after* that:

- How do you dub a translated clip with **multiple speakers**, each in
  a **consistent, distinguishable voice**, without any paid API and
  without asking a non-technical end user for an API key or account?
- How do you keep a machine translation from sounding like it was
  translated one subtitle line at a time?
- How do you get YouTube to actually serve 1080p instead of silently
  capping every download at 360p, once its bot-detection tightened in
  2026?
- How do you ship all of that as a **single-file desktop app** a
  non-technical user just double-clicks — no Python, no terminal, no
  install steps?

This project is my answer to each of those, built as a real, working
tool rather than a proof of concept.

## What it does

1. **Paste a YouTube URL** and pick a resolution — the app downloads
   the source video once, no matter how many clips you cut from it.
2. **Paste Start/End timestamp pairs** from any AI tool (ChatGPT,
   Claude, Gemini, Perplexity...) that you used to find the best
   moments in the transcript — the app parses them straight into a
   clip list.
3. For each clip, the app can:
   - Burn in **auto-translated Vietnamese subtitles**.
   - Generate a full **AI-dubbed Vietnamese voiceover**, with up to 4
     speakers detected and each kept in its own consistent voice.
   - Add a **platform-style watermark** (real X / TikTok / Facebook /
     YouTube glyph next to a handle, not just a plain text label).
4. Everything runs through **one batch pipeline** with live progress
   and an activity log, and exports straight to a chosen output
   folder.

## Engineering highlights

- **Local, no-API-key speaker diarization for dubbing.** Rather than
  reaching for `pyannote.audio` (great accuracy, but gated behind a
  Hugging Face account/token — unworkable for a downloadable .exe),
  the app pairs `webrtcvad` for voice-activity detection with
  `resemblyzer` speaker embeddings (weights ship inside the pip
  package — zero network calls) and scikit-learn clustering to group
  utterances into up to 4 speakers per clip, fully offline.
- **Sentence-aware translation & dubbing windows.** Subtitle cues are
  first grouped into natural sentence-level "windows" — capped by
  punctuation, pause length, and word count — before being sent to
  translation or text-to-speech, instead of processing each line in
  isolation. This alone cuts TTS network calls by 2–6x *and* makes
  dubbed speech sound like continuous sentences instead of
  disjointed fragments.
- **Voice-consistency trick with a single free TTS engine.** `edge-tts`
  only ships one Vietnamese neural voice per gender. The app assigns
  the first female/male speaker each base voice, and reuses/pitch-
  shifts that same voice via SSML for a second same-gender speaker —
  enough to sound distinguishable without needing a paid multi-voice
  API.
- **Root-caused and fixed YouTube's 2026 360p cap** — a two-part fix
  (a bundled JS runtime to solve the "n challenge" + a Proof-of-Origin
  token provider) documented in detail for anyone hitting the same
  wall with `yt-dlp`.
- **Cross-platform packaging.** Ships as a self-contained desktop
  build via PyInstaller, with a GitHub Actions workflow that builds
  the macOS `.app` on GitHub's own Mac runners — no physical Mac
  needed to ship a Mac build.

## Tech stack

`Python` · `PySide6` (Qt GUI) · `yt-dlp` · `FFmpeg` · `edge-tts` ·
`resemblyzer` + `webrtcvad` + `scikit-learn` (speaker diarization) ·
`PyInstaller` · `GitHub Actions`

## Run from source

```bash
pip install -r requirements.txt
python main.py
```

Requires FFmpeg (`ffmpeg` + `ffprobe`) on your system `PATH`, or set an
explicit path in the app's settings.

## Build a standalone app

- **Windows:** `build.bat` → produces a single `.exe`.
- **macOS:** push to GitHub and run the included
  [`build-macos.yml`](.github/workflows/build-macos.yml) GitHub
  Actions workflow — it builds a native `.app` on GitHub's macOS
  runners and uploads it as a downloadable artifact.

## Status

Actively developed as a personal project. Built solo — architecture,
pipeline, GUI, and packaging all designed and implemented end to end.

## Feedback & Contributing

Bug reports, feature ideas, and code review are all welcome — please
open an [issue](../../issues) or a pull request. If you just want to
say what you liked/didn't like about the code, that's welcome too.

## License

Released under the [MIT License](LICENSE) — free to use, modify, and
distribute, including for commercial purposes.

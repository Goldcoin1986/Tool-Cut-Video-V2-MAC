"""
Best-effort English -> Vietnamese machine translation for auto-
translated subtitles, using Google Translate's free public web
endpoint — the same undocumented endpoint translate.google.com's own
web page calls internally, with no API key, no account, and no cost.

IMPORTANT — this is NOT an official/supported Google API:
  - It can be rate-limited, geo-restricted, or changed/blocked by
    Google at any time without notice, since it isn't a published,
    versioned product. This is the same technique widely-used
    open-source translation libraries (e.g. deep-translator,
    googletrans) already rely on for free translation.
  - Every call here degrades gracefully on failure: if translation
    fails, the ORIGINAL English text is kept rather than raising and
    aborting the whole cut — a broken translation call should never be
    able to break subtitle burning entirely, worst case is
    English-only subtitles for that line.

Translation quality caveat: this gives a solid literal translation for
most spoken sentences, but like any machine translation it can still
miss idioms, jokes, or natural conversational phrasing a human
translator would catch — good enough to follow along, not
publication-quality.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger("clip_cutter")

_ENDPOINT = "https://translate.googleapis.com/translate_a/single"
_MAX_RETRIES = 2
_RETRY_DELAY_SECONDS = 1.5
_REQUEST_TIMEOUT_SECONDS = 8.0
_MIN_DELAY_BETWEEN_CALLS_SECONDS = 0.15  # light self-throttling, avoid tripping rate limits

# yt-dlp/YouTube caption language codes that this free Google Translate
# endpoint doesn't accept as-is for `sl=` — map them to the variant it
# does understand. Everything not listed here (en, ja, ko, fr, de, ...)
# is passed straight through to `sl=` unchanged, since the endpoint
# already accepts plain ISO-639-1 codes for the vast majority of
# languages. Only Chinese's script-qualified YouTube codes are known to
# need remapping in practice; extend this table if another language's
# YouTube code turns out to need one too.
_SOURCE_LANG_ALIASES: dict[str, str] = {
    "zh-Hans": "zh-CN",
    "zh-Hant": "zh-TW",
    "zh-CN": "zh-CN",
    "zh-TW": "zh-TW",
    "zh": "zh-CN",
}


def _normalize_source_lang(lang: str) -> str:
    """Map a yt-dlp/YouTube caption language code to whatever the free
    Google Translate endpoint's `sl=` parameter actually expects.

    Best-effort only: an unrecognized code is passed through unchanged
    rather than raising — if the endpoint then rejects it, `translate()`
    still degrades gracefully (keeps the original-language text) exactly
    like every other failure mode this class already handles.
    """
    if not lang:
        return lang
    if lang in _SOURCE_LANG_ALIASES:
        return _SOURCE_LANG_ALIASES[lang]
    # Some YouTube tracks come back as e.g. "zh-Hans-HK" (script + region)
    # — not just "zh-Hans" — so also check the script-only prefix before
    # giving up and passing the code through verbatim.
    prefix = lang.split("-")[0]
    if lang.startswith("zh"):
        return _SOURCE_LANG_ALIASES.get(prefix, "zh-CN")
    return lang


class Translator:
    """Small stateful wrapper around the free endpoint: caches
    identical lines within one run (transcript rolling-caption
    artifacts and repeated phrases are common) and self-throttles
    slightly between calls so a long transcript doesn't fire off dozens
    of requests back to back."""

    def __init__(self, source_lang: str = "en", target_lang: str = "vi") -> None:
        self._source = _normalize_source_lang(source_lang)
        self._target = target_lang
        self._cache: dict[str, str] = {}
        self._last_call = 0.0
        self.failure_count = 0
        self.success_count = 0

    def translate(self, text: str) -> str:
        text = text.strip()
        if not text:
            return text
        if text in self._cache:
            return self._cache[text]

        result = self._translate_uncached(text)
        self._cache[text] = result
        return result

    def _translate_uncached(self, text: str) -> str:
        params = urllib.parse.urlencode(
            {"client": "gtx", "sl": self._source, "tl": self._target, "dt": "t", "q": text}
        )
        url = f"{_ENDPOINT}?{params}"

        for attempt in range(_MAX_RETRIES + 1):
            elapsed = time.monotonic() - self._last_call
            if elapsed < _MIN_DELAY_BETWEEN_CALLS_SECONDS:
                time.sleep(_MIN_DELAY_BETWEEN_CALLS_SECONDS - elapsed)
            self._last_call = time.monotonic()
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT_SECONDS) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                translated = "".join(chunk[0] for chunk in data[0] if chunk and chunk[0])
                if translated.strip():
                    self.success_count += 1
                    return translated
                raise ValueError("empty translation response")
            except (urllib.error.URLError, TimeoutError, ValueError, IndexError, KeyError) as exc:
                logger.debug("Translate attempt %d/%d failed: %s", attempt + 1, _MAX_RETRIES + 1, exc)
                if attempt < _MAX_RETRIES:
                    time.sleep(_RETRY_DELAY_SECONDS)

        self.failure_count += 1
        logger.debug("Dịch thất bại, giữ nguyên văn bản gốc: %r", text[:60])
        return text  # graceful degradation: English line beats no line

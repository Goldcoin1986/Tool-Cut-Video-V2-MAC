"""
Downloads the source YouTube video using yt-dlp's Python API directly
(never via subprocess), so download progress can be reported accurately
through progress_hooks.

Downloaded videos are cached by YouTube video ID inside the temp
directory (source_<id>.mp4). If the user cuts more clips from the same
URL later in the same app session, the cached file is reused instead of
downloading again. The cache is cleared when the app closes (see
app.utils.file_utils.clear_temp_dir, called from MainWindow.closeEvent).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Callable

import yt_dlp

from app.core.pot_server import get_shared_manager

try:
    from yt_dlp.cookies import CookieLoadError
except ImportError:  # pragma: no cover - defensive, in case an older/newer
    # yt-dlp version moves or renames this class. Falls back to message-
    # based detection in _is_cookie_error() below.
    CookieLoadError = None  # type: ignore[assignment,misc]

from app.utils.exceptions import AppError, DownloadError, InvalidURLError, NetworkError, VideoUnavailableError

logger = logging.getLogger("clip_cutter")

_COOKIE_ERROR_MARKERS = (
    "could not copy",       # e.g. "Could not copy Chrome cookie database"
    "cookie database",
    "could not find",       # e.g. "could not find firefox cookies database"
    "failed to load cookies",
    "could not decrypt",
    "cookies file",         # e.g. "could not open cookies file", cookiefile mode
    "no such file",         # cookiefile pointing at a missing/moved path
    "does not exist",
    "unsupported cookies file",  # malformed / not Netscape-format cookies.txt
)

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")
_REPEATED_ERROR_PREFIX_RE = re.compile(r"^(?:ERROR:\s*)+", re.IGNORECASE)


# yt-dlp uses these temporary extensions while a download/post-processing
# step is still in progress. They must never be reused as cache entries or
# returned as the final source video.
_TEMPORARY_DOWNLOAD_SUFFIXES = {".part", ".ytdl", ".tmp", ".temp"}


def _is_completed_media_file(path: Path) -> bool:
    """Return True only for a non-empty, non-temporary downloaded file."""
    return (
        path.is_file()
        and path.stat().st_size > 0
        and path.suffix.lower() not in _TEMPORARY_DOWNLOAD_SUFFIXES
    )


def _clean_yt_dlp_message(exc: BaseException) -> str:
    """Return a readable, single 'ERROR:'-prefixed version of a yt-dlp
    exception's message.

    yt-dlp colors its own error text with raw ANSI escape codes (e.g.
    '\\x1b[0;31mERROR:\\x1b[0m') and embeds them directly *inside* the
    exception message itself, not just when printing to a real
    terminal. When certain internal failures (like this app's cookie-
    database fallback) get reported and then re-raised more than once
    internally, that colored 'ERROR:' prefix can even end up doubled.
    Left as-is, those raw escape bytes render as garbled text in the
    app's Activity Log (a plain Qt widget, not a terminal emulator).
    This strips the escape codes and collapses any repeated prefix so
    only clean, readable text ever reaches the log or an error dialog.
    """
    text = _ANSI_ESCAPE_RE.sub("", str(exc)).strip()
    return _REPEATED_ERROR_PREFIX_RE.sub("ERROR: ", text)


class _SilentYtDlpLogger:
    """Passed as yt-dlp's `logger` option so IT never writes directly to
    the real stdout/stderr.

    By default, for certain fatal errors (see YoutubeDL.report_error /
    .trouble in yt-dlp's own source), yt-dlp prints ANSI color-coded
    text straight to stderr regardless of the `quiet`/`no_warnings`
    options — and that same colored text ends up baked into the
    exception's message too. In a windowed GUI app that raw escape-code
    text has nowhere sensible to go (it renders as garbled bytes in a
    plain text widget), and this app already handles and reports every
    failure itself (see the try/except blocks in `download()`), so
    yt-dlp's own console output isn't needed. Everything is routed to
    the app's own logger at DEBUG level instead — still available in
    the on-disk log file for troubleshooting, but never shown raw in
    the Activity Log.
    """

    @staticmethod
    def debug(msg: str) -> None:
        logger.debug("[yt-dlp] %s", _ANSI_ESCAPE_RE.sub("", msg))

    info = debug
    warning = debug
    error = debug


def _is_cookie_error(exc: BaseException) -> bool:
    """True if `exc` means yt-dlp failed to *read the browser's cookie
    file* rather than an actual YouTube-side failure — e.g. the browser
    is still running and holds an exclusive lock on its cookie database
    (the most common cause on Windows/Chrome), antivirus is blocking
    access, or the OS denied permission.

    This is a local environment problem, not a video-download problem,
    so it should never be treated as fatal: see the automatic anonymous
    retry in `YouTubeDownloader.download()`.
    """
    if CookieLoadError is not None and isinstance(exc, CookieLoadError):
        return True
    text = _clean_yt_dlp_message(exc).lower()
    return "cookie" in text and any(marker in text for marker in _COOKIE_ERROR_MARKERS)


ProgressCallback = Callable[[float, str], None]
"""Signature: callback(fraction_0_to_1, status_message)"""

_YOUTUBE_URL_RE = re.compile(
    r"^(https?://)?(www\.)?(youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/|m\.youtube\.com/watch\?v=)[\w\-]+",
    re.IGNORECASE,
)

# Pulls the 11-character video ID directly out of the URL text for
# every standard YouTube URL shape (watch?v=, youtu.be/, /shorts/,
# including the m. mobile subdomain and any trailing ?t=/&list=... query
# junk after it) — see _extract_video_id() below for why this matters:
# it lets the very common case skip a network round-trip entirely.
_VIDEO_ID_IN_URL_RE = re.compile(
    r"(?:youtube\.com/watch\?(?:.*&)?v=|youtu\.be/|youtube\.com/shorts/)"
    r"([\w\-]{11})",
    re.IGNORECASE,
)


def is_valid_youtube_url(url: str) -> bool:
    """Quick client-side sanity check before attempting a download."""
    return bool(url and _YOUTUBE_URL_RE.match(url.strip()))


class YouTubeDownloader:
    """Wraps yt_dlp.YoutubeDL to download one video with progress reporting,
    reusing a cached copy from the same session when available."""

    def __init__(self) -> None:
        self.last_info: dict | None = None
        """The yt-dlp info dict from the most recent download() call.
        Reused by TranscriptDownloader so fetching captions never has to
        make an extra metadata request to YouTube (avoids HTTP 429)."""

        self.last_drm_detected: bool = False
        """True if a client OTHER than 'tv' reported this video as
        DRM-protected during the most recent download() call — meaning
        the video is genuinely encrypted and yt-dlp deliberately won't
        decrypt it. No PO Token setup, cookies, or further client
        fallback will raise the resolution past whatever unencrypted
        fallback (usually 360p) happens to still be offered."""

        self.last_tv_drm_false_positive: bool = False
        """True if ONLY the 'tv' client reported DRM during the most
        recent download() call. YouTube has run an account/IP-level
        "experiment" (yt-dlp issue #12563) that makes 'tv' falsely
        report every video as DRM-protected regardless of whether it
        actually is. A 'tv'-only DRM report is therefore not trustworthy
        on its own — see last_drm_detected, which additionally requires
        another client to agree, before treating it as real DRM."""

    def download(
        self,
        url: str,
        output_dir: Path,
        progress_callback: ProgressCallback | None = None,
        preferred_height: int | None = None,
        cookies_from_browser: str = "",
        cookies_file: str = "",
    ) -> Path:
        """Download `url` into `output_dir`, returning the local file path.

        If a cached copy of this exact video AND resolution already
        exists in `output_dir` (from an earlier clip-cutting run in this
        session), it is reused and no network request is made.

        Prefers a pre-merged MP4 format so the subsequent FFmpeg cut can
        use fast stream-copy without needing to merge separate audio and
        video streams.

        Args:
            preferred_height: Maximum desired vertical resolution in
                pixels (e.g. 1080, 720). None means "best available".
            cookies_from_browser: Optional browser keyword ('chrome',
                'firefox', 'edge', 'brave') to pull YouTube login cookies
                from. YouTube frequently restricts anonymous requests to
                low-resolution formats only; using a logged-in browser's
                cookies is often required to unlock 720p/1080p+. Ignored
                if `cookies_file` is also given.
            cookies_file: Optional path to a Netscape-format cookies.txt
                file exported from a browser while logged into YouTube.
                Takes priority over `cookies_from_browser` when both are
                given, since it reads a static file rather than a live
                browser's cookie database — immune to the "browser is
                still running and has the database locked" failure mode
                that browser-cookie mode is prone to.

        Raises:
            InvalidURLError: If the URL is not a recognizable YouTube URL.
            VideoUnavailableError: If the video is private, deleted, or
                region-locked.
            NetworkError: If a network failure interrupts the download.
            DownloadError: For any other yt-dlp failure.
        """
        if not is_valid_youtube_url(url):
            raise InvalidURLError(f"'{url}' is not a valid YouTube URL.")

        self.last_drm_detected = False
        self.last_tv_drm_false_positive = False

        output_dir.mkdir(parents=True, exist_ok=True)
        resolution_tag = str(preferred_height) if preferred_height else "best"

        cached = self._find_cached(url, output_dir, resolution_tag)
        if cached is not None:
            logger.info("Reusing cached download: %s", cached.name)
            if progress_callback:
                progress_callback(1.0, f"Using previously downloaded video ({cached.name}).")
            return cached

        # Give the PO Token HTTP server (started in the background at
        # app launch — see app/core/pot_server.py) a chance to finish
        # coming up before the 'web' client's first attempt below. On
        # machines where Node's BotGuard/V8 init is slow, the app-launch
        # start() call can still be warming up by the time the user
        # clicks "Cut Clips" for the very first video of the session;
        # without this wait, that first download would go out on the
        # low-resolution client fallback chain even though the server
        # would have been ready moments later. A no-op (returns
        # instantly) once the server is already up, which is every
        # download after the first in a session.
        get_shared_manager().wait_until_ready(timeout=90.0, progress_callback=progress_callback)

        output_template = str(output_dir / f"source_%(id)s_{resolution_tag}.%(ext)s")
        downloaded_path: dict[str, str] = {}

        def hook(status: dict) -> None:
            if status.get("status") == "downloading":
                total = status.get("total_bytes") or status.get("total_bytes_estimate")
                downloaded = status.get("downloaded_bytes", 0)
                fraction = (downloaded / total) if total else 0.0
                speed = status.get("speed")
                speed_str = f"{speed / 1_048_576:.1f} MB/s" if speed else "…"
                message = f"Downloading video… {fraction * 100:.0f}% ({speed_str})"
                if progress_callback:
                    progress_callback(min(fraction, 1.0), message)
            elif status.get("status") == "finished":
                downloaded_path["path"] = status.get("filename", "")
                if progress_callback:
                    progress_callback(1.0, "Download complete. Preparing to cut clips…")

        format_selector = self._build_format_selector(preferred_height)

        ydl_opts_base = {
            "format": format_selector,
            "outtmpl": output_template,
            "merge_output_format": "mp4",
            "progress_hooks": [hook],
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "retries": 3,
            "fragment_retries": 3,
            # Download fragments in parallel (equivalent to yt-dlp's -N
            # flag) instead of one at a time — most YouTube formats at
            # 720p+ are DASH/fragmented, so this meaningfully speeds up
            # downloads on decent connections. 4 is yt-dlp's own commonly
            # recommended value; higher rarely helps much further and
            # risks more rate-limiting/retries.
            "concurrent_fragment_downloads": 4,
            # NOTE: deliberately NOT forcing "extractor_args":
            # {"youtube": {"player_client": [...]}} here. Forcing the
            # 'web' client alone was causing YouTube to reject the
            # format list outright ("Requested format is not
            # available") even with a working PO Token — confirmed by
            # comparing against a plain
            #   yt-dlp --js-runtimes node -f "bestvideo+bestaudio/best" <url>
            # (no player_client override) succeeding at full
            # resolution on the exact same video/account/IP where the
            # forced-'web' attempt failed. Leaving player_client unset
            # lets yt-dlp use its own (actively-maintained, version-
            # specific) default client-selection/merging strategy,
            # which handles PO Token and format negotiation more
            # robustly than hard-pinning a single client here ever
            # could. The explicit tv/ios/android chain below remains
            # as a fallback if yt-dlp's default ever fails outright.
            # Required for the 'web' client to solve YouTube's "n challenge"
            # (separate from, and in addition to, the PO Token requirement
            # below). Without a JS runtime, yt-dlp can't decode the
            # real video/audio URLs at all for 'web' — only the video's
            # thumbnail images remain "downloadable" — so extraction falls
            # through to 'tv'/'ios'/'android', which currently only expose
            # itag 18 (360p) due to a separate YouTube SABR-only rollout.
            # Both deno and node are registered so this works whichever
            # runtime happens to be installed on the machine running this
            # app; yt-dlp silently skips whichever one isn't found instead
            # of erroring, as long as at least one is present. See
            # README.md, "Fixing the 360p cap".
            "js_runtimes": {"deno": {}, "node": {}},
            # Lets yt-dlp self-heal by fetching the EJS challenge-solver
            # scripts from GitHub if the bundled yt-dlp-ejs package (see
            # requirements.txt) is ever missing or out of date. Using
            # 'ejs:github' rather than 'ejs:npm' because npm-based fetching
            # only works with the deno/bun runtimes, not node.
            "remote_components": {"ejs:github"},
            "logger": _SilentYtDlpLogger(),
        }
        cookie_source_label = ""
        if cookies_file:
            cookies_path = Path(cookies_file).expanduser()
            if cookies_path.is_file():
                ydl_opts_base["cookiefile"] = str(cookies_path)
                cookie_source_label = f"file '{cookies_path.name}'"
            else:
                logger.warning(
                    "Cookies file not found, ignoring and downloading "
                    "anonymously: %s", cookies_path,
                )
                if progress_callback:
                    progress_callback(
                        0.0,
                        f"Không tìm thấy file cookies.txt ({cookies_path}) — "
                        "đang tải video ẩn danh…",
                    )
                cookies_file = ""  # so the exception-handling branch below
                # doesn't think a cookies file is still in play.
        elif cookies_from_browser:
            ydl_opts_base["cookiesfrombrowser"] = (cookies_from_browser,)
            cookie_source_label = cookies_from_browser.capitalize()

        if cookie_source_label:
            logger.info("Using YouTube login cookies from %s.", cookie_source_label)

        # Fallback clients tried in order when 'web' fails outright (HTTP
        # 403 or "no matching format"). Per yt-dlp's own PO Token Guide
        # (github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide, current as of
        # mid-2026): 'web' formats increasingly require a PO Token yt-dlp
        # doesn't automatically obtain, so 'web' alone can fail even with
        # valid cookies. 'tv' is tried first because — unlike 'android' —
        # it does NOT require a PO Token *and* still honors login cookies
        # (the same guide lists 'android' as "Account cookies not
        # supported" at all), so it's the fallback most likely to still
        # unlock higher resolutions for a logged-in user.
        #
        # However, YouTube has been running an account/IP-level
        # "experiment" (see yt-dlp issue #12563) that makes the 'tv'
        # client report EVERY video's https formats as DRM-protected,
        # regardless of whether the video is actually restricted — a
        # false positive, not a real per-video DRM lock. 'ios' is tried
        # next because it isn't affected by that same experiment and,
        # like 'tv', doesn't require a PO Token. 'android' remains the
        # final, cookie-blind, lower-quality safety net — it usually only
        # exposes capped progressive (often 360p) formats, but it is the
        # most reliable of all clients when nothing else works.
        _CLIENT_FALLBACK_CHAIN: tuple[tuple[str, bool], ...] = (
            ("tv", False),
            ("ios", True),
            ("android", True),
        )

        def attempt(opts: dict) -> tuple[dict, str]:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                final_path = ydl.prepare_filename(info)
                return info, final_path

        def attempt_with_client_fallback(opts: dict) -> tuple[dict, str]:
            """Runs `attempt(opts)`; on an HTTP 403 or "format not
            available" failure, retries in turn with each client in
            _CLIENT_FALLBACK_CHAIN until one succeeds. Raises AppError
            subclasses (DownloadError / VideoUnavailableError /
            NetworkError) on total failure; any other exception (e.g. a
            cookie-loading failure) propagates unchanged from the first
            attempt so the caller can inspect it.
            """
            try:
                info, final_path = attempt(opts)
                return info, final_path
            except yt_dlp.utils.DownloadError as exc:
                if _is_cookie_error(exc):
                    # Not a YouTube-side failure at all — re-raise as-is
                    # (unwrapped) so download()'s outer handler can detect
                    # it and retry anonymously, regardless of which
                    # yt-dlp version raised it (some wrap this in
                    # CookieLoadError, older ones raise this DownloadError
                    # directly).
                    raise
                message = _clean_yt_dlp_message(exc).lower()
                is_403 = "403" in message or "forbidden" in message
                is_no_format = (
                    "requested format is not available" in message
                    or "format is not available" in message
                )

                if is_403 or is_no_format:
                    reason = "HTTP 403" if is_403 else "no matching format on default client"
                    logger.warning(
                        "Download failed (%s); retrying with fallback "
                        "clients (%s)…", reason,
                        ", ".join(name for name, _po in _CLIENT_FALLBACK_CHAIN),
                    )
                    last_exc: BaseException = exc
                    for client_name, progressive_only in _CLIENT_FALLBACK_CHAIN:
                        retry_opts = dict(opts)
                        retry_opts["extractor_args"] = {
                            "youtube": {"player_client": [client_name]}
                        }
                        # Some fallback clients (e.g. 'android') often only
                        # expose progressive (pre-merged) formats, so cap
                        # by height without requiring separate video+audio
                        # streams; others (e.g. 'tv') can still be asked
                        # for an adaptive bestvideo+bestaudio pair first.
                        retry_opts["format"] = self._build_format_selector(
                            preferred_height, progressive_only=progressive_only
                        )
                        try:
                            info, final_path = attempt(retry_opts)
                        except Exception as retry_exc:  # noqa: BLE001 - try next client
                            last_exc = retry_exc
                            retry_msg = (
                                _clean_yt_dlp_message(retry_exc)
                                if isinstance(retry_exc, Exception) else str(retry_exc)
                            )
                            if "drm protected" in retry_msg.lower():
                                # A DRM report from 'tv' alone is not
                                # trustworthy (see last_tv_drm_false_positive
                                # docstring) — only trust it as real DRM if
                                # a different client independently agrees.
                                if client_name == "tv":
                                    self.last_tv_drm_false_positive = True
                                else:
                                    self.last_drm_detected = True
                            logger.warning(
                                "'%s' client also failed (%s); trying next "
                                "fallback…", client_name, retry_msg,
                            )
                            continue

                        actual_height = info.get("height")
                        if preferred_height and actual_height and actual_height < preferred_height:
                            logger.warning(
                                "Note: the '%s' fallback client only offered "
                                "%sp for this video (requested %sp) — this "
                                "is a YouTube-side limitation, not a "
                                "setting in this app.",
                                client_name, actual_height, preferred_height,
                            )
                        return info, final_path

                    raise DownloadError(
                        "YouTube refused this download, even after retrying "
                        "with multiple different clients and format selectors.",
                        details=(
                            f"Original error: {_clean_yt_dlp_message(exc)}\n"
                            f"Last fallback error: {_clean_yt_dlp_message(last_exc)}\n"
                            "This is usually fixed by updating yt-dlp: "
                            "pip install -U yt-dlp"
                        ),
                    ) from last_exc
                elif "private" in message or "unavailable" in message or "removed" in message:
                    raise VideoUnavailableError(
                        "This video is unavailable, private, or has been removed.",
                        details=_clean_yt_dlp_message(exc),
                    ) from exc
                elif "network" in message or "urlopen" in message or "timed out" in message:
                    raise NetworkError(
                        "A network error interrupted the download.",
                        details=_clean_yt_dlp_message(exc),
                    ) from exc
                else:
                    raise DownloadError(
                        "Failed to download the video.", details=_clean_yt_dlp_message(exc)
                    ) from exc

        try:
            info, final_path = attempt_with_client_fallback(ydl_opts_base)
            self.last_info = info
        except AppError:
            # Already a specific, user-facing error from the client-fallback
            # helper above (and not a cookie problem, otherwise it would
            # have been raised as a different exception type) — propagate
            # as-is rather than re-wrapping it.
            raise
        except Exception as exc:  # noqa: BLE001 - inspect broadly: cookie
            # loading failures surface as yt_dlp.cookies.CookieLoadError
            # (or, on older yt-dlp versions, a plain DownloadError with a
            # cookie-specific message), neither of which is necessarily an
            # AppError, so they land here rather than the branch above.
            if (cookies_from_browser or cookies_file) and _is_cookie_error(exc):
                if cookies_file:
                    reason = (
                        f"Không đọc được file cookies.txt ({cookies_file}) — "
                        "file có thể bị hỏng, sai định dạng, hoặc đã hết hạn"
                    )
                else:
                    reason = (
                        f"Không đọc được cookie từ {cookies_from_browser.capitalize()} "
                        "(có thể trình duyệt đang mở hoặc bị chặn quyền truy cập)"
                    )
                logger.warning(
                    "%s: %s. Retrying anonymously (without login cookies)…",
                    reason, _clean_yt_dlp_message(exc),
                )
                if progress_callback:
                    progress_callback(0.0, f"{reason} — đang tự động thử tải video ẩn danh…")
                no_cookie_opts = {
                    k: v for k, v in ydl_opts_base.items()
                    if k not in ("cookiesfrombrowser", "cookiefile")
                }
                try:
                    info, final_path = attempt_with_client_fallback(no_cookie_opts)
                    self.last_info = info
                except AppError:
                    raise
                except Exception as fallback_exc:  # noqa: BLE001
                    raise DownloadError(
                        "An unexpected error occurred while downloading "
                        "(also failed after retrying anonymously).",
                        details=_clean_yt_dlp_message(fallback_exc),
                    ) from fallback_exc
            else:
                raise DownloadError(
                    "An unexpected error occurred while downloading.",
                    details=_clean_yt_dlp_message(exc),
                ) from exc

        actual_height = info.get("height")
        if actual_height:
            logger.info("Downloaded at %sp (requested: %s).",
                        actual_height, f"{preferred_height}p" if preferred_height else "best")
            if self.last_drm_detected:
                logger.warning(
                    "Stuck at %sp. This video is DRM-protected on "
                    "YouTube's side — its higher-resolution streams are "
                    "encrypted, and yt-dlp deliberately never breaks that "
                    "encryption. This is unrelated to the PO Token setup "
                    "and won't be fixed by it; %sp (or whatever the "
                    "fallback client still exposes unencrypted) is the "
                    "ceiling for this specific video.",
                    actual_height, actual_height,
                )
            elif self.last_tv_drm_false_positive:
                logger.warning(
                    "Stuck at %sp. The 'tv' client reported this video as "
                    "DRM-protected, but no other client agreed — this "
                    "usually means YouTube's known 'tv'-client DRM "
                    "experiment (yt-dlp issue #12563) gave a false "
                    "positive, not that the video is actually DRM-locked. "
                    "The real question is why the 'web' client failed "
                    "first (see the earlier log line): if it's a missing "
                    "or non-functioning PO Token, fixing that (README.md, "
                    "'Fixing the 360p cap') should let 'web' succeed "
                    "directly and skip 'tv' entirely.",
                    actual_height,
                )
            elif actual_height <= 360 and (preferred_height is None or preferred_height > 360):
                logger.warning(
                    "Stuck at %sp. This almost always means yt-dlp's 'web' "
                    "client got refused and fell back to a client that "
                    "only exposes the old 360p stream. This is not a "
                    "resolution setting in this app — it's one of two "
                    "YouTube-side requirements: (1) a missing/invalid PO "
                    "Token, or (2) no supported JavaScript runtime "
                    "installed on this machine (deno or node — needed to "
                    "solve YouTube's 'n challenge'; check the on-disk log "
                    "file for 'No supported JavaScript runtime' or 'n "
                    "challenge solving failed' to confirm which one this "
                    "is). See README.md, section 'Fixing the 360p cap', "
                    "for both fixes.",
                    actual_height,
                )
            elif preferred_height and actual_height < preferred_height:
                logger.warning(
                    "Requested %sp but only %sp was available/selected for this video.",
                    preferred_height, actual_height,
                )

        resolved = self._resolve_final_path(
            info=info,
            final_path=final_path,
            hook_reported_path=downloaded_path.get("path"),
            output_dir=output_dir,
        )
        if resolved is None:
            raise DownloadError(
                "Download reported success but the output file could not be found.",
                details=(
                    f"Looked for video id '{info.get('id')}' in {output_dir}. "
                    "The file may have been merged under a different name than expected."
                ),
            )

        return resolved

    @staticmethod
    def _resolve_final_path(
        info: dict,
        final_path: str,
        hook_reported_path: str | None,
        output_dir: Path,
    ) -> Path | None:
        """Determine the actual merged output file on disk.

        yt-dlp reports several different candidate paths depending on
        whether a merge/postprocessing step ran (video+audio merge changes
        the final filename/extension after progress_hooks already fired),
        so no single source is reliable on its own. Try each candidate in
        order of trustworthiness, then fall back to searching output_dir
        directly for the video ID this download was for.
        """
        candidates: list[Path] = []

        # yt-dlp attaches the true final path(s) here after any merge.
        for rd in info.get("requested_downloads") or []:
            fp = rd.get("filepath") or rd.get("_filename")
            if fp:
                candidates.append(Path(fp))

        if info.get("filepath"):
            candidates.append(Path(info["filepath"]))
        if hook_reported_path:
            candidates.append(Path(hook_reported_path))
        if final_path:
            candidates.append(Path(final_path))

        for candidate in candidates:
            if _is_completed_media_file(candidate):
                return candidate
            alt = candidate.with_suffix(".mp4")
            if _is_completed_media_file(alt):
                return alt

        # Last resort: the merge_output_format is mp4 and outtmpl always
        # names files source_<id>_<resolution>.<ext>, so search directly
        # by video ID (resolution suffix may vary, hence the wildcard).
        video_id = info.get("id")
        if video_id:
            matches = [
                m for m in output_dir.glob(f"source_{video_id}_*")
                if _is_completed_media_file(m)
            ]
            if matches:
                matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                return matches[0]

        return None

    def _find_cached(self, url: str, output_dir: Path, resolution_tag: str) -> Path | None:
        """Return an already-downloaded file for this video ID AND
        resolution, if present.

        Extracts only metadata (no download) to get the video ID cheaply,
        then checks whether source_<id>_<resolution_tag>.* already exists
        on disk. Different resolutions are cached separately so switching
        the resolution picker always triggers a fresh download rather
        than silently reusing a lower-quality cached file.
        """
        video_id = self._extract_video_id(url)
        if video_id is None:
            return None

        for existing in output_dir.glob(f"source_{video_id}_{resolution_tag}.*"):
            if _is_completed_media_file(existing):
                return existing
        return None

    @staticmethod
    def _build_format_selector(
        preferred_height: int | None, progressive_only: bool = False
    ) -> str:
        """Build a yt-dlp format selector string for the desired resolution.

        Tries H.264/mp4 first (ideal — FFmpeg can stream-copy it directly),
        then falls back to ANY codec/container (e.g. VP9/webm, which is
        how YouTube serves most 1080p+ video) rather than hard-requiring
        mp4. Requiring mp4 unconditionally was excluding all high-
        resolution formats for many videos — sometimes leaving no match
        at all ("Requested format is not available").

        Args:
            preferred_height: Max height in pixels, or None for best
                available.
            progressive_only: If True, only request pre-merged formats
                (used for the android-client fallback, which frequently
                doesn't expose separate adaptive streams).
        """
        height_filter = f"[height<={preferred_height}]" if preferred_height else ""

        if progressive_only:
            parts = [
                f"best{height_filter}[ext=mp4]",
                f"best{height_filter}",
                "best",
            ]
        else:
            parts = [
                # Ideal case: H.264 video in mp4 — FFmpeg can stream-copy
                # this directly without re-encoding.
                f"bestvideo{height_filter}[ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]",
                # Fallback: any codec/container (commonly VP9/webm for
                # 1080p+) — FFmpeg will re-encode automatically if needed.
                f"bestvideo{height_filter}+bestaudio",
                f"best{height_filter}[ext=mp4]",
                f"best{height_filter}",
                "best",
            ]
        # Drop duplicate fallback entries (happens when height_filter is empty).
        deduped: list[str] = []
        for part in parts:
            if not deduped or deduped[-1] != part:
                deduped.append(part)
        return "/".join(deduped)

    @staticmethod
    def _extract_video_id(url: str) -> str | None:
        """Resolve `url` to an 11-char YouTube video ID, used only to
        check the on-disk cache before deciding whether to download at
        all.

        Tries a plain regex match against the URL text first — for the
        vast majority of real-world YouTube links (watch?v=, youtu.be/,
        /shorts/, with or without extra tracking query params tacked
        on) the ID is sitting right there in the URL itself, so this
        resolves instantly with zero network requests. Falls back to
        an actual (metadata-only, no video download) yt-dlp request
        only for URLs that don't match that shape — e.g. a bare
        channel/playlist link that redirects to a video, or a shape
        this regex doesn't recognize.

        Skipping that network round-trip in the common case measurably
        shortens the delay between clicking "Cut Clips" and the actual
        download starting, and also means cutting more clips from a
        video already cut once in this session (the cache-hit path)
        resolves near-instantly instead of waiting on a metadata
        request every single time.
        """
        match = _VIDEO_ID_IN_URL_RE.search(url)
        if match:
            return match.group(1)

        try:
            with yt_dlp.YoutubeDL(
                {
                    "quiet": True,
                    "no_warnings": True,
                    "skip_download": True,
                    "logger": _SilentYtDlpLogger(),
                }
            ) as ydl:
                info = ydl.extract_info(url, download=False, process=False)
                return info.get("id")
        except Exception as exc:  # noqa: BLE001 - cache lookup is best-effort only
            logger.debug("Could not resolve video ID for cache lookup: %s", _clean_yt_dlp_message(exc))
            return None

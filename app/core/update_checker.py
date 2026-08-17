"""
Periodic background check for a newer yt-dlp release.

Why this exists: YouTube changes its player/signature scheme often
enough that an otherwise-unchanged, previously-working yt-dlp install
can suddenly start failing to download or resolve formats — nothing in
THIS app's own code broke, yt-dlp's YouTube extractor just fell behind.
requirements.txt already tells developers to periodically run
`pip install -U "yt-dlp[default]"` by hand; this module automates that
check (and, when possible, the upgrade itself) so a user doesn't have
to hit a mysterious download failure first and go dig up that
instruction.

What this can and can't do, by install type — read this before
expecting it to "just fix" every failure:
  - Running from source (`python main.py` in a normal editable venv):
    a newer yt-dlp is pip-installed automatically in the background.
    Takes effect the NEXT time the app is launched — the copy already
    imported into this running process stays as-is until then, Python
    doesn't hot-swap an already-imported package mid-run.
  - Running the built .exe (PyInstaller --onefile, see build.bat):
    yt-dlp is frozen INSIDE that .exe at build time — there is no live
    site-packages folder to upgrade into while it's running. This
    module can only detect that a newer version exists and tell the
    user; it can never silently patch a running .exe. The fix in that
    case is: `pip install -U "yt-dlp[default]"` in the dev environment,
    then re-run build.bat to produce a new .exe.

Checked at most once per 24 hours (the timestamp is persisted in
AppConfig, so this throttle survives app restarts) — PyPI's JSON API is
free and unauthenticated but there's no reason to hit it more often.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.error import URLError
from urllib.request import urlopen

logger = logging.getLogger("clip_cutter")

_PYPI_URL = "https://pypi.org/pypi/yt-dlp/json"
_CHECK_INTERVAL = timedelta(hours=24)
_REQUEST_TIMEOUT_SECONDS = 6.0
_PIP_INSTALL_TIMEOUT_SECONDS = 180


@dataclass
class UpdateCheckResult:
    """Compact summary of one check_and_maybe_update() run — everything
    the Activity Log's logger.* calls already say, but structured so a
    caller (MainWindow's compact status pill) can display it without
    having to parse log text. `kind` matches the values
    UpdateStatusWidget.set_status() understands: 'idle' (skipped, still
    within the 24h throttle), 'ok' (already up to date), 'update' (a
    newer version exists — auto-installed, or just detected on a frozen
    .exe), 'error' (network/lookup failure)."""

    kind: str  # 'idle' | 'ok' | 'update' | 'error'
    text: str  # short, pill-friendly summary
    installed_version: str | None = None
    latest_version: str | None = None


def _parse_version(version: str) -> tuple:
    """Best-effort sortable key for yt-dlp's CalVer-ish version strings
    (e.g. '2026.07.04' or a nightly like '2026.07.04.123456'), without
    depending on the third-party `packaging` package — that rides along
    as *someone else's* dependency today, but nothing in
    requirements.txt actually declares it directly, so relying on it
    here would be fragile. Splits on '.' and compares each dot-
    separated chunk numerically; a non-numeric chunk (not expected in
    practice) sorts after any numeric one instead of crashing the
    comparison.
    """
    parts: list[tuple[int, object]] = []
    for chunk in version.split("."):
        try:
            parts.append((0, int(chunk)))
        except ValueError:
            parts.append((1, chunk))
    return tuple(parts)


def get_installed_version() -> str | None:
    """The yt-dlp version currently importable in this process, or None
    if yt-dlp somehow isn't importable at all (should never happen —
    it's a hard requirement — but this must never raise)."""
    try:
        import yt_dlp
        return getattr(getattr(yt_dlp, "version", None), "__version__", None) or getattr(
            yt_dlp, "__version__", None
        )
    except Exception:
        logger.debug("Could not read installed yt-dlp version.", exc_info=True)
        return None


def get_latest_version(timeout: float = _REQUEST_TIMEOUT_SECONDS) -> str | None:
    """The latest yt-dlp version published on PyPI, or None on any
    failure (offline, PyPI down, unexpected response shape, ...) — an
    update check is a nice-to-have, never allowed to look like a real
    app error or raise into the caller."""
    try:
        with urlopen(_PYPI_URL, timeout=timeout) as resp:
            data = json.load(resp)
        version = data.get("info", {}).get("version")
        return version if isinstance(version, str) and version else None
    except (URLError, TimeoutError, ValueError, OSError) as exc:
        logger.debug("yt-dlp update check: could not reach PyPI (%s).", exc)
        return None


def is_check_due(last_checked_iso: str, interval: timedelta = _CHECK_INTERVAL) -> bool:
    """True if it's been >= `interval` since `last_checked_iso` (or that
    timestamp is empty/unparsable, meaning "never checked")."""
    if not last_checked_iso:
        return True
    try:
        last = datetime.fromisoformat(last_checked_iso)
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - last >= interval


def _is_frozen() -> bool:
    """True when running as a PyInstaller-built .exe rather than a
    normal `python main.py` process — see the module docstring for why
    that changes what an update check can actually do."""
    return bool(getattr(sys, "frozen", False))


def _pip_upgrade_ytdlp() -> tuple[bool, str]:
    """Runs `<this interpreter> -m pip install -U "yt-dlp[default]"`.

    Only ever called when NOT frozen (see module docstring) — in that
    case sys.executable is a real Python interpreter with a live
    site-packages directory, the same one this app is already importing
    yt_dlp from, so this reliably lands the upgrade where it'll
    actually be picked up on next launch.
    """
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-U", "yt-dlp[default]"],
            capture_output=True,
            text=True,
            timeout=_PIP_INSTALL_TIMEOUT_SECONDS,
        )
        if proc.returncode == 0:
            return True, proc.stdout[-500:]
        return False, (proc.stderr or proc.stdout)[-500:]
    except (subprocess.SubprocessError, OSError) as exc:
        return False, str(exc)


def check_and_maybe_update(config, force: bool = False) -> UpdateCheckResult:
    """Check PyPI for a newer yt-dlp release and, if running from
    source, upgrade automatically; log everything through the app's
    normal logger either way, and return a compact UpdateCheckResult
    for callers that want to display it (see MainWindow's
    UpdateStatusWidget) without re-parsing log text.

    Safe to call from a background thread — this is meant to run
    exactly like app.core.pot_server's own startup check (see
    MainWindow.__init__): fire-and-forget in a daemon thread, results
    show up in the Activity Log via the same Qt log bus every other
    background log message already uses. Never raises: any failure
    (network, pip, a corrupt timestamp) is logged and swallowed so a
    flaky connection can never disrupt the rest of the app.

    Args:
        config: The loaded AppConfig — its last_ytdlp_update_check
            field is read and written back here so this only actually
            hits the network once per 24h across app restarts.
        force: Skip the 24h throttle (for a manual "check now" action).
            The outcome is still logged the same way and the timestamp
            is still updated on completion either way.
    """
    try:
        if not force and not is_check_due(config.last_ytdlp_update_check):
            return UpdateCheckResult("idle", "yt-dlp")

        installed = get_installed_version()
        latest = get_latest_version()

        # Stamp "checked now" even on a failed/empty lookup — otherwise
        # a persistent network hiccup would make every single app
        # launch retry the check instead of backing off for 24h the
        # same way a successful check would.
        config.last_ytdlp_update_check = datetime.now(timezone.utc).isoformat()
        config.save()

        if latest is None:
            logger.debug("yt-dlp update check: PyPI unreachable, skipping this time.")
            return UpdateCheckResult("error", "Không kiểm tra được (mất mạng?)", installed)
        if installed is None:
            logger.debug("yt-dlp update check: could not read the installed version.")
            return UpdateCheckResult("error", "Không đọc được bản yt-dlp đang cài", latest_version=latest)

        if _parse_version(latest) <= _parse_version(installed):
            logger.info("yt-dlp đã là bản mới nhất (%s).", installed)
            return UpdateCheckResult("ok", f"yt-dlp mới nhất ({installed})", installed, latest)

        logger.warning(
            "Có bản yt-dlp mới: %s (đang dùng %s). YouTube thay đổi liên "
            "tục — dùng bản yt-dlp cũ có thể khiến tải video/cắt clip bắt "
            "đầu lỗi bất ngờ dù app không hề thay đổi gì.",
            latest, installed,
        )

        if _is_frozen():
            logger.warning(
                "Bản .exe này đã đóng gói sẵn yt-dlp bên trong nên app "
                "không thể tự vá bản mới vào file .exe đang chạy. Cách "
                "cập nhật: chạy `pip install -U \"yt-dlp[default]\"` trong "
                "môi trường dev rồi build lại bằng build.bat để có bản "
                ".exe mới dùng yt-dlp %s.",
                latest,
            )
            return UpdateCheckResult(
                "update", f"Có bản mới {latest} — cần build lại .exe", installed, latest,
            )

        logger.info("Đang tự động cập nhật yt-dlp lên %s…", latest)
        ok, detail = _pip_upgrade_ytdlp()
        if ok:
            logger.info(
                "Đã cập nhật yt-dlp lên %s. Khởi động lại app để dùng "
                "bản mới.",
                latest,
            )
            return UpdateCheckResult(
                "update", f"Đã cập nhật lên {latest} — khởi động lại app", installed, latest,
            )
        else:
            logger.warning(
                "Tự động cập nhật yt-dlp thất bại — chạy tay: "
                "pip install -U \"yt-dlp[default]\" (chi tiết: %s)",
                detail,
            )
            return UpdateCheckResult(
                "error", f"Có bản mới {latest} nhưng tự cập nhật thất bại", installed, latest,
            )
    except Exception:  # noqa: BLE001 - a background update check must
        # never be able to take down the app or look like a real crash.
        logger.debug("yt-dlp update check failed unexpectedly.", exc_info=True)
        return UpdateCheckResult("error", "Lỗi khi kiểm tra cập nhật")

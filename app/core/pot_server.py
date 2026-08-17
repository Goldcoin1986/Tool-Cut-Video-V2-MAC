"""
Manages the local bgutil-ytdlp-pot-provider HTTP server (PO Token
provider) as a background subprocess owned by this app.

Why this exists: yt-dlp's 'web' client (the only one that can serve
720p/1080p+) requires a PO Token as of early 2026 (see README.md,
"Fixing the 360p cap"). The bgutil plugin can source one two ways —
"script mode" (spawn a fresh Node process per token request) or "HTTP
server mode" (one long-lived Node process, reused for every request).

Script mode is what yt-dlp falls back to when no HTTP server is
running, and it is unreliable in practice: each cold-start Node
invocation can occasionally exceed yt-dlp's hard-coded 15s subprocess
timeout (a known upstream issue — see
github.com/Brainicism/bgutil-ytdlp-pot-provider/issues/232), silently
marking the whole PO Token provider "unavailable" for that attempt and
dropping the 'web' client's format list down to whatever low-res
formats remain unauthenticated — this is what caused downloads to be
capped at 360p even with a JS runtime and the plugin correctly
installed.

Starting the HTTP server ourselves once, at app launch, avoids that
entirely: yt-dlp always prefers the HTTP provider over the script one
when both are available, and a persistent process never pays the
cold-start cost that made the script method flaky.
"""

from __future__ import annotations

import logging
import platform
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Callable

logger = logging.getLogger("clip_cutter")

_POT_SERVER_HOST = "127.0.0.1"
_POT_SERVER_PORT = 4416
_STARTUP_TIMEOUT_S = 10.0
_POLL_INTERVAL_S = 0.25


def _default_server_dir() -> Path:
    """Default location the bgutil plugin (and this app's README) expect
    the cloned+built provider repo to live at:
    ~/bgutil-ytdlp-pot-provider/server (%USERPROFILE% on Windows)."""
    return Path.home() / "bgutil-ytdlp-pot-provider" / "server"


def _is_port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    """True if something is already listening on host:port — whether
    that's our own previously-started server or one the user started
    manually in a separate terminal."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class PotServerManager:
    """Starts/stops the local PO Token HTTP server (build/main.js) as a
    child process of this app.

    Safe to call start() even if a server is already running (from a
    previous instance that crashed, or one the user started by hand)
    — it detects the open port and adopts that as "already running"
    rather than spawning a duplicate, which would just fail to bind
    the port anyway.
    """

    def __init__(self, server_dir: Path | None = None) -> None:
        self._server_dir = server_dir or _default_server_dir()
        self._process: subprocess.Popen | None = None
        self._we_own_process = False

    @property
    def is_running(self) -> bool:
        return _is_port_open(_POT_SERVER_HOST, _POT_SERVER_PORT)

    def start(self) -> bool:
        """Start the server if it isn't already running.

        Returns True if a server is confirmed listening on the PO
        Token port by the time this returns (whether this call started
        it or it was already up), False if it could not be started
        (missing Node, missing/not-yet-built server dir, or it failed
        to come up in time).

        Never raises — PO Token unavailability is a soft degradation
        (yt-dlp falls back to lower-resolution formats via the client
        fallback chain in downloader.py), not a fatal error, so
        failures here are logged and swallowed rather than propagated
        to the caller. Safe to call from a background thread: all
        logging goes through the app's existing thread-safe logger /
        Qt signal bus.
        """
        if self.is_running:
            logger.info(
                "PO Token HTTP server already listening on %s:%d "
                "(reusing existing instance).",
                _POT_SERVER_HOST, _POT_SERVER_PORT,
            )
            return True

        entry_point = self._server_dir / "build" / "main.js"
        if not entry_point.is_file():
            logger.warning(
                "PO Token HTTP server not set up (expected %s to exist) "
                "— skipping. yt-dlp will fall back to script mode, "
                "which may cap downloads at 360p. See README.md, "
                "'Fixing the 360p cap', Step 2.",
                entry_point,
            )
            return False

        node_path = shutil.which("node")
        if not node_path:
            logger.warning(
                "Node.js not found on PATH — cannot start the PO Token "
                "HTTP server. yt-dlp will fall back to script mode, "
                "which may cap downloads at 360p. See README.md, "
                "'Fixing the 360p cap', Step 1."
            )
            return False

        creationflags = 0
        startupinfo = None
        if platform.system() == "Windows":
            # Hide the Node console window (this app already has its own
            # GUI window; a second console popping up would look like a
            # bug) and put it in its own process group so closing THIS
            # app's window doesn't take Node down via a shared console-
            # close signal before stop() gets a chance to terminate it
            # cleanly.
            creationflags = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            )
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        try:
            self._process = subprocess.Popen(
                [node_path, str(entry_point)],
                cwd=str(self._server_dir),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=creationflags,
                startupinfo=startupinfo,
            )
        except OSError as exc:
            logger.warning("Failed to launch PO Token HTTP server: %s", exc)
            self._process = None
            return False

        self._we_own_process = True

        deadline = time.monotonic() + _STARTUP_TIMEOUT_S
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                logger.warning(
                    "PO Token HTTP server exited immediately (code %s) "
                    "— falling back to script mode, which may cap "
                    "downloads at 360p.", self._process.returncode,
                )
                self._process = None
                self._we_own_process = False
                return False
            if self.is_running:
                logger.info(
                    "PO Token HTTP server started on %s:%d (pid %d).",
                    _POT_SERVER_HOST, _POT_SERVER_PORT, self._process.pid,
                )
                return True
            time.sleep(_POLL_INTERVAL_S)

        logger.warning(
            "PO Token HTTP server did not come up within %.0fs — "
            "falling back to script mode, which may cap downloads at "
            "360p.", _STARTUP_TIMEOUT_S,
        )
        return False

    def wait_until_ready(
        self,
        timeout: float = 90.0,
        progress_callback: "Callable[[float, str], None] | None" = None,
    ) -> bool:
        """Block the calling thread until the PO Token HTTP server is
        reachable, or `timeout` seconds pass.

        Used as a safety net right before a download: start() (called
        once at app launch, from a background thread) sometimes hasn't
        finished by the time the user pastes a URL and clicks "Cut
        Clips" — on machines where Node's BotGuard/V8 initialization is
        slow (cold-start can take minutes, not seconds, on some
        hardware), the fixed short wait inside start() gives up and
        logs a warning, but the underlying Node process keeps running
        and does eventually finish. Without this second wait here, the
        very first download of a session would go out on the 360p
        fallback chain even though the server would have been ready
        moments later — exactly the situation this whole feature exists
        to avoid. Subsequent downloads in the same session are
        unaffected (is_running already True, returns immediately).
        """
        if self.is_running:
            return True
        if not self._we_own_process or self._process is None:
            # We never started a server (missing Node/plugin, or it
            # exited immediately) — nothing to wait for.
            return False

        deadline = time.monotonic() + timeout
        last_message_at = 0.0
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                return False
            if self.is_running:
                return True
            now = time.monotonic()
            if progress_callback and now - last_message_at > 4.0:
                remaining = max(0, int(deadline - now))
                progress_callback(
                    0.0,
                    "Đang khởi động PO Token server để mở khóa độ phân "
                    f"giải cao (lần đầu có thể mất một lúc, còn tối đa "
                    f"{remaining}s)…",
                )
                last_message_at = now
            time.sleep(_POLL_INTERVAL_S)
        return self.is_running

    def stop(self) -> None:
        """Terminate the server, but only if this instance started it.

        Does nothing if the server was already running before start()
        was called — this app never kills a server it didn't spawn,
        in case the user is running it deliberately for their own
        reasons (e.g. from a separate terminal, or as a background
        service shared with other tools)."""
        if not self._we_own_process or self._process is None:
            return
        if self._process.poll() is not None:
            self._process = None
            self._we_own_process = False
            return
        try:
            self._process.terminate()
            try:
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=3)
        except Exception as exc:  # noqa: BLE001 - shutdown must never crash the app
            logger.debug("Error stopping PO Token HTTP server: %s", exc)
        finally:
            self._process = None
            self._we_own_process = False


_shared_manager: "PotServerManager | None" = None


def get_shared_manager() -> PotServerManager:
    """Return the single PotServerManager instance for this process.

    MainWindow starts it once at app launch; downloader.py calls
    wait_until_ready() on this same instance right before a download,
    so both sides agree on whether a server is already up/starting
    rather than each independently spawning their own."""
    global _shared_manager
    if _shared_manager is None:
        _shared_manager = PotServerManager()
    return _shared_manager

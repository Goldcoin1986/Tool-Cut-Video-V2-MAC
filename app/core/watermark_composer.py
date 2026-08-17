"""
Composes the optional platform-icon watermark: a real X / Facebook /
TikTok / YouTube glyph drawn right next to the handle text (e.g. the
"X" in "X @toniboiboi" becomes the actual X logo instead of a plain
letter), rendered as one transparent PNG that FFmpeg burns into a
corner of every cut clip via the `overlay` filter.

Kept entirely separate from ffmpeg_cutter.py's plain-text `drawtext`
path: when no platform icon is selected, none of this is touched and
the original text-only watermark behaves exactly as before.

Icon source: the bundled PNGs in app/assets/icons/ were rasterized
from the official brand glyphs published by the Simple Icons project
(https://simpleicons.org, CC0 1.0 — public domain), as plain white
silhouettes. By default they're tinted at compose time to match
whatever text color the user picks, so the icon always matches the
handle text.

Optionally (`use_brand_color=True`), the icon is instead tinted with
that platform's own official brand color(s) instead of the text color
— X's black, Facebook's blue, YouTube's red, and for TikTok specifically
a recreation of its signature 3-layer "glitch" mark (an offset cyan
copy and an offset magenta copy peeking out from behind the solid
black glyph) instead of a flat single-color silhouette. The handle
text itself keeps using whatever text color the user picked either way
— this toggle only affects the icon glyph.

A third mode (`fixed_color_logo=True`) skips tinting entirely and
burns in the platform's real, fixed, full-color logo artwork (the
official multi-color mark — e.g. Facebook's blue circle + white "f",
YouTube's red rounded-rect + white triangle) exactly as-is, unaffected
by both `text_color` and `use_brand_color`. Those full-color source
files live in app/assets/icons/color/. `fixed_color_logo` takes
priority over `use_brand_color` when both are somehow set.
"""

from __future__ import annotations

import hashlib
import logging
import sys
import threading
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger("clip_cutter")

# Guards the check-then-render-then-save sequence in
# compose_platform_badge() below. Every clip cut in one run normally
# shares the exact same watermark settings (only timestamps differ),
# so they all resolve to the same cache file — ClipCutter.
# warm_watermark_cache() renders it once, up front, specifically so
# concurrent cut_clip() calls never hit this function's "not cached
# yet" branch at the same time in practice. This lock is just a
# defense-in-depth backstop for that (e.g. a future caller that
# forgets to warm the cache first) — cheap since the render itself
# only runs at most once per unique cache key either way.
_cache_lock = threading.Lock()

# Bumped whenever compose_platform_badge()'s rendering logic changes in
# a way that changes the output pixels for the *same* settings (e.g.
# the fixed-color-logo black-stroke fix below) — folded into the cache
# key so a stale PNG left over on disk from a previous version of this
# module can never silently get reused instead of being re-rendered.
# Without this, someone who cut a clip once, then updated/reinstalled
# the app and cut again with identical watermark settings, would keep
# seeing the old (buggy-looking) cached badge forever, since the cache
# key would otherwise be byte-for-byte identical between versions.
_BADGE_RENDER_VERSION = "2"

# Display key -> bundled icon filename (app/assets/icons/<key>.png).
# Keep this in sync with WatermarkPicker.PLATFORM_OPTIONS.
PLATFORM_ICON_FILES: dict[str, str] = {
    "x": "x.png",
    "facebook": "facebook.png",
    "tiktok": "tiktok.png",
    "youtube": "youtube.png",
}

# Same keys, but pointing at the real fixed-color logo artwork under
# app/assets/icons/color/ instead of the plain white silhouettes above
# — used when fixed_color_logo=True. Kept as a separate dict/directory
# rather than swapping the plain files in place, since the silhouettes
# above still have to stay recolorable for the other two modes.
PLATFORM_ICON_FILES_COLOR: dict[str, str] = {
    "x": "x.png",
    "facebook": "facebook.png",
    "tiktok": "tiktok.png",
    "youtube": "youtube.png",
}

_PADDING = 14          # inner padding around the whole composed badge
_ICON_TEXT_GAP = 10    # gap between the icon glyph and the handle text
_BOX_ALPHA = 115        # ~0.45 * 255, matches drawtext's `boxcolor=...@0.45`
_STROKE_WIDTH = 3       # matches drawtext's `borderw=3` no-box fallback
_CORNER_RADIUS = 8

# Official single-color brand colors, used when use_brand_color=True.
# TikTok is deliberately absent here — it doesn't get a flat tint, see
# _TIKTOK_CYAN / _TIKTOK_MAGENTA / _TIKTOK_BLACK and
# _build_brand_icon_layer() below instead.
PLATFORM_BRAND_COLORS: dict[str, tuple[int, int, int]] = {
    "x": (0, 0, 0),              # X (Twitter) brand black
    "facebook": (24, 119, 242),  # Facebook blue (#1877F2)
    "youtube": (255, 0, 0),      # YouTube red (#FF0000)
}

# TikTok's official mark is three overlapping copies of the same note
# glyph, each a flat color, offset diagonally from one another — not a
# single flat silhouette like the other three platforms.
_TIKTOK_CYAN = (37, 244, 238)      # #25F4EE
_TIKTOK_MAGENTA = (254, 44, 85)    # #FE2C55
_TIKTOK_BLACK = (0, 0, 0)
_TIKTOK_LAYER_OFFSET_RATIO = 0.055  # offset between layers, tuned by eye


class WatermarkComposeError(Exception):
    """Raised when the icon+text badge can't be composed (missing font,
    missing/corrupt bundled icon, bad color value, disk error, etc).
    Callers treat this like "no usable font" in the plain-text path:
    log a warning and fall back rather than failing the whole cut over
    a decorative feature."""


def _icons_dir() -> Path:
    """Locate the bundled icon PNGs, both running from source and as a
    frozen PyInstaller --onefile build. Mirrors the _MEIPASS lookup
    ffmpeg_locator.py already uses for the bundled ffmpeg binaries —
    --add-data "app/assets;app/assets" extracts here at every startup."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidate = Path(meipass) / "app" / "assets" / "icons"
        if candidate.is_dir():
            return candidate
    # app/core/watermark_composer.py -> app/core -> app -> app/assets/icons
    return Path(__file__).resolve().parent.parent / "assets" / "icons"


def _color_icons_dir() -> Path:
    """Same lookup as _icons_dir(), but for the fixed full-color logo
    artwork subfolder (app/assets/icons/color/)."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidate = Path(meipass) / "app" / "assets" / "icons" / "color"
        if candidate.is_dir():
            return candidate
    return Path(__file__).resolve().parent.parent / "assets" / "icons" / "color"


def _hex_to_rgb(hex_or_ffmpeg_color: str) -> tuple[int, int, int]:
    """Accepts either '#RRGGBB' (from the color-picker swatches) or
    FFmpeg's '0xRRGGBB' form (what WatermarkSettings actually stores)."""
    value = hex_or_ffmpeg_color.strip()
    if value.lower().startswith("0x"):
        value = value[2:]
    value = value.lstrip("#")
    if len(value) != 6:
        raise WatermarkComposeError(f"Invalid color value: {hex_or_ffmpeg_color!r}")
    try:
        return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))
    except ValueError as exc:
        raise WatermarkComposeError(f"Invalid color value: {hex_or_ffmpeg_color!r}") from exc


def _tint(icon: Image.Image, rgb: tuple[int, int, int]) -> Image.Image:
    """Recolor a plain white-silhouette icon to `rgb`, using its own
    alpha channel as the mask so the shape (including any cutouts, like
    YouTube's play triangle) is preserved exactly."""
    tinted = Image.new("RGBA", icon.size, (*rgb, 255))
    tinted.putalpha(icon.getchannel("A"))
    return tinted


def _build_brand_icon_layer(
    icon: Image.Image, icon_size: int, platform: str
) -> tuple[Image.Image, int]:
    """Build the icon glyph tinted with that platform's official brand
    color(s), for use_brand_color=True.

    Returns (layer, bleed): `layer` is a square RGBA image, and `bleed`
    is how many extra pixels of transparent margin it has on every side
    beyond `icon_size` (0 for every platform except TikTok, whose
    offset color copies need a few spare pixels so they aren't clipped).
    Callers should paste `layer` `bleed` pixels up/left of where a
    normal icon_size x icon_size icon would go, so the un-offset core
    lines up in the same place either way.
    """
    if platform == "tiktok":
        offset = max(2, round(icon_size * _TIKTOK_LAYER_OFFSET_RATIO))
        bleed = offset
        layer = Image.new(
            "RGBA", (icon_size + 2 * bleed, icon_size + 2 * bleed), (0, 0, 0, 0)
        )
        cyan = _tint(icon, _TIKTOK_CYAN)
        magenta = _tint(icon, _TIKTOK_MAGENTA)
        black = _tint(icon, _TIKTOK_BLACK)
        # Official TikTok mark: a cyan copy peeks out up-and-left, a
        # magenta copy peeks out down-and-right, and the solid black
        # glyph sits centered on top of both — recreates the brand's
        # signature offset "glitch" look instead of a flat silhouette.
        layer.alpha_composite(cyan, (bleed - offset, bleed - offset))
        layer.alpha_composite(magenta, (bleed + offset, bleed + offset))
        layer.alpha_composite(black, (bleed, bleed))
        return layer, bleed

    brand_rgb = PLATFORM_BRAND_COLORS.get(platform)
    if brand_rgb is None:
        raise WatermarkComposeError(f"No brand color defined for platform: {platform!r}")
    return _tint(icon, brand_rgb), 0


def compose_platform_badge(
    *,
    text: str,
    platform: str,
    font_path: str,
    text_color: str,
    box_enabled: bool,
    box_color: str,
    font_size: int,
    cache_dir: Path,
    use_brand_color: bool = False,
    fixed_color_logo: bool = False,
) -> tuple[Path, int, int]:
    """Render "[platform icon]  text" onto one transparent PNG.

    Results are cached on disk by a hash of every input that affects
    the pixels, so repeated calls with the same settings (the normal
    case — every clip in a run shares one watermark config) reuse the
    same file instead of re-rendering per clip.

    Args:
        use_brand_color: When True (and fixed_color_logo is False), the
            icon glyph is tinted with that platform's own official
            brand color(s) instead of `text_color` (see module
            docstring for the TikTok special case). The handle text
            itself always keeps using `text_color` regardless of this
            flag.
        fixed_color_logo: When True, the platform's real fixed
            full-color logo artwork is burned in as-is — no tinting at
            all, so `text_color`/`use_brand_color` don't affect the
            icon glyph (they still affect the handle text). Takes
            priority over use_brand_color.

    Returns (png_path, width, height). Raises WatermarkComposeError on
    any failure; callers should catch this and fall back to the
    plain-text watermark instead of aborting the whole cut.
    """
    icon_files = PLATFORM_ICON_FILES_COLOR if fixed_color_logo else PLATFORM_ICON_FILES
    icons_dir = _color_icons_dir() if fixed_color_logo else _icons_dir()

    icon_filename = icon_files.get(platform)
    if icon_filename is None:
        raise WatermarkComposeError(f"Unknown platform icon key: {platform!r}")

    icon_path = icons_dir / icon_filename
    if not icon_path.is_file():
        raise WatermarkComposeError(f"Bundled icon not found: {icon_path}")

    cache_key = "|".join(
        [
            _BADGE_RENDER_VERSION,
            platform, text, font_path, text_color, str(box_enabled), box_color,
            str(font_size), str(use_brand_color), str(fixed_color_logo),
        ]
    )
    digest = hashlib.sha1(cache_key.encode("utf-8")).hexdigest()[:16]
    out_path = Path(cache_dir) / f"clipcutter_badge_{digest}.png"
    if out_path.is_file():
        try:
            with Image.open(out_path) as cached:
                return out_path, cached.width, cached.height
        except OSError:
            pass  # cached file got corrupted/removed externally — re-render below

    with _cache_lock:
        # Re-check now that we hold the lock — another thread may have
        # rendered and saved this exact file while we were waiting.
        if out_path.is_file():
            try:
                with Image.open(out_path) as cached:
                    return out_path, cached.width, cached.height
            except OSError:
                pass

        return _render_and_cache_badge(
            icon_path=icon_path,
            text=text,
            font_path=font_path,
            text_color=text_color,
            box_enabled=box_enabled,
            box_color=box_color,
            font_size=font_size,
            use_brand_color=use_brand_color,
            fixed_color_logo=fixed_color_logo,
            platform=platform,
            out_path=out_path,
        )


def _render_and_cache_badge(
    *,
    icon_path: Path,
    text: str,
    font_path: str,
    text_color: str,
    box_enabled: bool,
    box_color: str,
    font_size: int,
    use_brand_color: bool,
    fixed_color_logo: bool,
    platform: str,
    out_path: Path,
) -> tuple[Path, int, int]:
    try:
        icon = Image.open(icon_path).convert("RGBA")
        font = ImageFont.truetype(font_path, font_size)
    except (OSError, ValueError) as exc:
        raise WatermarkComposeError(str(exc)) from exc

    # The icon glyph is drawn a bit taller than the text's own cap-height
    # so it doesn't read as undersized next to it — tuned by eye.
    icon_size = max(16, round(font_size * 1.35))
    icon = icon.resize((icon_size, icon_size), Image.LANCZOS)

    text_r, text_g, text_b = _hex_to_rgb(text_color)

    if fixed_color_logo:
        # Real fixed-color artwork — use it exactly as bundled, no
        # recoloring of any kind.
        icon_layer, icon_bleed = icon, 0
    elif use_brand_color:
        # Recolor the icon with that platform's own official brand
        # color(s) instead of the text color (a real multi-layer mark
        # for TikTok, a flat brand-color tint for the other three).
        icon_layer, icon_bleed = _build_brand_icon_layer(icon, icon_size, platform)
    else:
        # Recolor the (plain white-glyph) icon to match the user's chosen
        # text color, using its own alpha channel as the paste mask — so
        # the icon always matches whatever color scheme the user already
        # picked for the handle text, instead of always being flat white.
        icon_layer, icon_bleed = _tint(icon, (text_r, text_g, text_b)), 0

    # Mirrors the plain-text path: a background box means readability
    # is already handled, so no stroke; no box means a black outline
    # stroke is drawn around the glyphs instead (ffmpeg_cutter.py's
    # `borderw=3:bordercolor=0x000000` fallback).
    stroke_width = 0 if box_enabled else _STROKE_WIDTH
    measurer = ImageDraw.Draw(Image.new("RGBA", (4, 4)))
    left, top, right, bottom = measurer.textbbox(
        (0, 0), text, font=font, stroke_width=stroke_width
    )
    text_w, text_h = right - left, bottom - top

    content_h = max(icon_size, text_h)
    canvas_w = _PADDING * 2 + icon_size + _ICON_TEXT_GAP + text_w
    canvas_h = _PADDING * 2 + content_h
    canvas = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))

    if box_enabled:
        box_r, box_g, box_b = _hex_to_rgb(box_color)
        ImageDraw.Draw(canvas).rounded_rectangle(
            [0, 0, canvas_w - 1, canvas_h - 1],
            radius=_CORNER_RADIUS,
            fill=(box_r, box_g, box_b, _BOX_ALPHA),
        )

    icon_y = _PADDING + (content_h - icon_size) // 2
    # icon_layer may be larger than icon_size x icon_size (TikTok's
    # offset color copies bleed a few px past the plain glyph's own
    # bbox) — shift the paste position up/left by that bleed so the
    # un-offset core still lines up exactly where a normal icon would.
    icon_paste_pos = (_PADDING - icon_bleed, icon_y - icon_bleed)

    if not box_enabled and not fixed_color_logo:
        # Mirror the text's outline stroke onto the icon too, so with no
        # background box the logo stays readable against busy video the
        # same way the handle text already does — a thin black silhouette
        # drawn at every offset around the icon layer's own alpha mask
        # (which, for TikTok's 3-layer mark, is the union footprint of
        # all three offset copies), then the colored icon painted on top.
        # Skipped entirely for fixed_color_logo: that artwork is already
        # a solid, fully-opaque badge (a filled circle/rounded-rect), so
        # outlining its own silhouette just draws an ugly black ring
        # around the whole logo instead of helping it read against video
        # — the real logo's own contrast already does that job.
        black_icon = Image.new("RGBA", icon_layer.size, (0, 0, 0, 255))
        black_icon.putalpha(icon_layer.getchannel("A"))
        for dx in range(-_STROKE_WIDTH, _STROKE_WIDTH + 1):
            for dy in range(-_STROKE_WIDTH, _STROKE_WIDTH + 1):
                if dx == 0 and dy == 0:
                    continue
                if dx * dx + dy * dy > _STROKE_WIDTH * _STROKE_WIDTH:
                    continue
                canvas.alpha_composite(
                    black_icon, (icon_paste_pos[0] + dx, icon_paste_pos[1] + dy)
                )

    canvas.alpha_composite(icon_layer, icon_paste_pos)

    text_x = _PADDING + icon_size + _ICON_TEXT_GAP - left
    text_y = _PADDING + (content_h - text_h) // 2 - top
    draw = ImageDraw.Draw(canvas)
    if box_enabled:
        draw.text((text_x, text_y), text, font=font, fill=(text_r, text_g, text_b, 255))
    else:
        draw.text(
            (text_x, text_y), text, font=font,
            fill=(text_r, text_g, text_b, 255),
            stroke_width=_STROKE_WIDTH, stroke_fill=(0, 0, 0, 255),
        )

    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(out_path, format="PNG")
    except OSError as exc:
        raise WatermarkComposeError(str(exc)) from exc

    return out_path, canvas_w, canvas_h

#!/usr/bin/env python3
"""Generate monochrome symbolic tray icons for Antigravity Telegram Bridge."""

from pathlib import Path
import cairo

ICONS_DIR = Path(__file__).resolve().parent


def draw_plane(filename: str, badge_color=None):
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 24, 24)
    ctx = cairo.Context(surface)

    # Left wing
    ctx.move_to(21.5, 2.5)
    ctx.line_to(2.5, 10.5)
    ctx.line_to(8.5, 14.5)
    ctx.line_to(19.0, 5.5)
    ctx.close_path()
    ctx.set_source_rgba(1.0, 1.0, 1.0, 0.95)
    ctx.fill_preserve()
    ctx.set_line_width(0.6)
    ctx.set_source_rgba(0.1, 0.1, 0.1, 0.35)
    ctx.stroke()

    # Right wing & body
    ctx.move_to(21.5, 2.5)
    ctx.line_to(8.5, 14.5)
    ctx.line_to(11.5, 20.5)
    ctx.line_to(14.0, 16.5)
    ctx.line_to(19.0, 20.0)
    ctx.line_to(21.5, 2.5)
    ctx.close_path()
    ctx.set_source_rgba(1.0, 1.0, 1.0, 0.95)
    ctx.fill_preserve()
    ctx.set_line_width(0.6)
    ctx.set_source_rgba(0.1, 0.1, 0.1, 0.35)
    ctx.stroke()

    # Center fold
    ctx.move_to(21.5, 2.5)
    ctx.line_to(8.5, 14.5)
    ctx.line_to(14.0, 16.5)
    ctx.close_path()
    ctx.set_source_rgba(0.0, 0.0, 0.0, 0.22)
    ctx.fill()

    if badge_color:
        ctx.arc(4.5, 19.5, 3.2, 0, 6.28)
        ctx.set_source_rgba(*badge_color)
        ctx.fill()
        ctx.set_line_width(0.7)
        ctx.set_source_rgba(0.1, 0.1, 0.1, 0.6)
        ctx.stroke()

    out_path = ICONS_DIR / f"{filename}.png"
    surface.write_to_png(str(out_path))
    print(f"Generated {out_path.name}")


def main():
    ICONS_DIR.mkdir(parents=True, exist_ok=True)
    draw_plane("telegram-bridge")
    draw_plane("telegram-bridge-symbolic")
    draw_plane("telegram-bridge-active", badge_color=(0.2, 0.85, 0.3, 1.0))
    draw_plane("telegram-bridge-error", badge_color=(0.9, 0.25, 0.2, 1.0))


if __name__ == "__main__":
    main()

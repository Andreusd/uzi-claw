#!/usr/bin/env python3
"""Generate monochrome symbolic crab tray icons for UziClaw Telegram Bridge."""

import math
from pathlib import Path
import cairo

ICONS_DIR = Path(__file__).resolve().parent


def draw_crab(filename: str, badge_color=None):
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 24, 24)
    ctx = cairo.Context(surface)

    fg = (1.0, 1.0, 1.0, 0.95)
    dark_stroke = (0.1, 0.1, 0.1, 0.45)
    shade = (0.0, 0.0, 0.0, 0.22)

    ctx.set_line_cap(cairo.LINE_CAP_ROUND)
    ctx.set_line_join(cairo.LINE_JOIN_ROUND)

    # 1. Walking legs (3 on each side)
    legs = [
        # Left legs (base -> knee -> foot)
        [(7.5, 13.5), (3.8, 12.0), (1.8, 14.2)],
        [(7.5, 15.2), (3.5, 15.5), (2.0, 18.0)],
        [(8.0, 16.8), (4.5, 18.8), (3.5, 21.0)],
        # Right legs
        [(16.5, 13.5), (20.2, 12.0), (22.2, 14.2)],
        [(16.5, 15.2), (20.5, 15.5), (22.0, 18.0)],
        [(16.0, 16.8), (19.5, 18.8), (20.5, 21.0)],
    ]

    # Outer outline of legs
    ctx.set_line_width(2.6)
    ctx.set_source_rgba(*dark_stroke)
    for p1, p2, p3 in legs:
        ctx.move_to(*p1)
        ctx.line_to(*p2)
        ctx.line_to(*p3)
        ctx.stroke()

    # White core of legs
    ctx.set_line_width(1.6)
    ctx.set_source_rgba(*fg)
    for p1, p2, p3 in legs:
        ctx.move_to(*p1)
        ctx.line_to(*p2)
        ctx.line_to(*p3)
        ctx.stroke()

    # 2. Claw arms
    arms = [
        [(8.0, 13.0), (5.5, 10.5), (4.8, 7.5)],
        [(16.0, 13.0), (18.5, 10.5), (19.2, 7.5)],
    ]
    # Arm outlines
    ctx.set_line_width(3.2)
    ctx.set_source_rgba(*dark_stroke)
    for p1, p2, p3 in arms:
        ctx.move_to(*p1)
        ctx.curve_to(p2[0], p2[1], p2[0], p2[1], p3[0], p3[1])
        ctx.stroke()

    # Arm white core
    ctx.set_line_width(2.0)
    ctx.set_source_rgba(*fg)
    for p1, p2, p3 in arms:
        ctx.move_to(*p1)
        ctx.curve_to(p2[0], p2[1], p2[0], p2[1], p3[0], p3[1])
        ctx.stroke()

    # 3. Big Claws (pincers)
    def draw_pincer(cx, cy, angle, flip=False):
        ctx.save()
        ctx.translate(cx, cy)
        ctx.rotate(math.radians(angle))
        if flip:
            ctx.scale(-1, 1)

        # Draw outer major claw finger
        ctx.new_path()
        ctx.move_to(0, 1.5)
        # Outer arc
        ctx.curve_to(-3.2, 0.8, -4.5, -3.2, -1.8, -5.5)
        ctx.curve_to(-1.0, -6.2, 0.0, -5.8, -0.5, -4.5)
        # Inner curve of outer claw
        ctx.curve_to(-1.8, -3.0, -1.2, -1.0, 0.2, -1.2)
        # Inner curve of movable claw
        ctx.curve_to(1.5, -1.0, 2.2, -3.0, 1.2, -4.8)
        ctx.curve_to(2.0, -5.2, 2.8, -4.5, 2.5, -3.5)
        # Outer curve of movable claw
        ctx.curve_to(3.8, -1.5, 2.5, 1.2, 0, 1.5)
        ctx.close_path()

        ctx.set_source_rgba(*fg)
        ctx.fill_preserve()
        ctx.set_line_width(0.7)
        ctx.set_source_rgba(*dark_stroke)
        ctx.stroke()

        # Inner pincer shading
        ctx.new_path()
        ctx.move_to(0, 1.0)
        ctx.curve_to(1.8, 0.5, 2.2, -1.8, 1.2, -3.5)
        ctx.curve_to(0.8, -1.5, 0.0, -0.5, 0, 1.0)
        ctx.close_path()
        ctx.set_source_rgba(*shade)
        ctx.fill()

        ctx.restore()

    draw_pincer(4.5, 6.2, -10, flip=False)
    draw_pincer(19.5, 6.2, 10, flip=True)

    # 4. Eyestalks & Eyes
    # Stalks
    ctx.set_line_width(2.0)
    ctx.set_source_rgba(*dark_stroke)
    ctx.move_to(9.5, 11.5)
    ctx.line_to(9.2, 8.5)
    ctx.stroke()
    ctx.move_to(14.5, 11.5)
    ctx.line_to(14.8, 8.5)
    ctx.stroke()

    ctx.set_line_width(1.2)
    ctx.set_source_rgba(*fg)
    ctx.move_to(9.5, 11.5)
    ctx.line_to(9.2, 8.5)
    ctx.stroke()
    ctx.move_to(14.5, 11.5)
    ctx.line_to(14.8, 8.5)
    ctx.stroke()

    # Eye whites
    ctx.arc(9.2, 7.8, 1.6, 0, 2 * math.pi)
    ctx.set_source_rgba(*fg)
    ctx.fill_preserve()
    ctx.set_line_width(0.6)
    ctx.set_source_rgba(*dark_stroke)
    ctx.stroke()

    ctx.arc(14.8, 7.8, 1.6, 0, 2 * math.pi)
    ctx.set_source_rgba(*fg)
    ctx.fill_preserve()
    ctx.set_line_width(0.6)
    ctx.set_source_rgba(*dark_stroke)
    ctx.stroke()

    # Eye pupils
    ctx.arc(9.5, 7.8, 0.7, 0, 2 * math.pi)
    ctx.set_source_rgba(0.1, 0.1, 0.1, 0.85)
    ctx.fill()

    ctx.arc(14.5, 7.8, 0.7, 0, 2 * math.pi)
    ctx.set_source_rgba(0.1, 0.1, 0.1, 0.85)
    ctx.fill()

    # 5. Carapace (Body)
    ctx.save()
    ctx.translate(12.0, 14.5)
    ctx.scale(5.2, 3.8)
    ctx.arc(0, 0, 1.0, 0, 2 * math.pi)
    ctx.restore()
    ctx.set_source_rgba(*fg)
    ctx.fill_preserve()
    ctx.set_line_width(0.7)
    ctx.set_source_rgba(*dark_stroke)
    ctx.stroke()

    # Carapace subtle smile groove
    ctx.arc(12.0, 14.0, 2.5, 0.35 * math.pi, 0.65 * math.pi)
    ctx.set_line_width(0.8)
    ctx.set_source_rgba(*shade)
    ctx.stroke()

    # 6. Badge (if active or error)
    if badge_color:
        cx, cy, r = 19.0, 19.0, 2.5
        # Dark separation ring
        ctx.arc(cx, cy, r + 0.9, 0, 2 * math.pi)
        ctx.set_source_rgba(0.12, 0.12, 0.12, 0.95)
        ctx.fill()

        # Status dot
        ctx.arc(cx, cy, r, 0, 2 * math.pi)
        ctx.set_source_rgba(*badge_color)
        ctx.fill()

        ctx.set_line_width(0.5)
        ctx.set_source_rgba(0.1, 0.1, 0.1, 0.6)
        ctx.stroke()

    out_path = ICONS_DIR / f"{filename}.png"
    surface.write_to_png(str(out_path))
    print(f"Generated {out_path.name}")


def main():
    ICONS_DIR.mkdir(parents=True, exist_ok=True)
    # Generate uziclaw icons
    draw_crab("uziclaw")
    draw_crab("uziclaw-symbolic")
    draw_crab("uziclaw-active", badge_color=(0.2, 0.85, 0.3, 1.0))
    draw_crab("uziclaw-error", badge_color=(0.9, 0.25, 0.2, 1.0))

    # Also generate telegram-bridge aliases for backwards compatibility
    draw_crab("telegram-bridge")
    draw_crab("telegram-bridge-symbolic")
    draw_crab("telegram-bridge-active", badge_color=(0.2, 0.85, 0.3, 1.0))
    draw_crab("telegram-bridge-error", badge_color=(0.9, 0.25, 0.2, 1.0))


if __name__ == "__main__":
    main()

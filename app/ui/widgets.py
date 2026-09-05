"""Small drawing helpers shared by the screens."""

from __future__ import annotations

from app.ui import theme


def text_width(draw, text: str, font) -> int:
    return int(draw.textlength(text, font=font))


def ellipsise(draw, text: str, font, max_width: int) -> str:
    if text_width(draw, text, font) <= max_width:
        return text
    while text and text_width(draw, text + "…", font) > max_width:
        text = text[:-1]
    return text + "…"


def centred(draw, y: int, text: str, font, fill, width: int = theme.SCREEN_WIDTH):
    draw.text(((width - text_width(draw, text, font)) // 2, y), text,
              font=font, fill=fill)


def panel(draw, box, fill=theme.SURFACE, outline=None, radius: int = 8):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline)


def signal_bars(draw, x: int, y: int, rssi, height: int = 12):
    """Four ascending bars; unfilled bars stay visible as faint outlines.

    Height and colour carry it; a digit beside them said the same thing
    twice and crowded a header that has four things in it already.
    """
    level = theme.signal_level(rssi)
    colour = theme.signal_colour(level)
    for index in range(4):
        bar_height = 3 + index * (height - 3) // 3
        left = x + index * 5
        top = y + height - bar_height
        draw.rectangle(
            [left, top, left + 3, y + height],
            fill=colour if index < level else theme.SURFACE_HI,
        )


def meter(draw, box, fraction: float, fill, track=theme.SURFACE_HI, radius=3):
    """Horizontal progress/level bar clamped to 0..1."""
    x0, y0, x1, y1 = box
    draw.rounded_rectangle(box, radius=radius, fill=track)
    fraction = max(0.0, min(1.0, fraction))
    if fraction <= 0:
        return
    end = x0 + int((x1 - x0) * fraction)
    if end - x0 >= radius:
        draw.rounded_rectangle([x0, y0, end, y1], radius=radius, fill=fill)


def vu_meter(draw, box, level: float, segments: int = 18):
    """Segmented level meter -- reads better than a bar while speaking."""
    x0, y0, x1, y1 = box
    gap = 2
    seg_width = ((x1 - x0) - gap * (segments - 1)) / segments
    lit = int(level * segments + 0.5)
    for index in range(segments):
        left = x0 + index * (seg_width + gap)
        if index < lit:
            ratio = index / max(1, segments - 1)
            colour = theme.OK if ratio < 0.6 else (
                theme.WARN if ratio < 0.85 else theme.DANGER
            )
        else:
            colour = theme.SURFACE_HI
        draw.rectangle([left, y0, left + seg_width, y1], fill=colour)


def hint(draw, y: int, lines: list):
    """Bottom-of-screen gesture legend."""
    small = theme.font(12)
    for index, line in enumerate(lines):
        centred(draw, y + index * 15, line, small, theme.TEXT_FAINT)

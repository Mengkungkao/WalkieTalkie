"""Small drawing helpers shared by the screens."""

from __future__ import annotations

from app.ui import theme


def text_width(draw, text: str, font) -> int:
    return int(draw.textlength(text, font=font))


def two_line_row(primary_font, secondary_font, pad: int = 4, gap: int = 1):
    """Panel height and both text offsets for a name-over-detail row.

    Measured from the fonts rather than assumed. The rows were hardcoded
    at 32 px tall while a 15 px name over an 11 px detail needs 37, so
    the second line was drawn across the bottom of its own selection
    frame. Deriving it means changing a font cannot quietly reintroduce
    that.

    Returns (height, primary_y, secondary_y), the y values being offsets
    from the top of the panel to pass straight to `draw.text`.
    """
    from PIL import Image, ImageDraw

    measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    top1, bottom1 = measure.textbbox((0, 0), "Ag", font=primary_font)[1::2]
    top2, bottom2 = measure.textbbox((0, 0), "Ag", font=secondary_font)[1::2]

    primary_y = pad - top1
    secondary_y = pad + (bottom1 - top1) + gap - top2
    height = secondary_y + bottom2 + pad
    return height, primary_y, secondary_y


def ellipsise(draw, text: str, font, max_width: int) -> str:
    if text_width(draw, text, font) <= max_width:
        return text
    while text and text_width(draw, text + "…", font) > max_width:
        text = text[:-1]
    return text + "…"


def centred(draw, y: int, text: str, font, fill, width: int = theme.SCREEN_WIDTH):
    draw.text(((width - text_width(draw, text, font)) // 2, y), text,
              font=font, fill=fill)


def panel(draw, box, fill=theme.SURFACE, outline=None, radius: int = 10):
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

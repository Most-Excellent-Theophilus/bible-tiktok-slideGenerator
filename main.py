"""
Generate a vertical (TikTok/Shorts-style) Bible-verse video from a
Zefania-format Bible JSON export.

Visual style:
  - Full-bleed background photo
  - A ribbon-style banner at the top with the passage reference
    (e.g. "GENESIS 1:1-31 (NLT)") — recalculated per chapter when
    spanning multiple chapters. The banner is drawn on its own fixed
    layer (like the watermark) so it stays in place during slide
    transitions instead of sliding with the verse text.
  - Verse text drawn line-by-line on white "highlighter" boxes
  - A yellow bracket-shaped PROGRESS BAR on the left of the text
    block: two end-ticks mark the start/end of the verse's on-screen
    time, and the vertical bar between them fills from top to bottom
    as that verse plays, so it doubles as a countdown of how much
    longer the verse will stay on screen.
  - A horizontal "push" slide transition between every slide
  - Background music that fades in, fades out, and loops (restarts)
    if it's shorter than the finished video

PERFORMANCE NOTE (read this if you're wondering why this version is
different from a "plain MoviePy CompositeVideoClip" implementation):

The naive approach stacks three layers (static background, a masked
text layer that slides, and a masked banner/watermark overlay) inside
a single CompositeVideoClip for the ENTIRE video duration. MoviePy then
has to re-run full alpha-compositing math for every single output frame
— even though, for the vast majority of frames, nothing is moving and
the composited result is byte-for-byte identical to the previous frame.
For a many-minute video at 30fps that's tens of thousands of wasted
composites, which is why that approach can take hours.

This version instead:
  1. Pre-renders each slide's fully-composited BASE frame exactly once
     with PIL (background + text/verse content baked into that slide's
     own transparent layer, banner baked into its own fixed layer per
     slide, watermark baked into a shared overlay). The progress bar
     is deliberately left OUT of this base frame, since — unlike the
     rest of the slide — it changes on every single frame for as long
     as the verse is showing.
  2. Every output frame still only pays for two cheap operations on
     top of that cached base: a raw buffer copy of the base frame, and
     pasting a small, tightly-cropped progress-bar image (just the
     bracket's own bounding box, not the full 1080x1920 canvas) at the
     right fill level for that instant. Because the pasted region is
     tiny, this is nowhere near as expensive as re-compositing the
     whole frame the naive approach would do.
  3. Real full-canvas alpha work still only happens during the short
     (~0.6s) push transitions between slides, where the verse text
     itself is actually moving. The banner and watermark are pasted at
     a fixed position during transitions too, so neither one slides —
     only the verse text/progress-bar layer does, and each outgoing/
     incoming slide's progress bar is drawn at its own correct fill
     level for that moment (an outgoing verse finishing near 100%, an
     incoming verse starting near 0%).
  4. Feeds MoviePy a lightweight custom VideoClip that just looks up
     (or, for transition frames, cheaply computes) the right frame for
     a given timestamp.

Note: this does not draw the TikTok logo itself (that's a trademarked
asset) — WATERMARK_TEXT below is a plain text placeholder you can set
to your own handle if you want something in that corner.

Command-line usage
-------------------
Run with no arguments to use the CONFIG values below, or override
book / chapter / chapter range (and optionally a verse range) from the
command line:

    python main.py book:Genesis,chapter:1
    python main.py book:1,chapter:1
    python main.py book:Genesis,chapter:1,verse_start:1,verse_end:10
    python main.py book:Genesis,chapter_start:1,chapter_end:5

- "book" can be a book name ("Genesis") or a book number ("1"), matched
  against the JSON's bname / bnumber.
- If chapter is given but no verse_start/verse_end, the ENTIRE chapter
  is used.
- If chapter_start and chapter_end are both given, the video spans
  every chapter in that inclusive range (in full — verse_start/verse_end
  are ignored in this mode since they wouldn't make sense across
  multiple chapters). The banner reference at the top of each verse
  slide updates automatically as the chapter changes.
- Recognized keys: book, chapter, chapter_start, chapter_end,
  verse_start, verse_end, translation

Expected JSON shape:
{
  "XMLBIBLE": {
    "BIBLEBOOK": [
      {
        "bname": "Genesis",
        "bnumber": "1",
        "CHAPTER": [
          {
            "cnumber": "1",
            "VERS": [
              {"vnumber": "1", "text": "In the beginning ..."},
              ...
            ]
          }
        ]
      }
    ]
  }
}
"""

import bisect
import json
import os
import re
import sys
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from moviepy import (
    VideoClip,
    AudioFileClip,
    afx,
)

# --------------------------------------------------
# CONFIG — edit these for each video you generate
# (any of book/chapter/chapter_start/chapter_end/verse_start/verse_end
#  can be overridden from the command line — see the module docstring
#  above)
# --------------------------------------------------

BIBLE_JSON_PATH = "bible.json"       # path to the Zefania-format JSON file
BACKGROUND_IMAGE_PATH = "zip.png"    # full-bleed photo behind every slide
AUDIO_PATH = "audio.mp3"             # background music, or None for silent video

BOOK_NAME = "Genesis"

# --- Single chapter mode (used when CHAPTER_START/CHAPTER_END are None) ---
CHAPTER_NUMBER = "2"
VERSE_START = 1
VERSE_END = 25                       # or None for the whole chapter

# --- Multi-chapter mode ---
# Set both to generate one video spanning multiple whole chapters of
# BOOK_NAME (e.g. CHAPTER_START="1", CHAPTER_END="5" does chapters 1-5
# back to back). Leave both as None to use CHAPTER_NUMBER above instead.
# In this mode VERSE_START/VERSE_END are ignored — every chapter in the
# range is used in full.
CHAPTER_START = None
CHAPTER_END = None

# Auto-built per chapter as "GENESIS 1:1-31 (NLT)" — override with your
# own fixed string if you want the same banner text on every slide
# regardless of chapter.
TRANSLATION_LABEL = "NLT"
BANNER_TEXT = None

# Set to None to skip these slides entirely — output will be verse slides only.
TITLE_SLIDE_TEXT = None              # e.g. "Welcome to our video." or None to skip
OUTRO_SLIDE_TEXT = "Thanks For Watching. Like & Share !!!"            # e.g. "Thanks For Watching." or None to skip

# The watermark is drawn once onto a transparent overlay that sits on
# top of every frame, so it stays fixed in place while slides push/
# slide behind it. The banner works the same way now: it's drawn onto
# its own fixed-position layer per slide (rather than being baked into
# the sliding verse-text layer), so it also stays put during
# transitions — it just swaps to the new chapter's banner text at the
# start of the transition instead of sliding across the screen.
WATERMARK_TEXT = "@zipatsokalembedwe"  # e.g. "@yourhandle" or None to skip

WIDTH = 1080
HEIGHT = 1920
FPS = 30


GENERATE_COVER = True          # set False to skip generating a cover image

# If True, the cover art (title + coral banner over the background) is
# inserted as an actual first slide of the rendered video, shown for
# COVER_SLIDE_DURATION seconds before the normal slides begin — so
# whoever's watching sees it immediately on playback, not just as a
# separate thumbnail file. cover.jpg is still saved separately either
# way (grabbed from that same opening slide) so you also have a still
# image to pick as the TikTok thumbnail.
COVER_AS_FIRST_SLIDE = True
COVER_SLIDE_DURATION = 0.02   # seconds the cover slide is shown at the start of the video

COVER_TIME = 0.15              # seconds into the cover slide to grab the still cover.jpg frame from
                                # (kept just after 0 so a mid-transition frame isn't used)

# The cover gets its OWN look, separate from the in-video banner/verse
# style — a bold white-with-black-outline title near the top, plus a
# tilted, semi-transparent coral banner underneath it (like a stamp).
# This is drawn on top of the background ONLY for the cover slide /
# cover.jpg; it never appears on the regular verse slides.
COVER_TITLE_TEXT = None              # e.g. "Genesis 1 vs 26-31"; None = auto-built from the slides used
COVER_SUBTITLE_TEXT = f"God's Word  ({TRANSLATION_LABEL})"   # tilted coral banner text; None to skip the banner
COVER_TITLE_FONT_SIZE = 104
COVER_SUBTITLE_FONT_SIZE = 116
COVER_TITLE_STROKE_WIDTH = 7
COVER_BANNER_COLOR = (224, 122, 63, 205)   # coral, semi-transparent (R, G, B, A)
COVER_BANNER_TEXT_COLOR = "#1a1208"
COVER_BANNER_ROTATION = -6                  # degrees, tilts it like a stamp

WORDS_PER_SECOND = 2
MIN_DURATION = 4
MAX_DURATION = 40

TRANSITION_DURATION = 1.5           # seconds; horizontal slide/push between slides

FONT_SIZE = 58
BANNER_FONT_SIZE = 56
FALLBACK_FONT_SIZE = 70              # used for title/outro slides
PADDING = 90
LINE_SPACING = 18
HIGHLIGHT_PAD_X = 18
HIGHLIGHT_PAD_Y = 10

BG_OVERLAY_OPACITY = 60              # 0-255, subtle dark overlay for contrast
HIGHLIGHT_BG_COLOR = (255, 255, 255, 235)
HIGHLIGHT_TEXT_COLOR = "#111111"

# The left-hand "bracket" is a progress bar: BRACKET_COLOR is the
# fully-filled (already-elapsed) color, BRACKET_TRACK_ALPHA controls
# how faint the not-yet-elapsed remainder of the bar looks.
BRACKET_COLOR = "#CCCCCCC7"
BRACKET_TRACK_ALPHA = 70             # 0-255, opacity of the unfilled portion of the bar
BRACKET_THICKNESS = 15
BRACKET_TICK = 0
BRACKET_GAP = 34                     # space between bracket and text block

BANNER_FILL = "#F2E8D5"
BANNER_BORDER = "#3B1F1F"
BANNER_TEXT_COLOR = "#3B1F1F"

AUDIO_FADE_DURATION = 7.5            # seconds, applied at start and end of the video

# --------------------------------------------------
# CLI OVERRIDES
# --------------------------------------------------


def parse_cli_args(argv):
    parsed = {}
    if len(argv) <= 1:
        return parsed

    raw = " ".join(argv[1:]).strip().lstrip("?")
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        key, value = pair.split(":", 1)
        parsed[key.strip().lower()] = value.strip()

    return parsed


_cli_args = parse_cli_args(sys.argv)

if "book" in _cli_args:
    BOOK_NAME = _cli_args["book"]

if "chapter" in _cli_args:
    CHAPTER_NUMBER = _cli_args["chapter"]
    if "verse_start" not in _cli_args and "verse_end" not in _cli_args:
        VERSE_START = None
        VERSE_END = None

if "chapter_start" in _cli_args:
    CHAPTER_START = _cli_args["chapter_start"]

if "chapter_end" in _cli_args:
    CHAPTER_END = _cli_args["chapter_end"]

if "verse_start" in _cli_args:
    VERSE_START = int(_cli_args["verse_start"])

if "verse_end" in _cli_args:
    VERSE_END = int(_cli_args["verse_end"])

if "translation" in _cli_args:
    TRANSLATION_LABEL = _cli_args["translation"]

MULTI_CHAPTER_MODE = CHAPTER_START is not None and CHAPTER_END is not None

OUTPUT_DIR = f"output/book-{BOOK_NAME}-{CHAPTER_NUMBER}"
SLIDES_DIR = os.path.join(OUTPUT_DIR, "slides")
VIDEO_PATH = os.path.join(OUTPUT_DIR, "bible_shorts_video.mp4")

# --------------------------------------------------
# COVER PHOTO (TikTok lets you pick a custom cover/thumbnail on upload —
# this generates a matching still image you can select there)
# --------------------------------------------------
COVER_PATH = os.path.join(OUTPUT_DIR, "cover.jpg")
# --------------------------------------------------
# FONT LOADING (cross-platform, with a safe fallback)
# --------------------------------------------------

CANDIDATE_FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
]


def load_font(size):
    for path in CANDIDATE_FONT_PATHS:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    print("Warning: no system font found, falling back to PIL default font.")
    return ImageFont.load_default(size=size)


font = load_font(FONT_SIZE)
banner_font = load_font(BANNER_FONT_SIZE)
fallback_font = load_font(FALLBACK_FONT_SIZE)
watermark_font = load_font(28)
cover_title_font = load_font(COVER_TITLE_FONT_SIZE)
cover_subtitle_font = load_font(COVER_SUBTITLE_FONT_SIZE)

# --------------------------------------------------
# LOAD BIBLE JSON AND EXTRACT VERSES
# --------------------------------------------------


def load_bible(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_book(bible, book_name_or_number):
    books = bible["XMLBIBLE"]["BIBLEBOOK"]
    query = str(book_name_or_number).strip()

    for book in books:
        if book.get("bname", "").strip().lower() == query.lower():
            return book

    if query.isdigit():
        for book in books:
            if str(book.get("bnumber", "")).strip() == query:
                return book

    raise ValueError(f"Book '{book_name_or_number}' not found in JSON.")


def get_chapter(book, chapter_number):
    for chapter in book["CHAPTER"]:
        if str(chapter.get("cnumber")) == str(chapter_number):
            return chapter
    raise ValueError(
        f"Chapter '{chapter_number}' not found in book '{book.get('bname')}'."
    )


def extract_verses(chapter, verse_start=None, verse_end=None):
    verses = []
    for vers in chapter["VERS"]:
        vnum = int(vers["vnumber"])
        if verse_start is not None and vnum < verse_start:
            continue
        if verse_end is not None and vnum > verse_end:
            continue
        verses.append((vers["vnumber"], vers["text"]))
    if not verses:
        raise ValueError("No verses found in the requested range.")
    return verses


def chapter_banner_text(book, chapter, verses):
    if BANNER_TEXT:
        return BANNER_TEXT
    v_first = verses[0][0]
    v_last = verses[-1][0]
    verse_range = v_first if v_first == v_last else f"{v_first}-{v_last}"
    return (
        f"{book['bname'].upper()} {chapter['cnumber']} vs {verse_range} "
        f"({TRANSLATION_LABEL})"
    )


def build_slides_from_config():
    """
    Returns a list of slide dicts:
      {"kind": "title" | "outro", "text": ...}
      {"kind": "verse", "text": ..., "banner_text": ..., "book": ...,
       "chapter": ..., "verse": ...}

    As a side effect, updates the module-level BANNER_TEXT global to
    the banner text of the last chapter processed, so callers (e.g.
    main(), for naming the output video file) can read it back after
    this function returns without having to re-derive it themselves.
    """
    global BANNER_TEXT

    bible = load_bible(BIBLE_JSON_PATH)
    book = get_book(bible, BOOK_NAME)

    if MULTI_CHAPTER_MODE:
        start = int(CHAPTER_START)
        end = int(CHAPTER_END)
        if end < start:
            start, end = end, start
        chapter_numbers = [str(n) for n in range(start, end + 1)]
    else:
        chapter_numbers = [CHAPTER_NUMBER]

    slides = []

    if TITLE_SLIDE_TEXT:
        slides.append({"kind": "title", "text": TITLE_SLIDE_TEXT})

    for i, cnum in enumerate(chapter_numbers):
        chapter = get_chapter(book, cnum)

        if MULTI_CHAPTER_MODE:
            # Whole chapter every time — per-chapter verse ranges don't
            # make sense when spanning several chapters.
            verses = extract_verses(chapter)
        else:
            verses = extract_verses(chapter, VERSE_START, VERSE_END)

        banner_text = chapter_banner_text(book, chapter, verses)
        BANNER_TEXT = banner_text

        for vnumber, text in verses:
            slides.append({
                "kind": "verse",
                "text": f"{vnumber}. {text}",
                "banner_text": banner_text,
                "book": book["bname"],
                "chapter": chapter["cnumber"],
                "verse": vnumber,
            })

    if OUTRO_SLIDE_TEXT:
        slides.append({"kind": "outro", "text": OUTRO_SLIDE_TEXT})

    return slides


# --------------------------------------------------
# SLIDE FILE NAMING
# --------------------------------------------------
# Each slide's PNG (and the debug frame it produces) is named after
# what it actually is, so you can identify a slide from its filename
# alone instead of a bare running number — e.g.
#   003_Genesis_2_3.png   -> Genesis 2:3
#   000_title.png
#   026_outro.png


def slugify(text):
    text = re.sub(r"\s+", "_", text.strip())
    return re.sub(r"[^A-Za-z0-9_\-]", "", text)


def slide_filename(index, slide):
    if slide["kind"] == "verse":
        book_slug = slugify(slide["book"])
        return f"{index:03d}_{book_slug}_{slide['chapter']}_{slide['verse']}.png"
    return f"{index:03d}_{slide['kind']}.png"


# --------------------------------------------------
# DURATION
# --------------------------------------------------


def calculate_duration(text):
    words = len(text.split())
    duration = words / WORDS_PER_SECOND
    return max(MIN_DURATION, min(duration, MAX_DURATION))


# --------------------------------------------------
# TEXT WRAPPING
# --------------------------------------------------


def wrap_text(draw, text, font_obj, max_width):
    words = text.split()
    lines = []
    current_line = ""

    for word in words:
        test_line = (current_line + " " + word).strip()
        bbox = draw.textbbox((0, 0), test_line, font=font_obj)
        width = bbox[2] - bbox[0]

        if width <= max_width:
            current_line = test_line
        else:
            if current_line:
                lines.append(current_line)
            current_line = word

    if current_line:
        lines.append(current_line)

    return lines


# --------------------------------------------------
# BACKGROUND IMAGE (cover-crop to fill the frame)
# --------------------------------------------------


def load_background(path, width, height):
    img = Image.open(path).convert("RGB")
    img_ratio = img.width / img.height
    target_ratio = width / height

    if img_ratio > target_ratio:
        new_height = height
        new_width = int(img_ratio * new_height)
    else:
        new_width = width
        new_height = int(new_width / img_ratio)

    img = img.resize((new_width, new_height))
    left = (new_width - width) // 2
    top = (new_height - height) // 2
    return img.crop((left, top, left + width, top + height))


def make_base_image():
    if BACKGROUND_IMAGE_PATH and os.path.exists(BACKGROUND_IMAGE_PATH):
        base = load_background(BACKGROUND_IMAGE_PATH, WIDTH, HEIGHT)
    else:
        base = Image.new("RGB", (WIDTH, HEIGHT), "#111111")

    if BG_OVERLAY_OPACITY:
        overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, BG_OVERLAY_OPACITY))
        base = Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB")

    return base


# --------------------------------------------------
# DRAWING HELPERS
# --------------------------------------------------


def draw_banner(image, text):
    draw = ImageDraw.Draw(image)
    bbox = draw.textbbox((0, 0), text, font=banner_font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    box_w = text_w + 90
    box_h = text_h + 60
    box_x = (WIDTH - box_w) / 2
    box_y = 130

    draw.rounded_rectangle(
        [box_x, box_y, box_x + box_w, box_y + box_h],
        radius=2,
        fill=BANNER_FILL,
        outline=BANNER_BORDER,
        width=5,
    )

    text_x = box_x + (box_w - text_w) / 2 - bbox[0]
    text_y = box_y + (box_h - text_h) / 2 - bbox[1]
    draw.text((text_x, text_y), text, font=banner_font, fill=BANNER_TEXT_COLOR)

    return box_y + box_h


def measure_banner_bottom(text):
    scratch = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(scratch)
    bbox = draw.textbbox((0, 0), text, font=banner_font)
    text_h = bbox[3] - bbox[1]
    box_h = text_h + 60
    box_y = 130
    return box_y + box_h


def _hex_to_rgb(hex_color):
    hex_color = hex_color.lstrip("#")
    return tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))


BRACKET_RGB = _hex_to_rgb(BRACKET_COLOR)

# How far in from the left edge of the small progress-bar image the
# vertical bar itself sits, so a BRACKET_THICKNESS-wide stroke centered
# on that x doesn't get clipped by the edge of the (tightly-cropped)
# image it's drawn on.
_BRACKET_MARGIN = BRACKET_THICKNESS // 2 + 1


def render_bracket_image(y_top, y_bottom, progress):
    """Renders the bracket-shaped progress bar for one verse slide as
    a small, tightly-cropped RGBA image — NOT a full 1080x1920 frame —
    so pasting it onto a frame every tick only costs compositing over
    this small bounding box rather than the whole canvas.

    The vertical bar fills from y_top towards y_bottom as `progress`
    (0.0-1.0) increases, so it shows at a glance how much of this
    verse's on-screen time has elapsed. The top tick (start of the
    verse) is always drawn bright since it's already been "reached"
    the moment the verse appears; the bottom tick (end of the verse)
    only lights up once progress reaches 1.0.
    """
    progress = max(0.0, min(1.0, progress))
    height = max(1, int(round(y_bottom - y_top)))
    width = _BRACKET_MARGIN + BRACKET_TICK + BRACKET_THICKNESS

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    track_color = (*BRACKET_RGB, BRACKET_TRACK_ALPHA)
    fill_color = (*BRACKET_RGB, 255)
    x = _BRACKET_MARGIN

    # Unfilled track for the whole bar, then the elapsed portion drawn
    # on top of it in the bright color.
    draw.line([(x, 0), (x, height)], fill=track_color, width=BRACKET_THICKNESS)
    fill_h = int(round(height * progress))
    if fill_h > 0:
        draw.line([(x, 0), (x, fill_h)], fill=fill_color, width=BRACKET_THICKNESS)

    draw.line([(x, 0), (x + BRACKET_TICK, 0)], fill=fill_color, width=BRACKET_THICKNESS)
    bottom_tick_color = fill_color if progress >= 0.999 else track_color
    draw.line(
        [(x, height - 1), (x + BRACKET_TICK, height - 1)],
        fill=bottom_tick_color,
        width=BRACKET_THICKNESS,
    )

    return img


def draw_watermark(image):
    if not WATERMARK_TEXT:
        return
    draw = ImageDraw.Draw(image)
    bbox = draw.textbbox((0, 0), WATERMARK_TEXT, font=watermark_font)
    text_w = bbox[2] - bbox[0]
    x = WIDTH - PADDING - text_w
    y = HEIGHT - 90
    draw.text((x, y), WATERMARK_TEXT, font=watermark_font, fill="white")


def create_overlay_image():
    """Shared overlay drawn once and reused on every frame. Only the
    watermark lives here — the banner is NOT part of this shared
    overlay because it changes per chapter in multi-chapter mode, so
    it's baked into its own fixed-position layer per slide instead
    (see create_verse_banner_image below)."""
    overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw_watermark(overlay)
    path = os.path.join(SLIDES_DIR, "overlay.png")
    overlay.save(path)
    return path


# --------------------------------------------------
# SLIDE CREATION (each slide is an RGBA PNG, transparent everywhere
# except its own text/verse/banner content, saved to disk for
# debugging/inspection)
# --------------------------------------------------


def create_verse_banner_image(banner_text):
    """The banner, on its own transparent layer, fixed in position —
    just like the watermark overlay. This layer is never pasted at an
    offset during transitions, so it never slides; it just swaps to
    the next slide's banner text (usually identical, except right at
    a chapter boundary in multi-chapter mode) the moment a transition
    begins."""
    image = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw_banner(image, banner_text)
    return image


def create_verse_content_image(slide, filename):
    """The verse text + highlighter boxes only (no banner, no
    progress bar). This is the layer that actually slides during push
    transitions. Also returns the (y_top, y_bottom) bounds of the text
    block, in frame coordinates, so the caller can position that
    verse's progress bar — the bar itself is rendered separately, per
    frame, since (unlike everything else on this layer) it changes
    continuously for as long as the verse is on screen."""
    text = slide["text"]
    banner_bottom = measure_banner_bottom(slide["banner_text"])

    image = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    text_left = PADDING + BRACKET_TICK + BRACKET_GAP
    max_width = WIDTH - text_left - PADDING

    lines = wrap_text(draw, text, font, max_width)

    line_metrics = []
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        line_metrics.append((line, bbox))

    line_heights = [bbox[3] - bbox[1] for _, bbox in line_metrics]
    box_heights = [h + HIGHLIGHT_PAD_Y * 2 for h in line_heights]
    total_height = sum(box_heights) + LINE_SPACING * (len(lines) - 1)

    available_top = banner_bottom + 60
    available_bottom = HEIGHT - 220
    y = available_top + (available_bottom - available_top - total_height) / 2

    block_top = y
    block_bottom = y + total_height

    for (line, bbox), box_h in zip(line_metrics, box_heights):
        text_w = bbox[2] - bbox[0]
        box_w = text_w + HIGHLIGHT_PAD_X * 2

        draw.rectangle(
            [text_left, y, text_left + box_w, y + box_h],
            fill=HIGHLIGHT_BG_COLOR[:3],
        )

        text_y = y + HIGHLIGHT_PAD_Y - bbox[1]
        draw.text(
            (text_left + HIGHLIGHT_PAD_X, text_y),
            line,
            font=font,
            fill=HIGHLIGHT_TEXT_COLOR,
        )

        y += box_h + LINE_SPACING

    path = os.path.join(SLIDES_DIR, filename)
    image.save(path)
    return path, (block_top, block_bottom)


def create_plain_slide(text, filename):
    image = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    max_width = WIDTH - (PADDING * 2)
    lines = wrap_text(draw, text, fallback_font, max_width)

    line_heights = []
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=fallback_font)
        line_heights.append(bbox[3] - bbox[1])

    total_height = sum(line_heights) + LINE_SPACING * (len(lines) - 1)
    y = (HEIGHT - total_height) / 2

    for line, line_height in zip(lines, line_heights):
        bbox = draw.textbbox((0, 0), line, font=fallback_font)
        text_width = bbox[2] - bbox[0]
        x = (WIDTH - text_width) / 2
        draw.text((x, y), line, font=fallback_font, fill="white")
        y += line_height + LINE_SPACING

    path = os.path.join(SLIDES_DIR, filename)
    image.save(path)
    return path


# --------------------------------------------------
# COVER OVERLAY (bold outlined title + tilted coral banner)
# --------------------------------------------------
# Used to build the opening cover slide of the video (when
# COVER_AS_FIRST_SLIDE is True) and/or the standalone cover.jpg
# thumbnail. It intentionally does NOT reuse draw_banner()/BANNER_FILL
# etc. above, since the cover is meant to look like a punchy thumbnail
# (outlined title text, no box; a tilted semi-transparent stamp-style
# banner) rather than the in-video ribbon banner used on verse slides.


def cover_title_text_for_slides(slides):
    """Builds a 'Genesis 1 vs 26-31' style title (title-case book name,
    no translation label) from the verse slides actually used in this
    video, for the auto-generated cover — used when COVER_TITLE_TEXT
    is left as None."""
    verse_slides = [s for s in slides if s["kind"] == "verse"]
    if not verse_slides:
        return None

    first, last = verse_slides[0], verse_slides[-1]
    v_first, v_last = first["verse"], last["verse"]
    verse_range = v_first if v_first == v_last else f"{v_first}-{v_last}"

    if first["chapter"] == last["chapter"]:
        return f"{first['book']} {first['chapter']} vs {verse_range}"
    return f"{first['book']} {first['chapter']}-{last['chapter']}"


def draw_outlined_text_centered(draw, text, font_obj, center_x, top_y,
                                 fill="white", stroke_fill="black",
                                 stroke_width=COVER_TITLE_STROKE_WIDTH):
    """Draws one line of bold white text with a black outline (PIL's
    stroke_width/stroke_fill), horizontally centered on center_x with
    its top at top_y. Returns the y just below the drawn line."""
    bbox = draw.textbbox((0, 0), text, font=font_obj, stroke_width=stroke_width)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    x = center_x - text_w / 2 - bbox[0]
    y = top_y - bbox[1]
    draw.text(
        (x, y), text, font=font_obj, fill=fill,
        stroke_width=stroke_width, stroke_fill=stroke_fill,
    )
    return top_y + text_h


def create_rotated_banner(text, font_obj, fill_rgba, text_color, rotation_deg,
                           pad_x=70, pad_y=40):
    """Builds a small solid-color rectangle with centered text baked
    in, then rotates the whole thing (expanding the canvas so nothing
    gets clipped) so it can be pasted onto the cover like a tilted
    stamp/ribbon."""
    scratch = Image.new("RGBA", (10, 10), (0, 0, 0, 0))
    scratch_draw = ImageDraw.Draw(scratch)
    bbox = scratch_draw.textbbox((0, 0), text, font=font_obj)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    box_w = text_w + pad_x * 2
    box_h = text_h + pad_y * 2

    banner = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    banner_draw = ImageDraw.Draw(banner)
    banner_draw.rectangle([0, 0, box_w, box_h], fill=fill_rgba)

    text_x = pad_x - bbox[0]
    text_y = pad_y - bbox[1]
    banner_draw.text((text_x, text_y), text, font=font_obj, fill=text_color)

    return banner.rotate(rotation_deg, expand=True, resample=Image.BICUBIC)


def create_cover_overlay(title_text, subtitle_text):
    """Builds the cover overlay: an outlined title near the top plus
    (optionally) a tilted, semi-transparent coral banner beneath it —
    matching the reference thumbnail style. Returned as an RGBA image
    the same size as the frame, transparent everywhere except the
    title/banner content, so it can be used either as an actual video
    slide's content layer (see COVER_AS_FIRST_SLIDE) or composited
    onto a single grabbed frame for cover.jpg."""
    overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    if title_text:
        max_width = WIDTH - PADDING * 2
        lines = wrap_text(draw, title_text, cover_title_font, max_width)
        y = 420
        for line in lines:
            y = draw_outlined_text_centered(draw, line, cover_title_font, WIDTH / 2, y)
            y += 14

    if subtitle_text:
        banner = create_rotated_banner(
            subtitle_text,
            cover_subtitle_font,
            COVER_BANNER_COLOR,
            COVER_BANNER_TEXT_COLOR,
            COVER_BANNER_ROTATION,
        )
        bx = int((WIDTH - banner.width) / 2)
        by = 640
        overlay.alpha_composite(banner, (bx, by))

    return overlay


# --------------------------------------------------
# FAST FRAME ENGINE
# --------------------------------------------------
# Instead of stacking three MoviePy clips (background / sliding text /
# fixed overlay) and letting MoviePy re-composite all of them for every
# output frame, we:
#
#   1. Render each slide's BASE frame — background + fixed banner
#      layer + sliding verse-content layer + shared watermark overlay
#      — exactly once. The progress bar is NOT part of this base
#      frame; it's the one thing that legitimately changes every
#      frame while a verse is showing.
#   2. For every frame, take a cheap raw-buffer copy of the relevant
#      base frame and paste that verse's progress bar (a small,
#      tightly-cropped image, not a full-canvas composite) at the
#      fill level matching how far through the verse's total on-screen
#      time we currently are.
#   3. Do real per-frame alpha work for the full canvas only during
#      the short push-transition window between two consecutive
#      slides. During that window the banner and watermark stay
#      pasted at a fixed offset (0, 0) and only the verse-text content
#      (plus each slide's own progress bar, at its own offset and its
#      own fill level) is pasted at a sliding x offset.
#   4. Build a single lightweight VideoClip whose frame_function just
#      looks up / cheaply computes the right frame for a timestamp.


def build_fast_video(background_img, content_images, banner_images,
                      bracket_bounds, overlay_img, durations):
    n = len(content_images)
    transition = TRANSITION_DURATION
    if n > 1:
        transition = max(0.0, min(transition, min(durations) / 2))
    else:
        transition = 0.0

    def compose_base(idx):
        """Everything that's fixed for the entire time slide idx is on
        screen: background, banner, verse content, watermark. The
        progress bar is deliberately left out — see paste_bracket."""
        frame = background_img.copy()
        if banner_images[idx] is not None:
            frame.paste(banner_images[idx], (0, 0), banner_images[idx])
        frame.paste(content_images[idx], (0, 0), content_images[idx])
        if overlay_img is not None:
            frame.paste(overlay_img, (0, 0), overlay_img)
        return frame

    base_frames = [compose_base(i) for i in range(n)]

    def paste_bracket(frame, idx, progress, x_offset=0):
        bounds = bracket_bounds[idx]
        if bounds is None:
            return
        y_top, y_bottom = bounds
        bar = render_bracket_image(y_top, y_bottom, progress)
        frame.paste(bar, (int(round(PADDING + x_offset - _BRACKET_MARGIN)), int(round(y_top))), bar)

    # slide_start[i]: absolute time this slide's on-screen window
    # begins (the start of its incoming transition, or the start of
    # its static portion if it has no incoming transition). Needed to
    # compute this slide's progress (elapsed / durations[i]) the same
    # way whether we're currently in its transition-in, static, or
    # transition-out phase.
    slide_start = [0.0] * n

    def compose_static(idx, elapsed_in_slide):
        frame = base_frames[idx].copy()
        progress = elapsed_in_slide / durations[idx] if durations[idx] > 0 else 1.0
        paste_bracket(frame, idx, progress)
        return np.array(frame)

    def compose_transition(idx_out, idx_in, p, t_abs):
        x_out = int(round(-WIDTH * p))
        x_in = int(round(WIDTH * (1 - p)))
        frame = background_img.copy()

        # Banner stays fixed at (0, 0) the whole transition — it does
        # not slide with the content. It switches to the incoming
        # slide's banner right away (same text in almost every case;
        # only differs at a chapter boundary in multi-chapter mode).
        banner_img = banner_images[idx_in] if banner_images[idx_in] is not None else banner_images[idx_out]
        if banner_img is not None:
            frame.paste(banner_img, (0, 0), banner_img)

        frame.paste(content_images[idx_out], (x_out, 0), content_images[idx_out])
        frame.paste(content_images[idx_in], (x_in, 0), content_images[idx_in])

        progress_out = (
            min(1.0, (t_abs - slide_start[idx_out]) / durations[idx_out])
            if durations[idx_out] > 0 else 1.0
        )
        progress_in = (
            max(0.0, (t_abs - slide_start[idx_in]) / durations[idx_in])
            if durations[idx_in] > 0 else 0.0
        )
        paste_bracket(frame, idx_out, progress_out, x_offset=x_out)
        paste_bracket(frame, idx_in, progress_in, x_offset=x_in)

        if overlay_img is not None:
            frame.paste(overlay_img, (0, 0), overlay_img)
        return np.array(frame)

    segments = []
    t = 0.0
    for i in range(n):
        left_trans = transition if i > 0 else 0.0
        right_trans = transition if i < n - 1 else 0.0
        slide_start[i] = t - left_trans
        static_len = durations[i] - left_trans - right_trans
        if static_len > 0:
            segments.append((
                t, t + static_len,
                lambda t_local, idx=i, lt=left_trans: compose_static(idx, t_local + lt)
            ))
            t += static_len
        if i < n - 1 and transition > 0:
            idx_out, idx_in = i, i + 1
            seg_start = t
            segments.append((
                t,
                t + transition,
                lambda t_local, a=idx_out, b=idx_in, s0=seg_start: compose_transition(
                    a, b, max(0.0, min(1.0, t_local / transition)), s0 + t_local
                ),
            ))
            t += transition

    total_duration = t
    starts = [seg[0] for seg in segments]

    def make_frame(time_s):
        idx = bisect.bisect_right(starts, time_s) - 1
        idx = max(0, min(idx, len(segments) - 1))
        seg_start, seg_end, fn = segments[idx]
        return fn(time_s - seg_start)

    return make_frame, total_duration, base_frames


# --------------------------------------------------
# AUDIO
# --------------------------------------------------


def build_audio(video_duration):
    if not AUDIO_PATH or not os.path.exists(AUDIO_PATH):
        return None

    audio = AudioFileClip(AUDIO_PATH)

    effects = [afx.AudioLoop(duration=video_duration)]
    if AUDIO_FADE_DURATION:
        effects.append(afx.AudioFadeIn(AUDIO_FADE_DURATION))
        effects.append(afx.AudioFadeOut(AUDIO_FADE_DURATION))

    return audio.with_effects(effects)


# --------------------------------------------------
# OUTPUT VIDEO FILE NAMING
# --------------------------------------------------
# Named after the banner text (e.g. "GENESIS 2:1-25 (NLT)" ->
# "GENESIS_2_1-25_NLT.mp4") so the file itself is identifiable without
# opening it. Falls back to a generic name if no banner text was ever
# set (e.g. BIBLE_JSON_PATH produced zero slides, which build_slides_
# from_config would already have raised on).


def video_filename_from_banner():
    if BANNER_TEXT:
        return f"{slugify(BANNER_TEXT)}.mp4"
    return "bible_shorts_video.mp4"


# --------------------------------------------------
# MAIN
# --------------------------------------------------


def main():
    os.makedirs(SLIDES_DIR, exist_ok=True)

    t0 = time.time()
    slides = build_slides_from_config()

    if MULTI_CHAPTER_MODE:
        print(f"Multi-chapter mode: {BOOK_NAME} chapters {CHAPTER_START}-{CHAPTER_END}")

    content_images = []
    banner_images = []
    bracket_bounds = []
    banner_image_cache = {}
    durations = []
    for i, slide in enumerate(slides):
        filename = slide_filename(i, slide)
        label = (
            f"{slide['book']} {slide['chapter']}:{slide['verse']}"
            if slide["kind"] == "verse"
            else slide["kind"]
        )
        print(f"Creating slide {i + 1}/{len(slides)} ({label}) -> {filename}")

        if slide["kind"] == "verse":
            path, bracket_bound = create_verse_content_image(slide, filename)

            banner_text = slide["banner_text"]
            banner_img = banner_image_cache.get(banner_text)
            if banner_img is None:
                banner_img = create_verse_banner_image(banner_text)
                banner_image_cache[banner_text] = banner_img
        else:
            path = create_plain_slide(slide["text"], filename)
            banner_img = None
            bracket_bound = None

        content_images.append(Image.open(path).convert("RGBA"))
        banner_images.append(banner_img)
        bracket_bounds.append(bracket_bound)
        duration = calculate_duration(slide["text"])
        print(f"  Duration: {duration:.2f}s")
        durations.append(duration)

    print(f"[TIMER] slide generation: {time.time() - t0:.1f}s for {len(slides)} slides")

    # Cover slide: same title/banner overlay used for cover.jpg, but
    # now also insertable as an actual slide 0 in the rendered video
    # (see COVER_AS_FIRST_SLIDE) so it plays at the very start instead
    # of only existing as a separate thumbnail file.
    cover_title = COVER_TITLE_TEXT or cover_title_text_for_slides(slides)
    cover_overlay_img = None
    if GENERATE_COVER or COVER_AS_FIRST_SLIDE:
        if cover_title or COVER_SUBTITLE_TEXT:
            cover_overlay_img = create_cover_overlay(cover_title, COVER_SUBTITLE_TEXT)
            cover_debug_path = os.path.join(SLIDES_DIR, "000_cover.png")
            cover_overlay_img.save(cover_debug_path)

    if COVER_AS_FIRST_SLIDE and cover_overlay_img is not None:
        content_images.insert(0, cover_overlay_img)
        banner_images.insert(0, None)
        bracket_bounds.insert(0, None)
        durations.insert(0, COVER_SLIDE_DURATION)
        print(f"Inserting cover as slide 1/{len(content_images)} "
              f"(shown for {COVER_SLIDE_DURATION:.2f}s)")

    background_img = make_base_image()
    background_path = os.path.join(SLIDES_DIR, "background.png")
    background_img.save(background_path)

    overlay_img = None
    if WATERMARK_TEXT:
        overlay_path = create_overlay_image()
        overlay_img = Image.open(overlay_path).convert("RGBA")

    t1 = time.time()
    make_frame, total_duration, base_frames = build_fast_video(
        background_img, content_images, banner_images, bracket_bounds,
        overlay_img, durations,
    )
    print(f"[TIMER] frame precompute: {time.time() - t1:.1f}s")
    print(f"[TIMER] total video duration: {total_duration:.1f}s "
          f"-> ~{int(total_duration * FPS)} frames at {FPS}fps")

    if GENERATE_COVER:
        if COVER_AS_FIRST_SLIDE and cover_overlay_img is not None:
            # The cover slide IS slide 0 now, so grab the still image
            # straight from its own opening frames.
            cover_slide_duration = durations[0]
            cover_t = max(0.0, min(COVER_TIME, max(cover_slide_duration - 1 / FPS, 0.0)))
        else:
            cover_t = max(0.0, min(COVER_TIME, max(total_duration - 1 / FPS, 0.0)))
        cover_arr = make_frame(cover_t)
        Image.fromarray(cover_arr).convert("RGB").save(COVER_PATH, quality=95)
        print(f"Cover photo: {COVER_PATH}")

    video = VideoClip(frame_function=make_frame, duration=total_duration).with_fps(FPS)

    audio = build_audio(total_duration)
    if audio is not None:
        video = video.with_audio(audio)
    else:
        print("No audio file found — rendering silent video.")

    print("Rendering video...")
    t2 = time.time()

    video_path = os.path.join('vids', video_filename_from_banner())
    video.write_videofile(
        video_path,
        fps=FPS,
        codec="libx264",
        audio_codec="aac" if audio is not None else None,
        audio=audio is not None,
        preset="veryfast",
        threads=os.cpu_count(),
        ffmpeg_params=["-crf", "23"],
    )
    print(f"[TIMER] ffmpeg render: {time.time() - t2:.1f}s")

    print()
    print("Done!")
    print(f"Video: {video_path}")
    print(f"Screenshots: {SLIDES_DIR}")
    if GENERATE_COVER:
        print(f"Cover photo: {COVER_PATH}")


if __name__ == "__main__":
    main()
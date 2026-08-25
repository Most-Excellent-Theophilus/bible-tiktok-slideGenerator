"""
Generate a vertical (TikTok/Shorts-style) Bible-verse video from a
Zefania-format Bible JSON export.

Visual style:
  - Full-bleed background photo
  - A ribbon-style banner at the top with the passage reference
    (e.g. "GENESIS 1:1-31 (NLT)")
  - Verse text drawn line-by-line on white "highlighter" boxes
  - A yellow bracket on the left of the text block
  - A horizontal "push" slide transition between every slide
  - Background music that fades in, fades out, and loops (restarts)
    if it's shorter than the finished video

Note: this does not draw the TikTok logo itself (that's a trademarked
asset) — WATERMARK_TEXT below is a plain text placeholder you can set
to your own handle if you want something in that corner.

Command-line usage
-------------------
Run with no arguments to use the CONFIG values below, or override
book / chapter (and optionally a verse range) from the command line:

    python main.py book:Genesis,chapter:1
    python main.py book:1,chapter:1
    python main.py book:Genesis,chapter:1,verse_start:1,verse_end:10

- "book" can be a book name ("Genesis") or a book number ("1"), matched
  against the JSON's bname / bnumber.
- If chapter is given but no verse_start/verse_end, the ENTIRE chapter
  is used.
- Recognized keys: book, chapter, verse_start, verse_end, translation

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

import json
import os
import sys

from PIL import Image, ImageDraw, ImageFont
from moviepy import (
    ImageClip,
    AudioFileClip,
    CompositeVideoClip,
    afx,
)

# --------------------------------------------------
# CONFIG — edit these for each video you generate
# (any of book/chapter/verse_start/verse_end can be
#  overridden from the command line — see the module
#  docstring above)
# --------------------------------------------------

BIBLE_JSON_PATH = "bible.json"       # path to the Zefania-format JSON file
BACKGROUND_IMAGE_PATH = "zip.png"    # full-bleed photo behind every slide
AUDIO_PATH = "audio.mp3"             # background music, or None for silent video

BOOK_NAME = "Genesis"
CHAPTER_NUMBER = "2"
VERSE_START = 1
VERSE_END = 25                       # or None for the whole chapter

# Auto-built as "GENESIS 1:1-31 (NLT)" — override with your own string if you want
TRANSLATION_LABEL = "NLT"
BANNER_TEXT = None  # set to a string to override the auto-generated banner text

TITLE_SLIDE_TEXT = None              # e.g. "Welcome to our video." or None to skip
OUTRO_SLIDE_TEXT = "Thanks For Watching."

WATERMARK_TEXT = "@zipatsokalembedwe"  # e.g. "@yourhandle" or None to skip

WIDTH = 1080
HEIGHT = 1920
FPS = 12

OUTPUT_DIR = "screens"
SLIDES_DIR = os.path.join(OUTPUT_DIR, "slides")
VIDEO_PATH = os.path.join(OUTPUT_DIR, "bible_shorts_video.mp4")

WORDS_PER_SECOND = 2.5
MIN_DURATION = 2.5
MAX_DURATION = 12

TRANSITION_DURATION = 0.4            # seconds; horizontal slide/push between slides

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
BRACKET_COLOR = "#FFD54A"
BRACKET_THICKNESS = 9
BRACKET_TICK = 34
BRACKET_GAP = 34                     # space between bracket and text block

BANNER_FILL = "#F2E8D5"
BANNER_BORDER = "#3B1F1F"
BANNER_TEXT_COLOR = "#3B1F1F"

AUDIO_FADE_DURATION = 1.5            # seconds, applied at start and end of the video

# --------------------------------------------------
# CLI OVERRIDES
# --------------------------------------------------
# Accepts a single comma-separated "key:value" argument list, e.g.
#   python bible_shorts_generator.py book:Genesis,chapter:1
#   python bible_shorts_generator.py book:1,chapter:1,verse_start:1,verse_end:10
# A leading "?" (if you're used to query-string style) is tolerated too.


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
    # When book/chapter come from the CLI and no explicit verse range is
    # given, default to the entire chapter rather than the hardcoded
    # CONFIG range above.
    if "verse_start" not in _cli_args and "verse_end" not in _cli_args:
        VERSE_START = None
        VERSE_END = None

if "verse_start" in _cli_args:
    VERSE_START = int(_cli_args["verse_start"])

if "verse_end" in _cli_args:
    VERSE_END = int(_cli_args["verse_end"])

if "translation" in _cli_args:
    TRANSLATION_LABEL = _cli_args["translation"]

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


def build_slides_from_config():
    """
    Returns (banner_text, slides) where slides is a list of dicts:
    {"kind": "title" | "outro" | "verse", "text": ...}
    """
    bible = load_bible(BIBLE_JSON_PATH)
    book = get_book(bible, BOOK_NAME)
    chapter = get_chapter(book, CHAPTER_NUMBER)
    verses = extract_verses(chapter, VERSE_START, VERSE_END)

    if BANNER_TEXT:
        banner_text = BANNER_TEXT
    else:
        v_first = verses[0][0]
        v_last = verses[-1][0]
        verse_range = v_first if v_first == v_last else f"{v_first}-{v_last}"
        banner_text = (
            f"{book['bname'].upper()} {chapter['cnumber']}:{verse_range} "
            f"({TRANSLATION_LABEL})"
        )

    slides = []

    if TITLE_SLIDE_TEXT:
        slides.append({"kind": "title", "text": TITLE_SLIDE_TEXT})

    for vnumber, text in verses:
        slides.append({"kind": "verse", "text": f"{vnumber}. {text}"})

    if OUTRO_SLIDE_TEXT:
        slides.append({"kind": "outro", "text": OUTRO_SLIDE_TEXT})

    return banner_text, slides


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
        radius= 2,
        fill=BANNER_FILL,
        outline=BANNER_BORDER,
        width=5,
    )

    text_x = box_x + (box_w - text_w) / 2 - bbox[0]
    text_y = box_y + (box_h - text_h) / 2 - bbox[1]
    draw.text((text_x, text_y), text, font=banner_font, fill=BANNER_TEXT_COLOR)

    return box_y + box_h  # bottom edge, so verse slides can start below it


def draw_bracket(draw, x, y_top, y_bottom):
    draw.line([(x, y_top), (x, y_bottom)], fill=BRACKET_COLOR, width=BRACKET_THICKNESS)
    draw.line(
        [(x, y_top), (x + BRACKET_TICK, y_top)],
        fill=BRACKET_COLOR,
        width=BRACKET_THICKNESS,
    )
    draw.line(
        [(x, y_bottom), (x + BRACKET_TICK, y_bottom)],
        fill=BRACKET_COLOR,
        width=BRACKET_THICKNESS,
    )


def draw_watermark(image):
    if not WATERMARK_TEXT:
        return
    draw = ImageDraw.Draw(image)
    bbox = draw.textbbox((0, 0), WATERMARK_TEXT, font=watermark_font)
    text_w = bbox[2] - bbox[0]
    x = WIDTH - PADDING - text_w
    y = HEIGHT - 90
    draw.text((x, y), WATERMARK_TEXT, font=watermark_font, fill="white")


# --------------------------------------------------
# SLIDE CREATION
# --------------------------------------------------


def create_verse_slide(text, banner_text, index):
    image = make_base_image()
    draw = ImageDraw.Draw(image)

    banner_bottom = draw_banner(image, banner_text)

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

    draw_bracket(draw, PADDING, block_top, block_bottom)
    draw_watermark(image)

    path = os.path.join(SLIDES_DIR, f"slide_{index + 1:03d}.png")
    image.save(path)
    return path


def create_plain_slide(text, index):
    """Used for title/outro slides — simple centered text over the background."""
    image = make_base_image()
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

    draw_watermark(image)

    path = os.path.join(SLIDES_DIR, f"slide_{index + 1:03d}.png")
    image.save(path) 
    return path


# --------------------------------------------------
# HORIZONTAL SLIDE TRANSITION
# --------------------------------------------------
# Every slide pushes the previous one out horizontally: the incoming
# slide enters from the right edge and the outgoing slide exits off
# the left edge, both moving in sync so it reads as one continuous
# push rather than a simple wipe-over.


def make_position_func(duration, transition, is_first, is_last):
    def pos(t):
        x = 0

        if not is_first and t < transition:
            progress = t / transition
            x = WIDTH * (1 - progress)

        if not is_last and t > duration - transition:
            progress = (t - (duration - transition)) / transition
            x = -WIDTH * progress

        return (x, 0)

    return pos


def build_sliding_video(clips, transition_duration):
    if not clips:
        raise ValueError("No clips to assemble into a video.")

    # Never let the transition eat more than half of the shortest slide.
    shortest = min(clip.duration for clip in clips)
    transition_duration = max(0.0, min(transition_duration, shortest / 2))

    starts = [0.0]
    for i in range(1, len(clips)):
        starts.append(starts[i - 1] + clips[i - 1].duration - transition_duration)

    total_duration = starts[-1] + clips[-1].duration

    positioned_clips = []
    for i, (clip, start) in enumerate(zip(clips, starts)):
        is_first = i == 0
        is_last = i == len(clips) - 1
        pos_func = make_position_func(clip.duration, transition_duration, is_first, is_last)
        positioned_clips.append(clip.with_start(start).with_position(pos_func))

    return CompositeVideoClip(positioned_clips, size=(WIDTH, HEIGHT)).with_duration(
        total_duration
    )


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
# MAIN
# --------------------------------------------------


def main():
    os.makedirs(SLIDES_DIR, exist_ok=True)

    banner_text, slides = build_slides_from_config()

    clips = []
    for i, slide in enumerate(slides):
        print(f"Creating slide {i + 1}/{len(slides)} ({slide['kind']})")

        if slide["kind"] == "verse":
            path = create_verse_slide(slide["text"], banner_text, i)
        else:
            path = create_plain_slide(slide["text"], i)

        duration = calculate_duration(slide["text"])
        print(f"  Duration: {duration:.2f}s")

        clip = ImageClip(path).with_duration(duration)
        clips.append(clip)


if __name__ == "__main__":
    main()

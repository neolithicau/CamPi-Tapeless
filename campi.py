#!/usr/bin/env python3

import ipaddress
import math
import json
import os
import queue
import re
import shutil
import shlex
import signal
import socket
import subprocess
import textwrap
import threading
import time
from pathlib import Path

import spidev
from PIL import Image, ImageDraw, ImageFont
from luma.core.interface.serial import spi
from luma.lcd.device import ili9341

APP_VERSION = "1.0"
# CamPi v1.0
# Raspberry Pi 5 capture appliance for live DV/HDV, FireWire tape transport,
# and generic USB analog capture. Masters are recorded natively/losslessly.
# UI themes: Basic, Genuine Sorny, Genuine Panafonics and Genuine JayVC,
# with touch-first navigation, persistent settings and error logging.
# DV handling includes PAL/NTSC, progressive/interlaced content detection,
# source audio probing, Tape Deck AV/C control and cadence-aware HDMI playback.
# Storage supports internal SD plus dynamically discovered USB media, with
# per-device free-space reporting and Samba-friendly capture locations.
# Network tools use NetworkManager when available and report per-interface IPs.
# SonyCam Original is reserved for transport/status readouts; ordinary menu
# text uses Debian sans fonts, including DejaVu Sans Condensed for Genuine Sorny.

try:
    from gpiozero import Button, PWMOutputDevice
    GPIOZERO_AVAILABLE = True
except Exception:
    Button = None
    PWMOutputDevice = None
    GPIOZERO_AVAILABLE = False


# ============================================================
# DISPLAY / HARDWARE
# ============================================================

# Environment overrides allow the same release to run on different Pi 5
# builds without editing application source. Values are BCM GPIO numbers,
# not physical header positions. See examples/campi.env.example.
def config_int(name, default, minimum=None, maximum=None):
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
        if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
            raise ValueError("outside supported range")
        return value
    except ValueError:
        print(f"Ignoring invalid {name}={raw!r}; using {default}")
        return default


def config_path(name, default):
    return Path(os.environ.get(name) or str(default)).expanduser()


WIDTH = 320
HEIGHT = 240
HEADER_H = 32
FOOTER_H = 48

SPI_PORT = config_int("CAMPI_SPI_PORT", 0, 0)
LCD_SPI_DEVICE = config_int("CAMPI_LCD_SPI_DEVICE", 0, 0)
TOUCH_SPI_DEVICE = config_int("CAMPI_TOUCH_SPI_DEVICE", 1, 0)

LCD_DC = config_int("CAMPI_LCD_DC", 22, 0, 53)
LCD_RST = config_int("CAMPI_LCD_RST", 27, 0, 53)
TOUCH_IRQ_GPIO = config_int("CAMPI_TOUCH_IRQ_GPIO", 17, 0, 53)
BUZZER_GPIO_DEFAULT = 16

LCD_SPI_SPEED = config_int("CAMPI_LCD_SPI_SPEED", 32_000_000, 1_000)
TOUCH_SPI_SPEED = config_int("CAMPI_TOUCH_SPI_SPEED", 1_000_000, 1_000)
LCD_ROTATION = config_int("CAMPI_LCD_ROTATION", 0, 0, 3)

# Touch sampling / gesture thresholds. Kept together so the resistive panel can
# be tuned without hunting through the main event loop.
TOUCH_STARTUP_RELEASE_SECONDS = 0.12
TOUCH_GESTURE_THRESHOLD_PX = 16
TOUCH_SWIPE_STEP_PX = 24
TOUCH_SAMPLE_RETRIES = 3
TOUCH_SAMPLE_RETRY_DELAY = 0.002
TOUCH_LOOP_INTERVAL_SECONDS = 0.03


# 3-pin PASSIVE buzzer module: signal on BCM GPIO16 / physical pin 36.
# Original project module is powered from 5 V and uses a low-level trigger input.
BUZZER_GPIO = config_int("CAMPI_BUZZER_GPIO", BUZZER_GPIO_DEFAULT, 0, 53)
BUZZER_ACTIVE_HIGH = False
BUZZER_FREQUENCY_HZ = 2000
BUZZER_DUTY = 0.50

BUZZER_SHORT_ACTION_SECONDS = 0.070
BUZZER_SHORT_ERROR_SECONDS = 0.090
BUZZER_SHORT_ERROR_GAP = 0.065

BUZZER_LONG_ACTION_SECONDS = 0.160
BUZZER_LONG_ERROR_SECONDS = 0.150
BUZZER_LONG_ERROR_GAP = 0.080

# ============================================================
# PATHS / PERSISTENT SETTINGS
# ============================================================

HOME = config_path("CAMPI_HOME", Path.home())
# Preserve existing installations' settings path to avoid losing calibration
# and theme preferences when upgrading.
APP_DIR = config_path("CAMPI_CONFIG_DIR", HOME / ".config" / "tapeless")
SETTINGS_FILE = APP_DIR / "settings.json"
TOUCH_CAL_FILE = APP_DIR / "touch-calibration.json"
LEGACY_TOUCH_CAL_FILE = HOME / ".tapeless-touch.json"
CAPTURE_LOG_FILE = APP_DIR / "capture.log"
ERROR_LOG_FILE = APP_DIR / "errors.json"
MPV_IPC_SOCKET = "/tmp/tapeless-mpv.sock"
MPV_LOG_FILE = APP_DIR / "mpv.log"
HDMI_HOLDING_IMAGE = APP_DIR / "hdmi-holding.png"
HDMI_FRAMEBUFFER = config_path("CAMPI_HDMI_FB", "/dev/fb0")
HDMI_TARGET_WIDTH = config_int("CAMPI_HDMI_WIDTH", 1920, 1)
HDMI_TARGET_HEIGHT = config_int("CAMPI_HDMI_HEIGHT", 1080, 1)

INTERNAL_CAPTURE_DIR = config_path("CAMPI_INTERNAL_CAPTURE_DIR", HOME / "captures")
SSD_CAPTURE_DIR = config_path("CAMPI_USB_CAPTURE_DIR", "/mnt/usb-captures")

DEFAULT_SETTINGS = {
    "capture_mode": "dv",
    "storage_policy": "auto",
    "buzzer_mode": "short",
    # UI theme. "default" is the Basic CamPi list interface;
    "ui_theme": "sorny",
    # Four UI sizes: XS, SMALL, MEDIUM, LARGE.
    # Keep independent defaults/preferences per theme.
    "font_size_step": -1,          # legacy compatibility
    "default_font_size_step": -1,     # SMALL
    "sorny_font_size_step": -1,       # SMALL
    "panafonics_font_size_step": -1,  # SMALL
    "jayvc_font_size_step": -1,       # SMALL

    # Source standard is inferred from clearly PAL/NTSC V4L2 modes where
    # possible, with a manual override for ambiguous hardware. The selected
    # mode is validated against the capabilities of the detected USB device.
    "analog_standard": "PAL",
    "analog_pixel_format": "MJPG",
    "analog_width": 720,
    "analog_height": 576,
    "analog_fps": 50.0,

    # Only controls the user has changed are stored here.  They are re-applied
    # after reboot/device reconnect because UVC controls commonly reset.
    "analog_controls": {},
}

# Generic USB analog capture fallbacks. CamPi dynamically discovers the
# active V4L2 node and pairs its ALSA capture interface by USB parent device.
# These remain only as compatibility fallbacks for unusual installations.
ANALOG_VIDEO_DEVICE = os.environ.get("CAMPI_ANALOG_VIDEO_FALLBACK", "/dev/video0")
ANALOG_AUDIO_FALLBACK = os.environ.get("CAMPI_ANALOG_AUDIO_FALLBACK", "plughw:2,0")
ANALOG_PRE_CAPTURE_DELAY = 1.0

# ============================================================
# UI COLOURS / FONTS
# ============================================================

BG = (8, 11, 16)
PANEL = (19, 25, 34)
PANEL_ALT = (25, 32, 43)
TEXT = (238, 242, 247)
TEXT_DIM = (150, 160, 172)

# Classic Sony camcorder OSD-inspired accents.
SONY_CYAN = (0, 232, 238)
SONY_YELLOW = (248, 224, 24)
SONY_RED = (255, 76, 76)
SONY_MAGENTA = (238, 72, 210)
SONY_GREEN = (54, 222, 94)
SONY_GREEN_DARK = (18, 58, 31)
SONY_BLUE = (240, 94, 128)
SONY_BLUE_DARK = (92, 31, 52)
SONY_RED_DARK = (62, 24, 29)
SONY_YELLOW_DARK = (60, 52, 18)

# Global top/status bar. Keep this deliberately neutral so the CamPi header
# remains visually present regardless of which UI theme is active.
HEADER_BAR_BG = (52, 59, 66)
HEADER_BAR_EDGE = (91, 101, 111)

# Dedicated HOME/MENU blue.
MENU_BLUE = (68, 91, 255)
MENU_BLUE_DARK = (27, 35, 96)

# Genuine Sorny semantic palette.
# Blue is the single navigation/information accent. Green is reserved for
# positive/connected actions, red for record/destructive states, amber for
# warning/source attention, and neutral grey for BACK/CANCEL.
SORNY_BG = (48, 52, 56)
SORNY_BG_DARK = (35, 38, 42)
SORNY_PANEL = (196, 220, 222)
SORNY_PANEL_BRIGHT = (226, 240, 242)
SORNY_PANEL_EDGE = (147, 174, 181)
SORNY_TEXT = (38, 45, 50)
SORNY_TEXT_DIM = (158, 169, 176)
SORNY_RAIL = (63, 68, 73)

SORNY_ACCENT = (79, 137, 196)
SORNY_ACCENT_BRIGHT = (119, 169, 214)
SORNY_ACCENT_DARK = (35, 70, 104)

SORNY_DESC_BG = (27, 42, 58)
SORNY_DESC_BORDER = SORNY_ACCENT
SORNY_DESC_TEXT = (197, 220, 239)

SORNY_SUCCESS = (82, 174, 119)
SORNY_SUCCESS_DARK = (38, 91, 61)
SORNY_WARNING = (219, 173, 74)
SORNY_WARNING_DARK = (90, 70, 32)
SORNY_DANGER = (216, 84, 84)
SORNY_DANGER_DARK = (94, 39, 42)
SORNY_NEUTRAL = (92, 99, 106)
SORNY_NEUTRAL_BRIGHT = (154, 162, 170)
SORNY_HEADER_TEXT = (238, 242, 245)

# Genuine Panafonics: pre-2010 Panasonic-inspired text OSD. Menu-list text
# uses SonyCam Original, while descriptions, the global header and bottom
# navigation retain CamPi's standard UI fonts.
PANA_BG = (78, 82, 84)
PANA_BG_DARK = (51, 55, 57)
PANA_TITLE_BG = (221, 231, 226)
PANA_TITLE_TEXT = (31, 36, 37)
PANA_FRAME = (221, 230, 226)
PANA_TEXT = (244, 246, 242)
PANA_TEXT_DIM = (181, 188, 185)
PANA_GREEN = (104, 229, 91)
PANA_CYAN = (74, 230, 235)
PANA_YELLOW = (241, 203, 74)
PANA_ORANGE = (231, 116, 54)

# Genuine JayVC: 2000s JVC MiniDV / early Everio-inspired OSD. Period JVC
# menus repeatedly combine deep cobalt/royal blue chrome with silver-grey menu
# fields, bright blue selection bars and small multi-colour category icons.
# Keep the palette saturated enough to read on the small 320x240 LCD without
# losing the darker camcorder-era look.
JAYVC_BG = (7, 16, 43)
JAYVC_BAR = (13, 42, 116)
JAYVC_BAR_EDGE = (73, 126, 207)
JAYVC_FRAME = (67, 116, 196)
JAYVC_FRAME_HI = (104, 184, 241)
JAYVC_PANEL = (210, 215, 223)
JAYVC_PANEL_DARK = (182, 192, 209)
JAYVC_PANEL_EDGE = (76, 91, 119)
JAYVC_TEXT = (246, 249, 253)
JAYVC_TEXT_DIM = (169, 190, 221)
JAYVC_TEXT_DARK = (16, 27, 50)
JAYVC_SELECT = (43, 91, 204)
JAYVC_SELECT_DARK = (27, 57, 139)
JAYVC_CYAN = (83, 195, 230)
JAYVC_GREEN = (91, 181, 123)
JAYVC_MAGENTA = (171, 98, 182)
JAYVC_WARNING = (218, 177, 65)
JAYVC_RED = (204, 89, 80)

ACCENT = SONY_CYAN
GREEN = (46, 204, 113)
RED = SONY_RED
AMBER = SONY_YELLOW
SELECTED = (47, 67, 88)
BORDER = (70, 82, 96)

def first_existing_font(*paths):
    for path in paths:
        if Path(path).exists():
            return str(path)
    return str(paths[-1])


# Prefer Liberation Sans on Raspberry Pi OS / Debian, then FreeSans, with the
# original DejaVu family as a guaranteed fallback on minimal installations.
FONT_NORMAL_PATH = first_existing_font(
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)
FONT_BOLD_PATH = first_existing_font(
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)
PROJECT_DIR = Path(__file__).resolve().parent

FONT_STATUS_PATH = first_existing_font(
    config_path(
        "CAMPI_STATUS_FONT",
        PROJECT_DIR / "fonts" / "sonycam-original.otf",
    ),
    HOME / "fonts" / "sonycam-original.otf",
    FONT_NORMAL_PATH,
)

# Genuine Sorny uses the condensed DejaVu family, which is normally present
# on Debian / Raspberry Pi OS and more closely matches compact period menu UI.
SORNY_FONT_NORMAL_PATH = first_existing_font(
    "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed.ttf",
    FONT_NORMAL_PATH,
)
SORNY_FONT_BOLD_PATH = first_existing_font(
    "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
    FONT_BOLD_PATH,
)


def load_font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


# Base sizes are the typography established during the preceding v1.15 tests.
# The user-facing setting offsets every touchscreen font by -2..+1 points.
FONT_SIZE_MIN = -2
FONT_SIZE_MAX = 1
FONT_SIZE_DEFAULT = -1
FONT_BASE_SIZES = {
    "tiny": 12,
    "small": 14,
    "normal": 18,
    "bold": 19,
    "big": 35,
    "button": 17,
}


def apply_font_size_step(step):
    """Reload all touchscreen fonts using one global point-size offset."""
    global FONT_TINY, FONT_SMALL, FONT_NORMAL, FONT_BOLD, FONT_BIG, FONT_BUTTON
    global FONT_STATUS_TINY, FONT_STATUS_SMALL, FONT_STATUS_BOLD, FONT_STATUS_BIG
    global SORNY_FONT_TINY, SORNY_FONT_SMALL, SORNY_FONT_NORMAL
    global SORNY_FONT_BOLD, SORNY_FONT_BUTTON

    try:
        step = int(step)
    except Exception:
        step = FONT_SIZE_DEFAULT
    step = max(FONT_SIZE_MIN, min(FONT_SIZE_MAX, step))

    def sz(name):
        return max(8, FONT_BASE_SIZES[name] + step)

    FONT_TINY = load_font(FONT_NORMAL_PATH, sz("tiny"))
    FONT_SMALL = load_font(FONT_NORMAL_PATH, sz("small"))
    FONT_NORMAL = load_font(FONT_NORMAL_PATH, sz("normal"))
    FONT_BOLD = load_font(FONT_BOLD_PATH, sz("bold"))
    FONT_BIG = load_font(FONT_BOLD_PATH, sz("big"))
    FONT_BUTTON = load_font(FONT_BOLD_PATH, sz("button"))

    # SonyCam remains limited to transport/status text but follows the same
    # global size preference so the interface scales coherently.
    FONT_STATUS_TINY = load_font(FONT_STATUS_PATH, sz("tiny"))
    FONT_STATUS_SMALL = load_font(FONT_STATUS_PATH, sz("small"))
    FONT_STATUS_BOLD = load_font(FONT_STATUS_PATH, sz("bold"))
    FONT_STATUS_BIG = load_font(FONT_STATUS_PATH, sz("big"))

    SORNY_FONT_TINY = load_font(SORNY_FONT_NORMAL_PATH, sz("tiny"))
    SORNY_FONT_SMALL = load_font(SORNY_FONT_NORMAL_PATH, sz("small"))
    SORNY_FONT_NORMAL = load_font(SORNY_FONT_NORMAL_PATH, sz("normal"))
    SORNY_FONT_BOLD = load_font(SORNY_FONT_BOLD_PATH, sz("bold"))
    SORNY_FONT_BUTTON = load_font(SORNY_FONT_BOLD_PATH, sz("button"))
    return step


# Initialise module-level fonts at the default. UI reapplies any
# saved per-theme preference immediately after loading settings.
apply_font_size_step(FONT_SIZE_DEFAULT)
# Fixed-size header typography stays compact regardless of the global UI
# font-size setting. This prevents ordinary page titles such as DEVICE OVERVIEW
# from becoming wide enough to scroll simply because the menu font is enlarged.
FONT_HEADER_TINY = load_font(FONT_BOLD_PATH, 9)


# ============================================================
# GENERAL HELPERS
# ============================================================

def ensure_app_dirs():
    APP_DIR.mkdir(parents=True, exist_ok=True)
    INTERNAL_CAPTURE_DIR.mkdir(parents=True, exist_ok=True)


def save_json_atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with open(temp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(temp, path)


def load_error_log():
    try:
        with open(ERROR_LOG_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)][-40:]
    except Exception:
        pass
    return []


def save_error_log(entries):
    try:
        save_json_atomic(ERROR_LOG_FILE, list(entries)[-40:])
    except Exception:
        pass


def hdmi_display_connected():
    """Best-effort KMS/ELD test for a physically connected HDMI sink."""
    try:
        for status_path in Path("/sys/class/drm").glob("*HDMI*/status"):
            if status_path.read_text(encoding="utf-8", errors="ignore").strip().lower() == "connected":
                return True
    except Exception:
        pass

    # Fallback for Pi kernels where ALSA ELD is populated more reliably.
    try:
        for eld_path in Path("/proc/asound").glob("card*/eld#*"):
            text = eld_path.read_text(encoding="utf-8", errors="ignore")
            if "monitor_present		1" in text or "monitor_present 1" in text:
                return True
    except Exception:
        pass
    return False


def load_settings():
    settings = dict(DEFAULT_SETTINGS)
    settings["analog_controls"] = {}

    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as fh:
            loaded = json.load(fh)

        if loaded.get("capture_mode") in ("dv", "analog"):
            settings["capture_mode"] = loaded["capture_mode"]

        if loaded.get("storage_policy") in ("auto", "internal", "ssd"):
            settings["storage_policy"] = loaded["storage_policy"]

        if str(loaded.get("buzzer_mode", "")).lower() in ("short", "long", "off"):
            settings["buzzer_mode"] = str(loaded["buzzer_mode"]).lower()

        if str(loaded.get("ui_theme", "")).lower() in ("default", "sorny", "panafonics", "jayvc"):
            settings["ui_theme"] = str(loaded["ui_theme"]).lower()

        try:
            legacy_step = int(loaded.get("font_size_step", settings["font_size_step"]))
            if FONT_SIZE_MIN <= legacy_step <= FONT_SIZE_MAX:
                settings["font_size_step"] = legacy_step
                # Existing users keep their previous Basic-theme size.
                settings["default_font_size_step"] = int(
                    loaded.get("default_font_size_step", legacy_step)
                )
        except Exception:
            pass

        try:
            default_step = int(
                loaded.get("default_font_size_step", settings["default_font_size_step"])
            )
            if FONT_SIZE_MIN <= default_step <= FONT_SIZE_MAX:
                settings["default_font_size_step"] = default_step
        except Exception:
            pass

        try:
            sorny_step = int(
                loaded.get("sorny_font_size_step", settings["sorny_font_size_step"])
            )
            if FONT_SIZE_MIN <= sorny_step <= FONT_SIZE_MAX:
                settings["sorny_font_size_step"] = sorny_step
        except Exception:
            pass

        try:
            panafonics_step = int(
                loaded.get(
                    "panafonics_font_size_step",
                    settings["panafonics_font_size_step"],
                )
            )
            if FONT_SIZE_MIN <= panafonics_step <= FONT_SIZE_MAX:
                settings["panafonics_font_size_step"] = panafonics_step
        except Exception:
            pass

        try:
            jayvc_step = int(
                loaded.get(
                    "jayvc_font_size_step",
                    settings["jayvc_font_size_step"],
                )
            )
            if FONT_SIZE_MIN <= jayvc_step <= FONT_SIZE_MAX:
                settings["jayvc_font_size_step"] = jayvc_step
        except Exception:
            pass

        if str(loaded.get("analog_standard", "")).upper() in ("PAL", "NTSC"):
            settings["analog_standard"] = str(loaded["analog_standard"]).upper()

        if loaded.get("analog_pixel_format"):
            settings["analog_pixel_format"] = str(loaded["analog_pixel_format"])

        for key in ("analog_width", "analog_height"):
            try:
                value = int(loaded.get(key, settings[key]))
                if value > 0:
                    settings[key] = value
            except Exception:
                pass

        try:
            fps = float(loaded.get("analog_fps", settings["analog_fps"]))
            if fps > 0:
                settings["analog_fps"] = fps
        except Exception:
            pass

        controls = loaded.get("analog_controls")
        if isinstance(controls, dict):
            settings["analog_controls"] = dict(controls)

    except Exception:
        pass

    return settings


def draw_text(draw, xy, text, font, fill, fontmode=None):
    previous_mode = getattr(draw, "fontmode", "L")
    if fontmode is not None:
        draw.fontmode = fontmode
    try:
        draw.text(xy, str(text), font=font, fill=fill)
    finally:
        if fontmode is not None:
            draw.fontmode = previous_mode


def centered_text(draw, box, text, font, fill, fontmode=None):
    x1, y1, x2, y2 = box
    text = str(text)
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    x = x1 + ((x2 - x1) - tw) // 2
    y = y1 + ((y2 - y1) - th) // 2 - 1
    draw_text(draw, (x, y), text, font, fill, fontmode=fontmode)


def draw_text_noaa(draw, xy, text, font, fill):
    """Draw TrueType/OpenType text through a 1-bit mask for hard pixel edges."""
    text = str(text)
    bbox = font.getbbox(text)
    width = max(1, bbox[2] - bbox[0])
    height = max(1, bbox[3] - bbox[1])
    mask = Image.new("1", (width, height), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.fontmode = "1"
    mask_draw.text((-bbox[0], -bbox[1]), text, font=font, fill=1)
    x = int(xy[0] + bbox[0])
    y = int(xy[1] + bbox[1])
    draw._image.paste(fill, (x, y), mask)


def centered_text_noaa(draw, box, text, font, fill):
    x1, y1, x2, y2 = box
    text = str(text)
    bbox = font.getbbox(text)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]

    # Match centered_text() positioning exactly. draw_text_noaa() applies the
    # glyph bbox offsets internally, so do not pre-cancel them here.
    x = x1 + ((x2 - x1) - tw) // 2
    y = y1 + ((y2 - y1) - th) // 2 - 1
    draw_text_noaa(draw, (x, y), text, font, fill)


def scrolling_text(draw, box, text, font, fill, speed=24.0, hold=1.25, background=BG, phase_time=None):
    """Draw text normally if it fits, otherwise ping-pong scroll it inside box."""
    x1, y1, x2, y2 = box
    width = max(1, int(x2 - x1))
    height = max(1, int(y2 - y1))
    text = str(text)

    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    y = ((height - text_height) // 2) - bbox[1]

    if text_width <= width:
        draw_text(draw, (x1, y1 + y), text, font, fill)
        return

    overflow = text_width - width
    travel = overflow / max(1.0, float(speed))
    cycle = (hold * 2.0) + (travel * 2.0)
    clock_value = time.monotonic() if phase_time is None else float(phase_time)
    phase = clock_value % cycle

    if phase < hold:
        offset = 0
    elif phase < hold + travel:
        offset = int((phase - hold) * speed)
    elif phase < (hold * 2.0) + travel:
        offset = overflow
    else:
        offset = int(overflow - (phase - ((hold * 2.0) + travel)) * speed)

    offset = int(clamp(offset, 0, overflow))
    temp = Image.new("RGB", (width, height), background)
    temp_draw = ImageDraw.Draw(temp)
    temp_draw.fontmode = "L"
    temp_draw.text((-offset, y), text, font=font, fill=fill)
    draw._image.paste(temp, (x1, y1))


def scrolling_text_noaa(
    draw,
    box,
    text,
    font,
    fill,
    speed=24.0,
    hold=1.25,
    background=BG,
):
    """Ping-pong scrolling text rendered through a crisp 1-bit mask."""
    x1, y1, x2, y2 = box
    width = max(1, int(x2 - x1))
    height = max(1, int(y2 - y1))
    text = str(text)

    bbox = font.getbbox(text)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    y = ((height - text_height) // 2) - bbox[1]

    if text_width <= width:
        draw_text_noaa(draw, (x1, y1 + y), text, font, fill)
        return

    overflow = text_width - width
    travel = overflow / max(1.0, float(speed))
    cycle = (hold * 2.0) + (travel * 2.0)
    phase = time.monotonic() % cycle

    if phase < hold:
        offset = 0
    elif phase < hold + travel:
        offset = int((phase - hold) * speed)
    elif phase < (hold * 2.0) + travel:
        offset = overflow
    else:
        offset = int(
            overflow
            - (phase - ((hold * 2.0) + travel)) * speed
        )

    offset = int(clamp(offset, 0, overflow))
    temp = Image.new("RGB", (width, height), background)
    temp_draw = ImageDraw.Draw(temp)
    temp_draw.fontmode = "1"
    draw_text_noaa(temp_draw, (-offset, y), text, font, fill)
    draw._image.paste(temp, (x1, y1))


def format_elapsed(seconds):
    seconds = int(seconds)
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    seconds = seconds % 60
    return f"{hours:02}:{minutes:02}:{seconds:02}"


def human_size(size_bytes):
    value = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0 or unit == "TB":
            if unit in ("B", "KB"):
                return f"{value:.0f} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{size_bytes} B"


def clamp(value, low, high):
    return max(low, min(high, value))


def map_range(value, in_start, in_end, out_start, out_end):
    if in_end == in_start:
        return out_start
    return out_start + ((value - in_start) * (out_end - out_start) / (in_end - in_start))


def safe_text(text, maximum):
    if len(text) <= maximum:
        return text
    return text[: max(1, maximum - 1)] + "…"


def normalize_fps(fps):
    """Normalise frame-rate values, including malformed bare broadcast rates."""
    try:
        value = float(fps)
    except Exception:
        return 0.0

    if value > 1000:
        raw_rates = (
            (24000.0, 23.976),
            (25000.0, 25.0),
            (30000.0, 29.970),
            (50000.0, 50.0),
            (60000.0, 59.940),
        )
        for raw_rate, normal_rate in raw_rates:
            if abs(value - raw_rate) < 1.0:
                return normal_rate
    return value


def format_fps(fps):
    try:
        value = normalize_fps(fps)
    except Exception:
        return str(fps)

    common = (
        (23.976, "23.976"),
        (24.0, "24"),
        (25.0, "25"),
        (29.970, "29.97"),
        (30.0, "30"),
        (50.0, "50"),
        (59.940, "59.94"),
        (60.0, "60"),
    )
    for target, label in common:
        if abs(value - target) < 0.02:
            return label
    if abs(value - round(value)) < 0.001:
        return str(int(round(value)))
    return f"{value:.3f}".rstrip("0").rstrip(".")


def storage_display_name(name):
    name = str(name)
    return "INT" if name.upper() == "INTERNAL" else name.upper()


def storage_policy_label(policy, short=False):
    policy = str(policy or "auto").lower()
    if short:
        return {"auto": "AUTO", "internal": "INT", "ssd": "USB"}.get(policy, "AUTO")
    return {
        "auto": "Auto (USB first)",
        "internal": "Internal only",
        "ssd": "USB only (Internal fallback)",
    }.get(policy, "Auto (USB first)")


def compact_hdmi_mode_label(mode):
    if mode == "preferred":
        return "HDMI AUTO"
    match = re.match(r"^(\d+)x(\d+)@(.+)$", str(mode))
    if not match:
        return str(mode)
    _width, height, refresh = match.groups()
    try:
        refresh = format_fps(float(refresh))
    except Exception:
        pass
    return f"{height}p{refresh}"


def humanize_control_name(name):
    return str(name).replace("_", " ").strip().title()


def ffmpeg_v4l2_input_format(fourcc):
    mapping = {
        "YUYV": "yuyv422",
        "UYVY": "uyvy422",
        "MJPG": "mjpeg",
        "JPEG": "mjpeg",
        "NV12": "nv12",
        "YU12": "yuv420p",
        "YV12": "yuv420p",
        "RGB3": "rgb24",
        "BGR3": "bgr24",
        "GREY": "gray",
        "H264": "h264",
        "HEVC": "hevc",
    }
    code = str(fourcc).upper()
    return mapping.get(code, code.lower())


def parse_v4l2_formats(output):
    modes = []
    pixel_format = None
    description = ""
    width = None
    height = None

    for raw_line in output.splitlines():
        line = raw_line.strip()

        match = re.search(r"\[\d+\]:\s+'([^']+)'(?:\s+\(([^)]*)\))?", line)
        if match:
            pixel_format = match.group(1).strip()
            description = (match.group(2) or "").strip()
            width = None
            height = None
            continue

        match = re.search(r"Size:\s+Discrete\s+(\d+)x(\d+)", line, re.IGNORECASE)
        if match:
            width = int(match.group(1))
            height = int(match.group(2))
            continue

        match = re.search(r"Interval:\s+Discrete\s+[0-9.]+s\s+\(([0-9.]+)\s+fps\)", line, re.IGNORECASE)
        if match and pixel_format and width and height:
            fps = float(match.group(1))
            modes.append({
                "pixel_format": pixel_format,
                "description": description,
                "width": width,
                "height": height,
                "fps": fps,
            })

    unique = []
    seen = set()
    for mode in modes:
        key = (
            mode["pixel_format"],
            mode["width"],
            mode["height"],
            round(float(mode["fps"]), 3),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(mode)

    unique.sort(
        key=lambda mode: (
            -(mode["width"] * mode["height"]),
            -float(mode["fps"]),
            mode["pixel_format"],
        )
    )
    return unique


def parse_v4l2_controls(output):
    controls = []

    pattern = re.compile(
        r"^\s*([a-zA-Z0-9_]+)\s+0x[0-9a-fA-F]+\s+\(([^)]+)\)\s*:\s*(.*)$"
    )

    for raw_line in output.splitlines():
        match = pattern.match(raw_line)
        if not match:
            continue

        name = match.group(1)
        control_type = match.group(2).strip().lower()
        body = match.group(3)

        values = {}
        for key in ("min", "max", "step", "default", "value"):
            value_match = re.search(rf"(?:^|\s){key}=(-?\d+)(?:\s|$)", body)
            if value_match:
                values[key] = int(value_match.group(1))

        if control_type not in ("int", "integer", "integer64", "bool", "boolean"):
            continue

        if "value" not in values:
            continue

        controls.append({
            "name": name,
            "label": humanize_control_name(name),
            "type": control_type,
            "min": values.get("min", 0),
            "max": values.get("max", 1 if control_type in ("bool", "boolean") else values["value"]),
            "step": max(1, values.get("step", 1)),
            "default": values.get("default", values["value"]),
            "value": values["value"],
        })

    return controls


# ============================================================
# XPT2046 TOUCH CONTROLLER
# ============================================================

class XPT2046:
    CMD_X = 0xD0
    CMD_Y = 0x90
    CMD_Z1 = 0xB0
    CMD_Z2 = 0xC0

    def __init__(self, bus=0, device=1, speed=1_000_000, irq_gpio=TOUCH_IRQ_GPIO):
        self.spi = spidev.SpiDev()
        self.spi.open(bus, device)
        self.spi.max_speed_hz = speed
        self.spi.mode = 0

        # The MPI2411 exposes XPT2046 TP_IRQ on GPIO17.  It is active-low,
        # so a gpiozero Button with pull-up gives us a reliable press signal.
        # If gpiozero cannot claim the pin, fall back to pressure polling.
        self.irq = None
        if GPIOZERO_AVAILABLE and Button is not None:
            try:
                self.irq = Button(irq_gpio, pull_up=True, bounce_time=None)
            except Exception as exc:
                print("Touch IRQ unavailable; using pressure fallback:", exc)
                self.irq = None

    def close(self):
        try:
            if self.irq is not None:
                self.irq.close()
        except Exception:
            pass
        try:
            self.spi.close()
        except Exception:
            pass

    def read_adc(self, command):
        data = self.spi.xfer2([command, 0x00, 0x00])
        return ((((data[1] << 8) | data[2]) >> 3) & 0x0FFF)

    def pressure(self):
        z1 = self.read_adc(self.CMD_Z1)
        z2 = self.read_adc(self.CMD_Z2)
        return z1 + (4095 - z2)

    def is_touched(self):
        try:
            # Pressure polling is the authoritative touch test.  Some cheap
            # XPT2046 HATs have unreliable/stuck PENIRQ lines, while the SPI
            # ADC continues to report touch coordinates correctly.  Do not let
            # a bad IRQ make an otherwise healthy touchscreen appear dead.
            return self.pressure() > 180
        except Exception:
            return False

    def raw_point(self):
        if not self.is_touched():
            return None

        # Throw away first reads after switching channels.
        self.read_adc(self.CMD_X)
        self.read_adc(self.CMD_Y)

        xs = []
        ys = []

        for _ in range(9):
            if not self.is_touched():
                break
            xs.append(self.read_adc(self.CMD_X))
            ys.append(self.read_adc(self.CMD_Y))
            time.sleep(0.002)

        if len(xs) < 3:
            return None

        xs.sort()
        ys.sort()
        return xs[len(xs) // 2], ys[len(ys) // 2]

    def wait_for_release(self):
        while self.is_touched():
            time.sleep(0.025)
        time.sleep(0.08)

    def wait_for_touch(self):
        while not self.is_touched():
            time.sleep(0.02)

        point = None
        for _ in range(12):
            point = self.raw_point()
            if point is not None:
                break
            time.sleep(0.02)

        self.wait_for_release()
        return point


# ============================================================
# TOUCH CALIBRATION
# ============================================================

def draw_calibration_target(device, x, y, number):
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)
    draw.fontmode = "L"

    centered_text(draw, (0, 8, WIDTH, 34), "TOUCH CALIBRATION", FONT_BOLD, TEXT)
    centered_text(draw, (0, 35, WIDTH, 58), f"Tap target {number} firmly", FONT_SMALL, TEXT_DIM)

    radius = 13
    draw.ellipse((x - radius, y - radius, x + radius, y + radius), outline=ACCENT, width=2)
    draw.line((x - 20, y, x + 20, y), fill=TEXT, width=2)
    draw.line((x, y - 20, x, y + 20), fill=TEXT, width=2)
    device.display(image)


def calibration_valid(cal):
    required = {
        "screen_x_source", "screen_y_source",
        "x_raw_left", "x_raw_right", "y_raw_top", "y_raw_bottom",
        "x_target_left", "x_target_right", "y_target_top", "y_target_bottom",
    }
    if not isinstance(cal, dict) or not required.issubset(cal.keys()):
        return False
    if cal["screen_x_source"] not in ("x", "y") or cal["screen_y_source"] not in ("x", "y"):
        return False
    if abs(cal["x_raw_right"] - cal["x_raw_left"]) < 300:
        return False
    if abs(cal["y_raw_bottom"] - cal["y_raw_top"]) < 300:
        return False
    return True


def load_touch_calibration():
    # New persistent location.
    try:
        with open(TOUCH_CAL_FILE, "r", encoding="utf-8") as fh:
            cal = json.load(fh)
        if calibration_valid(cal):
            return cal
    except Exception:
        pass

    # One-time migration from the earlier location if it exists.
    try:
        with open(LEGACY_TOUCH_CAL_FILE, "r", encoding="utf-8") as fh:
            cal = json.load(fh)
        if calibration_valid(cal):
            save_json_atomic(TOUCH_CAL_FILE, cal)
            return cal
    except Exception:
        pass

    return None


def save_touch_calibration(calibration):
    save_json_atomic(TOUCH_CAL_FILE, calibration)


def calibrate_touch(device, touch):
    targets = [
        (30, 70),
        (WIDTH - 31, 70),
        (WIDTH - 31, HEIGHT - 31),
        (30, HEIGHT - 31),
    ]

    raw_points = []
    for index, (x, y) in enumerate(targets):
        draw_calibration_target(device, x, y, index + 1)
        point = touch.wait_for_touch()
        if point is None:
            raise RuntimeError("Unable to obtain touch coordinate")
        raw_points.append(point)
        time.sleep(0.12)

    tl, tr, br, bl = raw_points

    raw_x_horizontal = ((tr[0] + br[0]) / 2) - ((tl[0] + bl[0]) / 2)
    raw_y_horizontal = ((tr[1] + br[1]) / 2) - ((tl[1] + bl[1]) / 2)

    if abs(raw_x_horizontal) >= abs(raw_y_horizontal):
        screen_x_source = "x"
        screen_y_source = "y"
    else:
        screen_x_source = "y"
        screen_y_source = "x"

    def axis_value(point, source):
        return point[0] if source == "x" else point[1]

    x_left = (axis_value(tl, screen_x_source) + axis_value(bl, screen_x_source)) / 2
    x_right = (axis_value(tr, screen_x_source) + axis_value(br, screen_x_source)) / 2
    y_top = (axis_value(tl, screen_y_source) + axis_value(tr, screen_y_source)) / 2
    y_bottom = (axis_value(bl, screen_y_source) + axis_value(br, screen_y_source)) / 2

    calibration = {
        "screen_x_source": screen_x_source,
        "screen_y_source": screen_y_source,
        "x_raw_left": x_left,
        "x_raw_right": x_right,
        "y_raw_top": y_top,
        "y_raw_bottom": y_bottom,
        "x_target_left": targets[0][0],
        "x_target_right": targets[1][0],
        "y_target_top": targets[0][1],
        "y_target_bottom": targets[2][1],
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "display": "320x240",
    }

    if not calibration_valid(calibration):
        raise RuntimeError("Touch calibration values were not valid")

    save_touch_calibration(calibration)
    return calibration


def translate_touch(raw_point, calibration):
    raw_x, raw_y = raw_point

    source_x = raw_x if calibration["screen_x_source"] == "x" else raw_y
    source_y = raw_x if calibration["screen_y_source"] == "x" else raw_y

    screen_x = map_range(
        source_x,
        calibration["x_raw_left"], calibration["x_raw_right"],
        calibration["x_target_left"], calibration["x_target_right"],
    )
    screen_y = map_range(
        source_y,
        calibration["y_raw_top"], calibration["y_raw_bottom"],
        calibration["y_target_top"], calibration["y_target_bottom"],
    )

    return (
        int(clamp(screen_x, 0, WIDTH - 1)),
        int(clamp(screen_y, 0, HEIGHT - 1)),
    )


# ============================================================
# BUZZER FEEDBACK
# ============================================================

class FeedbackBuzzer:
    """On-demand 2 kHz PWM feedback for the passive buzzer on GPIO16.

    The module is low-level-triggered, so gpiozero is configured active-low.
    Nothing runs continuously: PWM is enabled only for the short chirp itself,
    and normal menu navigation remains silent by design.
    """

    def __init__(self, gpio=BUZZER_GPIO):
        self.device = None
        self.lock = threading.Lock()
        if GPIOZERO_AVAILABLE and PWMOutputDevice is not None:
            try:
                self.device = PWMOutputDevice(
                    gpio,
                    active_high=BUZZER_ACTIVE_HIGH,
                    initial_value=0,
                    frequency=BUZZER_FREQUENCY_HZ,
                )
            except Exception as exc:
                print("Buzzer unavailable:", exc)

    def _play(self, pulses, gap=0.0, frequency=BUZZER_FREQUENCY_HZ):
        if self.device is None:
            return

        def worker():
            with self.lock:
                try:
                    self.device.frequency = float(frequency)
                    for index, duration in enumerate(pulses):
                        self.device.value = BUZZER_DUTY
                        time.sleep(max(0.0, float(duration)))
                        self.device.value = 0
                        if gap > 0 and index < len(pulses) - 1:
                            time.sleep(gap)
                except Exception:
                    try:
                        self.device.value = 0
                    except Exception:
                        pass

        threading.Thread(target=worker, daemon=True).start()

    def action(self, mode="short"):
        mode = str(mode or "short").lower()
        if mode == "off":
            return

        duration = (
            BUZZER_LONG_ACTION_SECONDS
            if mode == "long"
            else BUZZER_SHORT_ACTION_SECONDS
        )
        self._play((duration,))

    def error(self, mode="short"):
        mode = str(mode or "short").lower()
        if mode == "off":
            return

        if mode == "long":
            duration = BUZZER_LONG_ERROR_SECONDS
            gap = BUZZER_LONG_ERROR_GAP
        else:
            duration = BUZZER_SHORT_ERROR_SECONDS
            gap = BUZZER_SHORT_ERROR_GAP

        self._play((duration, duration), gap)

    def close(self):
        try:
            if self.device is not None:
                self.device.value = 0
                self.device.close()
        except Exception:
            pass
        self.device = None


# ============================================================
# MAIN UI
# ============================================================

class TapelessUI:
    ROW_HEIGHT = 35
    MENU_TOP = HEADER_H + 7
    MAX_VISIBLE_ROWS = 4

    # Compact classic text-menu geometry used only by Genuine Panafonics.
    PANA_MENU_TOP = HEADER_H + 8
    PANA_ROW_HEIGHT = 29
    PANA_VISIBLE_ROWS = 4
    PANA_SCROLL_X1 = WIDTH - 34
    INFO_VISIBLE_LINES = 6
    INFO_ROW_TOP = HEADER_H + 5
    INFO_ROW_STEP = 25
    INFO_ROW_HEIGHT = 22
    FEEDBACK_DELAY = 0.07

    # Long menus get a dedicated touch scroll rail on the right.
    # This keeps scrolling separate from tapping a menu item.
    SCROLL_RAIL_W = 46
    SCROLL_RAIL_X1 = WIDTH - SCROLL_RAIL_W

    def __init__(self, device, touch=None, calibration=None):
        self.device = device
        self.touch = touch
        self.calibration = calibration
        self.touch_enabled = touch is not None and calibration is not None
        self.buzzer = FeedbackBuzzer()

        self.settings = load_settings()
        # Genuine Sorny with XS as its initial default. Apply
        # the new SMALL default once to existing installations, then leave
        # subsequent user font-size choices alone.
        if not self.settings.get("sorny_small_default_applied", False):
            self.settings["sorny_font_size_step"] = FONT_SIZE_DEFAULT
            self.settings["sorny_small_default_applied"] = True
            save_json_atomic(SETTINGS_FILE, self.settings)

        initial_theme = str(self.settings.get("ui_theme", "sorny")).lower()
        if initial_theme == "sorny":
            initial_font_step = self.settings.get("sorny_font_size_step", FONT_SIZE_DEFAULT)
        elif initial_theme == "panafonics":
            initial_font_step = self.settings.get(
                "panafonics_font_size_step",
                FONT_SIZE_DEFAULT,
            )
        elif initial_theme == "jayvc":
            initial_font_step = self.settings.get(
                "jayvc_font_size_step",
                FONT_SIZE_DEFAULT,
            )
        else:
            initial_font_step = self.settings.get(
                "default_font_size_step",
                self.settings.get("font_size_step", FONT_SIZE_DEFAULT),
            )
        apply_font_size_step(initial_font_step)
        self.error_log_entries = load_error_log()

        self.page = "home"
        self.selection = 0
        self.menu_scroll = 0
        self.menu_positions = {}
        self.bottom_focus = 0
        self.footer_feedback = None
        self.scroll_feedback = None
        self.header_feedback = None
        # Set briefly when Play on HDMI is requested without a connected sink.
        # draw_hdmi_icon() renders the normal HDMI marker in red during this state.
        self.hdmi_missing_flash = False

        # Genuine Sorny menu-wheel animation state.
        self.sorny_anim_anchor = None
        self.sorny_anim_shift = 0.0

        self.message = None
        self.message_until = 0.0

        self.recording = False
        self.record_started = None
        self.capture_process = None
        self.capture_log_handle = None
        self.capture_target = None

        # FireWire tape-deck session.  Tape mode keeps one interactive dvgrab
        # process alive so the touchscreen can drive AV/C transport commands
        # without starting a new process for every button press.
        self.dv_deck_process = None
        self.dv_deck_thread = None
        self.dv_deck_events = queue.SimpleQueue()
        self.dv_deck_transport = "OFFLINE"
        self.dv_deck_timecode = "--:--:--:--"
        self.dv_deck_recording = False
        self.dv_deck_record_pending = False
        self.dv_deck_session_prefix = None
        self.dv_tape_focus = 1
        self.dv_tape_feedback = None
        self.dv_status_checked = 0.0
        self.dv_status_thread = None
        self.dvcont_path = shutil.which("dvcont")
        self.dv_presence_checked = 0.0
        self.firewire_checked = 0.0
        self.firewire_controller_present = False
        self.firewire_camera_present = False

        # Cached description of the live DV payload currently arriving over
        # FireWire. Probing runs in a background thread so HOME rendering never
        # blocks waiting for a camera to emit a frame.
        self.dv_stream_standard = ""
        self.dv_stream_audio = ""
        self.dv_stream_scan = ""
        self.dv_scan_checked = 0.0
        self.dv_scan_thread = None
        self.dv_stream_checked = 0.0
        self.dv_stream_probe_thread = None
        self.dv_stream_probe_process = None
        # Stop/launch synchronisation: once the recorder asks for FireWire,
        # the HOME detector must not start its HDV fallback receiver.
        self.dv_stream_probe_cancel = threading.Event()
        self.dv_stream_probe_lock = threading.Lock()
        self.dv_transport_hint = ""
        self.dv_transport_checked = 0.0
        self.dv_transport_thread = None

        self.playback_process = None
        self.selected_file = None
        self.capture_filter = "ALL"
        self.storage_return_page = "menu"
        # Keep the file being browsed separate from the file currently loaded
        # in mpv. This avoids the playback screen changing its filename if the
        # user browses to another recording while HDMI playback continues.
        self.playback_file = None
        self.playback_position = 0.0
        self.playback_duration = 0.0
        self.playback_paused = False
        self.playback_status_checked = 0.0
        self.playback_focus = 1
        self.playback_feedback = None
        self.playback_source_fps = 0.0
        self.playback_source_width = 0
        self.playback_source_height = 0
        self.playback_output_refresh = 0.0
        self.playback_output_mode = "preferred"
        self.playback_interlaced = False
        self.playback_field_order = "unknown"
        self.playback_mode_candidates = ["preferred"]
        self.playback_mode_index = 0
        self.playback_base_command = None
        self.playback_started_at = 0.0

        # HDMI idle/status output.  Modern KMS/DRM hands the connector back to
        # the Linux console as soon as a DRM client exits, so a one-shot write
        # to /dev/fb0 is not reliable.  Keep a tiny mpv DRM client alive only
        # while the HDMI holding image is required; it is stopped before video
        # playback and restarted afterwards.
        self.hdmi_holding_signature = None
        self.hdmi_holding_process = None
        self.hdmi_detected = False
        self.hdmi_detect_checked = 0.0
        self.external_storage_cache = None
        self.external_storage_checked = 0.0

        self.info_title = "INFO"
        self.info_lines = []
        self.info_parent = "menu"
        self.info_scroll = 0
        # Per-row horizontal-scroll state for INFO/status screens. A tapped row
        # freezes at its current horizontal position; tapping it again resumes
        # from that exact point rather than jumping to another phase.
        self.info_scroll_paused = {}
        self.info_scroll_time_offsets = {}

        self.confirm_title = "CONFIRM"
        self.confirm_lines = []
        self.confirm_action = None
        self.confirm_parent = "menu"

        # USB analog capture is discovered dynamically. The original MS210x
        # path remains fully supported, but CamPi can also use other Linux V4L2
        # capture dongles and pair their ALSA audio interface automatically.
        self.analog_modes = []
        self.analog_controls = []
        self.analog_caps_checked = 0.0
        self.active_analog_control_name = None
        self.analog_video_device = None
        self.analog_device_name = ""
        self.analog_device_driver = ""
        self.analog_device_bus = ""
        self.analog_device_vidpid = ""
        self.analog_device_usb_root = ""
        self.analog_device_checked = 0.0
        self.analog_capture_candidates = []
        self.analog_audio_device = None
        self.analog_audio_name = ""
        self.analog_driver_standards = []

        # Network status is cached so the header can update live without
        # spawning nmcli on every 20 Hz render pass.
        self.network_state = {
            "lan_connected": False,
            "lan_iface": "",
            "lan_connection": "",
            "wifi_connected": False,
            "wifi_iface": "wlan0",
            "wifi_ssid": "",
            "wifi_signal": 0,
            "wifi_radio": False,
            "nmcli": shutil.which("nmcli") is not None,
        }
        self.network_checked = 0.0
        self.network_return_page = "system"
        # Power can be opened from System settings or directly from the HOME
        # CamPi header. BACK returns to whichever page launched the power menu.
        self.power_return_page = "system"
        self.network_refresh_feedback = ""
        self.network_refresh_feedback_until = 0.0
        self.wifi_networks = []
        self.wifi_scan_checked = 0.0
        self.wifi_target_ssid = ""
        self.wifi_target_security = ""
        self.wifi_password = ""
        self.wifi_password_visible = False
        self.keyboard_shift = False
        self.keyboard_symbols = False
        self.keyboard_index = 0

        self.refresh_analog_capabilities(force=True)
        self.validate_selected_analog_mode(save=False)
        self.apply_saved_analog_controls()

    # --------------------------------------------------------
    # SETTINGS
    # --------------------------------------------------------

    def save_settings(self):
        save_json_atomic(SETTINGS_FILE, self.settings)

    @property
    def capture_mode(self):
        return self.settings.get("capture_mode", "dv")

    @property
    def storage_policy(self):
        return self.settings.get("storage_policy", "auto")

    @property
    def buzzer_mode(self):
        value = str(self.settings.get("buzzer_mode", "short")).lower()
        return value if value in ("short", "long", "off") else "short"

    @property
    def ui_theme(self):
        value = str(self.settings.get("ui_theme", "sorny")).lower()
        return value if value in ("default", "sorny", "panafonics", "jayvc") else "sorny"

    def set_ui_theme(self, theme, save=True):
        theme = str(theme or "sorny").lower()
        if theme not in ("default", "sorny", "panafonics", "jayvc"):
            theme = "sorny"
        self.settings["ui_theme"] = theme
        self.sorny_anim_anchor = None
        self.sorny_anim_shift = 0.0
        self.menu_scroll = 0

        # Each theme remembers its own font-size preference.
        apply_font_size_step(self.font_size_step)

        if save:
            self.save_settings()
        return theme

    def theme_label(self):
        if self.ui_theme == "sorny":
            return "Genuine Sorny"
        if self.ui_theme == "panafonics":
            return "Genuine Panafonics"
        if self.ui_theme == "jayvc":
            return "Genuine JayVC"
        return "Basic"

    def _theme_font_size_key(self):
        if self.ui_theme == "sorny":
            return "sorny_font_size_step"
        if self.ui_theme == "panafonics":
            return "panafonics_font_size_step"
        if self.ui_theme == "jayvc":
            return "jayvc_font_size_step"
        return "default_font_size_step"

    @property
    def font_size_step(self):
        key = self._theme_font_size_key()
        fallback = FONT_SIZE_DEFAULT
        try:
            value = int(self.settings.get(key, fallback))
        except Exception:
            value = fallback
        return int(max(FONT_SIZE_MIN, min(FONT_SIZE_MAX, value)))

    def set_font_size_step(self, step, save=True):
        step = int(max(FONT_SIZE_MIN, min(FONT_SIZE_MAX, int(step))))
        key = self._theme_font_size_key()
        self.settings[key] = step
        # Mirror the current value into the legacy key for backwards
        # compatibility with older CamPi builds.
        self.settings["font_size_step"] = step
        apply_font_size_step(step)
        if save:
            self.save_settings()
        return step

    def adjust_font_size(self, direction):
        direction = 1 if int(direction) > 0 else -1
        new_step = int(max(FONT_SIZE_MIN, min(FONT_SIZE_MAX, self.font_size_step + direction)))
        if new_step != self.font_size_step:
            self.set_font_size_step(new_step, save=True)
        return new_step

    def cycle_storage_policy(self):
        order = ("auto", "internal", "ssd")
        current = self.storage_policy if self.storage_policy in order else "auto"
        next_policy = order[(order.index(current) + 1) % len(order)]
        self.settings["storage_policy"] = next_policy
        self.external_storage_checked = 0.0
        self.save_settings()
        self.show_message(storage_policy_label(next_policy), 1.8)
        self.refresh_hdmi_holding(force=True)
        return next_policy

    def refresh_hdmi_detection(self, force=False):
        now = time.monotonic()
        if not force and now - self.hdmi_detect_checked < 2.0:
            return self.hdmi_detected
        self.hdmi_detect_checked = now
        self.hdmi_detected = hdmi_display_connected()
        return self.hdmi_detected

    def append_error_log(self, text):
        message = " ".join(str(text).split()).strip()
        if not message:
            return
        entry = {
            "time": time.strftime("%d/%m %H:%M"),
            "message": message[:180],
        }
        self.error_log_entries.append(entry)
        self.error_log_entries = self.error_log_entries[-40:]
        save_error_log(self.error_log_entries)

    def firewire_camera_available(self, force=False):
        """Return True only when a remote FireWire node/camera is enumerated."""
        now = time.monotonic()
        if not force and now - self.firewire_checked < 0.75:
            return self.firewire_camera_present

        self.firewire_checked = now
        controller = False
        remote = False
        sysfs_flags_found = False

        # Prefer actual kernel is_local flags over assuming that the adapter
        # always enumerates at fw0 (which is not true with multiple hosts).
        try:
            for node in Path("/sys/bus/firewire/devices").glob("fw[0-9]*"):
                if not re.fullmatch(r"fw\d+", node.name):
                    continue
                local_flag = node / "is_local"
                if local_flag.exists():
                    sysfs_flags_found = True
                    value = local_flag.read_text(encoding="utf-8", errors="ignore").strip()
                    if value == "1":
                        controller = True
                    elif value == "0":
                        remote = True
        except Exception:
            pass

        # Older kernels may not expose is_local. With one FireWire host, the
        # typical fallback is fw0 (host) plus fw1+ (remote nodes).
        if not sysfs_flags_found:
            try:
                fw_numbers = sorted(
                    int(match.group(1))
                    for node in Path("/dev").glob("fw[0-9]*")
                    if (match := re.fullmatch(r"fw(\d+)", node.name))
                )
                controller = len(fw_numbers) >= 1
                remote = len(fw_numbers) >= 2
            except Exception:
                pass

        self.firewire_controller_present = controller
        self.firewire_camera_present = bool(controller and remote)
        if not self.firewire_camera_present:
            self.dv_stream_standard = ""
            self.dv_stream_audio = ""
            self.dv_stream_scan = ""
        return self.firewire_camera_present

    def _dv_frame_size_from_bytes(self, data):
        """Return raw SD-DV frame size from the DIF system flag."""
        if not data or len(data) < 4:
            return 0
        if len(data) >= 377 and data[0] == 0x47 and data[188] == 0x47 and data[376] == 0x47:
            return 0
        return 144000 if (data[3] & 0x80) else 120000

    def _detect_dv_scan_from_bytes(self, data):
        """Best-effort content scan: progressive vs interlaced SD DV.

        Consumer DV can carry progressively acquired pictures inside the normal
        525/60 or 625/50 DV transport, so the DV system bit alone cannot tell us
        whether the camera acquired the picture progressively. ffmpeg's idet
        filter examines several decoded frames and is used only as a content
        classifier. The raw master is never altered.
        """
        if not data or shutil.which("ffmpeg") is None:
            return ""

        frame_size = self._dv_frame_size_from_bytes(data)
        if frame_size <= 0 or len(data) < frame_size * 6:
            return ""

        sample = data[: frame_size * 12]
        try:
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel", "info",
                    "-f", "dv",
                    "-i", "pipe:0",
                    "-map", "0:v:0",
                    "-vf", "idet",
                    "-frames:v", "12",
                    "-an",
                    "-f", "null",
                    "-",
                ],
                input=sample,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=3.0,
            )
            output = result.stderr.decode("utf-8", errors="ignore")
            match = re.search(
                r"Multi frame detection:\s*"
                r"TFF:\s*(\d+)\s*"
                r"BFF:\s*(\d+)\s*"
                r"Progressive:\s*(\d+)\s*"
                r"Undetermined:\s*(\d+)",
                output,
                re.IGNORECASE,
            )
            if not match:
                return ""

            tff, bff, progressive, undetermined = map(int, match.groups())
            interlaced = tff + bff
            determined = interlaced + progressive
            if determined < 4:
                return ""

            if progressive >= 4 and progressive >= interlaced * 2:
                return "progressive"
            if interlaced >= 4 and interlaced >= progressive * 2:
                return "interlaced"
        except Exception:
            pass
        return ""

    def _schedule_dv_scan_analysis(self, data, force=False):
        """Analyse scan type off the UI thread when recording from FireWire."""
        if not data:
            return
        frame_size = self._dv_frame_size_from_bytes(data)
        if frame_size <= 0 or len(data) < frame_size * 6:
            return
        if self.dv_scan_thread is not None and self.dv_scan_thread.is_alive():
            return

        now = time.monotonic()
        if not force and now - self.dv_scan_checked < 3.0:
            return
        self.dv_scan_checked = now
        sample = bytes(data[: frame_size * 12])

        def worker():
            scan = self._detect_dv_scan_from_bytes(sample)
            if scan:
                self.dv_stream_scan = scan
                self._set_dv_stream_standard_from_bytes(sample, scan=scan)

        self.dv_scan_thread = threading.Thread(target=worker, daemon=True)
        self.dv_scan_thread.start()

    @staticmethod
    def _is_mpeg_ts_bytes(data):
        """Best-effort MPEG-TS detection allowing for a non-zero packet offset."""
        if not data or len(data) < 377:
            return False
        max_offset = min(188, len(data) - 376)
        for offset in range(max_offset):
            try:
                if (
                    data[offset] == 0x47
                    and data[offset + 188] == 0x47
                    and data[offset + 376] == 0x47
                ):
                    return True
            except Exception:
                break
        return False

    @staticmethod
    def _stream_rate(stream):
        """Return a normalised frame rate from an ffprobe stream mapping."""
        for key in ("avg_frame_rate", "r_frame_rate"):
            value = str(stream.get(key) or "0/0")
            try:
                if "/" in value:
                    num, den = value.split("/", 1)
                    den_f = float(den)
                    rate = float(num) / den_f if den_f else 0.0
                else:
                    rate = float(value)
            except (TypeError, ValueError, ZeroDivisionError):
                rate = 0.0
            rate = normalize_fps(rate)
            if rate > 0:
                return rate
        return 0.0

    @staticmethod
    def _hdv_mode_label(info, include_width=True):
        """Build a compact HDV raster/scan label from probed stream metadata."""
        try:
            width = int(info.get("width") or 0)
            height = int(info.get("height") or 0)
            fps = normalize_fps(float(info.get("fps") or 0.0))
        except Exception:
            width = 0
            height = 0
            fps = 0.0

        if height <= 0:
            return ""

        field_order = str(info.get("field_order") or "unknown").lower()
        interlaced = bool(info.get("interlaced"))

        # HDV720 is defined as progressive. HDV1080 can be interlaced or use
        # progressive modes carried by the same MPEG-2 transport stream.
        if height == 720:
            interlaced = False
            field_order = "progressive"

        if interlaced:
            cadence = fps * 2.0 if 0 < fps <= 35.0 else fps
            scan = "i"
        elif field_order == "progressive" or height == 720:
            cadence = fps
            scan = "p"
        else:
            cadence = fps
            scan = ""

        rate_text = format_fps(cadence) if cadence > 0 else ""
        raster = f"{height}{scan}{rate_text}" if scan else f"{height}"
        if include_width and width > 0:
            raster = f"{width}x{raster}"
        return raster

    def _probe_hdv_video_from_bytes(self, data):
        """Probe HDV MPEG-2 TS video metadata from an incoming FireWire sample."""
        info = {
            "codec_name": "",
            "width": 0,
            "height": 0,
            "fps": 0.0,
            "field_order": "unknown",
            "interlaced": False,
        }
        probe = shutil.which("ffprobe")
        if not probe or not self._is_mpeg_ts_bytes(data):
            return info

        try:
            result = subprocess.run(
                [
                    probe,
                    "-v", "error",
                    "-f", "mpegts",
                    "-i", "pipe:0",
                    "-select_streams", "v:0",
                    "-show_entries",
                    "stream=codec_name,width,height,avg_frame_rate,r_frame_rate,field_order",
                    "-of", "json",
                ],
                input=data,
                capture_output=True,
                timeout=1.8,
            )
            if result.returncode != 0:
                return info

            payload = json.loads(
                result.stdout.decode("utf-8", errors="ignore") or "{}"
            )
            streams = payload.get("streams") or []
            if not streams:
                return info

            stream = streams[0]
            info["codec_name"] = str(stream.get("codec_name") or "").lower()
            info["width"] = int(stream.get("width") or 0)
            info["height"] = int(stream.get("height") or 0)
            info["fps"] = self._stream_rate(stream)
            info["field_order"] = str(
                stream.get("field_order") or "unknown"
            ).lower()

            if info["height"] == 720:
                info["field_order"] = "progressive"
                info["interlaced"] = False
            elif info["field_order"] == "progressive":
                info["interlaced"] = False
            elif info["field_order"] not in ("unknown", ""):
                info["interlaced"] = True
            elif info["height"] == 1080:
                # HDV1080 is normally the interlaced family. Progressive
                # variants are expected to be identified by ffprobe above.
                info["interlaced"] = True
                info["field_order"] = "tff"

        except Exception:
            pass
        return info

    def dv_stream_is_hdv(self):
        return str(self.dv_stream_standard or "").upper().startswith("HDV")

    def _probe_dv_stream_sync(self, timeout=2.0, frames=12):
        """Synchronously identify DV vs HDV immediately before capture."""
        if self.recording or self.dv_deck_running():
            return bool(self.dv_stream_standard)

        dvgrab = shutil.which("dvgrab")
        if not dvgrab:
            return False

        if not self._stop_dv_stream_probe():
            return False
        process = None
        data = b""
        try:
            def collect_probe(extra_args=None, probe_timeout=None):
                nonlocal process
                command = [
                    dvgrab,
                    "-noavc",
                    "-frames", str(max(2, int(frames))),
                ]
                if extra_args:
                    command.extend(extra_args)
                # A trailing '-' asks dvgrab to send the incoming native DV or
                # HDV payload to stdout. Do not force raw here: doing so can
                # misclassify an HDV MPEG-2 TS source as absent DV.
                command.append("-")
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                payload = b""
                try:
                    payload, _ = process.communicate(
                        timeout=float(probe_timeout or timeout)
                    )
                except subprocess.TimeoutExpired as exc:
                    if exc.output:
                        payload = exc.output
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                        more, _ = process.communicate(timeout=0.4)
                        if more:
                            payload = more if len(more) >= len(payload) else payload + more
                    except Exception:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                            process.wait(timeout=0.4)
                        except Exception:
                            pass
                return payload

            # First let dvgrab identify the native FireWire payload itself.
            data = collect_probe()

            # Some HDV devices are reluctant to yield MPEG-2 TS data until the
            # HDV receiver is selected explicitly. Retry as HDV before giving
            # up and incorrectly falling back to SD-DV capture.
            if not data:
                data = collect_probe(["-format", "hdv"], probe_timeout=max(2.0, float(timeout)))

            if not data:
                return False

            scan = ""
            if not self._is_mpeg_ts_bytes(data):
                scan = self._detect_dv_scan_from_bytes(data)
                if scan:
                    self.dv_stream_scan = scan

            detected = self._set_dv_stream_standard_from_bytes(
                data, scan=scan or None
            )
            self._set_dv_audio_from_bytes(data)
            return detected
        except Exception:
            return False
        finally:
            if process is not None and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except Exception:
                    pass

    def _set_dv_stream_standard_from_bytes(self, data, scan=None):
        """Inspect raw DV/HDV bytes and cache a compact HOME source label."""
        if not data:
            return False

        # HDV is MPEG-2 transport stream data. Probe the MPEG-2 video stream so
        # HOME can report the same kind of raster/scan detail used for DV.
        if self._is_mpeg_ts_bytes(data):
            info = self._probe_hdv_video_from_bytes(data)
            mode = self._hdv_mode_label(info, include_width=True)
            self.dv_stream_standard = f"HDV {mode}" if mode else "HDV MPEG-2 TS"

            if info.get("interlaced"):
                self.dv_stream_scan = "interlaced"
            elif str(info.get("field_order") or "").lower() == "progressive":
                self.dv_stream_scan = "progressive"
            else:
                # Prevent repeated SD-DV scan attempts against MPEG-TS if the
                # short sample was sufficient to identify HDV but not scan type.
                self.dv_stream_scan = "unknown"
            return True

        if len(data) < 4:
            return False

        scan = str(scan or self.dv_stream_scan or "").lower()
        if scan in ("progressive", "interlaced"):
            self.dv_stream_scan = scan

        # IEC 61834 identifies the 625/50 vs 525/60 DV system, but that does
        # not by itself identify progressive acquisition. Progressive modes on
        # some consumer Handycams are carried inside the normal DV system.
        if data[3] & 0x80:
            if scan == "progressive":
                self.dv_stream_standard = "PAL DV 720x576p25"
            else:
                self.dv_stream_standard = "PAL DV 720x576i50"
        else:
            if scan == "progressive":
                self.dv_stream_standard = "NTSC DV 720x480p29.97"
            else:
                self.dv_stream_standard = "NTSC DV 720x480i59.94"
        return True

    def _set_dv_audio_from_bytes(self, data):
        """Probe DV/HDV audio characteristics from a short incoming sample."""
        if not data or shutil.which("ffprobe") is None:
            return False
        try:
            is_ts = self._is_mpeg_ts_bytes(data)
            command = [
                "ffprobe", "-v", "error",
                "-f", "mpegts" if is_ts else "dv",
                "-i", "pipe:0",
                "-select_streams", "a:0",
                "-show_entries",
                "stream=codec_name,sample_rate,channels,bits_per_sample,bits_per_raw_sample,sample_fmt",
                "-of", "json",
            ]
            result = subprocess.run(
                command, input=data, capture_output=True, timeout=1.5
            )
            if result.returncode != 0:
                return False
            payload = json.loads(result.stdout.decode("utf-8", errors="ignore") or "{}")
            streams = payload.get("streams") or []
            if not streams:
                return False
            stream = streams[0]
            rate = int(float(stream.get("sample_rate") or 0))
            channels = int(stream.get("channels") or 0)
            bits = int(stream.get("bits_per_raw_sample") or stream.get("bits_per_sample") or 0)
            # ffprobe commonly decodes DV audio to s16 and leaves the encoded
            # bit-depth fields at zero. Use the standard DV audio modes only
            # when the sample rate makes the original mode unambiguous.
            if bits <= 0:
                if rate == 48000:
                    bits = 16
                elif rate == 32000:
                    bits = 12
            if rate <= 0:
                return False
            rate_text = f"{rate / 1000:g} kHz"
            bit_text = f" {bits}-bit" if bits > 0 else ""
            if channels == 1:
                channel_text = " mono"
            elif channels == 2:
                channel_text = " stereo"
            elif channels > 2:
                channel_text = f" {channels}ch"
            else:
                channel_text = ""
            codec_name = str(stream.get("codec_name") or "").lower()
            codec_text = ""
            if is_ts:
                codec_names = {
                    "mp2": "MP2",
                    "mp3": "MP3",
                    "aac": "AAC",
                    "pcm_s16be": "PCM",
                    "pcm_s16le": "PCM",
                }
                codec_text = codec_names.get(codec_name, codec_name.upper())
                codec_text = f"{codec_text} " if codec_text else ""

            self.dv_stream_audio = (
                f"{codec_text}{rate_text}{bit_text}{channel_text}"
            )
            return True
        except Exception:
            return False

    def dv_standard_slug(self):
        """Return a compact filename slug for the detected DV/HDV standard."""
        value = str(self.dv_stream_standard or "").upper()

        if value.startswith("HDV"):
            # Keep HDV filenames as simple as the existing PAL/NTSC DV names.
            # The detailed cadence remains available in the UI/recording info,
            # but filenames only need the familiar scan format, e.g. 1080i or
            # 720p, rather than 1080i59p94.
            match = re.search(r"X(720|1080)([IP]?)", value)
            if match:
                height, scan = match.groups()
                return f"hdv_{height}{scan.lower()}".rstrip("_")
            return "hdv"

        if "PAL" in value or "576" in value:
            return "pal"
        if "NTSC" in value or "480" in value:
            return "ntsc"
        return ""

    def _stop_dv_stream_probe(self):
        """Cancel HOME probing and wait until its FireWire reader is gone.

        The probe retries as HDV when automatic format detection returns no
        bytes. Just terminating the currently visible Popen is not sufficient:
        its worker could launch the fallback Popen after the caller has already
        started the real recorder. Cancellation and Popen publication are
        guarded by the same lock to eliminate that race.
        """
        with self.dv_stream_probe_lock:
            self.dv_stream_probe_cancel.set()
            process = self.dv_stream_probe_process
            thread = self.dv_stream_probe_thread

        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=0.9)
            except Exception:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=0.8)
                except Exception:
                    pass

        if thread is not None and thread is not threading.current_thread():
            # A worker may still be probing MPEG metadata with ffprobe after
            # dvgrab exits. Joining ensures it cannot relaunch a reader.
            try:
                thread.join(timeout=3.0)
            except Exception:
                pass

            if thread.is_alive():
                return False

        with self.dv_stream_probe_lock:
            if self.dv_stream_probe_thread is thread:
                self.dv_stream_probe_thread = None
            if self.dv_stream_probe_process is process:
                self.dv_stream_probe_process = None
        # Do not clear the cancellation flag here. It is cleared only when the
        # idle HOME renderer intentionally starts a new detection cycle.
        return True

    def _probe_dv_stream_worker(self):
        """Sample live FireWire data; stop cleanly when REC takes ownership."""
        process = None
        data = b""
        try:
            dvgrab = shutil.which("dvgrab")
            if not dvgrab:
                return

            def collect_probe(command, timeout_seconds):
                nonlocal process
                # Atomic cancellation check + process launch/publication:
                # _stop_dv_stream_probe() cannot miss a newly created reader.
                with self.dv_stream_probe_lock:
                    if (
                        self.dv_stream_probe_cancel.is_set()
                        or self.recording
                        or self.capture_process is not None
                        or self.dv_deck_running()
                    ):
                        return b""
                    process = subprocess.Popen(
                        command,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                    self.dv_stream_probe_process = process

                payload = b""
                try:
                    payload, _ = process.communicate(timeout=timeout_seconds)
                except subprocess.TimeoutExpired as exc:
                    if exc.output:
                        payload = exc.output
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                        more, _ = process.communicate(timeout=0.45)
                        if more:
                            payload = more if len(more) >= len(payload) else payload + more
                    except Exception:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                            more, _ = process.communicate(timeout=0.3)
                            if more:
                                payload = more if len(more) >= len(payload) else payload + more
                        except Exception:
                            pass
                finally:
                    # Reap the reader before the fallback can claim the same
                    # FireWire channel; the canceled path is never retried.
                    if process.poll() is None:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                            process.wait(timeout=0.5)
                        except Exception:
                            pass
                    with self.dv_stream_probe_lock:
                        if self.dv_stream_probe_process is process:
                            self.dv_stream_probe_process = None
                return payload

            data = collect_probe(
                [dvgrab, "-noavc", "-frames", "12", "-"],
                2.2,
            )

            # Only try the HDV-specific fallback if this probe was allowed to
            # complete normally. Never restart dvgrab after REC cancelled us.
            if not data and not self.dv_stream_probe_cancel.is_set():
                data = collect_probe(
                    [dvgrab, "-noavc", "-frames", "12", "-format", "hdv", "-"],
                    2.4,
                )

            if data and not self.dv_stream_probe_cancel.is_set():
                scan = self._detect_dv_scan_from_bytes(data)
                if scan:
                    self.dv_stream_scan = scan
                self._set_dv_stream_standard_from_bytes(data, scan=scan)
                if not self.dv_stream_audio:
                    self._set_dv_audio_from_bytes(data)

        except Exception:
            pass
        finally:
            if process is not None and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=0.6)
                except Exception:
                    pass
            with self.dv_stream_probe_lock:
                if self.dv_stream_probe_process is process:
                    self.dv_stream_probe_process = None
                if self.dv_stream_probe_thread is threading.current_thread():
                    self.dv_stream_probe_thread = None

    def _refresh_dv_standard_from_capture(self):
        """Inspect the growing DV or HDV recording without touching FireWire."""
        if self.capture_mode != "dv" or not self.capture_target:
            return False
        try:
            prefix = Path(str(self.capture_target))
            candidates = []
            if prefix.is_file():
                candidates.append(prefix)
            else:
                for extension in ("*.dv", "*.m2t", "*.m2ts", "*.ts"):
                    candidates.extend(prefix.parent.glob(prefix.name + extension))
            if not candidates:
                return False

            newest = max(candidates, key=lambda item: item.stat().st_mtime)
            with open(newest, "rb") as fh:
                data = fh.read(3145728)

            detected = self._set_dv_stream_standard_from_bytes(data)
            if not self.dv_stream_audio:
                self._set_dv_audio_from_bytes(data)

            if not self._is_mpeg_ts_bytes(data) and not self.dv_stream_scan:
                self._schedule_dv_scan_analysis(data)
            return detected
        except Exception:
            return False

    def refresh_dv_stream_standard(self, force=False):
        """Refresh the HOME FireWire video-standard label without blocking the UI."""
        if self.capture_mode != "dv":
            return self.dv_stream_standard

        if not self.firewire_camera_available():
            self.dv_stream_standard = ""
            return ""

        # Never launch a second FireWire receiver during an active recording.
        # The growing raw-DV file contains the same system flag and is safer to
        # inspect locally.
        if self.recording:
            self._refresh_dv_standard_from_capture()
            return self.dv_stream_standard

        # Likewise, do not compete with the interactive tape-deck dvgrab process.
        if self.dv_deck_running():
            return self.dv_stream_standard

        now = time.monotonic()
        if not force and now - self.dv_stream_checked < 2.0:
            return self.dv_stream_standard
        self.dv_stream_checked = now

        with self.dv_stream_probe_lock:
            if self.dv_stream_probe_thread is not None and self.dv_stream_probe_thread.is_alive():
                return self.dv_stream_standard
            # Never run a background receiver during a real dvgrab startup.
            if self.capture_process is not None:
                return self.dv_stream_standard
            self.dv_stream_probe_cancel.clear()
            self.dv_stream_probe_thread = threading.Thread(
                target=self._probe_dv_stream_worker,
                daemon=True,
            )
            self.dv_stream_probe_thread.start()
        return self.dv_stream_standard

    def _normalise_dv_transport_hint(self, status):
        """Convert dvcont AV/C status into a compact HOME tape-mode hint."""
        value = str(status or "").strip()
        lower = value.lower()
        if not value or "config rom" in lower or "error" in lower:
            return ""
        if "pause" in lower:
            return "TAPE PLAYBACK: PAUSED"
        if "rewind" in lower or "reverse" in lower:
            return "TAPE PLAYBACK: REW"
        if "forward" in lower or re.search(r"\bff\b", lower):
            return "TAPE PLAYBACK: FF"
        if "play" in lower:
            return "TAPE PLAYBACK: PLAYING"
        if "stop" in lower:
            return "TAPE PLAYBACK: STOPPED"
        # Recording/camera-mode statuses are not tape-playback hints.
        return ""

    def refresh_dv_transport_hint(self, force=False):
        """Poll AV/C transport state without interfering with live DV capture."""
        if self.capture_mode != "dv" or not self.dvcont_path:
            self.dv_transport_hint = ""
            return ""
        if not self.firewire_camera_available():
            self.dv_transport_hint = ""
            return ""
        if self.recording or self.dv_deck_running():
            return self.dv_transport_hint

        now = time.monotonic()
        if not force and now - self.dv_transport_checked < 2.0:
            return self.dv_transport_hint
        self.dv_transport_checked = now

        if self.dv_transport_thread is not None and self.dv_transport_thread.is_alive():
            return self.dv_transport_hint

        def worker():
            try:
                result = subprocess.run(
                    [self.dvcont_path, "status"],
                    capture_output=True,
                    text=True,
                    timeout=1.5,
                )
                # Only stdout is meaningful here. stderr can contain harmless
                # CONFIG ROM discovery chatter which CamPi deliberately hides.
                if result.returncode == 0:
                    self.dv_transport_hint = self._normalise_dv_transport_hint(result.stdout)
                else:
                    self.dv_transport_hint = ""
            except Exception:
                self.dv_transport_hint = ""
            finally:
                self.dv_transport_thread = None

        self.dv_transport_thread = threading.Thread(target=worker, daemon=True)
        self.dv_transport_thread.start()
        return self.dv_transport_hint

    def selected_source_available(self):
        if self.capture_mode == "dv":
            return shutil.which("dvgrab") is not None and self.firewire_camera_available()
        device = self.discover_analog_video_device()
        return bool(device and os.path.exists(device))

    def selected_source_offline_text(self):
        if self.capture_mode != "dv":
            return "DEVICE MISSING"
        if shutil.which("dvgrab") is None:
            return "DVGRAB MISSING"
        self.firewire_camera_available()
        if not self.firewire_controller_present:
            return "FIREWIRE MISSING"
        if not self.firewire_camera_present:
            return "CAMERA OFFLINE"
        return "DV INPUT UNAVAILABLE"

    @property
    def analog_standard(self):
        value = str(self.settings.get("analog_standard", "PAL")).upper()
        return value if value in ("PAL", "NTSC") else "PAL"

    # --------------------------------------------------------
    # ANALOG CAPABILITIES / SETTINGS
    # --------------------------------------------------------

    @staticmethod
    def _sysfs_text(path):
        try:
            return Path(path).read_text(encoding="utf-8", errors="ignore").strip()
        except Exception:
            return ""

    @staticmethod
    def _find_usb_device_root(path):
        """Return the physical USB device directory containing idVendor/idProduct."""
        try:
            resolved = Path(path).resolve()
        except Exception:
            return None
        for candidate in (resolved, *resolved.parents):
            try:
                if (candidate / "idVendor").exists() and (candidate / "idProduct").exists():
                    return candidate
            except Exception:
                continue
        return None

    def _video_node_usb_info(self, node):
        node = Path(node)
        class_path = Path("/sys/class/video4linux") / node.name
        name = self._sysfs_text(class_path / "name") or node.name
        device_link = class_path / "device"
        usb_root = self._find_usb_device_root(device_link)

        driver = ""
        bus = ""
        vidpid = ""
        manufacturer = ""
        product = ""

        try:
            resolved = device_link.resolve()
            driver_link = resolved / "driver"
            if driver_link.exists():
                driver = driver_link.resolve().name
        except Exception:
            pass

        if usb_root is not None:
            vendor = self._sysfs_text(usb_root / "idVendor")
            product_id = self._sysfs_text(usb_root / "idProduct")
            if vendor and product_id:
                vidpid = f"{vendor}:{product_id}"
            manufacturer = self._sysfs_text(usb_root / "manufacturer")
            product = self._sysfs_text(usb_root / "product")
            bus = usb_root.name

        return {
            "path": str(node),
            "name": name,
            "driver": driver,
            "bus": bus,
            "vidpid": vidpid,
            "manufacturer": manufacturer,
            "product": product,
            "usb_root": str(usb_root) if usb_root is not None else "",
        }

    def _probe_v4l2_modes(self, device):
        if not device or shutil.which("v4l2-ctl") is None:
            return []
        try:
            result = subprocess.run(
                ["v4l2-ctl", "-d", str(device), "--list-formats-ext"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if result.returncode == 0:
                return parse_v4l2_formats(result.stdout)
        except Exception:
            pass
        return []

    def _analog_candidate_score(self, info, modes):
        """Prefer USB devices whose advertised modes resemble analog video capture."""
        score = 0
        name = " ".join(
            str(info.get(key, ""))
            for key in ("name", "manufacturer", "product")
        ).lower()

        if info.get("usb_root"):
            score += 100

        positive_tokens = (
            "capture", "grabber", "easycap", "easy cap", "ms210",
            "macrosilicon", "dazzle", "empia", "em28", "stk1160",
            "usbtv", "video grab", "av to usb", "usb video",
        )
        negative_tokens = ("webcam", "web cam", "conference", "integrated camera")
        if any(token in name for token in positive_tokens):
            score += 90
        if any(token in name for token in negative_tokens):
            score -= 100

        if self.analog_video_device and info.get("path") == self.analog_video_device:
            score += 25

        heights = {int(mode.get("height", 0)) for mode in modes}
        rasters = {
            (int(mode.get("width", 0)), int(mode.get("height", 0)))
            for mode in modes
        }
        if 576 in heights or 480 in heights:
            score += 90
        if (720, 576) in rasters or (720, 480) in rasters:
            score += 90

        max_pixels = max(
            (int(mode.get("width", 0)) * int(mode.get("height", 0)) for mode in modes),
            default=0,
        )
        if 0 < max_pixels <= 720 * 576:
            score += 35
        elif max_pixels > 1920 * 1080:
            score -= 20

        # Multiple SD broadcast cadences are a strong capture-dongle clue.
        rates = {round(float(mode.get("fps", 0.0)), 2) for mode in modes}
        if any(abs(rate - 25.0) < 0.2 or abs(rate - 50.0) < 0.2 for rate in rates):
            score += 15
        if any(abs(rate - 29.97) < 0.2 or abs(rate - 59.94) < 0.2 for rate in rates):
            score += 15

        return score

    def discover_analog_video_device(self, force=False):
        """Auto-select a usable USB V4L2 capture node, preferring analog-like modes."""
        now = time.monotonic()
        if (
            not force
            and now - self.analog_device_checked < 2.0
            and self.analog_video_device
            and os.path.exists(self.analog_video_device)
        ):
            return self.analog_video_device

        self.analog_device_checked = now
        candidates = []

        def node_sort(path):
            match = re.search(r"(\d+)$", path.name)
            return int(match.group(1)) if match else 9999

        for node in sorted(Path("/dev").glob("video[0-9]*"), key=node_sort):
            info = self._video_node_usb_info(node)

            # CamPi is specifically looking for external capture hardware.
            # USB video nodes are preferred; a compatibility fallback is used
            # below only when no USB-backed capture node can be identified.
            if not info.get("usb_root"):
                continue

            modes = self._probe_v4l2_modes(node)
            if not modes:
                continue

            info["modes"] = modes
            info["score"] = self._analog_candidate_score(info, modes)
            candidates.append(info)

        # Preserve compatibility for systems where sysfs does not expose the
        # USB parent correctly but /dev/video0 remains a valid capture device.
        if not candidates and os.path.exists(ANALOG_VIDEO_DEVICE):
            modes = self._probe_v4l2_modes(ANALOG_VIDEO_DEVICE)
            if modes:
                info = self._video_node_usb_info(ANALOG_VIDEO_DEVICE)
                info["modes"] = modes
                info["score"] = self._analog_candidate_score(info, modes)
                candidates.append(info)

        self.analog_capture_candidates = list(candidates)

        if not candidates:
            changed = self.analog_video_device is not None
            self.analog_video_device = None
            self.analog_device_name = ""
            self.analog_device_driver = ""
            self.analog_device_bus = ""
            self.analog_device_vidpid = ""
            self.analog_device_usb_root = ""
            self.analog_audio_device = None
            self.analog_audio_name = ""
            if changed:
                self.analog_modes = []
                self.analog_controls = []
                self.analog_driver_standards = []
            return None

        chosen = max(candidates, key=lambda item: (item["score"], -node_sort(Path(item["path"]))))
        changed = chosen["path"] != self.analog_video_device

        self.analog_video_device = chosen["path"]
        self.analog_device_name = chosen.get("product") or chosen.get("name") or Path(chosen["path"]).name
        self.analog_device_driver = chosen.get("driver") or ""
        self.analog_device_bus = chosen.get("bus") or ""
        self.analog_device_vidpid = chosen.get("vidpid") or ""
        self.analog_device_usb_root = chosen.get("usb_root") or ""

        if changed:
            self.analog_audio_device = None
            self.analog_audio_name = ""
            self.analog_controls = []
            self.analog_driver_standards = []

        # Reuse the discovery probe rather than immediately spawning another
        # format query for the same node.
        self.analog_modes = list(chosen.get("modes") or [])
        return self.analog_video_device

    def _query_analog_driver_standards(self, device):
        """Return named PAL/NTSC standards when the V4L2 driver exposes them."""
        if not device or shutil.which("v4l2-ctl") is None:
            return []
        try:
            result = subprocess.run(
                ["v4l2-ctl", "-d", str(device), "--list-standards"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if result.returncode != 0:
                return []
            standards = []
            for line in result.stdout.splitlines():
                match = re.search(r"^\s*Name\s*:\s*(.+?)\s*$", line, re.IGNORECASE)
                if match:
                    name = match.group(1).strip()
                    if name and name not in standards:
                        standards.append(name)
            return standards
        except Exception:
            return []

    def _apply_driver_analog_standard(self, standard):
        """Best-effort VIDIOC_S_STD for classic analog capture drivers."""
        device = self.analog_video_device
        if not device or shutil.which("v4l2-ctl") is None:
            return False

        standard = str(standard or "").upper()
        if standard not in ("PAL", "NTSC"):
            return False

        if not self.analog_driver_standards:
            self.analog_driver_standards = self._query_analog_driver_standards(device)
        if not self.analog_driver_standards:
            return False

        matching = [
            item for item in self.analog_driver_standards
            if standard in item.upper()
        ]
        attempts = [standard] + matching
        seen = set()

        for target in attempts:
            if target in seen:
                continue
            seen.add(target)
            try:
                result = subprocess.run(
                    ["v4l2-ctl", "-d", str(device), f"--set-standard={target}"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=2,
                )
                if result.returncode == 0:
                    return True
            except Exception:
                pass
        return False

    def refresh_analog_capabilities(self, force=False):
        now = time.monotonic()
        if not force and now - self.analog_caps_checked < 5.0:
            return
        self.analog_caps_checked = now

        device = self.discover_analog_video_device(force=force)
        if not device:
            self.analog_modes = []
            self.analog_controls = []
            self.analog_driver_standards = []
            return

        modes = self._probe_v4l2_modes(device)
        if modes:
            self.analog_modes = modes

        try:
            result = subprocess.run(
                ["v4l2-ctl", "-d", device, "--list-ctrls-menus"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if result.returncode == 0:
                self.analog_controls = parse_v4l2_controls(result.stdout)
            else:
                self.analog_controls = []
        except Exception:
            self.analog_controls = []

        self.analog_driver_standards = self._query_analog_driver_standards(device)
        self.find_analog_audio_device(force=force)

    def stored_analog_mode(self):
        return {
            "pixel_format": str(self.settings.get("analog_pixel_format", "MJPG")),
            "description": "",
            "width": int(self.settings.get("analog_width", 720)),
            "height": int(self.settings.get("analog_height", 576)),
            "fps": float(self.settings.get("analog_fps", 50.0)),
        }

    def analog_mode_key(self, mode):
        return (
            str(mode.get("pixel_format", "")),
            int(mode.get("width", 0)),
            int(mode.get("height", 0)),
            round(float(mode.get("fps", 0.0)), 3),
        )

    def mode_matches_standard(self, mode, standard=None):
        standard = (standard or self.analog_standard).upper()
        height = int(mode.get("height", 0))
        fps = float(mode.get("fps", 0.0))

        # Known SD raster wins over cadence so a 480-line mode can never be
        # presented as PAL and a 576-line mode can never be presented as NTSC.
        if height == 576:
            return standard == "PAL"
        if height == 480:
            return standard == "NTSC"

        if standard == "PAL":
            return abs(fps - 25.0) < 0.1 or abs(fps - 50.0) < 0.1

        return (
            abs(fps - 29.97) < 0.1
            or abs(fps - 30.0) < 0.1
            or abs(fps - 59.94) < 0.1
            or abs(fps - 60.0) < 0.1
        )

    def analog_standard_for_mode(self, mode):
        """Infer PAL/NTSC when a UVC mode clearly belongs to one family."""
        try:
            height = int(mode.get("height", 0))
            fps = float(mode.get("fps", 0.0))
        except Exception:
            return None

        # Raster is the strongest signal for SD capture modes.
        if height == 576:
            return "PAL"
        if height == 480:
            return "NTSC"

        # Fall back to cadence for modes whose raster is not decisive.
        if any(abs(fps - rate) < 0.15 for rate in (25.0, 50.0)):
            return "PAL"
        if any(abs(fps - rate) < 0.15 for rate in (29.97, 30.0, 59.94, 60.0)):
            return "NTSC"
        return None

    def best_mode_for_standard(self, standard=None):
        standard = (standard or self.analog_standard).upper()
        candidates = [mode for mode in self.analog_modes if self.mode_matches_standard(mode, standard)]
        if not candidates:
            candidates = list(self.analog_modes)
        if not candidates:
            return self.stored_analog_mode()

        target_height = 576 if standard == "PAL" else 480
        target_fps = 50.0 if standard == "PAL" else 59.94

        def score(mode):
            width = int(mode.get("width", 0))
            height = int(mode.get("height", 0))
            fps = float(mode.get("fps", 0.0))
            return (
                1000 if height == target_height else 0,
                500 if width == 720 else 0,
                -abs(fps - target_fps),
                width * height,
                fps,
            )

        return max(candidates, key=score)

    @staticmethod
    def analog_mode_standard_label(mode):
        """Infer PAL/NTSC from a reported V4L2 mode for filenames/display only."""
        try:
            height = int(mode.get("height", 0))
            fps = float(mode.get("fps", 0.0))
        except Exception:
            return ""

        # Raster is the strongest signal for ordinary SD analog capture.
        if height == 576:
            return "PAL"
        if height == 480:
            return "NTSC"

        # Fall back to the familiar broadcast-rate families for devices that
        # expose unusual cropped or scaled analog modes.
        if any(abs(fps - value) < 0.35 for value in (25.0, 50.0)):
            return "PAL"
        if any(abs(fps - value) < 0.35 for value in (29.97, 30.0, 59.94, 60.0)):
            return "NTSC"
        return ""

    def best_reported_analog_mode(self):
        """Choose a sensible full-rate mode only from modes reported by the dongle."""
        if not self.analog_modes:
            return self.stored_analog_mode()

        def score(mode):
            width = int(mode.get("width", 0))
            height = int(mode.get("height", 0))
            fps = float(mode.get("fps", 0.0))
            pixel_format = str(mode.get("pixel_format", "")).upper()
            full_sd = 1 if width == 720 and height in (480, 576) else 0
            uncompressed = 1 if pixel_format in ("YUYV", "YUY2", "UYVY") else 0
            return (full_sd, width * height, fps, uncompressed)

        return max(self.analog_modes, key=score)

    def selected_analog_mode(self):
        stored = self.stored_analog_mode()
        stored_key = self.analog_mode_key(stored)
        for mode in self.analog_modes:
            if self.analog_mode_key(mode) == stored_key:
                return mode
        return stored

    def set_selected_analog_mode(self, mode, save=True):
        self.settings["analog_pixel_format"] = str(mode["pixel_format"])
        self.settings["analog_width"] = int(mode["width"])
        self.settings["analog_height"] = int(mode["height"])
        self.settings["analog_fps"] = float(mode["fps"])
        if save:
            self.save_settings()

    def validate_selected_analog_mode(self, save=True):
        if not self.analog_modes:
            return
        current_key = self.analog_mode_key(self.stored_analog_mode())
        for mode in self.analog_modes:
            if self.analog_mode_key(mode) == current_key:
                return
        self.set_selected_analog_mode(self.best_reported_analog_mode(), save=save)


    def analog_mode_label(self, mode, include_format=True):
        label = f"{mode['width']}x{mode['height']}p{format_fps(mode['fps'])}"
        if include_format:
            label += f" {mode['pixel_format']}"
        return label

    def set_analog_standard(self, standard):
        standard = standard.upper()
        if standard not in ("PAL", "NTSC"):
            return
        self.settings["analog_standard"] = standard
        self.discover_analog_video_device(force=True)
        self._apply_driver_analog_standard(standard)
        self.refresh_analog_capabilities(force=True)
        self.set_selected_analog_mode(self.best_mode_for_standard(standard), save=False)
        self.save_settings()

    def find_analog_control(self, name):
        for control in self.analog_controls:
            if control.get("name") == name:
                return control
        return None

    def set_analog_control(self, name, value, persist=True):
        control = self.find_analog_control(name)
        if control is None:
            return False

        value = int(clamp(int(value), int(control["min"]), int(control["max"])))
        device = self.discover_analog_video_device()
        if not device:
            return False
        try:
            result = subprocess.run(
                [
                    "v4l2-ctl",
                    "-d", device,
                    "--set-ctrl", f"{name}={value}",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
            if result.returncode != 0:
                return False
        except Exception:
            return False

        control["value"] = value
        if persist:
            saved = self.settings.setdefault("analog_controls", {})
            saved[name] = value
            self.save_settings()
        return True

    def apply_saved_analog_controls(self):
        saved = self.settings.get("analog_controls", {})
        if not isinstance(saved, dict) or not self.analog_controls:
            return
        for name, value in saved.items():
            self.set_analog_control(name, value, persist=False)

    def reset_analog_controls(self):
        for control in self.analog_controls:
            self.set_analog_control(control["name"], control["default"], persist=False)
        self.settings["analog_controls"] = {}
        self.save_settings()
        self.refresh_analog_capabilities(force=True)

    # --------------------------------------------------------
    # STORAGE
    # --------------------------------------------------------

    def external_storage_info(self, force=False):
        """Discover mounted or present USB block storage without polling lsblk every frame."""
        now = time.monotonic()
        if (
            not force
            and self.external_storage_cache is not None
            and now - self.external_storage_checked < 2.0
        ):
            return dict(self.external_storage_cache)

        self.external_storage_checked = now
        info = {
            "present": False,
            "mounted": False,
            "source": None,
            "mountpoint": None,
            "label": "",
            "writable": False,
        }
        candidates = []

        try:
            result = subprocess.run(
                [
                    "lsblk", "-J", "-o",
                    "NAME,PATH,TYPE,TRAN,RM,ROTA,MOUNTPOINTS,FSTYPE,LABEL,RO",
                ],
                capture_output=True,
                text=True,
                timeout=2,
            )
            payload = json.loads(result.stdout or "{}") if result.returncode == 0 else {}

            def walk(device, parent_tran=""):
                path = str(device.get("path") or "")
                tran = str(device.get("tran") or parent_tran or "").lower()
                dev_type = str(device.get("type") or "").lower()
                removable = bool(device.get("rm"))
                external = tran == "usb" or path.startswith("/dev/sd") or (removable and dev_type in ("disk", "part"))

                if external and dev_type in ("disk", "part"):
                    info["present"] = True
                    if info["source"] is None:
                        info["source"] = path or None
                        info["label"] = str(device.get("label") or "")

                    mounts = device.get("mountpoints") or []
                    if isinstance(mounts, str):
                        mounts = [mounts]
                    for mountpoint in mounts:
                        if not mountpoint:
                            continue
                        mountpoint = str(mountpoint)
                        if mountpoint in ("/", "/boot", "/boot/firmware"):
                            continue
                        if not os.path.isdir(mountpoint):
                            continue
                        writable = os.access(mountpoint, os.W_OK)
                        priority = 0
                        if mountpoint == str(SSD_CAPTURE_DIR):
                            priority = 100
                        elif mountpoint.startswith("/media/"):
                            priority = 80
                        elif mountpoint.startswith("/mnt/"):
                            priority = 70
                        else:
                            priority = 40
                        if writable:
                            priority += 10
                        candidates.append((priority, path, mountpoint, str(device.get("label") or ""), writable))

                for child in device.get("children") or []:
                    walk(child, tran)

            for device in payload.get("blockdevices") or []:
                walk(device)
        except Exception:
            pass

        # Retain compatibility with the dedicated /mnt/usb-captures arrangement.
        try:
            if os.path.ismount(SSD_CAPTURE_DIR):
                source = None
                with open("/proc/self/mounts", "r", encoding="utf-8") as fh:
                    for line in fh:
                        parts = line.split()
                        if len(parts) >= 2 and parts[1].replace("\040", " ") == str(SSD_CAPTURE_DIR):
                            source = parts[0].replace("\040", " ")
                            break
                candidates.append((120, source, str(SSD_CAPTURE_DIR), "", os.access(SSD_CAPTURE_DIR, os.W_OK)))
                info["present"] = True
        except Exception:
            pass

        if candidates:
            _priority, source, mountpoint, label, writable = max(candidates, key=lambda item: item[0])
            info.update({
                "present": True,
                "mounted": True,
                "source": source or info["source"],
                "mountpoint": mountpoint,
                "label": label or info["label"],
                "writable": bool(writable),
            })

        self.external_storage_cache = dict(info)
        return info

    def ssd_mount_source(self):
        info = self.external_storage_info()
        return info.get("source") if info.get("mounted") else None

    def ssd_available(self):
        info = self.external_storage_info()
        return bool(info.get("mounted") and info.get("writable") and info.get("mountpoint"))

    def storage(self):
        external = self.external_storage_info()
        use_external = (
            self.storage_policy in ("auto", "ssd")
            and external.get("mounted")
            and external.get("writable")
            and external.get("mountpoint")
        )

        if use_external:
            name = "USB"
            capture_path = Path(external["mountpoint"])
            usage_path = capture_path
        else:
            name = "INTERNAL"
            capture_path = INTERNAL_CAPTURE_DIR
            usage_path = Path("/")

        try:
            usage = shutil.disk_usage(str(usage_path))
            free_gb = usage.free / (1024 ** 3)
        except Exception:
            free_gb = None

        return name, capture_path, free_gb

    # --------------------------------------------------------
    # MESSAGE / INFO / CONFIRM
    # --------------------------------------------------------

    def show_message(self, text, seconds=2.5):
        self.message = text
        self.message_until = time.monotonic() + seconds

    def beep_action(self):
        self.buzzer.action(self.buzzer_mode)

    def show_error(self, text, seconds=3.5):
        self.append_error_log(text)
        self.buzzer.error(self.buzzer_mode)
        self.show_message(text, seconds)

    def update_message(self):
        if self.message and time.monotonic() >= self.message_until:
            self.message = None

    def open_info(self, title, lines, parent=None):
        # INFO pages are entered directly rather than via goto(). Give Sorny
        # the same forward level transition used by ordinary submenus.
        if (
            self.ui_theme == "sorny"
            and self.page != "home"
            and self._sorny_transition_page(self.page)
        ):
            self.animate_sorny_level_transition(forward=True)
        self.remember_menu_position()
        self.info_title = title
        self.info_lines = list(lines)
        self.info_parent = parent or self.page
        self.page = "info"
        self.selection = 0
        self.menu_scroll = 0
        self.info_scroll = 0
        self.info_scroll_paused = {}
        self.info_scroll_time_offsets = {}

    def open_confirm(self, title, lines, callback, parent=None):
        # CONFIRM pages also bypass goto(), so animate the forward level change
        # here. BACK/CANCEL then uses goto() and receives the reverse animation.
        if (
            self.ui_theme == "sorny"
            and self.page != "home"
            and self._sorny_transition_page(self.page)
        ):
            self.animate_sorny_level_transition(forward=True)
        self.remember_menu_position()
        self.confirm_title = title
        self.confirm_lines = list(lines)
        self.confirm_action = callback
        self.confirm_parent = parent or self.page
        self.page = "confirm"
        self.bottom_focus = 0

    # --------------------------------------------------------
    # MENU DEFINITIONS
    # --------------------------------------------------------

    def current_menu(self):
        if self.page == "menu":
            # Keep the top level to four fully visible jobs; no scrolling is
            # required on the main menu. Network remains under System and is
            # also available instantly from the persistent header icons.
            return [
                ("Capture source", "capture"),
                ("Recordings", "captures"),
                ("Storage", "storage"),
                ("System settings", "system"),
            ]

        if self.page == "capture":
            dv_mark = "  *" if self.capture_mode == "dv" else ""
            analog_mark = "  *" if self.capture_mode == "analog" else ""
            return [
                (f"Live DV/Firewire{dv_mark}", "dv_live"),
                ("DV Tape Deck", "dv_tape"),
                (f"Analog{analog_mark}", "set_analog"),
                ("Analog settings", "analog_settings"),
                (f"Storage: {storage_policy_label(self.storage_policy, short=True)}", "storage"),
            ]

        if self.page == "analog_settings":
            self.refresh_analog_capabilities()
            self.validate_selected_analog_mode(save=True)
            mode = self.selected_analog_mode()
            return [
                (f"Video mode: {self.analog_mode_label(mode)}", "analog_modes"),
                ("Picture controls", "analog_picture"),
                ("Device capabilities", "analog_caps"),
                ("Reset analog defaults", "analog_reset"),
            ]

        if self.page == "analog_modes":
            self.refresh_analog_capabilities()
            if not self.analog_modes:
                return [("No video modes detected", "noop"), ("Refresh", "refresh_analog")]
            current_key = self.analog_mode_key(self.selected_analog_mode())
            items = []
            for index, mode in enumerate(self.analog_modes):
                mark = "  *" if self.analog_mode_key(mode) == current_key else ""
                items.append((f"{self.analog_mode_label(mode)}{mark}", f"analog_mode::{index}"))
            items.append(("Refresh device modes", "refresh_analog"))
            return items

        if self.page == "analog_picture":
            self.refresh_analog_capabilities()
            if not self.analog_controls:
                return [("No adjustable controls", "noop"), ("Refresh", "refresh_analog")]
            items = []
            for control in self.analog_controls:
                items.append((
                    f"{control['label']}: {control['value']}",
                    f"analog_control::{control['name']}",
                ))
            items.append(("Reset picture defaults", "reset_picture"))
            return items

        if self.page == "storage":
            auto_mark = "  *" if self.storage_policy == "auto" else ""
            int_mark = "  *" if self.storage_policy == "internal" else ""
            ssd_mark = "  *" if self.storage_policy == "ssd" else ""
            return [
                ("Status / free space", "storage_status"),
                (f"Auto (USB first){auto_mark}", "storage_auto"),
                (f"Internal only{int_mark}", "storage_internal"),
                (f"USB only (Internal fallback){ssd_mark}", "storage_ssd"),
            ]

        if self.page == "captures":
            items = [(f"Filter: {self.capture_filter}", "capture_filter")]
            files = self.capture_files(limit=20, storage_filter=self.capture_filter)
            if not files:
                items.append(("No recordings found", "noop"))
                return items

            items.extend((path.name, f"file::{path}") for path in files)
            return items

        if self.page == "file_actions":
            menu = [("Play on HDMI", "play_file")]
            menu.extend([
                ("File details", "file_info"),
                ("Delete recording", "delete_file"),
            ])
            return menu

        if self.page == "system":
            return [
                ("Device overview", "device_status"),
                ("Network/WiFi", "network"),
                ("Interface", "interface"),
                ("Power", "power"),
                ("Error log", "error_log"),
                ("Credits", "credits"),
            ]

        if self.page == "interface":
            size_label = ("XS", "SMALL", "MEDIUM", "LARGE")[self.font_size_step + 2]
            # The long "Genuine Panafonics" value does not fit cleanly in the
            # classic Panafonics right-hand value column. The theme submenu is
            # where the current choice is marked, so make the parent row an
            # explicit invitation to enter that submenu instead.
            theme_row = (
                "Theme: Select"
                if self.ui_theme in ("panafonics", "jayvc")
                else f"Theme: {self.theme_label()}"
            )
            return [
                ("Touch calibration", "recalibrate"),
                (f"Font size: {size_label}", "font_size"),
                (theme_row, "theme"),
                (f"Buzzer: {self.buzzer_mode.title()}", "buzzer"),
            ]

        if self.page == "theme":
            default_mark = "  *" if self.ui_theme == "default" else ""
            sorny_mark = "  *" if self.ui_theme == "sorny" else ""
            panafonics_mark = "  *" if self.ui_theme == "panafonics" else ""
            jayvc_mark = "  *" if self.ui_theme == "jayvc" else ""
            return [
                (f"Basic{default_mark}", "theme_default"),
                (f"Genuine Sorny{sorny_mark}", "theme_sorny"),
                (f"Genuine Panafonics{panafonics_mark}", "theme_panafonics"),
                (f"Genuine JayVC{jayvc_mark}", "theme_jayvc"),
            ]

        if self.page == "buzzer":
            short_mark = "  *" if self.buzzer_mode == "short" else ""
            long_mark = "  *" if self.buzzer_mode == "long" else ""
            off_mark = "  *" if self.buzzer_mode == "off" else ""
            return [
                (f"Short{short_mark}", "buzzer_short"),
                (f"Long{long_mark}", "buzzer_long"),
                (f"Off{off_mark}", "buzzer_off"),
            ]

        if self.page == "error_log":
            if not self.error_log_entries:
                return [("No CamPi errors recorded", "noop")]
            return [
                (f"{item.get('time', '--:--')}  {item.get('message', '')}", "noop")
                for item in reversed(self.error_log_entries)
            ]

        if self.page == "power":
            return [
                ("Reboot", "reboot"),
                ("Shut down", "shutdown"),
            ]

        if self.page == "network":
            state = self.refresh_network_status()
            radio = "ON" if state.get("wifi_radio") else "OFF"
            refresh_label = "Refresh connections"
            if (
                self.network_refresh_feedback
                and time.monotonic() < self.network_refresh_feedback_until
            ):
                refresh_label = self.network_refresh_feedback
            return [
                ("Connection status", "network_status"),
                ("Wi-Fi networks", "wifi_networks"),
                (f"Wi-Fi radio: {radio}", "wifi_toggle"),
                (refresh_label, "network_refresh"),
            ]

        if self.page == "wifi_networks":
            self.refresh_wifi_networks()
            if not self.network_state.get("nmcli"):
                return [("NetworkManager unavailable", "noop")]
            if not self.network_state.get("wifi_radio"):
                return [("Enable Wi-Fi", "wifi_enable")]
            if not self.wifi_networks:
                return [("No networks found", "noop"), ("Rescan networks", "wifi_rescan")]
            items = []
            for index, entry in enumerate(self.wifi_networks):
                items.append((safe_text(self.wifi_network_label(entry), 31), f"wifi::{index}"))
            items.append(("Rescan networks", "wifi_rescan"))
            return items

        return []

    def page_title(self):
        titles = {
            "menu": "MENU",
            "capture": "CAPTURE SOURCE",
            "dv_tape": "TAPE DECK",
            "analog_settings": "ANALOG SETTINGS",
            "analog_modes": "VIDEO MODE",
            "analog_picture": "PICTURE",
            "analog_control": "PICTURE CONTROL",
            "storage": "CAPTURE STORAGE",
            "captures": "RECORDINGS",
            "file_actions": "RECORDING",
            "playback": "HDMI PLAYBACK",
            "system": "SYSTEM SETTINGS",
            "interface": "INTERFACE",
            "font_size": "FONT SIZE",
            "theme": "THEME",
            "buzzer": "BUZZER",
            "error_log": "ERROR LOG",
            "power": "POWER",
            "network": "NETWORK",
            "wifi_networks": "WI-FI NETWORKS",
            "wifi_password": "WI-FI PASSWORD",
            "info": self.info_title,
            "confirm": self.confirm_title,
        }
        return titles.get(self.page, "CamPi")

    # --------------------------------------------------------
    # NAVIGATION
    # --------------------------------------------------------

    def parent_page(self):
        parents = {
            "menu": "home",
            "capture": "menu",
            "dv_tape": "capture",
            "analog_settings": "capture",
            "analog_modes": "analog_settings",
            "analog_picture": "analog_settings",
            "analog_control": "analog_picture",
            "storage": self.storage_return_page,
            "captures": "menu",
            "file_actions": "captures",
            "playback": "captures",
            "system": "menu",
            "interface": "system",
            "font_size": "interface",
            "theme": "interface",
            "buzzer": "interface",
            "error_log": "system",
            "power": self.power_return_page,
            "network": self.network_return_page,
            "wifi_networks": "network",
            "wifi_password": "wifi_networks",
            "info": self.info_parent,
            "confirm": self.confirm_parent,
        }
        return parents.get(self.page, "home")

    def hdmi_playback_context(self, page=None):
        """True only while the user is inside the Recordings/playback workflow."""
        page = self.page if page is None else page
        playback_pages = {"captures", "file_actions", "playback"}
        if page in playback_pages:
            return True
        # File details/delete confirmation are still part of the playback
        # workflow even though they use the shared INFO/CONFIRM screens.
        if page == "info":
            return self.info_parent in playback_pages
        if page == "confirm":
            return self.confirm_parent in playback_pages
        return False

    def remember_menu_position(self, page=None):
        page = self.page if page is None else page
        if page != self.page:
            return
        try:
            menu = self.current_menu()
        except Exception:
            menu = []
        if menu:
            self.menu_positions[page] = (int(self.selection), int(self.menu_scroll))

    def goto(self, page):
        if page == "capture" and self.recording:
            self.show_message("Stop recording before changing source", 2.8)
            return

        from_page = self.page
        if (
            page != from_page
            and page != "home"
            and from_page != "home"
            and self.ui_theme == "sorny"
            and self._sorny_transition_page(from_page)
            and self._sorny_transition_page(page)
        ):
            going_back = page == self.parent_page()
            self.animate_sorny_level_transition(forward=not going_back)

        self.remember_menu_position()

        if self.page == "dv_tape" and page != "dv_tape":
            self.close_dv_deck(show_message=False)

        old_hdmi_context = self.hdmi_playback_context()
        if self.page == "playback" and page != "playback" and self.playback_running():
            self.stop_playback(show_message=False, restore_holding=False)

        self.page = page
        self.selection = 0
        self.menu_scroll = 0
        self.footer_feedback = None

        menu = self.current_menu()
        restored = False
        saved = self.menu_positions.get(page)
        if menu and saved is not None:
            self.selection = int(clamp(saved[0], 0, len(menu) - 1))
            self.menu_scroll = int(max(0, saved[1]))
            self.ensure_selection_visible()
            restored = True

        if page == "captures" and menu and self.selected_file is not None:
            wanted = f"file::{self.selected_file}"
            for index, (_label, action) in enumerate(menu):
                if action == wanted:
                    self.selection = index
                    self.ensure_selection_visible()
                    restored = True
                    break

        if page == "capture" and not restored:
            self.selection = 0 if self.capture_mode == "dv" else 2
            self.ensure_selection_visible()
        elif page == "theme" and not restored:
            self.selection = {
                "default": 0,
                "sorny": 1,
                "panafonics": 2,
                "jayvc": 3,
            }.get(self.ui_theme, 1)
            self.ensure_selection_visible()
        elif page == "dv_tape":
            self.dv_tape_focus = 1
            self.ensure_dv_deck()

        if self.hdmi_playback_context():
            if not self.playback_running():
                self.refresh_hdmi_holding(force=not old_hdmi_context)
        else:
            self._stop_hdmi_holding()

    def back(self):
        self.goto(self.parent_page())

    def ensure_selection_visible(self):
        menu = self.current_menu()
        if not menu:
            self.menu_scroll = 0
            return

        self.selection = int(clamp(self.selection, 0, len(menu) - 1))

        if self.selection < self.menu_scroll:
            self.menu_scroll = self.selection
        elif self.selection >= self.menu_scroll + self.MAX_VISIBLE_ROWS:
            self.menu_scroll = self.selection - self.MAX_VISIBLE_ROWS + 1

        max_scroll = max(0, len(menu) - self.MAX_VISIBLE_ROWS)
        self.menu_scroll = int(clamp(self.menu_scroll, 0, max_scroll))

    def sorny_block_enabled(self, page=None):
        """Use Sony-style tile blocks for top-level and terminal menu levels."""
        page = self.page if page is None else page
        if self.ui_theme != "sorny":
            return False
        return page in {
            "menu",
            "theme",
            "buzzer",
            "power",
            "storage",
            "analog_modes",
            "file_actions",
        }

    def sorny_cylinder_enabled(self, page=None):
        """Use the rotating wheel on intermediate Genuine Sorny menu levels."""
        page = self.page if page is None else page
        if self.ui_theme != "sorny" or self.sorny_block_enabled(page):
            return False
        # Dense/data-heavy lists and purpose-built controllers remain conventional.
        return page not in {
            "home", "captures", "error_log", "wifi_networks",
            "info", "confirm", "playback", "dv_tape", "analog_control",
            "font_size", "wifi_password",
        }

    @staticmethod
    def _sorny_transition_page(page):
        # Every navigable level below HOME participates in the same Sony-style
        # rectangle transition, including shared INFO/CONFIRM pages and the
        # purpose-built controller screens that were previously omitted.
        return page in {
            "menu", "capture", "dv_tape", "analog_settings", "analog_modes",
            "analog_picture", "analog_control", "storage", "captures",
            "file_actions", "playback", "system", "interface", "font_size",
            "theme", "buzzer", "error_log", "power", "network",
            "wifi_networks", "wifi_password", "info", "confirm",
        }

    def animate_sorny_level_transition(self, forward=True):
        """Brief Sony-style rectangle zoom between menu hierarchy levels."""
        if self.ui_theme != "sorny":
            return
        frames = 5
        for frame in range(frames):
            t = frame / max(1, frames - 1)
            eased = t * t * (3.0 - 2.0 * t)
            scale = (0.28 + 0.72 * eased) if forward else (1.0 - 0.72 * eased)

            image = Image.new("RGB", (WIDTH, HEIGHT), SORNY_BG_DARK)
            d = ImageDraw.Draw(image)
            d.rectangle((0, 0, WIDTH - 1, HEIGHT - 1), fill=SORNY_BG)

            rw = max(30, int((WIDTH - 12) * scale))
            rh = max(22, int((HEIGHT - 12) * scale))
            x1 = (WIDTH - rw) // 2
            y1 = (HEIGHT - rh) // 2
            x2 = x1 + rw
            y2 = y1 + rh
            d.rectangle((x1, y1, x2, y2), outline=SORNY_PANEL_BRIGHT, width=2)
            d.rectangle((x1 + 3, y1 + 3, x1 + 5, y2 - 3), fill=SORNY_ACCENT)
            self.device.display(image)
            time.sleep(0.014)

    def _sorny_first_level_description(self, action):
        descriptions = {
            "dv_live": "Live FireWire capture from a connected DV/HDV source.",
            "dv_tape": "Playback and capture a recorded tape.",
            "set_analog": "Capture from a detected USB analog video device.",
            "analog_settings": "Configure video mode, picture settings, and view device information.",
            "storage": "Choose where new recordings will be stored.",
            "device_status": "Quick overview of storage and inputs.",
            "network": "Configure network options and view status.",
            "network_refresh": "Update connection, address, and signal information.",
            "interface": "Touchscreen, font, theme and buzzer settings.",
            "power": "Restart or safely shut down CamPi.",
            "error_log": "Review CamPi errors and recent failures.",
            "credits": "Script version and contributors.",
        }
        if self.page not in ("capture", "system"):
            return ""
        return descriptions.get(str(action or ""), "")

    @staticmethod
    def _sorny_relative(index, anchor, count):
        """Shortest signed circular distance between two menu indices."""
        if count <= 1:
            return 0.0
        delta = (int(index) - int(anchor)) % count
        if delta > count / 2.0:
            delta -= count
        return float(delta)

    @staticmethod
    def _sorny_slot_y(relative):
        # Sine projection compresses the outer rows like labels wrapped around
        # the visible face of a horizontal cylinder.
        relative = max(-2.55, min(2.55, float(relative)))
        return 112 + (64.0 * math.sin(relative * 0.66))

    def animate_sorny_move(self, direction):
        menu = self.current_menu()
        if not menu:
            return
        direction = 1 if int(direction) > 0 else -1
        old_selection = int(self.selection)
        target = (old_selection + direction) % len(menu)
        if target == old_selection:
            return

        self.sorny_anim_anchor = old_selection
        # Smoothstep produces a quick camcorder-style roll without looking
        # mechanical. Seven frames keeps the animation short on the 320x240 TFT.
        for frame in range(1, 8):
            t = frame / 7.0
            eased = t * t * (3.0 - 2.0 * t)
            self.sorny_anim_shift = direction * eased
            self.render()
            time.sleep(0.018)

        self.selection = target
        self.sorny_anim_anchor = None
        self.sorny_anim_shift = 0.0
        self.ensure_selection_visible()
        self.render()

    def _sorny_block_boxes(self, menu=None):
        menu = self.current_menu() if menu is None else menu
        page_count = self._sorny_block_page_count(menu)

        # Multi-page menus retain the Sony-style navigation gutter.
        if page_count > 1:
            return [
                (70, 42, 185, 107),
                (194, 42, 312, 107),
                (70, 115, 185, 180),
                (194, 115, 312, 180),
            ]

        # A 1/1 page does not need to reserve space for page navigation.
        # Expand the available tiles to use the content area with a consistent
        # outer margin and inter-tile gap.
        visible_count = max(1, min(4, len(menu)))
        left, right = 10, WIDTH - 10
        top, bottom = HEADER_H + 8, HEIGHT - FOOTER_H - 8
        gap = 8

        if visible_count == 1:
            return [(left, top, right, bottom)]

        if visible_count == 2:
            mid = (left + right) // 2
            return [
                (left, top, mid - gap // 2, bottom),
                (mid + gap // 2, top, right, bottom),
            ]

        row_mid = (top + bottom) // 2
        col_mid = (left + right) // 2
        boxes = [
            (left, top, col_mid - gap // 2, row_mid - gap // 2),
            (col_mid + gap // 2, top, right, row_mid - gap // 2),
        ]

        if visible_count == 3:
            boxes.append((left, row_mid + gap // 2, right, bottom))
            return boxes

        boxes.extend([
            (left, row_mid + gap // 2, col_mid - gap // 2, bottom),
            (col_mid + gap // 2, row_mid + gap // 2, right, bottom),
        ])
        return boxes

    def _sorny_block_page_start(self):
        return (int(self.selection) // 4) * 4

    def _sorny_block_page_count(self, menu=None):
        menu = self.current_menu() if menu is None else menu
        return max(1, (len(menu) + 3) // 4)

    def _sorny_block_at(self, x, y):
        page_start = self._sorny_block_page_start()
        for local_index, box in enumerate(self._sorny_block_boxes(self.current_menu())):
            x1, y1, x2, y2 = box
            if x1 <= x <= x2 and y1 <= y <= y2:
                return page_start + local_index
        return None

    def _sorny_change_block_page(self, direction):
        menu = self.current_menu()
        if not menu:
            return
        page_count = self._sorny_block_page_count(menu)
        if page_count <= 1:
            return
        current_page = self._sorny_block_page_start() // 4
        next_page = (current_page + (1 if direction > 0 else -1)) % page_count
        self.selection = min(next_page * 4, len(menu) - 1)
        self.render()

    def _sorny_cylinder_hit_index(self, y, menu):
        """Return the visible wheel item nearest a touch Y coordinate."""
        if not menu:
            return None
        best = None
        seen = set()
        for offset in (-2, -1, 0, 1, 2):
            index = (self.selection + offset) % len(menu)
            if index in seen:
                continue
            seen.add(index)
            cy = self._sorny_slot_y(offset)
            distance = abs(float(y) - cy)
            if best is None or distance < best[0]:
                best = (distance, index)
        if best is not None and best[0] <= 24:
            return best[1]
        return None

    def _sorny_move_to_index(self, target):
        menu = self.current_menu()
        if not menu or target == self.selection:
            return
        count = len(menu)
        delta = (int(target) - int(self.selection)) % count
        if delta > count / 2:
            delta -= count
        direction = 1 if delta > 0 else -1
        for _ in range(abs(int(delta))):
            self.animate_sorny_move(direction)

    def move(self, direction):
        if self.page == "home":
            self.bottom_focus = (self.bottom_focus + direction) % 3
            return

        if self.page == "dv_tape":
            self.dv_tape_focus = (self.dv_tape_focus + int(direction)) % 5
            return

        if self.page == "playback":
            self.playback_focus = (self.playback_focus + int(direction)) % 3
            return

        if self.page == "wifi_password":
            total = len(self.keyboard_keys()) + 3
            self.keyboard_index = (self.keyboard_index + int(direction)) % total
            return

        if self.page == "analog_control":
            control = self.find_analog_control(self.active_analog_control_name)
            if control is not None:
                self.adjust_active_analog_control(direction)
            return

        if self.page == "font_size":
            self.adjust_font_size(direction)
            return

        if self.page == "confirm":
            # Only CANCEL and YES participate in footer selection.
            self.bottom_focus = 1 if self.bottom_focus == 0 else 0
            return

        if self.page == "info":
            max_scroll = max(0, len(self.info_lines) - self.INFO_VISIBLE_LINES)
            self.info_scroll = int(clamp(self.info_scroll + int(direction), 0, max_scroll))
            return

        if self.sorny_block_enabled():
            menu = self.current_menu()
            if menu:
                self.selection = (self.selection + int(direction)) % len(menu)
            return

        if self.sorny_cylinder_enabled():
            self.animate_sorny_move(direction)
            return

        menu = self.current_menu()
        if not menu:
            return

        self.selection = (self.selection + direction) % len(menu)
        self.ensure_selection_visible()

    def press(self):
        if self.page == "home":
            self.activate_footer_with_feedback(self.bottom_focus)
            return

        if self.page == "dv_tape":
            self.activate_dv_tape_control_with_feedback(self.dv_tape_focus)
            return

        if self.page == "playback":
            self.activate_playback_control_with_feedback(self.playback_focus)
            return

        if self.page == "wifi_password":
            keys = self.keyboard_keys()
            if self.keyboard_index < len(keys):
                self.activate_keyboard_key(keys[self.keyboard_index])
            else:
                self.activate_footer_with_feedback(self.keyboard_index - len(keys))
            return

        if self.page == "analog_control":
            self.activate_footer_with_feedback(1)
            return

        if self.page == "font_size":
            self.activate_footer_with_feedback(1)
            return

        if self.page == "confirm":
            self.activate_footer_with_feedback(self.bottom_focus)
            return

        if self.current_menu():
            self.activate_selection_with_feedback()

    def scroll_info_with_feedback(self, direction):
        max_scroll = max(0, len(self.info_lines) - self.INFO_VISIBLE_LINES)
        old_scroll = self.info_scroll
        self.info_scroll = int(clamp(self.info_scroll + int(direction), 0, max_scroll))
        if self.info_scroll == old_scroll:
            return
        self.scroll_feedback = int(direction)
        self.render()
        time.sleep(0.07)
        self.scroll_feedback = None
        self.render()

    def toggle_info_line_scroll(self, absolute_index):
        """Pause/resume one INFO row's horizontal marquee at its exact phase."""
        absolute_index = int(absolute_index)
        if absolute_index < 0 or absolute_index >= len(self.info_lines):
            return

        now = time.monotonic()
        if absolute_index in self.info_scroll_paused:
            frozen_effective_time = self.info_scroll_paused.pop(absolute_index)
            # Shift the row's effective clock by however long it was paused so
            # the marquee continues from the same visual position on resume.
            self.info_scroll_time_offsets[absolute_index] = (
                now - float(frozen_effective_time)
            )
        else:
            offset = float(self.info_scroll_time_offsets.get(absolute_index, 0.0))
            self.info_scroll_paused[absolute_index] = now - offset

        self.render()

    def tap(self, x, y):
        footer_top = HEIGHT - FOOTER_H

        # Persistent header network icons are direct touch shortcuts.
        # LAN opens the general network/status area; Wi-Fi jumps straight
        # to the Wi-Fi network picker/settings screen.
        if y < HEADER_H:
            # HOME shortcut: tapping the CamPi title opens the power menu. Keep
            # the hit area to the title side of the header so HDMI/LAN/Wi-Fi
            # status marks retain their own behaviour.
            if self.page == "home" and x < 180:
                self.activate_header_icon_with_feedback("power")
                return
            if 246 <= x < 280:
                self.activate_header_icon_with_feedback("lan")
                return
            if x >= 280:
                self.activate_header_icon_with_feedback("wifi")
                return

        if y >= footer_top:
            index = int(clamp(int(x / (WIDTH / 3)), 0, 2))
            self.activate_footer_with_feedback(index)
            return

        if self.page == "dv_tape":
            index = self.dv_tape_control_at(x, y)
            if index is not None:
                self.dv_tape_focus = index
                self.activate_dv_tape_control_with_feedback(index)
            return

        if self.page == "playback":
            if self.playback_seek_bar_hit(x, y):
                self.seek_playback_from_x(x)
                return
            index = self.playback_control_at(x, y)
            if index is not None:
                self.playback_focus = index
                self.activate_playback_control_with_feedback(index)
            return

        if self.page == "wifi_password":
            # The eye icon in the password field toggles masked/unmasked preview.
            if self.password_eye_hit(x, y):
                self.wifi_password_visible = not self.wifi_password_visible
                self.render()
                return

            key = self.keyboard_key_at(x, y)
            if key is not None:
                keys = self.keyboard_keys()
                try:
                    self.keyboard_index = keys.index(key)
                except ValueError:
                    pass
                self.render()
                time.sleep(0.06)
                self.activate_keyboard_key(key)
            return

        if self.page == "info":
            has_scroll_rail = len(self.info_lines) > self.INFO_VISIBLE_LINES
            if has_scroll_rail and self.MENU_TOP <= y < footer_top and x >= self.SCROLL_RAIL_X1:
                split_y = (self.MENU_TOP + footer_top) // 2
                self.scroll_info_with_feedback(-1 if y < split_y else 1)
                return

            # Tapping an information row pauses/resumes that row's horizontal
            # marquee. Ignore the small gaps between cards so accidental taps
            # do not toggle the neighbouring line.
            if self.INFO_ROW_TOP <= y < self.INFO_ROW_TOP + self.INFO_VISIBLE_LINES * self.INFO_ROW_STEP:
                visible_index = int((y - self.INFO_ROW_TOP) / self.INFO_ROW_STEP)
                row_y = self.INFO_ROW_TOP + visible_index * self.INFO_ROW_STEP
                if 0 <= visible_index < self.INFO_VISIBLE_LINES and y <= row_y + self.INFO_ROW_HEIGHT:
                    absolute_index = self.info_scroll + visible_index
                    if absolute_index < len(self.info_lines):
                        self.toggle_info_line_scroll(absolute_index)
            return

        menu = self.current_menu()
        if not menu:
            return

        if self.ui_theme == "panafonics":
            pana_bottom = (
                self.PANA_MENU_TOP
                + self.PANA_VISIBLE_ROWS * self.PANA_ROW_HEIGHT
            )

            if (
                len(menu) > self.PANA_VISIBLE_ROWS
                and self.PANA_MENU_TOP <= y < pana_bottom
                and x >= self.PANA_SCROLL_X1
            ):
                split_y = (
                    self.PANA_MENU_TOP + pana_bottom
                ) // 2
                self.scroll_menu_with_feedback(
                    -1 if y < split_y else 1
                )
                return

            if self.PANA_MENU_TOP <= y < pana_bottom:
                visible_index = int(
                    (y - self.PANA_MENU_TOP)
                    / self.PANA_ROW_HEIGHT
                )
                if 0 <= visible_index < self.PANA_VISIBLE_ROWS:
                    actual_index = self.menu_scroll + visible_index
                    if 0 <= actual_index < len(menu):
                        self.selection = actual_index
                        self.ensure_selection_visible()
                        self.render()
                        time.sleep(self.FEEDBACK_DELAY)
                        self.activate_menu_item()
                return

        if self.sorny_block_enabled():
            page_count = self._sorny_block_page_count(menu)
            if page_count > 1 and x <= 62 and 38 <= y <= 82:
                self._sorny_change_block_page(-1)
                return
            if page_count > 1 and x <= 62 and 140 <= y <= 184:
                self._sorny_change_block_page(1)
                return

            index = self._sorny_block_at(x, y)
            if index is not None and index < len(menu):
                self.selection = index
                self.render()
                time.sleep(self.FEEDBACK_DELAY)
                self.activate_menu_item()
            return

        if self.sorny_cylinder_enabled():
            # Dedicated up/down controls remain direct scroll targets.
            if x <= 78 and 34 <= y <= 76:
                self.animate_sorny_move(-1)
                return
            if x <= 78 and 149 <= y <= 191:
                self.animate_sorny_move(1)
                return

            index = self._sorny_cylinder_hit_index(y, menu)
            if index is None:
                return
            if index == self.selection:
                # Only an intentional tap on the centre strip can enter a
                # menu. A just-finished scroll is guarded against a second
                # touch sample from the same held finger.
                if x < 80 or not (88 <= y <= 136):
                    return
                self.render()
                time.sleep(self.FEEDBACK_DELAY)
                self.activate_menu_item()
            else:
                self._sorny_move_to_index(index)
            return

        # Long lists get a large dedicated UP/DOWN touch rail on the right.
        # Check this before row hit-testing so the arrows cannot accidentally
        # activate the menu item underneath them.
        if len(menu) > self.MAX_VISIBLE_ROWS and self.MENU_TOP <= y < footer_top:
            if x >= self.SCROLL_RAIL_X1:
                split_y = (self.MENU_TOP + footer_top) // 2
                if y < split_y:
                    self.scroll_menu_with_feedback(-1)
                else:
                    self.scroll_menu_with_feedback(1)
                return

        if self.MENU_TOP <= y < footer_top:
            visible_index = int((y - self.MENU_TOP) / self.ROW_HEIGHT)
            if visible_index < 0 or visible_index >= self.MAX_VISIBLE_ROWS:
                return

            actual_index = self.menu_scroll + visible_index
            if 0 <= actual_index < len(menu):
                self.selection = actual_index
                self.ensure_selection_visible()
                # Draw the touched row highlighted before navigation.
                self.render()
                time.sleep(self.FEEDBACK_DELAY)
                self.activate_menu_item()

    def activate_header_icon_with_feedback(self, icon):
        # Render a visible pressed state before changing pages, matching the
        # feedback behaviour used by menu rows and footer buttons.
        self.header_feedback = icon
        self.render()
        time.sleep(self.FEEDBACK_DELAY)
        self.header_feedback = None

        if icon == "power":
            # This shortcut is intentionally HOME-specific. BACK from POWER
            # returns straight HOME rather than stepping through System settings.
            self.power_return_page = "home"
            self.goto("power")
            return

        if icon == "wifi":
            if self.page not in ("network", "wifi_networks", "wifi_password"):
                self.network_return_page = self.page
            self.refresh_network_status(force=True)
            self.wifi_scan_checked = 0.0
            self.goto("wifi_networks")
            return

        if icon == "lan":
            if self.page not in ("network", "wifi_networks", "wifi_password"):
                self.network_return_page = self.page
            self.refresh_network_status(force=True)
            self.goto("network")
            return

    def scroll_menu_with_feedback(self, direction):
        menu = self.current_menu()
        if not menu:
            return

        old_selection = self.selection
        self.selection = int(clamp(self.selection + int(direction), 0, len(menu) - 1))
        self.ensure_selection_visible()

        # Give both the arrow and the newly selected row visible feedback.
        self.scroll_feedback = -1 if direction < 0 else 1
        self.render()
        time.sleep(0.09)
        self.scroll_feedback = None

        # Keep the new row highlighted after the arrow feedback disappears.
        if self.selection != old_selection:
            self.render()

    def activate_selection_with_feedback(self):
        # The selected row is deliberately rendered before the action.
        self.ensure_selection_visible()
        self.render()
        time.sleep(self.FEEDBACK_DELAY)
        self.activate_menu_item()

    def activate_footer_with_feedback(self, index):
        labels = self.bottom_buttons()
        if index < 0 or index >= len(labels) or not labels[index]:
            return

        self.footer_feedback = index
        if self.page in ("home", "confirm"):
            self.bottom_focus = index
        self.render()
        time.sleep(self.FEEDBACK_DELAY)
        self.activate_bottom(index)
        self.footer_feedback = None

    # --------------------------------------------------------
    # FOOTER BUTTONS
    # --------------------------------------------------------

    def bottom_buttons(self):
        if self.page == "home":
            return ["STOP" if self.recording else "REC", "SOURCE", "MENU"]

        if self.page == "dv_tape":
            return ["BACK", "", "HOME"]

        if self.page == "playback":
            return ["BACK", "STOP", "HOME"]

        if self.page == "wifi_password":
            return ["BACK", "SPACE", "JOIN"]

        if self.page == "analog_control":
            return ["-", "DONE", "+"]

        if self.page == "font_size":
            return ["-", "DONE", "+"]

        if self.page == "error_log":
            return ["BACK", "", "HOME"]

        if self.page == "confirm":
            return ["CANCEL", "YES", "HOME"]

        if self.page == "info":
            return ["BACK", "", "HOME"]

        if self.ui_theme == "sorny" and (
            self.sorny_block_enabled() or self.sorny_cylinder_enabled()
        ):
            return ["BACK", "", "HOME"]

        return ["BACK", "SELECT", "HOME"]

    def activate_bottom(self, index):
        if self.page == "home":
            if index == 0:
                if self.recording:
                    self.stop_recording()
                else:
                    self.start_recording()
            elif index == 1:
                if self.recording:
                    self.show_message("Stop recording before changing source", 2.8)
                else:
                    self.goto("capture")
            elif index == 2:
                self.goto("menu")
            return

        if self.page == "playback":
            if index == 0:
                self.goto("captures")
            elif index == 1:
                self.stop_playback()
                self.goto("captures")
            elif index == 2:
                self.goto("home")
            return

        if self.page == "wifi_password":
            if index == 0:
                self.wifi_password = ""
                self.wifi_password_visible = False
                self.goto("wifi_networks")
            elif index == 1:
                if len(self.wifi_password) < 63:
                    self.wifi_password += " "
            elif index == 2:
                self.join_wifi_with_password()
            return

        if self.page == "analog_control":
            if index == 0:
                self.adjust_active_analog_control(-1)
            elif index == 1:
                self.goto("analog_picture")
            elif index == 2:
                self.adjust_active_analog_control(1)
            return

        if self.page == "font_size":
            if index == 0:
                self.adjust_font_size(-1)
            elif index == 1:
                self.goto("interface")
                self.selection = 1
                self.ensure_selection_visible()
            elif index == 2:
                self.adjust_font_size(1)
            return

        if self.page == "confirm":
            if index == 0:
                self.goto(self.confirm_parent)
            elif index == 1:
                callback = self.confirm_action
                parent = self.confirm_parent
                self.confirm_action = None
                if callback:
                    callback()
                if self.page == "confirm":
                    self.goto(parent)
            elif index == 2:
                self.goto("home")
            return

        if self.page == "info":
            if index == 0:
                self.goto(self.info_parent)
            elif index == 2:
                self.goto("home")
            return

        if index == 0:
            self.back()
        elif index == 1:
            if self.current_menu():
                self.activate_menu_item()
        elif index == 2:
            self.goto("home")

    # --------------------------------------------------------
    # MENU ACTIONS
    # --------------------------------------------------------

    def activate_menu_item(self):
        menu = self.current_menu()
        if not menu:
            return

        _, action = menu[self.selection]

        if self.page == "menu":
            if action == "capture" and self.recording:
                self.show_message("Stop recording before changing source", 2.8)
                return
            if action == "storage":
                self.storage_return_page = "menu"
            self.goto(action)
            return

        if self.page == "capture":
            if self.recording and action in ("dv_live", "dv_tape", "set_analog", "analog_settings"):
                self.show_message("Stop recording before changing source", 2.8)
                return
            if action == "storage":
                self.storage_return_page = "capture"
                self.goto("storage")
                return
            if action == "dv_live":
                self.settings["capture_mode"] = "dv"
                self.save_settings()
                self.goto("home")
                self.refresh_hdmi_holding(force=True)
                self.show_message("Live DV selected; tape control off", 3.0)
            elif action == "dv_tape":
                self.settings["capture_mode"] = "dv"
                self.save_settings()
                self.goto("dv_tape")
            elif action == "set_analog":
                self.settings["capture_mode"] = "analog"
                self.save_settings()
                self.show_message("Analog selected")
                self.goto("home")
                self.refresh_hdmi_holding(force=True)
            elif action == "analog_settings":
                self.refresh_analog_capabilities(force=True)
                self.validate_selected_analog_mode(save=True)
                self.goto("analog_settings")
            return

        if self.page == "analog_settings":
            if action == "analog_modes":
                self.refresh_analog_capabilities(force=True)
                self.goto("analog_modes")
                current_key = self.analog_mode_key(self.selected_analog_mode())
                for index, mode in enumerate(self.analog_modes):
                    if self.analog_mode_key(mode) == current_key:
                        self.selection = index
                        break
                self.ensure_selection_visible()
            elif action == "analog_picture":
                self.refresh_analog_capabilities(force=True)
                self.goto("analog_picture")
            elif action == "analog_caps":
                self.open_analog_capabilities()
            elif action == "analog_reset":
                self.confirm_reset_analog()
            return

        if self.page == "analog_modes":
            if action == "noop":
                return
            if action == "refresh_analog":
                self.refresh_analog_capabilities(force=True)
                self.validate_selected_analog_mode(save=True)
                self.show_message("Video modes refreshed")
                self.goto("analog_modes")
                return
            if action.startswith("analog_mode::"):
                try:
                    index = int(action.split("::", 1)[1])
                    mode = self.analog_modes[index]
                except Exception:
                    return
                self.set_selected_analog_mode(mode)
                self.show_message(self.analog_mode_label(mode))
                self.goto("analog_settings")
            return

        if self.page == "analog_picture":
            if action == "noop":
                return
            if action == "refresh_analog":
                self.refresh_analog_capabilities(force=True)
                self.apply_saved_analog_controls()
                self.goto("analog_picture")
                return
            if action == "reset_picture":
                self.confirm_reset_picture()
                return
            if action.startswith("analog_control::"):
                self.active_analog_control_name = action.split("::", 1)[1]
                self.goto("analog_control")
            return

        if self.page == "storage":
            if action == "storage_status":
                self.open_storage_status()
            elif action == "storage_auto":
                self.settings["storage_policy"] = "auto"
                self.save_settings()
                self.show_message("USB preferred; internal fallback")
                self.goto("storage")
                self.refresh_hdmi_holding(force=True)
            elif action == "storage_internal":
                self.settings["storage_policy"] = "internal"
                self.save_settings()
                self.show_message("Internal storage selected")
                self.goto("storage")
                self.refresh_hdmi_holding(force=True)
            elif action == "storage_ssd":
                self.settings["storage_policy"] = "ssd"
                self.save_settings()
                self.show_message("USB preferred; internal fallback")
                self.goto("storage")
                self.refresh_hdmi_holding(force=True)
            return

        if self.page == "captures":
            if action == "noop":
                return
            if action == "capture_filter":
                order = ("ALL", "SD", "USB")
                try:
                    index = order.index(self.capture_filter)
                except ValueError:
                    index = 0
                self.capture_filter = order[(index + 1) % len(order)]
                self.selection = 0
                self.menu_scroll = 0
                self.refresh_hdmi_holding(force=True)
                return
            if action.startswith("file::"):
                self.selected_file = Path(action[6:])
                self.goto("file_actions")
            return

        if self.page == "file_actions":
            if action == "play_file":
                self.play_selected_file()
            elif action == "file_info":
                self.open_file_info()
            elif action == "delete_file":
                self.confirm_delete_file()
            return

        if self.page == "system":
            if action == "device_status":
                self.open_device_status()
            elif action == "network":
                self.network_return_page = "system"
                self.refresh_network_status(force=True)
                self.goto("network")
            elif action == "interface":
                self.goto("interface")
            elif action == "power":
                self.power_return_page = "system"
                self.goto("power")
            elif action == "error_log":
                self.goto("error_log")
            elif action == "credits":
                self.open_info(
                    "CREDITS",
                    [
                        f"CamPi v{APP_VERSION}",
                        "Producer: Sam Mortimer",
                        "Code: ChatGPT",
                        "Font: SonyCam Original by Xane123",
                    ],
                    parent="system",
                )
            return

        if self.page == "interface":
            if action == "recalibrate":
                self.confirm_recalibration()
            elif action == "font_size":
                self.goto("font_size")
            elif action == "theme":
                self.goto("theme")
            elif action == "buzzer":
                self.goto("buzzer")
                self.selection = {"short": 0, "long": 1, "off": 2}.get(self.buzzer_mode, 0)
                self.ensure_selection_visible()
            return

        if self.page == "theme":
            theme_map = {
                "theme_default": "default",
                "theme_sorny": "sorny",
                "theme_panafonics": "panafonics",
                "theme_jayvc": "jayvc",
            }
            if action in theme_map:
                theme = self.set_ui_theme(theme_map[action], save=True)
                label = self.theme_label()
                self.goto("interface")
                self.selection = 2
                self.ensure_selection_visible()
                self.show_message(f"Theme: {label}", 1.8)
                self.render()
            return

        if self.page == "buzzer":
            mode_map = {
                "buzzer_short": "short",
                "buzzer_long": "long",
                "buzzer_off": "off",
            }
            if action in mode_map:
                mode = mode_map[action]
                self.settings["buzzer_mode"] = mode
                self.save_settings()
                if mode != "off":
                    self.buzzer.action(mode)
                self.show_message(f"Buzzer: {mode.upper()}", 1.5)
                self.goto("buzzer")
                self.selection = {"short": 0, "long": 1, "off": 2}[mode]
                self.ensure_selection_visible()
            return

        if self.page == "error_log":
            return

        if self.page == "power":
            if action == "reboot":
                self.confirm_reboot()
            elif action == "shutdown":
                self.confirm_shutdown()
            return

        if self.page == "network":
            if action == "network_status":
                self.open_network_status()
            elif action == "wifi_networks":
                self.refresh_wifi_networks(force=True)
                self.goto("wifi_networks")
            elif action == "wifi_toggle":
                state = self.refresh_network_status(force=True)
                self.set_wifi_radio(not state.get("wifi_radio"))
                self.goto("network")
            elif action == "network_refresh":
                state = self.refresh_network_status(force=True)
                if state.get("lan_connected") and state.get("wifi_connected"):
                    summary = "Updated: Ethernet + Wi-Fi"
                elif state.get("lan_connected"):
                    summary = "Updated: Ethernet connected"
                elif state.get("wifi_connected"):
                    ssid = state.get("wifi_ssid") or "Wi-Fi connected"
                    summary = f"Updated: {safe_text(ssid, 20)}"
                else:
                    summary = "Updated: no network"
                self.network_refresh_feedback = summary
                self.network_refresh_feedback_until = time.monotonic() + 3.5
                self.show_message(summary, 2.5)
                self.render()
            return

        if self.page == "wifi_networks":
            if action == "noop":
                return
            if action == "wifi_enable":
                if self.set_wifi_radio(True):
                    self.refresh_wifi_networks(force=True)
                    self.goto("wifi_networks")
                return
            if action == "wifi_rescan":
                self.refresh_wifi_networks(force=True)
                self.goto("wifi_networks")
                self.show_message("Wi-Fi scan complete", 2.0)
                return
            if action.startswith("wifi::"):
                try:
                    index = int(action.split("::", 1)[1])
                    entry = self.wifi_networks[index]
                except Exception:
                    return
                self.connect_wifi_entry(entry)
            return
    # --------------------------------------------------------
    # ANALOG UI ACTIONS
    # --------------------------------------------------------

    def adjust_active_analog_control(self, direction):
        control = self.find_analog_control(self.active_analog_control_name)
        if control is None:
            return
        step = int(control.get("step", 1))
        new_value = int(control["value"]) + (step * int(direction))
        new_value = int(clamp(new_value, int(control["min"]), int(control["max"])))
        if self.set_analog_control(control["name"], new_value, persist=True):
            self.show_message(f"{control['label']}: {new_value}", 1.0)
        else:
            self.show_message("Control update failed", 2.5)

    def open_analog_capabilities(self):
        self.refresh_analog_capabilities(force=True)
        formats = sorted({mode["pixel_format"] for mode in self.analog_modes})
        mode = self.selected_analog_mode()
        device = self.analog_video_device
        audio_device = self.find_analog_audio_device(force=True)

        device_label = self.analog_device_name or (Path(device).name if device else "none")
        if self.analog_device_vidpid:
            device_label += f" ({self.analog_device_vidpid})"

        audio_label = self.analog_audio_name or ("none detected" if not audio_device else audio_device)

        lines = [
            f"Device: {device_label}",
            f"Video: {device or 'none'}",
            f"Driver: {self.analog_device_driver or 'unknown'}",
            f"Nodes: {len(self.analog_capture_candidates)}",
            f"Modes: {len(self.analog_modes)}",
            f"Formats: {', '.join(formats) if formats else 'none'}",
            f"Controls: {len(self.analog_controls)}",
            f"Mode: {self.analog_mode_label(mode)}",
            f"Audio: {audio_device or 'none'}",
            f"Audio dev: {audio_label}",
            "Audio fmt: 48 kHz stereo PCM 16-bit",
            "Codec: FFV1 level 3",
            "Container: Matroska (.mkv)",
        ]
        self.open_info("DEVICE CAPABILITIES", lines, parent="analog_settings")

    def confirm_reset_picture(self):
        def do_reset():
            self.reset_analog_controls()
            self.goto("analog_picture")
            self.show_message("Picture controls reset", 2.5)

        self.open_confirm(
            "RESET PICTURE?",
            ["Restore device defaults?"],
            do_reset,
            parent="analog_picture",
        )

    def confirm_reset_analog(self):
        def do_reset():
            self.refresh_analog_capabilities(force=True)
            self.set_selected_analog_mode(self.best_reported_analog_mode(), save=False)
            self.reset_analog_controls()
            self.save_settings()
            self.goto("analog_settings")
            self.show_message("Analog defaults restored", 3.0)

        self.open_confirm(
            "RESET ANALOG?",
            ["Restore source and picture", "controls to default?"],
            do_reset,
            parent="analog_settings",
        )

    # --------------------------------------------------------
    # FIREWIRE TAPE DECK / AV/C
    # --------------------------------------------------------

    def dv_deck_running(self):
        return self.dv_deck_process is not None and self.dv_deck_process.poll() is None

    def dv_tape_control_boxes(self):
        # 320x240 display: two large touch rows above the persistent footer.
        return [
            (6, 78, 103, 122),
            (111, 78, 209, 122),
            (217, 78, 314, 122),
            (6, 129, 154, 176),
            (162, 129, 314, 176),
        ]

    def dv_tape_control_at(self, x, y):
        for index, box in enumerate(self.dv_tape_control_boxes()):
            x1, y1, x2, y2 = box
            if x1 <= x <= x2 and y1 <= y <= y2:
                return index
        return None

    def activate_dv_tape_control_with_feedback(self, index):
        self.dv_tape_feedback = index
        self.render()
        time.sleep(0.09)
        self.dv_tape_feedback = None

        actions = [
            self.dv_tape_rewind,
            self.dv_tape_play_pause,
            self.dv_tape_fast_forward,
            self.dv_tape_stop,
            self.dv_tape_play_record,
        ]
        if 0 <= index < len(actions):
            actions[index]()

    def _append_capture_log(self, text):
        try:
            APP_DIR.mkdir(parents=True, exist_ok=True)
            with open(CAPTURE_LOG_FILE, "a", encoding="utf-8") as fh:
                fh.write(f"[{time.strftime('%F %T')}] {text}\n")
        except Exception:
            pass

    def _dv_deck_reader(self, process):
        # dvgrab's interactive status is carriage-return delimited rather than
        # newline-delimited, so consume stderr byte-by-byte and handle both.
        buffer = bytearray()
        stream = process.stderr
        if stream is None:
            return

        try:
            while True:
                chunk = stream.read(1)
                if not chunk:
                    break
                if chunk in (b"\r", b"\n"):
                    if buffer:
                        line = buffer.decode("utf-8", errors="replace").strip()
                        buffer.clear()
                        if line:
                            self._handle_dv_deck_line(line)
                else:
                    buffer.extend(chunk)
        except Exception as exc:
            self.dv_deck_events.put(("error", f"Deck status reader: {exc}"))

    def _handle_dv_deck_line(self, line):
        # Status format from dvgrab interactive mode:
        #   "Playing" 00:12:34:05 "filename" 12.34 sec
        match = re.match(r'^"([^"]*)"\s+(\S+)\s+"([^"]*)"\s*(.*?)\s+sec$', line)
        if match:
            transport, timecode, _filename, _duration = match.groups()
            if transport:
                self.dv_deck_transport = transport.upper()
            if timecode:
                self.dv_deck_timecode = timecode
            return

        lower = line.lower()
        if "config rom" in lower and ("error" in lower or "directory node" in lower):
            # libraw1394/libavc1394 may emit this while enumerating FireWire
            # nodes. Keep the raw CONFIG ROM scan message out of the user UI.
            # Genuine camera/capture failures are surfaced separately by the
            # camera-presence checks and dvgrab process failure handling.
            self._append_capture_log(f"FireWire scan note: {line}")
            return
        if any(token in lower for token in ("no tape", "no medium", "no cassette", "tape not loaded")):
            self.dv_deck_transport = "NO TAPE"
            self.dv_deck_timecode = "--:--:--:--"
            return
        if "capture started" in lower:
            self.dv_deck_events.put(("capture_started", line))
        elif "capture stopped" in lower:
            self.dv_deck_events.put(("capture_stopped", line))
        elif "error:" in lower or line.lower().startswith("error"):
            self.dv_deck_events.put(("error", line))
        elif "warning:" in lower:
            self.dv_deck_events.put(("warning", line))
        elif "going interactive" not in lower:
            self._append_capture_log(f"dvgrab: {line}")

    def refresh_dv_media_status(self, force=False):
        """Best-effort no-tape detection using dvcont without blocking the UI."""
        if not self.dvcont_path or not self.dv_deck_running():
            return
        if self.dv_deck_recording or self.dv_deck_record_pending:
            return
        if self.dv_status_thread is not None and self.dv_status_thread.is_alive():
            return

        now = time.monotonic()
        if not force and now - self.dv_status_checked < 2.5:
            return
        self.dv_status_checked = now

        def worker():
            try:
                result = subprocess.run(
                    [self.dvcont_path, "status"],
                    capture_output=True,
                    text=True,
                    timeout=1.5,
                )
                if result.returncode == 0:
                    # stderr can contain harmless CONFIG ROM enumeration noise.
                    # Only stdout carries the AV/C media/transport status.
                    status = (result.stdout or "").strip()
                    if status:
                        self.dv_deck_events.put(("media_status", status))
            except Exception:
                pass

        self.dv_status_thread = threading.Thread(target=worker, daemon=True)
        self.dv_status_thread.start()

    def refresh_dv_tape_camera_presence(self, force=False):
        """Keep Tape Deck camera state live while the page remains open."""
        if self.page != "dv_tape":
            return self.firewire_camera_present

        now = time.monotonic()
        if not force and now - self.dv_presence_checked < 1.0:
            return self.firewire_camera_present
        self.dv_presence_checked = now

        was_present = bool(self.firewire_camera_present)
        present = self.firewire_camera_available(force=True)

        if not present:
            # If the camera disappears, do not leave a stale READY/PLAY state.
            was_recording = self.dv_deck_recording or self.dv_deck_record_pending

            if self.dv_deck_running():
                # Tear down a stale interactive session so a later reconnect
                # can initialise cleanly without leaving dvgrab attached to a
                # vanished node.
                self.close_dv_deck(show_message=False)

            self.dv_deck_transport = "NO CAMERA"
            self.dv_deck_timecode = "--:--:--:--"

            if was_recording:
                self.show_error("FireWire camera disconnected", 3.5)
            return False

        # A camera that appears after this screen was opened should become
        # usable immediately.  Only auto-start on the absent -> present edge,
        # avoiding repeated dvgrab respawns after unrelated deck failures.
        if not was_present and not self.dv_deck_running():
            self.dv_deck_transport = "CAMERA CONNECTED"
            self.dv_deck_timecode = "--:--:--:--"
            self.ensure_dv_deck()

        return True

    def ensure_dv_deck(self):
        if self.dv_deck_running():
            return True

        if shutil.which("dvgrab") is None:
            self.dv_deck_transport = "DVGRAB MISSING"
            self.show_message("dvgrab is not installed", 4.0)
            return False

        if not self.firewire_camera_available(force=True):
            self.dv_deck_transport = "NO CAMERA"
            self.dv_deck_timecode = "--:--:--:--"
            self.show_error("FireWire camera not detected", 3.0)
            return False

        # HOME may have a short-lived dvgrab process sampling the incoming
        # stream for format detection. Release FireWire before Tape Deck opens.
        if not self._stop_dv_stream_probe():
            self.show_error("FireWire detection still stopping", 3.5)
            return False

        storage_name, capture_dir, _free_gb = self.storage()
        try:
            Path(capture_dir).mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            self.dv_deck_transport = "STORAGE ERROR"
            self.show_error(f"Storage error: {exc}", 4.0)
            return False

        stamp = time.strftime("%Y%m%d_%H%M%S")
        is_hdv = self.dv_stream_is_hdv()
        capture_format = "hdv" if is_hdv else "raw"
        dv_slug = self.dv_standard_slug()

        if is_hdv:
            hdv_mode = dv_slug[4:] if dv_slug.startswith("hdv_") else ""
            prefix_name = (
                f"hdv_tape_{hdv_mode}_{stamp}_"
                if hdv_mode
                else f"hdv_tape_{stamp}_"
            )
        else:
            prefix_name = (
                f"dv_tape_{dv_slug}_{stamp}_"
                if dv_slug
                else f"dv_tape_{stamp}_"
            )

        prefix = str(Path(capture_dir) / prefix_name)
        command = [
            "dvgrab",
            "-interactive",
            "-format", capture_format,
            "-size", "0",
            "-showstatus",
            prefix,
        ]

        try:
            self.dv_deck_process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                start_new_session=True,
                bufsize=0,
            )
            self.dv_deck_session_prefix = prefix
            self.dv_deck_transport = "PAUSE / READY"
            self.dv_deck_timecode = "--:--:--:--"
            self.dv_deck_recording = False
            self.dv_deck_record_pending = False
            self._append_capture_log(f"TAPE SESSION START {' '.join(command)}")

            self.dv_deck_thread = threading.Thread(
                target=self._dv_deck_reader,
                args=(self.dv_deck_process,),
                daemon=True,
            )
            self.dv_deck_thread.start()
            self.show_message(f"Tape deck ready ({storage_name})", 2.0)
            return True
        except Exception as exc:
            self.dv_deck_process = None
            self.dv_deck_transport = "DECK ERROR"
            self.show_error(f"Tape deck failed: {exc}", 4.0)
            return False

    def send_dv_deck(self, keys, action_name=None):
        if not self.ensure_dv_deck():
            return False
        try:
            data = keys.encode("ascii") if isinstance(keys, str) else keys
            self.dv_deck_process.stdin.write(data)
            self.dv_deck_process.stdin.flush()
            if action_name:
                self._append_capture_log(f"TAPE CONTROL {action_name}")
            return True
        except Exception as exc:
            self.show_error(f"Tape control failed: {exc}", 4.0)
            return False

    def dv_tape_rewind(self):
        if self.dv_deck_recording or self.dv_deck_record_pending:
            self.show_error("STOP recording before REW", 2.5)
            return
        if self.send_dv_deck("a", "REW"):
            self.dv_deck_transport = "REW"
            self.beep_action()

    def dv_tape_fast_forward(self):
        if self.dv_deck_recording or self.dv_deck_record_pending:
            self.show_error("STOP recording before FF", 2.5)
            return
        if self.send_dv_deck("z", "FF"):
            self.dv_deck_transport = "FF"
            self.beep_action()

    def dv_tape_play_pause(self):
        if self.dv_deck_recording or self.dv_deck_record_pending:
            self.show_error("Use STOP to finish recording", 2.5)
            return
        if self.send_dv_deck(" ", "PLAY/PAUSE"):
            self.beep_action()
            self.show_message("Play / pause", 1.0)

    def dv_tape_play_record(self):
        if self.dv_deck_recording or self.dv_deck_record_pending:
            self.show_error("Already recording", 2.0)
            return
        if not self.ensure_dv_deck():
            return

        # In dvgrab interactive mode, 'c' calls startCapture(); startCapture()
        # itself sends AV/C PLAY before opening the DV writer.  Therefore this
        # one command is the requested PLAY + REC operation.
        self.dv_deck_record_pending = True
        if self.send_dv_deck("c", "PLAY+REC"):
            self.beep_action()
            self.show_message("Starting tape capture…", 2.0)
        else:
            self.dv_deck_record_pending = False

    def dv_tape_stop(self):
        if not self.dv_deck_running():
            self.recording = False
            self.dv_deck_recording = False
            self.dv_deck_record_pending = False
            self.dv_deck_transport = "STOP"
            return

        # dvgrab's first 's' stops an active capture and pauses the VTR.
        # A second 's' then issues AV/C STOP.  Sending both guarantees this
        # button means STOP for both playback-only and PLAY+REC operation.
        if self.send_dv_deck("ss", "STOP"):
            self.dv_deck_record_pending = False
            self.beep_action()
            self.show_message("Tape stopped", 1.5)

    def poll_dv_deck(self):
        while True:
            try:
                event, detail = self.dv_deck_events.get_nowait()
            except queue.Empty:
                break

            if event == "capture_started":
                self.dv_deck_record_pending = False
                self.dv_deck_recording = True
                self.recording = True
                self.record_started = time.monotonic()
                self.capture_target = self.dv_deck_session_prefix
                self.dv_stream_scan = ""
                self.dv_scan_checked = 0.0
                self.show_message("Tape capture recording", 1.5)
            elif event == "capture_stopped":
                self.dv_deck_record_pending = False
                self.dv_deck_recording = False
                self.recording = False
                self.record_started = None
            elif event == "warning":
                self._append_capture_log(detail)
            elif event == "media_status":
                status = str(detail).lower()
                if any(token in status for token in ("no tape", "no medium", "no cassette", "ejected", "eject")):
                    self.dv_deck_transport = "NO TAPE"
                    self.dv_deck_timecode = "--:--:--:--"
                elif self.dv_deck_transport == "NO TAPE":
                    self.dv_deck_transport = "PAUSE / READY"
            elif event == "error":
                if "config rom" in str(detail).lower():
                    self._append_capture_log(f"FireWire scan note: {detail}")
                    continue
                self._append_capture_log(detail)
                self.show_error(safe_text(detail, 46), 4.0)

        self.refresh_dv_tape_camera_presence()
        self.refresh_dv_media_status()

        if self.dv_deck_recording and not self.dv_stream_scan:
            self._refresh_dv_standard_from_capture()

        process = self.dv_deck_process
        if process is not None and process.poll() is not None:
            code = process.returncode
            was_recording = self.dv_deck_recording or self.dv_deck_record_pending
            self.dv_deck_process = None
            self.dv_deck_recording = False
            self.dv_deck_record_pending = False
            if was_recording:
                self.recording = False
                self.record_started = None
            self.dv_deck_transport = f"OFFLINE ({code})"
            if self.page == "dv_tape":
                self.show_error(f"Tape deck ended (code {code})", 3.5)

    def close_dv_deck(self, show_message=False):
        process = self.dv_deck_process
        if process is None:
            self.dv_deck_recording = False
            self.dv_deck_record_pending = False
            return

        try:
            if process.poll() is None and process.stdin is not None:
                # STOP capture, STOP transport, then quit interactive dvgrab.
                process.stdin.write(b"ssq")
                process.stdin.flush()
                process.wait(timeout=3.0)
        except Exception:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=2.0)
            except Exception:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except Exception:
                    pass

        self._append_capture_log("TAPE SESSION END")
        self.dv_deck_process = None
        self.dv_deck_recording = False
        self.dv_deck_record_pending = False
        self.recording = False
        self.record_started = None
        self.dv_deck_transport = "OFFLINE"
        if show_message:
            self.show_message("Tape deck closed", 1.5)

    # --------------------------------------------------------
    # CAPTURE BACKEND
    # --------------------------------------------------------

    def _alsa_capture_devices(self):
        devices = []
        if shutil.which("arecord") is None:
            return devices
        try:
            output = subprocess.check_output(
                ["arecord", "-l"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
        except Exception:
            return devices

        pattern = re.compile(
            r"card\s+(\d+):\s*([^\[]*)\[([^\]]*)\],\s*"
            r"device\s+(\d+):\s*([^\[]*)\[([^\]]*)\]",
            re.IGNORECASE,
        )

        for line in output.splitlines():
            match = pattern.search(line)
            if not match:
                continue
            card = int(match.group(1))
            short_name = match.group(2).strip()
            long_name = match.group(3).strip()
            device_num = int(match.group(4))
            pcm_name = match.group(5).strip()
            pcm_long = match.group(6).strip()

            card_path = Path("/sys/class/sound") / f"card{card}" / "device"
            usb_root = self._find_usb_device_root(card_path)
            vidpid = ""
            if usb_root is not None:
                vendor = self._sysfs_text(usb_root / "idVendor")
                product_id = self._sysfs_text(usb_root / "idProduct")
                if vendor and product_id:
                    vidpid = f"{vendor}:{product_id}"

            display = long_name or short_name or pcm_long or pcm_name or f"card {card}"
            devices.append({
                "card": card,
                "device": device_num,
                "pcm": f"plughw:{card},{device_num}",
                "name": display,
                "line": line.strip(),
                "usb_root": str(usb_root) if usb_root is not None else "",
                "vidpid": vidpid,
            })
        return devices

    def find_analog_audio_device(self, force=False):
        """Pair ALSA capture audio to the selected video dongle where possible."""
        video_device = self.discover_analog_video_device(force=force)
        if not video_device:
            self.analog_audio_device = None
            self.analog_audio_name = ""
            return None

        devices = self._alsa_capture_devices()
        if not devices:
            self.analog_audio_device = None
            self.analog_audio_name = ""
            return None

        video_root = self.analog_device_usb_root
        video_vidpid = self.analog_device_vidpid
        video_name = (self.analog_device_name or "").lower()

        def score(item):
            value = 0
            if video_root and item.get("usb_root") == video_root:
                value += 1000
            elif video_vidpid and item.get("vidpid") == video_vidpid:
                value += 500

            line = (item.get("line") or "").lower()
            if "ms210" in line or "macrosilicon" in line:
                # Preserve the known-good path for the original CamPi dongle.
                value += 120

            # Shared product words are a weaker fallback for devices whose
            # sysfs sound/video interfaces are exposed through different paths.
            video_tokens = {
                token for token in re.findall(r"[a-z0-9]+", video_name)
                if len(token) >= 4 and token not in {"video", "device", "usb"}
            }
            audio_tokens = set(re.findall(r"[a-z0-9]+", line))
            value += 15 * len(video_tokens & audio_tokens)

            if item.get("usb_root"):
                value += 20
            return value

        chosen = max(devices, key=score)

        # When USB-parent matching is unavailable, a single capture PCM is a
        # safe fallback. With several unrelated sound cards, require at least
        # some positive association rather than silently recording the wrong one.
        chosen_score = score(chosen)
        if chosen_score <= 20 and len(devices) > 1:
            fallback = next(
                (
                    item for item in devices
                    if item["pcm"] == ANALOG_AUDIO_FALLBACK
                    or "ms210" in item["line"].lower()
                    or "macrosilicon" in item["line"].lower()
                ),
                None,
            )
            chosen = fallback

        if chosen is None:
            self.analog_audio_device = None
            self.analog_audio_name = ""
            return None

        self.analog_audio_device = chosen["pcm"]
        self.analog_audio_name = chosen["name"]
        return self.analog_audio_device

    # Backward-compatible internal name retained for any older local patches.
    def find_ms210x_audio_device(self):
        """Compatibility alias for older MS210x-specific integrations."""
        return self.find_analog_audio_device()

    def start_recording(self):
        if self.recording:
            return

        storage_name, capture_dir, free_gb = self.storage()

        try:
            Path(capture_dir).mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            self.show_error(f"Storage error: {exc}", 4.0)
            return

        stamp = time.strftime("%Y%m%d_%H%M%S")

        if self.capture_mode == "dv":
            if shutil.which("dvgrab") is None:
                self.show_error("dvgrab is not installed", 4.0)
                return
            if not self.firewire_camera_available(force=True):
                self.show_error("FireWire camera not detected", 4.0)
                return

            # Resolve DV vs HDV before choosing dvgrab's file writer. The HOME
            # background probe normally has this cached already; a short
            # synchronous probe covers a freshly connected source.
            if (
                not self.dv_stream_standard
                or self.dv_stream_standard == "HDV MPEG-2 TS"
            ):
                self._probe_dv_stream_sync(timeout=2.2, frames=12)

            # A FireWire node alone does not prove that a usable DV/HDV stream
            # is arriving. Never guess "raw DV" when classification failed,
            # because that makes an HDV camera appear to record while dvgrab
            # immediately exits without producing a file.
            if not self.dv_stream_standard:
                self.show_error("No DV/HDV stream detected", 4.0)
                return

            # HOME format detection also uses a short-lived -noavc dvgrab.
            # It must be fully stopped before the real recorder claims the
            # FireWire stream, otherwise libraw1394/dvgrab can abort.
            if not self._stop_dv_stream_probe():
                self.show_error("FireWire detection still stopping", 4.0)
                return

            # Give the kernel's isochronous receiver a moment to release its
            # channel after probe teardown before claiming it for recording.
            time.sleep(0.20)

            # Disable AV/C for live camera capture. HDV is stored as its native
            # MPEG-2 transport stream (.m2t); SD DV remains raw .dv.
            is_hdv = self.dv_stream_is_hdv()
            capture_format = "hdv" if is_hdv else "raw"
            dv_slug = self.dv_standard_slug()

            if is_hdv:
                prefix_name = f"{dv_slug or 'hdv'}_{stamp}_"
            elif dv_slug:
                prefix_name = f"dv_{dv_slug}_{stamp}_"
            else:
                prefix_name = f"dv_live_{stamp}_"

            prefix = str(Path(capture_dir) / prefix_name)
            command = [
                "dvgrab",
                "-noavc",
                "-format", capture_format,
                "-size", "0",
                "-showstatus",
                prefix,
            ]
            self.capture_target = prefix

        else:
            if shutil.which("ffmpeg") is None:
                self.show_error("ffmpeg is not installed", 4.0)
                return

            video_device = self.discover_analog_video_device(force=True)
            if not video_device:
                self.show_error("Analog video device not detected", 4.0)
                return

            self.refresh_analog_capabilities(force=True)
            self.validate_selected_analog_mode(save=True)
            self.apply_saved_analog_controls()

            mode = self.selected_analog_mode()
            input_format = ffmpeg_v4l2_input_format(mode["pixel_format"])
            frame_rate = format_fps(mode["fps"])
            video_size = f"{mode['width']}x{mode['height']}"

            standard_label = self.analog_mode_standard_label(mode)
            standard_slug = standard_label.lower() if standard_label else "video"
            output_file = Path(capture_dir) / f"analog_{standard_slug}_{stamp}.mkv"
            audio_device = self.find_analog_audio_device(force=True)

            # The selected V4L2 mode is authoritative. CamPi passes the exact
            # format, resolution and frame rate reported by the dongle to FFmpeg.
            command = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel", "warning",
                "-thread_queue_size", "1024",
                "-f", "v4l2",
                "-input_format", input_format,
                "-video_size", video_size,
                "-framerate", frame_rate,
                "-i", video_device,
            ]

            if audio_device:
                # plughw lets ALSA convert common rate/channel differences,
                # which is more portable across generic USB capture dongles.
                command += [
                    "-thread_queue_size", "1024",
                    "-f", "alsa",
                    "-sample_fmt", "s16",
                    "-ac", "2",
                    "-ar", "48000",
                    "-i", audio_device,
                ]

            command += [
                "-ss", str(ANALOG_PRE_CAPTURE_DELAY),
                "-map", "0:v:0",
            ]
            if audio_device:
                command += ["-map", "1:a:0"]

            command += [
                "-c:v", "ffv1",
                "-level", "3",
                "-g", "1",
                "-slicecrc", "1",
                "-pix_fmt", "yuv422p",
            ]
            if audio_device:
                command += ["-c:a", "pcm_s16le"]

            command += [
                "-avoid_negative_ts", "make_zero",
                "-y",
                str(output_file),
            ]
            self.capture_target = str(output_file)

        try:
            self.capture_log_handle = open(CAPTURE_LOG_FILE, "a", encoding="utf-8")
            self.capture_log_handle.write(f"\n[{time.strftime('%F %T')}] START {' '.join(command)}\n")
            self.capture_log_handle.flush()

            if self.capture_mode == "analog" and ANALOG_PRE_CAPTURE_DELAY > 0:
                self.show_message("Starting analog capture…", 1.0)
                self.render()

            self.capture_process = subprocess.Popen(
                command,
                stdout=self.capture_log_handle,
                stderr=self.capture_log_handle,
                start_new_session=True,
            )

            if self.capture_mode == "dv":
                # Do not announce REC merely because dvgrab spawned. Confirm it
                # survives startup and actually creates a native capture file.
                # This catches wrong-format/no-stream HDV failures immediately.
                deadline = time.monotonic() + 5.0
                output_seen = False
                prefix = Path(str(self.capture_target))
                while time.monotonic() < deadline:
                    code = self.capture_process.poll()
                    if code is not None:
                        break
                    try:
                        candidates = []
                        for extension in ("*.dv", "*.m2t", "*.m2ts", "*.ts"):
                            candidates.extend(prefix.parent.glob(prefix.name + extension))
                        if any(item.is_file() and item.stat().st_size > 0 for item in candidates):
                            output_seen = True
                            break
                    except Exception:
                        pass
                    time.sleep(0.08)

                code = self.capture_process.poll()
                if code is not None or not output_seen:
                    if self.capture_process is not None and self.capture_process.poll() is None:
                        try:
                            os.killpg(self.capture_process.pid, signal.SIGTERM)
                            self.capture_process.wait(timeout=0.8)
                        except Exception:
                            try:
                                os.killpg(self.capture_process.pid, signal.SIGKILL)
                            except Exception:
                                pass
                    self.capture_process = None
                    self.recording = False
                    self.record_started = None
                    self._close_capture_log()
                    if code is not None:
                        try:
                            with open(CAPTURE_LOG_FILE, "rb") as fh:
                                fh.seek(0, os.SEEK_END)
                                fh.seek(max(0, fh.tell() - 8192))
                                recent_log = fh.read().decode("utf-8", errors="replace").lower()
                        except Exception:
                            recent_log = ""
                        if "device or resource busy" in recent_log or "failed to get channels available" in recent_log:
                            self.show_error("FireWire busy: check other dvgrab processes", 4.0)
                        else:
                            self.show_error(f"FireWire receiver exited (code {code})", 4.0)
                    else:
                        self.show_error("FireWire stream produced no file (5s)", 4.0)
                    return

            self.recording = True
            self.record_started = time.monotonic()
            self.refresh_hdmi_holding(force=True)
            self.beep_action()
            self.show_message(f"Recording to {storage_name}", 2.0)

        except Exception as exc:
            self.capture_process = None
            self.recording = False
            self.record_started = None
            self._close_capture_log()
            self.show_error(f"Capture failed: {exc}", 4.0)

    def stop_recording(self):
        if self.dv_deck_recording or self.dv_deck_record_pending:
            self.dv_tape_stop()
            return

        process = self.capture_process
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGINT)
                process.wait(timeout=8)
            except Exception:
                try:
                    process.terminate()
                    process.wait(timeout=3)
                except Exception:
                    try:
                        process.kill()
                    except Exception:
                        pass

        self.capture_process = None
        self.recording = False
        self.record_started = None
        self._close_capture_log()
        self.refresh_hdmi_holding(force=True)
        self.beep_action()
        self.show_message("Capture stopped", 2.5)

    def _close_capture_log(self):
        try:
            if self.capture_log_handle:
                self.capture_log_handle.flush()
                self.capture_log_handle.close()
        except Exception:
            pass
        self.capture_log_handle = None

    def poll_capture(self):
        self.poll_dv_deck()
        self.poll_playback()

        if self.recording and self.capture_mode == "dv" and (
            not self.dv_stream_standard or not self.dv_stream_scan
        ):
            self._refresh_dv_standard_from_capture()

        if not self.recording or self.capture_process is None:
            return
        code = self.capture_process.poll()
        if code is not None:
            self.recording = False
            self.record_started = None
            self.capture_process = None
            self._close_capture_log()
            self.refresh_hdmi_holding(force=True)
            if code == 0:
                self.show_message("Capture ended", 2.5)
            else:
                if code < 0:
                    try:
                        reason = signal.Signals(-code).name
                    except Exception:
                        reason = f"signal {-code}"
                    self.show_error(f"Capture ended ({reason})", 4.0)
                else:
                    self.show_error(f"Capture ended (code {code})", 4.0)

    # --------------------------------------------------------
    # FILES / PLAYBACK
    # --------------------------------------------------------

    def capture_files(self, limit=20, storage_filter="ALL"):
        candidates = []
        storage_filter = str(storage_filter or "ALL").upper()
        roots = []
        if storage_filter in ("ALL", "SD"):
            roots.append(INTERNAL_CAPTURE_DIR)

        external = self.external_storage_info()
        if storage_filter in ("ALL", "USB") and external.get("mounted") and external.get("mountpoint"):
            external_root = Path(external["mountpoint"])
            if external_root not in roots:
                roots.append(external_root)

        extensions = {".dv", ".m2t", ".m2ts", ".ts", ".mkv", ".avi", ".mov", ".mp4"}
        for root in roots:
            try:
                for path in root.iterdir():
                    if path.is_file() and path.suffix.lower() in extensions:
                        candidates.append(path)
            except Exception:
                pass

        candidates.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
        return candidates[:limit]

    def recording_storage_kind(self, path):
        """Return SD or USB for a recording path for the Recordings list."""
        try:
            Path(path).resolve().relative_to(INTERNAL_CAPTURE_DIR.resolve())
            return "SD"
        except Exception:
            return "USB"

    def playback_storage_summary(self):
        """Return recording counts and free space for the HDMI holding screen."""
        extensions = {".dv", ".m2t", ".m2ts", ".ts", ".mkv", ".avi", ".mov", ".mp4"}

        def count_recordings(root):
            root = Path(root)
            try:
                return sum(
                    1
                    for path in root.iterdir()
                    if path.is_file() and path.suffix.lower() in extensions
                )
            except Exception:
                return 0

        def free_gb_for(path, fallback=None):
            try:
                target = Path(path)
                if not target.exists() and fallback is not None:
                    target = Path(fallback)
                usage = shutil.disk_usage(str(target))
                return usage.free / (1024 ** 3)
            except Exception:
                return None

        sd_count = count_recordings(INTERNAL_CAPTURE_DIR)
        sd_free = free_gb_for(INTERNAL_CAPTURE_DIR, "/")

        external = self.external_storage_info(force=True)
        usb_connected = bool(external.get("mounted") and external.get("mountpoint"))
        usb_root = Path(external["mountpoint"]) if usb_connected else None
        usb_count = count_recordings(usb_root) if usb_root is not None else 0
        usb_free = free_gb_for(usb_root) if usb_root is not None else None

        return {
            "sd_count": sd_count,
            "sd_free_gb": sd_free,
            "usb_connected": usb_connected,
            "usb_count": usb_count,
            "usb_free_gb": usb_free,
            "total_count": sd_count + usb_count,
        }

    def _probe_video_for_playback(self, path):
        """Return video timing metadata, including robust DV interlace detection."""
        info = {
            "width": 0,
            "height": 0,
            "fps": 0.0,
            "codec_name": "",
            "field_order": "unknown",
            "interlaced": False,
            "is_hdv": False,
        }

        probe = shutil.which("ffprobe")
        if probe is None:
            return info

        try:
            result = subprocess.run(
                [
                    probe,
                    "-v", "error",
                    "-select_streams", "v:0",
                    "-show_entries",
                    "stream=codec_name,width,height,avg_frame_rate,r_frame_rate,field_order",
                    "-of", "json",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=4,
            )
            if result.returncode != 0:
                return info

            payload = json.loads(result.stdout or "{}")
            streams = payload.get("streams") or []
            if not streams:
                return info

            stream = streams[0]
            info["codec_name"] = str(stream.get("codec_name") or "").lower()
            info["width"] = int(stream.get("width") or 0)
            info["height"] = int(stream.get("height") or 0)

            field_order = str(stream.get("field_order") or "unknown").lower()
            info["field_order"] = field_order
            info["interlaced"] = field_order not in ("progressive", "unknown", "")

            for key in ("avg_frame_rate", "r_frame_rate"):
                value = str(stream.get(key) or "0/0")
                try:
                    if "/" in value:
                        num, den = value.split("/", 1)
                        den_f = float(den)
                        rate = float(num) / den_f if den_f else 0.0
                    else:
                        rate = float(value)
                except (TypeError, ValueError, ZeroDivisionError):
                    rate = 0.0
                rate = normalize_fps(rate)
                if rate > 0:
                    info["fps"] = rate
                    break

            # Stream-level field_order is sometimes "unknown" for raw DV even
            # though decoded frames carry interlace flags. Probe a short slice
            # of the first decoded frame before falling back to DV semantics.
            if not info["interlaced"] and field_order in ("unknown", ""):
                try:
                    frame_result = subprocess.run(
                        [
                            probe,
                            "-v", "error",
                            "-select_streams", "v:0",
                            "-read_intervals", "%+0.25",
                            "-show_entries", "frame=interlaced_frame,top_field_first",
                            "-of", "json",
                            str(path),
                        ],
                        capture_output=True,
                        text=True,
                        timeout=4,
                    )
                    if frame_result.returncode == 0:
                        frame_payload = json.loads(frame_result.stdout or "{}")
                        frames = frame_payload.get("frames") or []
                        if frames:
                            frame = frames[0]
                            interlaced_frame = int(frame.get("interlaced_frame") or 0)
                            top_field_first = int(frame.get("top_field_first") or 0)
                            if interlaced_frame:
                                info["interlaced"] = True
                                info["field_order"] = "tff" if top_field_first else "bff"
                except Exception:
                    pass

            suffix = Path(path).suffix.lower()
            info["is_hdv"] = bool(
                info["codec_name"] == "mpeg2video"
                and suffix in (".m2t", ".m2ts", ".ts")
                and info["height"] in (720, 1080)
            )

            # HDV720 is always progressive. HDV1080 uses stream/frame field
            # metadata already probed above, including progressive variants.
            if info["is_hdv"] and info["height"] == 720:
                info["interlaced"] = False
                info["field_order"] = "progressive"

            # Raw SD DV identifies the 525/60 or 625/50 transport system, but
            # some consumer cameras record progressively acquired pictures
            # inside that ordinary DV transport. Analyse several decoded frames
            # before falling back to the usual interlaced-DV assumption.
            is_dv = (
                info["codec_name"] == "dvvideo"
                or Path(path).suffix.lower() == ".dv"
            )
            if is_dv and info["height"] in (480, 576):
                content_scan = ""
                try:
                    with open(path, "rb") as fh:
                        dv_sample = fh.read(1728000)
                    content_scan = self._detect_dv_scan_from_bytes(dv_sample)
                except Exception:
                    content_scan = ""

                if content_scan == "progressive":
                    info["interlaced"] = False
                    info["field_order"] = "progressive"
                elif content_scan == "interlaced":
                    info["interlaced"] = True
                    if info["field_order"] in ("unknown", "", "progressive"):
                        info["field_order"] = "bff"
                elif info["field_order"] == "progressive":
                    info["interlaced"] = False
                else:
                    # Conservative fallback for ordinary SD DV when no content
                    # decision can be made.
                    info["interlaced"] = True
                    if info["field_order"] in ("unknown", ""):
                        info["field_order"] = "bff"

                # DV transport frame rates remain 25 or 29.97 fps regardless of
                # whether the pictures were acquired progressively or interlaced.
                if info["height"] == 576:
                    if info["fps"] <= 0 or info["fps"] > 30.0:
                        info["fps"] = 25.0
                elif info["height"] == 480:
                    if info["fps"] <= 0 or info["fps"] > 35.0:
                        info["fps"] = 29.970

        except Exception:
            pass

        return info

    def _playback_hdmi_profile(self, media_info):
        """Return ordered DRM mode candidates and nominal output refresh."""
        fps = float(media_info.get("fps") or 0.0)

        # Try a cadence-correct 1080p mode first, then 720p at the same refresh,
        # then finally the television's preferred mode. This keeps PAL at 50 Hz
        # whenever the display advertises any suitable 50 Hz mode.
        if abs(fps - 25.0) < 0.6 or abs(fps - 50.0) < 0.6:
            return [
                "1920x1080@50",
                "1280x720@50",
                "preferred",
            ], 50.0

        # 29.97/59.94 sources prefer 59.94 where EDID exposes it, with 60 Hz
        # as the normal compatibility fallback. True 30/60 uses the same family.
        if abs(fps - 29.970) < 0.35 or abs(fps - 59.940) < 0.35:
            return [
                "1920x1080@59.94",
                "1920x1080@60",
                "1280x720@59.94",
                "1280x720@60",
                "preferred",
            ], 59.94

        if abs(fps - 30.0) < 0.35 or abs(fps - 60.0) < 0.35:
            return [
                "1920x1080@60",
                "1280x720@60",
                "preferred",
            ], 60.0

        if abs(fps - 23.976) < 0.25:
            return [
                "1920x1080@23.976",
                "1920x1080@24",
                "preferred",
            ], 23.976

        if abs(fps - 24.0) < 0.25:
            return [
                "1920x1080@24",
                "preferred",
            ], 24.0

        return ["preferred"], 0.0

    def _open_mpv_log(self):
        try:
            APP_DIR.mkdir(parents=True, exist_ok=True)
            fh = open(MPV_LOG_FILE, "a", encoding="utf-8")
            fh.write(f"\n[{time.strftime('%F %T')}] playback start\n")
            fh.flush()
            return fh
        except Exception:
            return subprocess.DEVNULL

    def _start_mpv_mode(self, mode):
        """Start mpv for one DRM mode candidate."""
        self._remove_mpv_socket()
        if not self.playback_base_command or self.playback_file is None:
            return False

        command = list(self.playback_base_command)
        command.append(f"--drm-mode={mode}")
        command.append(str(self.playback_file))

        log_handle = self._open_mpv_log()
        try:
            if hasattr(log_handle, "write"):
                log_handle.write("COMMAND: " + " ".join(shlex.quote(str(part)) for part in command) + "\n")
                log_handle.flush()
            self.playback_process = subprocess.Popen(
                command,
                stdout=log_handle,
                stderr=log_handle,
            )
        except Exception:
            try:
                if hasattr(log_handle, "close"):
                    log_handle.close()
            except Exception:
                pass
            self.playback_process = None
            return False

        # Popen duplicates the fd; close our copy immediately.
        try:
            if hasattr(log_handle, "close"):
                log_handle.close()
        except Exception:
            pass

        self.playback_output_mode = mode
        self.playback_started_at = time.monotonic()
        return True

    def _advance_playback_mode(self):
        """Retry playback with the next HDMI mode candidate."""
        while self.playback_mode_index + 1 < len(self.playback_mode_candidates):
            self.playback_mode_index += 1
            mode = self.playback_mode_candidates[self.playback_mode_index]
            if self._start_mpv_mode(mode):
                return True
        return False

    def _framebuffer_geometry(self):
        sysfb = Path("/sys/class/graphics") / HDMI_FRAMEBUFFER.name
        try:
            width_s, height_s = (sysfb / "virtual_size").read_text().strip().split(",", 1)
            width = int(width_s)
            height = int(height_s)
            bpp = int((sysfb / "bits_per_pixel").read_text().strip())
            try:
                stride = int((sysfb / "stride").read_text().strip())
            except Exception:
                stride = width * (bpp // 8)
            # Pi 5 KMS/fbdev normally exposes XRGB8888 (32 bpp). Keep the
            # one-shot writer deliberately narrow rather than guessing a
            # 16-bit framebuffer channel layout.
            if width > 0 and height > 0 and bpp == 32:
                return width, height, bpp, stride
        except Exception:
            pass
        return None

    def _write_image_to_framebuffer(self, image):
        """Compatibility framebuffer fallback retained for simple HDMI output."""
        geometry = self._framebuffer_geometry()
        if geometry is None or not HDMI_FRAMEBUFFER.exists():
            return False

        width, height, bpp, stride = geometry
        try:
            frame = image.convert("RGB").resize((width, height), Image.Resampling.LANCZOS)
            raw = frame.tobytes("raw", "BGRX")
            row_bytes = width * 4

            with open(HDMI_FRAMEBUFFER, "r+b", buffering=0) as fb:
                for y in range(height):
                    start = y * row_bytes
                    fb.seek(y * stride)
                    fb.write(raw[start:start + row_bytes])
            return True
        except Exception:
            return False

    def _build_hdmi_holding_image(self, summary):
        width, height = HDMI_TARGET_WIDTH, HDMI_TARGET_HEIGHT
        image = Image.new("RGB", (width, height), (8, 11, 16))
        draw = ImageDraw.Draw(image)
        draw.fontmode = "L"

        title_font = load_font(FONT_BOLD_PATH, 64)
        heading_font = load_font(FONT_BOLD_PATH, 38)
        value_font = load_font(FONT_NORMAL_PATH, 34)
        detail_font = load_font(FONT_NORMAL_PATH, 28)
        selected_font = load_font(FONT_NORMAL_PATH, 26)

        def centre(text, y, font, fill):
            text = str(text)
            box = draw.textbbox((0, 0), text, font=font)
            tw = box[2] - box[0]
            draw_text(draw, ((width - tw) // 2, y), text, font, fill)

        def fit_text(text, max_width, font_path, start_size, min_size=20):
            text = str(text)
            size = int(start_size)
            while size > min_size:
                font = load_font(font_path, size)
                box = draw.textbbox((0, 0), text, font=font)
                if box[2] - box[0] <= max_width:
                    return font
                size -= 2
            return load_font(font_path, min_size)

        centre("Use the touchscreen to play media", 115, title_font, TEXT)
        centre(
            f"{summary['total_count']} recording{'s' if summary['total_count'] != 1 else ''} available",
            205,
            detail_font,
            TEXT_DIM,
        )

        # Two storage cards make it immediately clear where recordings live.
        card_y1, card_y2 = 300, 610
        left = (170, card_y1, 900, card_y2)
        right = (1020, card_y1, 1750, card_y2)

        for box, heading, accent in (
            (left, "SD CARD", SONY_BLUE),
            (right, "USB", SONY_CYAN),
        ):
            draw.rounded_rectangle(box, radius=28, fill=PANEL, outline=accent, width=3)
            x1, y1, x2, y2 = box
            heading_box = draw.textbbox((0, 0), heading, font=heading_font)
            heading_w = heading_box[2] - heading_box[0]
            draw_text(
                draw,
                ((x1 + x2 - heading_w) // 2, y1 + 42),
                heading,
                heading_font,
                accent,
            )

        sd_count = summary["sd_count"]
        sd_free = summary["sd_free_gb"]
        centre_x_left = (left[0] + left[2]) // 2
        sd_line = f"{sd_count} recording{'s' if sd_count != 1 else ''}"
        sd_free_line = (
            f"{sd_free:.1f} GB free" if sd_free is not None else "Free space unavailable"
        )
        for line, y, font, colour in (
            (sd_line, card_y1 + 135, value_font, TEXT),
            (sd_free_line, card_y1 + 200, detail_font, TEXT_DIM),
        ):
            box = draw.textbbox((0, 0), line, font=font)
            draw_text(draw, (centre_x_left - (box[2] - box[0]) // 2, y), line, font, colour)

        centre_x_right = (right[0] + right[2]) // 2
        if summary["usb_connected"]:
            usb_count = summary["usb_count"]
            usb_free = summary["usb_free_gb"]
            usb_line = f"{usb_count} recording{'s' if usb_count != 1 else ''}"
            usb_free_line = (
                f"{usb_free:.1f} GB free" if usb_free is not None else "Free space unavailable"
            )
            usb_colour = TEXT
        else:
            usb_line = "Not connected"
            usb_free_line = "Connect USB storage to browse it"
            usb_colour = SONY_YELLOW

        for line, y, font, colour in (
            (usb_line, card_y1 + 135, value_font, usb_colour),
            (usb_free_line, card_y1 + 200, detail_font, TEXT_DIM),
        ):
            box = draw.textbbox((0, 0), line, font=font)
            draw_text(draw, (centre_x_right - (box[2] - box[0]) // 2, y), line, font, colour)

        centre("Select a recording on the touchscreen", 700, selected_font, TEXT_DIM)

        centre(
            "Playback opens on this HDMI display",
            790,
            detail_font,
            SONY_GREEN,
        )
        return image

    def _stop_hdmi_holding(self):
        """Release the lightweight DRM client used for the idle HDMI screen."""
        process = self.hdmi_holding_process
        self.hdmi_holding_process = None
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=1.0)
        except Exception:
            try:
                process.kill()
                process.wait(timeout=0.5)
            except Exception:
                pass

    def _hdmi_holding_running(self):
        return (
            self.hdmi_holding_process is not None
            and self.hdmi_holding_process.poll() is None
        )

    def _start_hdmi_holding(self):
        """Show the generated status image using a minimal native-DRM mpv."""
        player = shutil.which("mpv")
        if player is None or not HDMI_HOLDING_IMAGE.exists():
            return False

        self._stop_hdmi_holding()
        command = [
            player,
            "--no-config",
            "--fs",
            "--no-terminal",
            "--osd-level=0",
            "--audio=no",
            "--vo=drm",
            "--profile=sw-fast",
            "--drm-mode=preferred",
            "--keep-open=yes",
            "--loop-file=inf",
            "--image-display-duration=inf",
            str(HDMI_HOLDING_IMAGE),
        ]
        try:
            self.hdmi_holding_process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            # Catch immediate DRM/argument failures without keeping a polling
            # loop alive during normal idle operation.
            time.sleep(0.18)
            if self.hdmi_holding_process.poll() is not None:
                self.hdmi_holding_process = None
                return False
            return True
        except Exception:
            self.hdmi_holding_process = None
            return False

    def refresh_hdmi_holding(self, force=False):
        # Never touch HDMI outside the Recordings/playback workflow. Calls from
        # capture/storage state changes are intentionally harmless no-ops.
        if not self.hdmi_playback_context():
            self._stop_hdmi_holding()
            return False
        if self.playback_running():
            return False

        summary = self.playback_storage_summary()
        signature = (
            summary["sd_count"],
            round(summary["sd_free_gb"], 1) if summary["sd_free_gb"] is not None else None,
            summary["usb_connected"],
            summary["usb_count"],
            round(summary["usb_free_gb"], 1) if summary["usb_free_gb"] is not None else None,
        )

        # If nothing relevant changed and the holder still owns the DRM
        # connector, leave it alone.
        if (
            not force
            and signature == self.hdmi_holding_signature
            and self._hdmi_holding_running()
        ):
            return True

        image = self._build_hdmi_holding_image(summary)
        try:
            APP_DIR.mkdir(parents=True, exist_ok=True)
            image.save(HDMI_HOLDING_IMAGE)
        except Exception:
            return False

        shown = self._start_hdmi_holding()
        if shown:
            self.hdmi_holding_signature = signature
        return shown

    def playback_running(self):
        return self.playback_process is not None and self.playback_process.poll() is None

    def _remove_mpv_socket(self):
        try:
            Path(MPV_IPC_SOCKET).unlink(missing_ok=True)
        except Exception:
            pass

    def _mpv_request(self, command, timeout=0.20):
        """Send one JSON IPC command to mpv and return the reply's data field."""
        if not self.playback_running() or not os.path.exists(MPV_IPC_SOCKET):
            return None

        request_id = int(time.monotonic_ns() & 0x7FFFFFFF)
        payload = {"command": command, "request_id": request_id}

        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(timeout)
                client.connect(MPV_IPC_SOCKET)
                client.sendall((json.dumps(payload) + "\n").encode("utf-8"))

                buffer = b""
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    buffer += chunk
                    while b"\n" in buffer:
                        raw, buffer = buffer.split(b"\n", 1)
                        if not raw.strip():
                            continue
                        reply = json.loads(raw.decode("utf-8", errors="replace"))
                        if reply.get("request_id") == request_id:
                            if reply.get("error") == "success":
                                return reply.get("data")
                            return None
        except (OSError, ValueError, json.JSONDecodeError):
            return None

        return None

    def refresh_playback_status(self, force=False):
        if not self.playback_running():
            return

        now = time.monotonic()
        if not force and now - self.playback_status_checked < 0.25:
            return
        self.playback_status_checked = now

        position = self._mpv_request(["get_property", "time-pos"])
        duration = self._mpv_request(["get_property", "duration"])
        paused = self._mpv_request(["get_property", "pause"])
        eof_reached = self._mpv_request(["get_property", "eof-reached"])

        try:
            if position is not None:
                self.playback_position = max(0.0, float(position))
        except (TypeError, ValueError):
            pass

        try:
            if duration is not None:
                self.playback_duration = max(0.0, float(duration))
        except (TypeError, ValueError):
            pass

        if isinstance(paused, bool):
            self.playback_paused = paused

        if eof_reached is True:
            if self.playback_duration > 0:
                self.playback_position = self.playback_duration
            self.playback_paused = True
            # keep-open holds the last decoded frame; explicitly pause so the
            # controller state is stable and PLAY can be treated as a replay.
            self._mpv_request(["set_property", "pause", True])

    def poll_playback(self):
        if self.playback_process is None:
            return

        code = self.playback_process.poll()
        if code is None:
            self.refresh_playback_status()
            return

        # If a requested DRM timing is rejected, mpv can exit after more than
        # the old 180 ms startup window. Retry the next cadence-correct mode
        # automatically before giving up.
        if code != 0 and self.playback_output_mode != "preferred":
            self.playback_process = None
            self._remove_mpv_socket()
            if self._advance_playback_mode():
                self.show_message(f"Retry HDMI: {self.playback_output_mode}", 1.3)
                return

        # Natural EOF or a player that could not be recovered.
        if self.playback_duration > 0 and code == 0:
            self.playback_position = self.playback_duration
        self.playback_process = None
        self.playback_paused = False
        self._remove_mpv_socket()
        self.refresh_hdmi_holding(force=True)
        if code == 0:
            self.show_message("Playback finished", 2.5)
        else:
            self.show_error(f"Playback stopped ({code}) - see mpv.log", 3.5)

    def play_selected_file(self):
        if self.selected_file is None or not self.selected_file.exists():
            self.show_error("File is missing", 3.0)
            return

        player = shutil.which("mpv")
        if player is None:
            self.show_error("Install mpv for HDMI playback", 4.0)
            return

        if not self.refresh_hdmi_detection(force=True):
            self.flash_hdmi_missing()
            return

        self.stop_playback(show_message=False, restore_holding=False)
        self._stop_hdmi_holding()
        self._remove_mpv_socket()

        media_info = self._probe_video_for_playback(self.selected_file)
        mode_candidates, refresh = self._playback_hdmi_profile(media_info)
        fps = float(media_info.get("fps") or 0.0)
        interlaced = bool(media_info.get("interlaced"))

        # On Raspberry Pi OS Lite, use mpv's native DRM/KMS video output.
        # This is deliberately simpler than gpu-next+DRM/EGL: the latter can
        # leave audio playing while tty1 remains visible if EGL/plane setup
        # fails. The native DRM VO owns the KMS output directly and supports
        # --drm-mode for cadence-correct 50/59.94/60 Hz playback.
        base_command = [
            player,
            "--no-config",
            "--fs",
            "--no-terminal",
            "--osd-level=0",
            "--vo=drm",
            "--profile=sw-fast",
            "--loop-file=no",
            "--keep-open=yes",
            f"--input-ipc-server={MPV_IPC_SOCKET}",
        ]

        # Deinterlacing is playback-only; masters on disk remain untouched.
        if interlaced:
            base_command.append("--deinterlace=yes")

        try:
            self.playback_file = self.selected_file
            self.playback_position = 0.0
            self.playback_duration = 0.0
            self.playback_paused = False
            self.playback_status_checked = 0.0
            self.playback_focus = 1
            self.playback_source_fps = fps
            self.playback_source_width = int(media_info.get("width") or 0)
            self.playback_source_height = int(media_info.get("height") or 0)
            self.playback_output_refresh = refresh
            self.playback_interlaced = interlaced
            self.playback_field_order = str(media_info.get("field_order") or "unknown")
            self.playback_mode_candidates = list(mode_candidates)
            self.playback_mode_index = 0
            self.playback_base_command = base_command

            if not self._start_mpv_mode(self.playback_mode_candidates[0]):
                raise RuntimeError("mpv could not be started")

            # Give KMS/file initialisation enough time and verify that a real
            # video output has actually been configured. mpv can otherwise keep
            # audio running after a failed video-output initialisation.
            while True:
                deadline = time.monotonic() + 1.5
                socket_seen = False
                vo_name = None

                while time.monotonic() < deadline:
                    code = self.playback_process.poll() if self.playback_process else 1
                    if code is not None:
                        break

                    if os.path.exists(MPV_IPC_SOCKET):
                        socket_seen = True
                        vo_name = self._mpv_request(["get_property", "current-vo"], timeout=0.30)
                        if vo_name:
                            break
                    time.sleep(0.05)

                code = self.playback_process.poll() if self.playback_process else 1
                if code is None and socket_seen and vo_name == "drm":
                    break

                # A process can remain alive with audio only if video output
                # failed. Kill that attempt before trying the next HDMI mode.
                if self.playback_process is not None and self.playback_process.poll() is None:
                    try:
                        self.playback_process.terminate()
                        self.playback_process.wait(timeout=0.8)
                    except Exception:
                        try:
                            self.playback_process.kill()
                        except Exception:
                            pass
                self.playback_process = None
                self._remove_mpv_socket()

                if not self._advance_playback_mode():
                    if code is None:
                        raise RuntimeError(f"mpv started without a DRM video output; see {MPV_LOG_FILE}")
                    raise RuntimeError(f"mpv exited with status {code}; see {MPV_LOG_FILE}")

            self.goto("playback")
            self.beep_action()
            if self.playback_output_mode == "preferred":
                self.show_message("Playing on HDMI", 1.5)
            else:
                self.show_message(f"HDMI {self.playback_output_mode}", 1.8)
        except Exception as exc:
            self.playback_process = None
            self._remove_mpv_socket()
            self.refresh_hdmi_holding(force=True)
            self.show_error(f"Playback failed: {exc}", 4.0)

    def stop_playback(self, show_message=True, restore_holding=True):
        was_running = self.playback_running()
        if self.playback_process is not None and self.playback_process.poll() is None:
            # Prefer a clean mpv quit so files/devices are released normally.
            self._mpv_request(["quit"])
            try:
                self.playback_process.wait(timeout=1.5)
            except Exception:
                try:
                    self.playback_process.terminate()
                    self.playback_process.wait(timeout=1.0)
                except Exception:
                    try:
                        self.playback_process.kill()
                    except Exception:
                        pass
        self.playback_process = None
        self.playback_paused = False
        self.playback_base_command = None
        self.playback_mode_candidates = ["preferred"]
        self.playback_mode_index = 0
        self._remove_mpv_socket()
        if restore_holding:
            self.refresh_hdmi_holding(force=True)
        if show_message:
            if was_running:
                self.beep_action()
            self.show_message("Playback stopped", 2.0)

    def playback_seek_bar_box(self):
        return (12, 94, WIDTH - 12, 120)

    def playback_seek_bar_hit(self, x, y):
        x1, y1, x2, y2 = self.playback_seek_bar_box()
        return x1 <= x <= x2 and y1 <= y <= y2

    def playback_control_boxes(self):
        return [
            (8, 128, 101, 178),
            (109, 128, 211, 178),
            (219, 128, 312, 178),
        ]

    def playback_control_at(self, x, y):
        for index, box in enumerate(self.playback_control_boxes()):
            x1, y1, x2, y2 = box
            if x1 <= x <= x2 and y1 <= y <= y2:
                return index
        return None

    def activate_playback_control_with_feedback(self, index):
        self.playback_feedback = index
        self.render()
        time.sleep(0.08)
        self.playback_feedback = None

        if index == 0:
            self.seek_playback_relative(-10.0)
        elif index == 1:
            self.toggle_playback_pause()
        elif index == 2:
            self.seek_playback_relative(10.0)

    def toggle_playback_pause(self):
        if not self.playback_running():
            replay = self.playback_file if self.playback_file is not None else self.selected_file
            if replay is not None and replay.exists():
                self.selected_file = replay
                self.play_selected_file()
            else:
                self.show_message("Playback is not running", 2.0)
            return
        if (
            self.playback_duration > 0
            and self.playback_position >= max(0.0, self.playback_duration - 0.15)
        ):
            self._mpv_request(["seek", 0.0, "absolute+exact"])
            self.playback_position = 0.0
            self._mpv_request(["set_property", "pause", False])
        elif self._mpv_request(["cycle", "pause"]) is None:
            # Commands with no data return None even when successful; force a
            # property refresh to obtain the resulting state.
            pass
        time.sleep(0.03)
        self.playback_status_checked = 0.0
        self.refresh_playback_status(force=True)
        self.beep_action()

    def seek_playback_relative(self, seconds):
        if not self.playback_running():
            self.show_message("Playback is not running", 2.0)
            return
        self._mpv_request(["seek", float(seconds), "relative+exact"])
        self.playback_status_checked = 0.0
        self.refresh_playback_status(force=True)

    def seek_playback_from_x(self, x):
        if not self.playback_running() or self.playback_duration <= 0:
            self.show_message("Seek not ready", 1.5)
            return
        x1, _y1, x2, _y2 = self.playback_seek_bar_box()
        fraction = clamp((float(x) - x1) / max(1.0, float(x2 - x1)), 0.0, 1.0)
        target = fraction * self.playback_duration

        # Update immediately so the slider follows the finger before the next
        # status poll arrives from mpv.
        self.playback_position = target
        self.render()
        self._mpv_request(["seek", target, "absolute+exact"])
        self.playback_status_checked = 0.0

    @staticmethod
    def _recording_codec_label(codec_name):
        codec = str(codec_name or "").lower()
        labels = {
            "dvvideo": "DV Video",
            "ffv1": "FFV1 lossless",
            "mpeg2video": "MPEG-2 Video",
            "h264": "H.264 / AVC",
            "hevc": "H.265 / HEVC",
            "mjpeg": "Motion JPEG",
            "rawvideo": "Raw Video",
            "prores": "Apple ProRes",
            "vp8": "VP8",
            "vp9": "VP9",
            "av1": "AV1",
        }
        if codec in labels:
            return labels[codec]
        if not codec:
            return "unknown"
        return codec.upper()

    @staticmethod
    def _recording_audio_codec_label(codec_name):
        codec = str(codec_name or "").lower()
        labels = {
            "pcm_s16le": "PCM",
            "pcm_s24le": "PCM",
            "pcm_s32le": "PCM",
            "pcm_s16be": "PCM",
            "pcm_s24be": "PCM",
            "aac": "AAC",
            "mp3": "MP3",
            "ac3": "AC-3",
            "eac3": "E-AC-3",
            "mp2": "MP2",
            "opus": "Opus",
            "vorbis": "Vorbis",
        }
        if codec in labels:
            return labels[codec]
        if not codec:
            return ""
        return codec.upper()

    @staticmethod
    def _recording_standard_label(media_info):
        """Identify the recording standard from detected stream metadata."""
        if media_info.get("is_hdv"):
            mode = TapelessUI._hdv_mode_label(media_info, include_width=False)
            return f"HDV {mode}" if mode else "HDV"

        try:
            height = int(media_info.get("height") or 0)
            fps = float(media_info.get("fps") or 0.0)
        except Exception:
            height = 0
            fps = 0.0

        if height == 576:
            return "PAL"
        if height == 480:
            return "NTSC"

        fps = normalize_fps(fps)
        if any(abs(fps - value) < 0.6 for value in (25.0, 50.0)):
            return "PAL / 50 Hz"
        if any(abs(fps - value) < 0.6 for value in (29.970, 30.0, 59.940, 60.0)):
            return "NTSC / 60 Hz"
        return "unknown"

    @staticmethod
    def _recording_video_mode_label(media_info):
        """Build a compact source mode using the same scan logic as playback."""
        try:
            width = int(media_info.get("width") or 0)
            height = int(media_info.get("height") or 0)
            fps = float(media_info.get("fps") or 0.0)
        except Exception:
            width = 0
            height = 0
            fps = 0.0

        if width <= 0 or height <= 0:
            return "unknown"

        fps = normalize_fps(fps)
        interlaced = bool(media_info.get("interlaced"))
        field_order = str(media_info.get("field_order") or "").lower()

        if interlaced:
            field_rate = fps * 2.0 if fps > 0 else 0.0
            cadence = format_fps(field_rate) if field_rate > 0 else ""
            return f"{width}x{height}i{cadence}" if cadence else f"{width}x{height}i"

        if field_order == "progressive":
            cadence = format_fps(fps) if fps > 0 else ""
            return f"{width}x{height}p{cadence}" if cadence else f"{width}x{height}p"

        if fps > 0:
            return f"{width}x{height} @ {format_fps(fps)} fps"
        return f"{width}x{height}"

    def _probe_recording_details(self, path):
        """Probe selected recording using CamPi's existing video scan logic."""
        path = Path(path)
        media_info = self._probe_video_for_playback(path)

        details = {
            "video_stream": self._recording_codec_label(media_info.get("codec_name")),
            "video_mode": self._recording_video_mode_label(media_info),
            "video_standard": self._recording_standard_label(media_info),
            "sound": "none",
        }

        probe = shutil.which("ffprobe")
        if probe is None:
            return details

        try:
            result = subprocess.run(
                [
                    probe,
                    "-v", "error",
                    "-show_entries",
                    (
                        "stream=codec_type,codec_name,pix_fmt,sample_rate,channels,"
                        "channel_layout,bits_per_sample,bits_per_raw_sample,sample_fmt"
                    ),
                    "-of", "json",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=4,
            )
            if result.returncode != 0:
                return details

            payload = json.loads(result.stdout or "{}")
            streams = payload.get("streams") or []

            video_stream = next(
                (stream for stream in streams if str(stream.get("codec_type")) == "video"),
                None,
            )
            audio_stream = next(
                (stream for stream in streams if str(stream.get("codec_type")) == "audio"),
                None,
            )

            if video_stream is not None:
                codec_name = str(video_stream.get("codec_name") or media_info.get("codec_name") or "")
                details["video_stream"] = self._recording_codec_label(codec_name)

                pix_fmt = str(video_stream.get("pix_fmt") or "").strip()
                if pix_fmt:
                    details["video_stream"] += f" ({pix_fmt})"

            if audio_stream is not None:
                codec_name = str(audio_stream.get("codec_name") or "")
                codec_label = self._recording_audio_codec_label(codec_name)

                try:
                    rate = int(float(audio_stream.get("sample_rate") or 0))
                except Exception:
                    rate = 0

                try:
                    channels = int(audio_stream.get("channels") or 0)
                except Exception:
                    channels = 0

                try:
                    bits = int(
                        audio_stream.get("bits_per_raw_sample")
                        or audio_stream.get("bits_per_sample")
                        or 0
                    )
                except Exception:
                    bits = 0

                sample_fmt = str(audio_stream.get("sample_fmt") or "").lower()

                # Match CamPi's live-DV audio reporting. ffprobe often exposes
                # decoded DV audio as s16 even when the encoded 32 kHz mode is
                # the original 12-bit DV mode.
                is_dv = str(media_info.get("codec_name") or "").lower() == "dvvideo"
                if is_dv:
                    if rate == 48000:
                        bits = 16
                    elif rate == 32000:
                        bits = 12
                elif bits <= 0:
                    match = re.search(r"(?:s|u|flt|dbl)(\d+)", sample_fmt)
                    if match:
                        try:
                            bits = int(match.group(1))
                        except Exception:
                            bits = 0

                parts = []
                if codec_label:
                    parts.append(codec_label)
                if rate > 0:
                    parts.append(f"{rate / 1000:g} kHz")
                if bits > 0:
                    parts.append(f"{bits}-bit")

                if channels == 1:
                    parts.append("mono")
                elif channels == 2:
                    parts.append("stereo")
                elif channels > 2:
                    layout = str(audio_stream.get("channel_layout") or "").strip()
                    parts.append(layout if layout else f"{channels}ch")

                details["sound"] = " ".join(parts) if parts else "detected"

        except Exception:
            pass

        return details

    def open_file_info(self):
        path = self.selected_file
        if path is None or not path.exists():
            self.show_error("File is missing", 3.0)
            return

        self.show_message("Reading recording details...", 1.5)
        self.render()

        stat = path.stat()
        details = self._probe_recording_details(path)

        lines = [
            f"File: {safe_text(path.name, 38)}",
            f"Size: {human_size(stat.st_size)}",
            f"Date: {time.strftime('%Y-%m-%d %H:%M', time.localtime(stat.st_mtime))}",
            f"Stream: {details['video_stream']}",
            f"Mode: {details['video_mode']}",
            f"Standard: {details['video_standard']}",
            f"Audio: {details['sound']}",
            f"Folder: {safe_text(str(path.parent), 38)}",
        ]
        self.open_info("RECORDING DETAILS", lines, parent="file_actions")

    def confirm_delete_file(self):
        path = self.selected_file
        if path is None:
            return
        if self.playback_running() and self.playback_file == path:
            self.show_message("Stop playback before deleting", 3.0)
            return

        def do_delete():
            try:
                path.unlink()
                self.selected_file = None
                self.goto("captures")
                self.show_message("Recording deleted", 2.5)
            except Exception as exc:
                self.goto("file_actions")
                self.show_error(f"Delete failed: {exc}", 4.0)

        self.open_confirm(
            "DELETE RECORDING?",
            [safe_text(path.name, 36), "This cannot be undone."],
            do_delete,
            parent="file_actions",
        )

    # --------------------------------------------------------
    # NETWORK / WI-FI
    # --------------------------------------------------------

    def _run_nmcli(self, args, timeout=8, mutate=False):
        """Run nmcli without ever prompting for a sudo password."""
        if shutil.which("nmcli") is None:
            return None

        commands = [["nmcli", *args]]
        if mutate:
            # Raspberry Pi OS normally permits NetworkManager changes for the
            # logged-in user.  If policy requires elevation, try passwordless
            # sudo rather than hanging the appliance waiting for a password.
            commands.append(["sudo", "-n", "nmcli", *args])

        last = None
        for command in commands:
            try:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                last = result
                if result.returncode == 0:
                    return result
            except Exception:
                continue
        return last

    def refresh_network_status(self, force=False):
        now = time.monotonic()
        if not force and now - self.network_checked < 2.0:
            return self.network_state
        self.network_checked = now

        state = {
            "lan_connected": False,
            "lan_iface": "",
            "lan_connection": "",
            "wifi_connected": False,
            "wifi_iface": "wlan0",
            "wifi_ssid": "",
            "wifi_signal": 0,
            "wifi_radio": False,
            "nmcli": shutil.which("nmcli") is not None,
        }

        if state["nmcli"]:
            radio = self._run_nmcli(["radio", "wifi"], timeout=2)
            if radio and radio.returncode == 0:
                state["wifi_radio"] = radio.stdout.strip().lower() == "enabled"

            result = self._run_nmcli(
                ["-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"],
                timeout=3,
            )
            if result and result.returncode == 0:
                for raw in result.stdout.splitlines():
                    parts = raw.split(":", 3)
                    if len(parts) < 3:
                        continue
                    device = parts[0]
                    dev_type = parts[1]
                    dev_state = parts[2].lower()
                    connection = parts[3] if len(parts) > 3 else ""
                    connected = dev_state.startswith("connected")

                    if dev_type == "ethernet" and not state["lan_iface"]:
                        state["lan_iface"] = device
                        state["lan_connected"] = connected
                        state["lan_connection"] = connection if connected else ""

                    if dev_type == "wifi":
                        if not state["wifi_iface"] or state["wifi_iface"] == "wlan0":
                            state["wifi_iface"] = device
                        if connected:
                            state["wifi_iface"] = device
                            state["wifi_connected"] = True
                            state["wifi_ssid"] = connection

            if state["wifi_connected"] and state["wifi_iface"]:
                result = self._run_nmcli(
                    [
                        "-t", "-f", "IN-USE,SIGNAL,SSID",
                        "device", "wifi", "list", "ifname", state["wifi_iface"],
                    ],
                    timeout=3,
                )
                if result and result.returncode == 0:
                    for raw in result.stdout.splitlines():
                        parts = raw.rsplit(":", 2)
                        if len(parts) >= 3 and parts[0] == "*":
                            try:
                                state["wifi_signal"] = int(parts[1])
                            except Exception:
                                state["wifi_signal"] = 0
                            if parts[2]:
                                state["wifi_ssid"] = parts[2]
                            break
        else:
            # Minimal fallback for systems not using NetworkManager.
            net_root = Path("/sys/class/net")
            if net_root.exists():
                for iface in net_root.iterdir():
                    name = iface.name
                    if name == "lo":
                        continue
                    try:
                        oper = (iface / "operstate").read_text().strip().lower()
                    except Exception:
                        oper = "down"
                    is_wifi = (iface / "wireless").exists()
                    if is_wifi:
                        state["wifi_iface"] = name
                        state["wifi_connected"] = oper == "up"
                    elif not state["lan_iface"]:
                        state["lan_iface"] = name
                        state["lan_connected"] = oper == "up"

        self.network_state = state
        return state

    def interface_ip_addresses(self, iface):
        """Return IPv4/IPv6 addresses assigned specifically to one interface."""
        result = {"ipv4": [], "ipv6": []}
        iface = str(iface or "").strip()
        if not iface:
            return result

        ip_cmd = shutil.which("ip")
        if ip_cmd is not None:
            try:
                proc = subprocess.run(
                    [ip_cmd, "-j", "address", "show", "dev", iface],
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                if proc.returncode == 0:
                    payload = json.loads(proc.stdout or "[]")
                    for item in payload:
                        for addr in item.get("addr_info") or []:
                            family = str(addr.get("family") or "").lower()
                            local = str(addr.get("local") or "").split("%", 1)[0]
                            if not local:
                                continue
                            try:
                                parsed = ipaddress.ip_address(local)
                            except ValueError:
                                continue
                            if parsed.is_loopback:
                                continue
                            if family == "inet" and parsed.version == 4:
                                result["ipv4"].append(parsed.compressed)
                            elif family == "inet6" and parsed.version == 6:
                                if parsed.is_link_local:
                                    result["ipv6"].append(parsed.compressed)
                                else:
                                    result["ipv6"].insert(0, parsed.compressed)
                    return result
            except Exception:
                pass

        if self.network_state.get("nmcli"):
            try:
                proc = self._run_nmcli(
                    ["-g", "IP4.ADDRESS,IP6.ADDRESS", "device", "show", iface],
                    timeout=2,
                )
                if proc and proc.returncode == 0:
                    for raw in proc.stdout.splitlines():
                        raw = raw.strip()
                        if not raw:
                            continue
                        address = raw.split("/", 1)[0].split("%", 1)[0]
                        try:
                            parsed = ipaddress.ip_address(address)
                        except ValueError:
                            continue
                        if parsed.version == 4:
                            result["ipv4"].append(parsed.compressed)
                        elif not parsed.is_loopback:
                            result["ipv6"].append(parsed.compressed)
            except Exception:
                pass

        return result

    def refresh_wifi_networks(self, force=False):
        now = time.monotonic()
        if not force and now - self.wifi_scan_checked < 8.0 and self.wifi_networks:
            return
        self.wifi_scan_checked = now
        self.refresh_network_status(force=True)
        self.wifi_networks = []

        if not self.network_state.get("nmcli"):
            return
        if not self.network_state.get("wifi_radio"):
            return

        iface = self.network_state.get("wifi_iface") or "wlan0"
        result = self._run_nmcli(
            [
                "-t", "-f", "IN-USE,SIGNAL,SECURITY,SSID",
                "device", "wifi", "list", "--rescan", "yes", "ifname", iface,
            ],
            timeout=12,
        )
        if not result or result.returncode != 0:
            return

        # Deduplicate SSIDs, retaining the strongest AP for each network name.
        found = {}
        for raw in result.stdout.splitlines():
            parts = raw.split(":", 3)
            if len(parts) < 4:
                continue
            in_use, signal_text, security, ssid = parts
            ssid = ssid.strip()
            if not ssid:
                continue
            try:
                signal = int(signal_text)
            except Exception:
                signal = 0
            entry = {
                "ssid": ssid,
                "signal": signal,
                "security": security.strip(),
                "in_use": in_use == "*",
            }
            previous = found.get(ssid)
            if previous is None or entry["in_use"] or signal > previous["signal"]:
                found[ssid] = entry

        self.wifi_networks = sorted(
            found.values(),
            key=lambda item: (not item["in_use"], -item["signal"], item["ssid"].lower()),
        )

    def wifi_network_label(self, entry):
        mark = "* " if entry.get("in_use") else ""
        security = entry.get("security", "")
        sec = "OPEN" if not security or security == "--" else "SEC"
        return f"{mark}{entry['ssid']}  {entry.get('signal', 0)}% {sec}"

    def set_wifi_radio(self, enabled):
        result = self._run_nmcli(
            ["radio", "wifi", "on" if enabled else "off"],
            timeout=6,
            mutate=True,
        )
        if result and result.returncode == 0:
            self.refresh_network_status(force=True)
            self.wifi_scan_checked = 0.0
            self.show_message("Wi-Fi enabled" if enabled else "Wi-Fi disabled", 2.0)
            return True
        self.show_message("Unable to change Wi-Fi radio", 3.5)
        return False

    def wifi_security_mode(self, security):
        """Map nmcli scan security text to a NetworkManager key-mgmt mode."""
        sec = (security or "").upper().strip()

        if not sec or sec == "--":
            return "open"
        if "802.1X" in sec or "EAP" in sec or "ENTERPRISE" in sec:
            return "enterprise"
        if "WEP" in sec:
            return "wep"
        if "OWE" in sec:
            return "owe"
        if "SAE" in sec:
            return "sae"
        if "WPA3" in sec and "WPA2" not in sec and "WPA1" not in sec:
            return "sae"
        if "WPA" in sec or "PSK" in sec:
            # NetworkManager documents wpa-psk as supporting WPA2 and WPA3
            # personal transition networks; pure WPA3/SAE is handled above.
            return "wpa-psk"
        return "unknown"

    def find_saved_wifi_profile(self, ssid):
        """Return a saved Wi-Fi profile for this SSID, if NetworkManager has one."""
        result = self._run_nmcli(
            ["-t", "-f", "NAME,UUID,TYPE", "connection", "show"],
            timeout=4,
        )
        if not result or result.returncode != 0:
            return None

        for raw in result.stdout.splitlines():
            parts = raw.split(":", 2)
            if len(parts) != 3:
                continue
            name, uuid, con_type = parts
            if con_type not in ("wifi", "802-11-wireless"):
                continue

            probe = self._run_nmcli(
                ["-g", "802-11-wireless.ssid", "connection", "show", "uuid", uuid],
                timeout=3,
            )
            if probe and probe.returncode == 0 and probe.stdout.strip() == ssid:
                return {"name": name, "uuid": uuid}

        return None

    def wifi_error(self, error, parent="wifi_networks"):
        """Show the full NetworkManager error instead of truncating it in the footer."""
        message = (error or "Wi-Fi connection failed").strip()
        # Strip the common leading 'Error:' to save space on the tiny LCD.
        message = re.sub(r"^Error:\s*", "", message, flags=re.I)
        lines = []
        for paragraph in message.splitlines() or [message]:
            wrapped = textwrap.wrap(paragraph, width=36) or [""]
            lines.extend(wrapped)
        self.open_info("WI-FI ERROR", lines[:5], parent=parent)

    def connect_saved_wifi_profile(self, profile, iface):
        result = self._run_nmcli(
            ["connection", "up", "uuid", profile["uuid"], "ifname", iface],
            timeout=20,
            mutate=True,
        )
        return result

    def connect_wifi_entry(self, entry):
        ssid = entry.get("ssid", "")
        if not ssid:
            return
        if entry.get("in_use"):
            self.show_message(f"Connected: {ssid}", 2.0)
            return

        iface = self.network_state.get("wifi_iface") or "wlan0"
        security = entry.get("security", "")
        mode = self.wifi_security_mode(security)

        if mode == "enterprise":
            self.open_info(
                "WI-FI",
                ["Enterprise / 802.1X", "Wi-Fi is not configured", "by this applet."],
                parent="wifi_networks",
            )
            return

        # If NetworkManager already has a profile for this SSID, activate it
        # directly.  This avoids 'device wifi connect' creating/reusing a
        # half-configured profile with a missing key-mgmt property.
        saved = self.find_saved_wifi_profile(ssid)
        if saved is not None:
            result = self.connect_saved_wifi_profile(saved, iface)
            if result and result.returncode == 0:
                self.refresh_network_status(force=True)
                self.refresh_wifi_networks(force=True)
                self.goto("network")
                self.show_message(f"Connected: {ssid}", 3.0)
                return

        if mode in ("wpa-psk", "sae", "wep"):
            self.wifi_target_ssid = ssid
            self.wifi_target_security = security
            self.wifi_password = ""
            self.wifi_password_visible = False
            self.keyboard_shift = False
            self.keyboard_symbols = False
            self.keyboard_index = 0
            self.goto("wifi_password")
            return

        if mode == "owe":
            # OWE is passwordless but still requires an explicit security mode.
            profile_name = f"CamPi WiFi - {ssid}"[:100]
            existing = self.find_saved_wifi_profile(ssid)
            if existing is None:
                add = self._run_nmcli(
                    ["connection", "add", "type", "wifi", "ifname", iface,
                     "con-name", profile_name, "ssid", ssid],
                    timeout=8,
                    mutate=True,
                )
                if not add or add.returncode != 0:
                    error = ((add.stderr or add.stdout).strip() if add else "Unable to create Wi-Fi profile")
                    self.wifi_error(error)
                    return
                existing = self.find_saved_wifi_profile(ssid)

            ident = ["uuid", existing["uuid"]] if existing else ["id", profile_name]
            setsec = self._run_nmcli(
                ["connection", "modify", *ident,
                 "802-11-wireless-security.key-mgmt", "owe",
                 "connection.autoconnect", "yes"],
                timeout=8,
                mutate=True,
            )
            if not setsec or setsec.returncode != 0:
                error = ((setsec.stderr or setsec.stdout).strip() if setsec else "Unable to configure OWE")
                self.wifi_error(error)
                return
            profile = existing or self.find_saved_wifi_profile(ssid)
            result = self.connect_saved_wifi_profile(profile, iface) if profile else None
            if result and result.returncode == 0:
                self.refresh_network_status(force=True)
                self.refresh_wifi_networks(force=True)
                self.goto("network")
                self.show_message(f"Connected: {ssid}", 3.0)
                return
            error = ((result.stderr or result.stdout).strip() if result else "Connection failed")
            self.wifi_error(error)
            return

        # Truly open network.
        result = self._run_nmcli(
            ["device", "wifi", "connect", ssid, "ifname", iface],
            timeout=18,
            mutate=True,
        )
        if result and result.returncode == 0:
            self.refresh_network_status(force=True)
            self.refresh_wifi_networks(force=True)
            self.goto("network")
            self.show_message(f"Connected: {ssid}", 3.0)
            return

        error = ""
        if result:
            error = (result.stderr or result.stdout).strip()
        self.wifi_error(error or "Connection failed")

    def join_wifi_with_password(self):
        ssid = self.wifi_target_ssid
        password = self.wifi_password
        if not ssid:
            self.goto("wifi_networks")
            return

        mode = self.wifi_security_mode(self.wifi_target_security)

        if mode == "wpa-psk":
            is_64_hex = len(password) == 64 and all(ch in "0123456789abcdefABCDEF" for ch in password)
            if not (8 <= len(password) <= 63 or is_64_hex):
                self.show_message("WPA password: 8-63 chars", 3.0)
                return
        elif mode == "sae":
            if not password:
                self.show_message("Enter the Wi-Fi password", 3.0)
                return
        elif mode == "wep":
            if not password:
                self.show_message("Enter the WEP key", 3.0)
                return
        else:
            self.wifi_error("Unsupported Wi-Fi security type", parent="wifi_password")
            return

        iface = self.network_state.get("wifi_iface") or "wlan0"
        saved = self.find_saved_wifi_profile(ssid)

        # Reuse an existing NetworkManager profile if there is one; otherwise
        # create a profile owned by this Tapeless UI.  Crucially, configure
        # key-mgmt explicitly before trying to activate the profile.
        if saved is None:
            profile_name = f"CamPi WiFi - {ssid}"[:100]
            add = self._run_nmcli(
                ["connection", "add", "type", "wifi", "ifname", iface,
                 "con-name", profile_name, "ssid", ssid],
                timeout=8,
                mutate=True,
            )
            if not add or add.returncode != 0:
                error = ((add.stderr or add.stdout).strip() if add else "Unable to create Wi-Fi profile")
                self.wifi_error(error, parent="wifi_password")
                return
            saved = self.find_saved_wifi_profile(ssid)

        if saved is None:
            self.wifi_error("Wi-Fi profile was created but could not be found", parent="wifi_password")
            return

        ident = ["uuid", saved["uuid"]]

        if mode in ("wpa-psk", "sae"):
            modify_args = [
                "connection", "modify", *ident,
                "802-11-wireless-security.key-mgmt", mode,
                "802-11-wireless-security.psk", password,
                "802-11-wireless-security.psk-flags", "0",
                "connection.autoconnect", "yes",
            ]
        else:
            # Legacy WEP.  If the text already looks like a raw WEP key use
            # key type 1; otherwise let NetworkManager derive one as a passphrase.
            raw_lengths = (5, 13)
            raw_hex = len(password) in (10, 26) and all(ch in "0123456789abcdefABCDEF" for ch in password)
            wep_type = "1" if len(password) in raw_lengths or raw_hex else "2"
            modify_args = [
                "connection", "modify", *ident,
                "802-11-wireless-security.key-mgmt", "none",
                "802-11-wireless-security.wep-key0", password,
                "802-11-wireless-security.wep-key-type", wep_type,
                "802-11-wireless-security.wep-key-flags", "0",
                "connection.autoconnect", "yes",
            ]

        configured = self._run_nmcli(modify_args, timeout=10, mutate=True)
        if not configured or configured.returncode != 0:
            error = ((configured.stderr or configured.stdout).strip() if configured else "Unable to configure Wi-Fi security")
            self.wifi_error(error, parent="wifi_password")
            return

        result = self.connect_saved_wifi_profile(saved, iface)
        if result and result.returncode == 0:
            self.wifi_password = ""
            self.wifi_password_visible = False
            self.refresh_network_status(force=True)
            self.refresh_wifi_networks(force=True)
            self.goto("network")
            self.show_message(f"Connected: {ssid}", 3.0)
            return

        error = ""
        if result:
            error = (result.stderr or result.stdout).strip()
        self.wifi_error(error or "Wi-Fi connection failed", parent="wifi_password")

    def keyboard_keys(self):
        if self.keyboard_symbols:
            return [
                "!", "@", "#", "$", "%", "^", "&", "*", "(", ")",
                "`", "~", "-", "_", "=", "+", "[", "]", "{", "}",
                ";", ":", "'", '"', ",", ".", "<", ">", "?", "/",
                "ABC", "DEL", "\\", "|", "~", "`", "^", "&", "*", "+",
            ]

        letters1 = list("qwertyuiop")
        letters2 = list("asdfghjkl@")
        letters3 = ["SHIFT", "SYM", "z", "x", "c", "v", "b", "n", "m", "DEL"]
        if self.keyboard_shift:
            letters1 = [char.upper() for char in letters1]
            letters2 = [char.upper() if len(char) == 1 and char.isalpha() else char for char in letters2]
            letters3 = [char.upper() if len(char) == 1 and char.isalpha() else char for char in letters3]
        return list("1234567890") + letters1 + letters2 + letters3

    def activate_keyboard_key(self, key):
        if key == "SHIFT":
            self.keyboard_shift = not self.keyboard_shift
            return
        if key == "SYM":
            self.keyboard_symbols = True
            self.keyboard_shift = False
            self.keyboard_index = 0
            return
        if key == "ABC":
            self.keyboard_symbols = False
            self.keyboard_shift = False
            self.keyboard_index = 0
            return
        if key == "DEL":
            self.wifi_password = self.wifi_password[:-1]
            return
        if len(self.wifi_password) < 63:
            self.wifi_password += key

    def open_network_status(self):
        state = self.refresh_network_status(force=True)

        try:
            hostname = socket.gethostname()
        except Exception:
            hostname = "campi"

        lan_iface = state.get("lan_iface") or ""
        wifi_iface = state.get("wifi_iface") or ""
        lan_ips = self.interface_ip_addresses(lan_iface) if lan_iface else {"ipv4": [], "ipv6": []}
        wifi_ips = self.interface_ip_addresses(wifi_iface) if wifi_iface else {"ipv4": [], "ipv6": []}

        lan_label = f"LAN ({lan_iface})" if lan_iface else "LAN"
        wifi_label = f"Wi-Fi ({wifi_iface})" if wifi_iface else "Wi-Fi"

        if state.get("wifi_connected"):
            wifi_state = state.get("wifi_ssid") or "connected"
            signal = int(state.get("wifi_signal", 0) or 0)
            if signal:
                wifi_state += f" {signal}%"
        else:
            wifi_state = "disconnected"

        lines = [
            f"Host: {hostname}",
            f"{lan_label}: " + ("connected" if state.get("lan_connected") else "disconnected"),
        ]

        if state.get("lan_connected"):
            lines.append(f"LAN IPv4: {lan_ips['ipv4'][0] if lan_ips['ipv4'] else 'none'}")
            lines.append(f"LAN IPv6: {lan_ips['ipv6'][0] if lan_ips['ipv6'] else 'none'}")

        lines.append(f"{wifi_label}: {safe_text(wifi_state, 27)}")

        if state.get("wifi_connected"):
            lines.append(f"Wi-Fi IPv4: {wifi_ips['ipv4'][0] if wifi_ips['ipv4'] else 'none'}")
            lines.append(f"Wi-Fi IPv6: {wifi_ips['ipv6'][0] if wifi_ips['ipv6'] else 'none'}")

        lines.append("Manager: " + ("OK" if state.get("nmcli") else "unavailable"))
        self.open_info("NETWORK STATUS", lines, parent="network")

    # --------------------------------------------------------
    # STATUS / SYSTEM
    # --------------------------------------------------------

    def storage_free_space_lines(self, force_external=False):
        """Return SD free space and mounted USB free space when available."""
        lines = []
        try:
            usage = shutil.disk_usage("/")
            lines.append(f"SD: {usage.free / (1024 ** 3):.1f} GB free")
        except Exception:
            lines.append("SD: unavailable")

        external = self.external_storage_info(force=force_external)
        if external.get("mounted") and external.get("mountpoint"):
            try:
                usage = shutil.disk_usage(str(external["mountpoint"]))
                lines.append(f"USB: {usage.free / (1024 ** 3):.1f} GB free")
            except Exception:
                lines.append("USB: unavailable")
        return lines

    def open_storage_status(self):
        name, path, _free_gb = self.storage()
        lines = [
            f"Policy: {storage_policy_label(self.storage_policy)}",
            f"Active: {name}",
        ]
        lines.extend(self.storage_free_space_lines(force_external=True))
        lines.append(f"Folder: {path}")
        self.open_info("STORAGE STATUS", lines, parent="storage")

    def open_device_status(self):
        name, _, _free_gb = self.storage()
        self.firewire_camera_available(force=True)
        if self.firewire_camera_present:
            firewire = "Camera connected"
        elif self.firewire_controller_present:
            firewire = "Controller only"
        else:
            firewire = "Not detected"
        analog_device = self.discover_analog_video_device(force=True)
        analog = "OK" if analog_device else "Not detected"
        touch = "OK" if self.touch_enabled else "Unavailable"
        hdmi = "Connected" if self.refresh_hdmi_detection(force=True) else "Not detected"

        state = self.refresh_network_status(force=True)
        lan_iface = state.get("lan_iface") or ""
        wifi_iface = state.get("wifi_iface") or ""
        lan_ips = self.interface_ip_addresses(lan_iface) if state.get("lan_connected") else {"ipv4": []}
        wifi_ips = self.interface_ip_addresses(wifi_iface) if state.get("wifi_connected") else {"ipv4": []}

        lines = [
            f"FireWire: {firewire}",
            f"Analog: {analog}",
            f"Touch: {touch}",
            f"HDMI: {hdmi}",
            f"Storage: {name}",
        ]
        lines.extend(self.storage_free_space_lines(force_external=True))
        if lan_ips.get("ipv4"):
            lines.append(f"LAN IPv4: {lan_ips['ipv4'][0]}")
        if wifi_ips.get("ipv4"):
            lines.append(f"Wi-Fi IPv4: {wifi_ips['ipv4'][0]}")
        self.open_info("DEVICE OVERVIEW", lines, parent="system")

    def confirm_recalibration(self):
        def do_recalibrate():
            if self.touch is None:
                self.goto("system")
                self.show_error("Touch controller unavailable", 4.0)
                return
            try:
                self.calibration = calibrate_touch(self.device, self.touch)
                self.touch_enabled = True
                self.goto("system")
                self.show_message("Touch calibration saved", 3.0)
            except Exception as exc:
                self.goto("system")
                self.show_error(f"Calibration failed: {exc}", 4.0)

        self.open_confirm(
            "RECALIBRATE?",
            ["Only needed if touch", "alignment becomes incorrect."],
            do_recalibrate,
            parent="system",
        )

    def _system_command(self, action):
        command = ["sudo", "-n", "systemctl", action]
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
            if result.returncode != 0:
                self.goto("power")
                self.show_message("System command needs sudo permission", 4.0)
        except subprocess.TimeoutExpired:
            # A successful reboot/poweroff can interrupt the command while the system changes state.
            pass
        except Exception as exc:
            self.goto("power")
            self.show_error(f"System command failed: {exc}", 4.0)

    def confirm_reboot(self):
        self.open_confirm(
            "REBOOT?",
            ["Restart the Raspberry Pi?"],
            lambda: self._system_command("reboot"),
            parent="power",
        )

    def confirm_shutdown(self):
        self.open_confirm(
            "SHUT DOWN?",
            ["Safely power off the Pi?"],
            lambda: self._system_command("poweroff"),
            parent="power",
        )

    def keyboard_layout(self):
        keys = self.keyboard_keys()
        rows = [keys[0:10], keys[10:20], keys[20:30], keys[30:40]]
        return rows

    def keyboard_key_boxes(self):
        rows = self.keyboard_layout()
        boxes = []

        # Password screen has very little reason to waste vertical space.
        # Start immediately below the password field and use virtually the
        # entire area down to the persistent footer.
        y0 = 60
        row_h = 33
        margin = 4

        for row_index, row in enumerate(rows):
            if row_index < 3:
                widths = [31] * 10
            else:
                # Long special keys get more room while still fitting 320 px.
                widths = []
                for key in row:
                    if key in ("SHIFT", "SYM", "ABC", "DEL"):
                        widths.append(43)
                    else:
                        widths.append(23)
                total = sum(widths)
                if total > WIDTH - (margin * 2):
                    scale = (WIDTH - (margin * 2)) / total
                    widths = [max(19, int(w * scale)) for w in widths]

            x = margin
            y = y0 + (row_index * row_h)
            for key, width in zip(row, widths):
                boxes.append((key, (x, y, x + width - 2, y + row_h - 3)))
                x += width

        return boxes

    def keyboard_key_at(self, x, y):
        for key, box in self.keyboard_key_boxes():
            x1, y1, x2, y2 = box
            if x1 <= x <= x2 and y1 <= y <= y2:
                return key
        return None

    def password_eye_box(self):
        # Generous hit target for a resistive panel; the visible eye itself is
        # smaller and centred inside this box.
        return (278, 35, 313, 57)

    def password_eye_hit(self, x, y):
        x1, y1, x2, y2 = self.password_eye_box()
        return x1 <= x <= x2 and y1 <= y <= y2

    def draw_password_eye(self, draw):
        x1, y1, x2, y2 = self.password_eye_box()
        cx = (x1 + x2) // 2
        cy = (y1 + y2) // 2

        # Simple eye glyph drawn in Pillow so no extra icon font is required.
        eye_colour = ACCENT if self.wifi_password_visible else TEXT_DIM
        draw.arc((cx - 11, cy - 7, cx + 11, cy + 7), 200, 340, fill=eye_colour, width=2)
        draw.arc((cx - 11, cy - 7, cx + 11, cy + 7), 20, 160, fill=eye_colour, width=2)
        draw.ellipse((cx - 2, cy - 2, cx + 2, cy + 2), fill=eye_colour)

        # A slash communicates the current hidden state.
        if not self.wifi_password_visible:
            draw.line((cx - 10, cy - 8, cx + 10, cy + 8), fill=RED, width=2)

    def draw_wifi_password(self, draw):
        # Deliberately do not display the SSID on this screen. This keeps the
        # password-entry view private when the unit is being used in public.
        preview_box = (7, 35, WIDTH - 7, 57)
        draw.rounded_rectangle(preview_box, radius=4, fill=PANEL_ALT, outline=BORDER)

        if self.wifi_password:
            if self.wifi_password_visible:
                display = self.wifi_password[-31:]
            else:
                display = "*" * min(len(self.wifi_password), 31)
            preview_fill = TEXT
        else:
            display = "Enter password"
            preview_fill = TEXT_DIM

        # Reserve the right side of the field for the eye button.
        draw.text((12, 39), display, font=FONT_SMALL, fill=preview_fill)
        draw.line((275, 38, 275, 54), fill=BORDER, width=1)
        self.draw_password_eye(draw)

        for index, (key, box) in enumerate(self.keyboard_key_boxes()):
            focused = self.keyboard_index == index
            fill = SELECTED if focused else PANEL_ALT
            outline = ACCENT if focused else BORDER
            draw.rounded_rectangle(box, radius=3, fill=fill, outline=outline, width=2 if focused else 1)
            label = key
            font = FONT_TINY if len(label) > 2 else FONT_SMALL
            centered_text(draw, box, label, font, TEXT)

    def draw_network_icons(self, draw):
        state = self.refresh_network_status()
        cy = 16

        # LAN: 20x18 RJ45-style port, centre-aligned with the other status icons.
        cx = 258
        if self.header_feedback == "lan":
            draw.rounded_rectangle((246, 3, 270, 29), radius=4, fill=SELECTED, outline=ACCENT, width=2)
        colour = GREEN if state.get("lan_connected") else TEXT_DIM
        draw.rounded_rectangle((cx - 9, cy - 6, cx + 9, cy + 5), radius=2, outline=colour, width=2)
        for pin_x in (cx - 5, cx - 1, cx + 3):
            draw.line((pin_x, cy - 6, pin_x, cy - 2), fill=colour, width=1)
        draw.line((cx - 5, cy + 8, cx + 5, cy + 8), fill=colour, width=2)
        draw.line((cx, cy + 5, cx, cy + 8), fill=colour, width=2)
        if not state.get("lan_connected"):
            draw.line((cx - 9, cy - 8, cx + 9, cy + 8), fill=RED, width=2)

        # Wi-Fi: 20x18 arc glyph, centred on the same y axis.
        cx = 294
        if self.header_feedback == "wifi":
            draw.rounded_rectangle((282, 3, 318, 29), radius=4, fill=SELECTED, outline=ACCENT, width=2)
        connected = state.get("wifi_connected")
        signal = int(state.get("wifi_signal", 0) or 0)
        levels = 1 + (1 if signal >= 35 else 0) + (1 if signal >= 65 else 0) if connected else 0
        arcs = [
            (cx - 9, cy - 8, cx + 9, cy + 10),
            (cx - 6, cy - 5, cx + 6, cy + 7),
            (cx - 3, cy - 2, cx + 3, cy + 4),
        ]
        for i, box in enumerate(arcs):
            needed = 3 - i
            colour = GREEN if levels >= needed else TEXT_DIM
            draw.arc(box, start=215, end=325, fill=colour, width=2)
        draw.ellipse((cx - 2, cy + 6, cx + 2, cy + 10), fill=GREEN if connected else TEXT_DIM)
        if not connected:
            draw.line((cx - 9, cy - 8, cx + 9, cy + 8), fill=RED, width=2)

    def draw_hdmi_icon(self, draw):
        connected = self.refresh_hdmi_detection()
        if not connected and not self.hdmi_missing_flash:
            return
        # Compact text mark is visually distinct from LAN/Wi-Fi glyphs. When a
        # user requests playback with no HDMI sink, the same marker flashes red
        # six times so the warning appears exactly where HDMI status normally lives.
        label = "HDMI"
        font = FONT_HEADER_TINY
        bbox = draw.textbbox((0, 0), label, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        x = 224 - (tw // 2)
        y = 16 - (th // 2) - bbox[1]
        colour = SONY_RED if self.hdmi_missing_flash else SONY_GREEN
        draw.text((x, y), label, font=font, fill=colour)

    def flash_hdmi_missing(self):
        """Flash the header HDMI marker red six times without starting playback."""
        for _ in range(6):
            self.hdmi_missing_flash = True
            self.render()
            time.sleep(0.12)
            self.hdmi_missing_flash = False
            self.render()
            time.sleep(0.10)

    # --------------------------------------------------------
    # DRAWING
    # --------------------------------------------------------

    def _header_title_font(self, title, max_width, sorny=False):
        """Fit ordinary header titles before resorting to horizontal scrolling."""
        path = SORNY_FONT_BOLD_PATH if sorny else FONT_BOLD_PATH
        for size in (16, 15, 14, 13):
            font = load_font(path, size)
            try:
                bbox = font.getbbox(str(title))
                width = bbox[2] - bbox[0]
            except Exception:
                width = max_width + 1
            if width <= max_width:
                return font
        return load_font(path, 13)

    def draw_header(self, draw):
        sorny_menu = (
            self.ui_theme == "sorny"
            and (self.sorny_block_enabled() or self.sorny_cylinder_enabled())
        )
        panafonics = self.ui_theme == "panafonics"
        jayvc = self.ui_theme == "jayvc" and not self.hdmi_playback_context()
        header_fill = PANA_TITLE_BG if panafonics else (JAYVC_BAR if jayvc else HEADER_BAR_BG)
        header_text = (
            PANA_TITLE_TEXT
            if panafonics
            else (JAYVC_TEXT if jayvc else (SORNY_HEADER_TEXT if sorny_menu else TEXT))
        )

        # Use all genuinely available title space. Reserve the recording dot
        # only while recording, HDMI only when connected, otherwise stop just
        # before the LAN icon. Titles are reduced slightly before any scrolling
        # is allowed, so short headings never move while blank bar space exists.
        hdmi_present = self.refresh_hdmi_detection()
        hdmi_visible = hdmi_present or self.hdmi_missing_flash
        if self.recording:
            title_right = 164
        elif hdmi_visible:
            title_right = 205
        else:
            title_right = 240

        title = self.page_title()
        title_width = max(20, title_right - 8)
        header_font = self._header_title_font(title, title_width, sorny=sorny_menu)

        draw.rectangle((0, 0, WIDTH, HEADER_H), fill=header_fill)
        draw.line(
            (0, HEADER_H - 1, WIDTH, HEADER_H - 1),
            fill=PANA_BG_DARK if panafonics else (JAYVC_BAR_EDGE if jayvc else HEADER_BAR_EDGE),
            width=1,
        )

        if self.page == "home" and self.header_feedback == "power":
            # Match the brief pressed feedback used by the network shortcuts.
            # Keep it behind the HOME title and clear of the HDMI/LAN/Wi-Fi area.
            feedback_right = min(178, title_right - 4)
            if feedback_right > 8:
                feedback_fill = PANA_BG if panafonics else (JAYVC_SELECT_DARK if jayvc else SELECTED)
                feedback_outline = PANA_ORANGE if panafonics else (JAYVC_FRAME_HI if jayvc else ACCENT)
                draw.rounded_rectangle(
                    (4, 3, feedback_right, 29),
                    radius=4,
                    fill=feedback_fill,
                    outline=feedback_outline,
                    width=2,
                )

        if panafonics or jayvc:
            try:
                title_bbox = header_font.getbbox(title)
                title_text_width = title_bbox[2] - title_bbox[0]
            except Exception:
                title_text_width = title_width + 1

            if title_text_width <= title_width:
                centered_text(
                    draw,
                    (8, 4, title_right, 28),
                    title,
                    header_font,
                    header_text,
                )
            else:
                scrolling_text(
                    draw,
                    (8, 4, title_right, 28),
                    title,
                    header_font,
                    header_text,
                    speed=34.0,
                    hold=0.8,
                    background=header_fill,
                )
        else:
            scrolling_text(
                draw,
                (8, 4, title_right, 28),
                title,
                header_font,
                header_text,
                speed=34.0,
                hold=0.8,
                background=header_fill,
            )

        # Recording dot is deliberately separated from the HDMI text mark.
        if self.recording:
            draw.ellipse((172, 10, 184, 22), fill=SONY_RED, outline=TEXT, width=1)

        self.draw_hdmi_icon(draw)
        self.draw_network_icons(draw)

    def draw_home(self, draw):
        source_available = self.selected_source_available()
        sorny = self.ui_theme == "sorny"
        panafonics = self.ui_theme == "panafonics"
        jayvc = self.ui_theme == "jayvc"

        if sorny:
            home_bg = SORNY_BG
            info_colour = SORNY_ACCENT_BRIGHT
            ready_colour = SORNY_SUCCESS
            warning_colour = SORNY_WARNING
            dim_colour = SORNY_TEXT_DIM
        elif panafonics:
            home_bg = PANA_BG
            info_colour = PANA_CYAN
            ready_colour = PANA_GREEN
            warning_colour = PANA_YELLOW
            dim_colour = PANA_TEXT_DIM
            draw.rectangle(
                (0, HEADER_H, WIDTH, HEIGHT - FOOTER_H),
                fill=PANA_BG,
            )
        elif jayvc:
            home_bg = JAYVC_BG
            info_colour = JAYVC_FRAME_HI
            ready_colour = JAYVC_GREEN
            warning_colour = JAYVC_WARNING
            dim_colour = JAYVC_TEXT_DIM
            draw.rectangle(
                (0, HEADER_H, WIDTH, HEIGHT - FOOTER_H),
                fill=JAYVC_BG,
            )
        else:
            home_bg = BG
            info_colour = SONY_CYAN
            ready_colour = SONY_YELLOW
            warning_colour = AMBER
            dim_colour = TEXT_DIM

        if self.recording:
            # Sony-style recording readout: REC and elapsed time share one
            # horizontal band, avoiding the vertical collision seen with large
            # status fonts while keeping both immediately legible.
            timer_text = format_elapsed(time.monotonic() - self.record_started)
            centered_text(
                draw,
                (18, 28, 112, 72),
                "REC",
                FONT_STATUS_BIG,
                SONY_RED,
                fontmode="1",
            )
            centered_text(
                draw,
                (120, 33, WIDTH - 12, 69),
                timer_text,
                FONT_STATUS_BOLD,
                info_colour,
                fontmode="1",
            )
        else:
            main_text = "NO INPUT" if not source_available else "READY"
            colour = warning_colour if not source_available else ready_colour
            bbox = draw.textbbox((0, 0), main_text, font=FONT_STATUS_BIG)
            text_width = bbox[2] - bbox[0]
            draw_text(
                draw,
                ((WIDTH - text_width) // 2, 33),
                main_text,
                FONT_STATUS_BIG,
                colour,
                fontmode="1",
            )

        # HOME detail rows use a strict shared grid. Labels share one left
        # edge, values share another, and every row uses the same height/gap.
        # Using scrolling_text for all value cells also keeps short strings
        # left-aligned instead of centring only some rows.
        row_input = (88, 109)
        row_video = (111, 132)
        row_audio = (134, 155)
        row_store = (157, 178)
        label_x1 = 22
        label_x2 = 80
        value_x1 = 92
        value_x2 = WIDTH - 8

        mode_text = "LIVE DV" if self.capture_mode == "dv" else "ANALOG"
        home_label_colour = PANA_TEXT_DIM if panafonics else (JAYVC_TEXT_DIM if jayvc else TEXT_DIM)
        main_text_colour = JAYVC_TEXT if jayvc else TEXT
        input_colour = main_text_colour if source_available else warning_colour
        scrolling_text(
            draw,
            (label_x1, row_input[0], label_x2, row_input[1]),
            "INPUT",
            FONT_SMALL,
            home_label_colour,
            background=home_bg,
        )
        scrolling_text(
            draw,
            (value_x1, row_input[0], value_x2, row_input[1]),
            mode_text,
            FONT_BOLD,
            input_colour,
            background=home_bg,
        )

        if not source_available:
            video_text = self.selected_source_offline_text()
            video_colour = warning_colour
        elif self.capture_mode == "analog":
            mode = self.selected_analog_mode()
            video_text = self.analog_mode_label(mode)
            video_colour = main_text_colour
        else:
            dv_standard = self.refresh_dv_stream_standard()
            if dv_standard:
                video_text = dv_standard
                video_colour = info_colour
            else:
                transport_hint = self.refresh_dv_transport_hint()
                video_text = transport_hint or "DV STREAM DETECTING"
                video_colour = warning_colour if transport_hint else dim_colour

        scrolling_text(
            draw,
            (label_x1, row_video[0], label_x2, row_video[1]),
            "VIDEO",
            FONT_SMALL,
            home_label_colour,
            background=home_bg,
        )
        scrolling_text(
            draw,
            (value_x1, row_video[0], value_x2, row_video[1]),
            video_text,
            FONT_NORMAL,
            video_colour,
            background=home_bg,
        )

        if self.capture_mode == "dv" and source_available:
            audio_text = self.dv_stream_audio or "AUDIO DETECTING"
            audio_colour = main_text_colour if self.dv_stream_audio else dim_colour
        elif self.capture_mode == "analog" and source_available:
            audio_text = self.analog_audio_name or "USB AUDIO"
            audio_colour = main_text_colour
        else:
            audio_text = "--"
            audio_colour = dim_colour

        scrolling_text(
            draw,
            (label_x1, row_audio[0], label_x2, row_audio[1]),
            "AUDIO",
            FONT_SMALL,
            home_label_colour,
            background=home_bg,
        )
        scrolling_text(
            draw,
            (value_x1, row_audio[0], value_x2, row_audio[1]),
            audio_text,
            FONT_NORMAL,
            audio_colour,
            background=home_bg,
        )

        storage_name, _, free_gb = self.storage()
        display_name = storage_display_name(storage_name)
        if free_gb is None:
            storage_text = f"{display_name} unavailable"
        else:
            storage_text = f"{display_name}  {free_gb:.1f} GB FREE"

        scrolling_text(
            draw,
            (label_x1, row_store[0], label_x2, row_store[1]),
            "STORE",
            FONT_SMALL,
            home_label_colour,
            background=home_bg,
        )
        scrolling_text(
            draw,
            (value_x1, row_store[0], value_x2, row_store[1]),
            storage_text,
            FONT_NORMAL,
            main_text_colour,
            background=home_bg,
        )

        if self.message:
            centered_text(
                draw,
                (8, 180, WIDTH - 8, HEIGHT - FOOTER_H - 2),
                safe_text(self.message, 44),
                FONT_TINY,
                warning_colour,
            )

    def draw_menu(self, draw):
        if self.ui_theme == "panafonics":
            if self.hdmi_playback_context() and self.page != "playback":
                self.draw_default_menu(draw)
            else:
                self.draw_panafonics_menu(draw)
            return

        if self.ui_theme == "jayvc":
            if self.hdmi_playback_context() and self.page != "playback":
                self.draw_default_menu(draw)
            else:
                self.draw_jayvc_menu(draw)
            return

        if self.ui_theme == "sorny":
            if self.sorny_block_enabled():
                self.draw_sorny_blocks(draw)
                return
            if self.sorny_cylinder_enabled():
                self.draw_sorny_cylinder(draw)
                return
        self.draw_default_menu(draw)

    def _jayvc_split_label(self, label):
        """Split ordinary 'label: value' rows into JVC-style two-part fields."""
        value = str(label or "")
        if self.page in ("error_log", "wifi_networks"):
            return value, ""

        if ":" in value:
            left, right = value.split(":", 1)
            if left.strip() and right.strip():
                return left.strip(), right.strip()
        return value, ""

    def _jayvc_fit_font(self, text, width, bold=True, max_size=16, min_size=9):
        """Fit a JVC menu string into a known pixel width before scrolling it."""
        path = FONT_BOLD_PATH if bold else FONT_NORMAL_PATH
        text = str(text or "")
        width = max(8, int(width))
        for size in range(int(max_size), int(min_size) - 1, -1):
            font = load_font(path, size)
            try:
                bbox = font.getbbox(text)
                text_width = bbox[2] - bbox[0]
            except Exception:
                text_width = len(text) * size
            if text_width <= width:
                return font
        return load_font(path, int(min_size))

    def _jayvc_action_colour(self, action):
        """Muted multi-colour icon accents taken from period JVC menu language."""
        action = str(action or "").lower()
        if action in ("capture", "dv_live", "analog_modes") or action.startswith("analog_mode::"):
            return JAYVC_CYAN
        if action in ("captures", "play_file", "file_info") or action.startswith("file::"):
            return JAYVC_GREEN
        if action in ("storage", "storage_status", "storage_auto", "storage_internal", "storage_ssd"):
            return JAYVC_WARNING
        if action in ("system", "interface", "recalibrate", "font_size", "theme") or action.startswith("theme_"):
            return JAYVC_MAGENTA
        if action in ("network", "network_status", "wifi_networks", "wifi_toggle", "network_refresh") or action.startswith("wifi::"):
            return JAYVC_CYAN
        if action in ("power", "shutdown", "delete_file", "analog_reset", "reset_picture"):
            return JAYVC_RED
        if action in ("reboot", "refresh_analog", "error_log"):
            return JAYVC_WARNING
        if action in ("credits", "device_status", "analog_caps"):
            return JAYVC_GREEN
        if action in ("set_analog", "analog_settings", "analog_picture") or action.startswith("analog_control::"):
            return JAYVC_MAGENTA
        if action.startswith("buzzer"):
            return JAYVC_GREEN
        return JAYVC_FRAME_HI

    def _draw_jayvc_menu_icon(self, draw, box, action, selected=False):
        """Re-use the compact CamPi glyph language, recoloured for JVC menus."""
        action = str(action or "")
        if action.startswith("file::") or action in ("noop", "capture_filter"):
            return

        scratch_size = 32
        scratch = Image.new("RGBA", (scratch_size, scratch_size), (0, 0, 0, 0))
        scratch_draw = ImageDraw.Draw(scratch)
        self._draw_sorny_category_glyph(
            scratch_draw,
            (0, 0, scratch_size, scratch_size),
            action,
            selected=False,
            _zoom_pass=True,
            _rgba=True,
        )

        alpha = scratch.getchannel("A")
        colour = JAYVC_TEXT if selected else self._jayvc_action_colour(action)
        tinted = Image.new("RGBA", scratch.size, tuple(colour) + (0,))
        tinted.putalpha(alpha)

        x1, y1, x2, y2 = box
        target = max(12, min(x2 - x1, y2 - y1, 21))
        resample = Image.Resampling.NEAREST if hasattr(Image, "Resampling") else Image.NEAREST
        tinted = tinted.resize((target, target), resample=resample)
        px = x1 + ((x2 - x1) - target) // 2
        py = y1 + ((y2 - y1) - target) // 2
        draw._image.paste(tinted, (px, py), tinted)

    def _draw_jayvc_row_text(self, draw, box, text, fill, background, bold=True, max_size=15):
        """Fit text to a menu cell; scroll only when the minimum size still cannot fit."""
        x1, y1, x2, y2 = box
        text = str(text or "")
        available = max(8, x2 - x1)
        font = self._jayvc_fit_font(
            text,
            available,
            bold=bold,
            max_size=max_size,
            min_size=9,
        )
        try:
            bbox = font.getbbox(text)
            text_width = bbox[2] - bbox[0]
        except Exception:
            text_width = available + 1

        if text_width <= available:
            bbox = draw.textbbox((0, 0), text, font=font)
            th = bbox[3] - bbox[1]
            y = y1 + ((y2 - y1) - th) // 2 - bbox[1]
            draw.text((x1, y), text, font=font, fill=fill)
        else:
            scrolling_text(
                draw,
                box,
                text,
                font,
                fill,
                speed=44.0,
                hold=0.8,
                background=background,
            )

    def draw_jayvc_menu(self, draw):
        """Full-width JVC MiniDV/Everio-style list menu with a dedicated nav rail."""
        menu = self.current_menu()
        self.ensure_selection_visible()

        body_bottom = HEIGHT - FOOTER_H
        draw.rectangle((0, HEADER_H, WIDTH, body_bottom), fill=JAYVC_BG)

        has_scroll = len(menu) > self.MAX_VISIBLE_ROWS
        visible = menu[self.menu_scroll:self.menu_scroll + self.MAX_VISIBLE_ROWS]

        # Use essentially all available LCD width. The global touch handler
        # reserves x >= SCROLL_RAIL_X1 for scrolling, so mirror that geometry
        # exactly and never let labels/value cells drift under the arrow rail.
        row_left = 6
        row_right = (self.SCROLL_RAIL_X1 - 7) if has_scroll else (WIDTH - 6)
        icon_left = row_left + 4
        icon_right = icon_left + 23

        for visible_index, (label, action) in enumerate(visible):
            actual_index = self.menu_scroll + visible_index
            selected = actual_index == self.selection
            y = self.MENU_TOP + visible_index * self.ROW_HEIGHT
            y1 = y + 2
            y2 = y + self.ROW_HEIGHT - 5

            row_fill = JAYVC_SELECT if selected else JAYVC_PANEL
            row_outline = JAYVC_FRAME_HI if selected else JAYVC_PANEL_EDGE
            row_text = JAYVC_TEXT if selected else JAYVC_TEXT_DARK

            draw.rounded_rectangle(
                (row_left, y1, row_right, y2),
                radius=5,
                fill=row_fill,
                outline=row_outline,
                width=2 if selected else 1,
            )

            # A slim coloured category band echoes the small coloured menu icons
            # visible on JVC MiniDV screens while remaining legible at 320x240.
            accent = JAYVC_TEXT if selected else self._jayvc_action_colour(action)
            draw.rectangle(
                (row_left + 2, y1 + 3, row_left + 5, y2 - 3),
                fill=accent,
            )

            is_file = self.page == "captures" and str(action).startswith("file::")
            if is_file:
                path = Path(str(action)[6:])
                kind = self.recording_storage_kind(path)
                badge_text = "USB" if kind == "USB" else "SD"
                badge_colour = JAYVC_CYAN if kind == "USB" else JAYVC_GREEN
                badge_x1 = row_left + 9
                badge_x2 = badge_x1 + 31
                badge_fill = JAYVC_SELECT_DARK if selected else JAYVC_PANEL_DARK
                draw.rounded_rectangle(
                    (badge_x1, y1 + 5, badge_x2, y2 - 5),
                    radius=3,
                    fill=badge_fill,
                    outline=JAYVC_TEXT if selected else badge_colour,
                    width=1,
                )
                centered_text(
                    draw,
                    (badge_x1, y1 + 4, badge_x2, y2 - 4),
                    badge_text,
                    FONT_TINY,
                    JAYVC_TEXT if selected else JAYVC_TEXT_DARK,
                )
                text_left = badge_x2 + 7
            else:
                self._draw_jayvc_menu_icon(
                    draw,
                    (icon_left, y1 + 3, icon_right, y2 - 3),
                    action,
                    selected=selected,
                )
                text_left = icon_right + 5

            # Keep the selected-row chevron inside the row itself. This leaves
            # the external right rail completely clear for UP/DOWN navigation.
            arrow_left = row_right - 16
            content_right = arrow_left - 4 if selected else row_right - 8
            if selected:
                cy = (y1 + y2) // 2
                draw.polygon(
                    [(arrow_left + 10, cy), (arrow_left + 1, cy - 7), (arrow_left + 1, cy + 7)],
                    fill=JAYVC_TEXT,
                )

            left_text, value_text = self._jayvc_split_label(label)
            left_text = str(left_text).upper()
            value_text = str(value_text).upper()

            if value_text and not is_file:
                # Size the value pane from its actual content instead of using a
                # fixed width. Clamp it so the label side always retains useful
                # space, then fit/scroll both cells independently.
                probe_font = self._jayvc_fit_font(value_text, 120, bold=True, max_size=14, min_size=9)
                try:
                    vb = probe_font.getbbox(value_text)
                    natural_value_w = (vb[2] - vb[0]) + 18
                except Exception:
                    natural_value_w = 76

                total_content_w = max(90, content_right - text_left)
                value_w = int(clamp(natural_value_w, 48, min(112, max(48, total_content_w - 82))))
                gap = 5
                value_x1 = content_right - value_w
                label_x2 = value_x1 - gap

                value_fill = JAYVC_SELECT_DARK if selected else JAYVC_PANEL_DARK
                value_outline = JAYVC_FRAME_HI if selected else JAYVC_PANEL_EDGE
                draw.rounded_rectangle(
                    (value_x1, y1 + 3, content_right, y2 - 3),
                    radius=4,
                    fill=value_fill,
                    outline=value_outline,
                    width=1,
                )

                # Small JVC-style transfer notch between label and value cells.
                cy = (y1 + y2) // 2
                notch_colour = JAYVC_FRAME_HI if selected else JAYVC_PANEL_EDGE
                draw.polygon(
                    [(value_x1 - 5, cy), (value_x1 + 1, cy - 5), (value_x1 + 1, cy + 5)],
                    fill=notch_colour,
                )

                self._draw_jayvc_row_text(
                    draw,
                    (text_left, y1 + 2, label_x2, y2 - 2),
                    left_text,
                    row_text,
                    row_fill,
                    bold=True,
                    max_size=15,
                )
                self._draw_jayvc_row_text(
                    draw,
                    (value_x1 + 7, y1 + 2, content_right - 5, y2 - 2),
                    value_text,
                    JAYVC_TEXT if selected else JAYVC_TEXT_DARK,
                    value_fill,
                    bold=True,
                    max_size=14,
                )
            else:
                self._draw_jayvc_row_text(
                    draw,
                    (text_left, y1 + 2, content_right, y2 - 2),
                    left_text,
                    row_text,
                    row_fill,
                    bold=True,
                    max_size=15,
                )

        if has_scroll:
            rail_x1 = self.SCROLL_RAIL_X1 + 3
            rail_x2 = WIDTH - 4
            rail_top = self.MENU_TOP
            rail_bottom = body_bottom - 3
            split_y = (rail_top + rail_bottom) // 2
            can_up = self.selection > 0
            can_down = self.selection < len(menu) - 1
            up_active = self.scroll_feedback == -1
            down_active = self.scroll_feedback == 1

            up_fill = JAYVC_SELECT if up_active else JAYVC_BAR
            down_fill = JAYVC_SELECT if down_active else JAYVC_BAR
            up_outline = JAYVC_FRAME_HI if can_up else JAYVC_BAR_EDGE
            down_outline = JAYVC_FRAME_HI if can_down else JAYVC_BAR_EDGE

            draw.rounded_rectangle(
                (rail_x1, rail_top, rail_x2, split_y - 3),
                radius=5,
                fill=up_fill,
                outline=up_outline,
                width=2 if up_active else 1,
            )
            draw.rounded_rectangle(
                (rail_x1, split_y + 3, rail_x2, rail_bottom),
                radius=5,
                fill=down_fill,
                outline=down_outline,
                width=2 if down_active else 1,
            )

            cx = (rail_x1 + rail_x2) // 2
            up_cy = (rail_top + split_y - 3) // 2
            down_cy = (split_y + 3 + rail_bottom) // 2
            up_colour = JAYVC_TEXT if can_up else JAYVC_TEXT_DIM
            down_colour = JAYVC_TEXT if can_down else JAYVC_TEXT_DIM
            draw.polygon(
                [(cx, up_cy - 11), (cx - 11, up_cy + 7), (cx + 11, up_cy + 7)],
                fill=up_colour,
            )
            draw.polygon(
                [(cx, down_cy + 11), (cx - 11, down_cy - 7), (cx + 11, down_cy - 7)],
                fill=down_colour,
            )

        description = ""
        if menu and 0 <= self.selection < len(menu):
            description = self._sorny_first_level_description(menu[self.selection][1])

        if self.message:
            scrolling_text(
                draw,
                (10, body_bottom - 18, self.SCROLL_RAIL_X1 - 6 if has_scroll else WIDTH - 10, body_bottom - 3),
                safe_text(self.message, 48),
                FONT_TINY,
                JAYVC_WARNING,
                speed=34.0,
                hold=0.7,
                background=JAYVC_BG,
            )
        elif description:
            scrolling_text(
                draw,
                (10, body_bottom - 18, self.SCROLL_RAIL_X1 - 6 if has_scroll else WIDTH - 10, body_bottom - 3),
                description.upper(),
                FONT_TINY,
                JAYVC_TEXT_DIM,
                speed=30.0,
                hold=0.8,
                background=JAYVC_BG,
            )

    def _panafonics_split_label(self, label):
        """Split simple 'label: value' rows into classic left/right columns."""
        value = str(label or "")
        if self.page in ("captures", "error_log", "wifi_networks"):
            return value, ""

        if ":" in value:
            left, right = value.split(":", 1)
            if left.strip() and right.strip():
                return left.strip(), right.strip()
        return value, ""

    def draw_panafonics_menu(self, draw):
        """Pre-2010 Panasonic-inspired text OSD, isolated to this theme."""
        menu = self.current_menu()
        self.ensure_selection_visible()

        body_bottom = HEIGHT - FOOTER_H
        draw.rectangle((0, HEADER_H, WIDTH, body_bottom), fill=PANA_BG)

        # Period setup screens frequently used simple open-corner frames.
        frame_left = 7
        frame_right = WIDTH - 7
        frame_top = HEADER_H + 4
        frame_bottom = body_bottom - 4
        draw.line((frame_left, frame_top, frame_left, frame_bottom), fill=PANA_FRAME, width=1)
        draw.line((frame_right, frame_top, frame_right, frame_bottom), fill=PANA_FRAME, width=1)
        draw.line((frame_left, frame_top, frame_left + 22, frame_top), fill=PANA_FRAME, width=1)
        draw.line((frame_right - 22, frame_top, frame_right, frame_top), fill=PANA_FRAME, width=1)

        has_scroll = len(menu) > self.PANA_VISIBLE_ROWS
        row_right = self.PANA_SCROLL_X1 - 5 if has_scroll else WIDTH - 15
        visible = menu[
            self.menu_scroll:self.menu_scroll + self.PANA_VISIBLE_ROWS
        ]

        for visible_index, (label, _action) in enumerate(visible):
            actual_index = self.menu_scroll + visible_index
            selected = actual_index == self.selection
            y1 = self.PANA_MENU_TOP + visible_index * self.PANA_ROW_HEIGHT
            y2 = y1 + self.PANA_ROW_HEIGHT - 2

            left_text, value_text = self._panafonics_split_label(label)
            left_text = left_text.upper()
            value_text = value_text.upper()

            label_x1 = 17
            label_x2 = 194 if value_text else row_right - 24
            label_bg = PANA_GREEN if selected else PANA_BG
            label_colour = PANA_TITLE_TEXT if selected else PANA_TEXT
            label_font = FONT_STATUS_BOLD if selected else FONT_STATUS_SMALL

            if selected:
                draw.rectangle(
                    (label_x1 - 3, y1 + 3, label_x2, y2 - 2),
                    fill=PANA_GREEN,
                )

            scrolling_text_noaa(
                draw,
                (label_x1, y1 + 2, label_x2 - 5, y2),
                left_text,
                label_font,
                label_colour,
                speed=36.0,
                hold=0.75,
                background=label_bg,
            )

            if value_text:
                value_font = FONT_STATUS_SMALL
                value_colour = PANA_GREEN if selected else PANA_TEXT
                value = safe_text(value_text, 16)
                bbox = value_font.getbbox(value)
                value_width = bbox[2] - bbox[0]
                value_x = max(202, row_right - 4 - value_width)
                draw_text_noaa(
                    draw,
                    (value_x, y1 + 6),
                    value,
                    value_font,
                    value_colour,
                )
                marker_x = 200
            else:
                marker_x = row_right - 13

            if selected:
                cy = (y1 + y2) // 2
                draw.polygon(
                    [
                        (marker_x, cy - 7),
                        (marker_x + 8, cy),
                        (marker_x, cy + 7),
                    ],
                    fill=PANA_ORANGE,
                )

        if has_scroll:
            can_up = self.menu_scroll > 0
            can_down = (
                self.menu_scroll + self.PANA_VISIBLE_ROWS < len(menu)
            )
            rail_x = WIDTH - 20
            upper_y = self.PANA_MENU_TOP + 17
            lower_y = (
                self.PANA_MENU_TOP
                + self.PANA_VISIBLE_ROWS * self.PANA_ROW_HEIGHT
                - 18
            )
            up_colour = PANA_CYAN if can_up else PANA_TEXT_DIM
            down_colour = PANA_CYAN if can_down else PANA_TEXT_DIM
            draw.line(
                (
                    rail_x,
                    self.PANA_MENU_TOP + 5,
                    rail_x,
                    self.PANA_MENU_TOP
                    + self.PANA_VISIBLE_ROWS * self.PANA_ROW_HEIGHT
                    - 5,
                ),
                fill=PANA_TEXT_DIM,
                width=1,
            )
            draw.polygon(
                [
                    (rail_x, upper_y - 7),
                    (rail_x - 7, upper_y + 5),
                    (rail_x + 7, upper_y + 5),
                ],
                fill=up_colour,
            )
            draw.polygon(
                [
                    (rail_x, lower_y + 7),
                    (rail_x - 7, lower_y - 5),
                    (rail_x + 7, lower_y - 5),
                ],
                fill=down_colour,
            )

        # Descriptions deliberately use the normal CamPi font, not SonyCam.
        description = ""
        if menu and 0 <= self.selection < len(menu):
            description = self._sorny_first_level_description(
                menu[self.selection][1]
            )

        desc_top = self.PANA_MENU_TOP + (
            self.PANA_VISIBLE_ROWS * self.PANA_ROW_HEIGHT
        ) + 2

        if self.message:
            scrolling_text(
                draw,
                (14, desc_top, WIDTH - 14, body_bottom - 4),
                safe_text(self.message, 48),
                FONT_TINY,
                PANA_YELLOW,
                speed=36.0,
                hold=0.7,
                background=PANA_BG,
            )
        elif description:
            scrolling_text(
                draw,
                (14, desc_top, WIDTH - 14, body_bottom - 4),
                description,
                FONT_TINY,
                PANA_TEXT_DIM,
                speed=32.0,
                hold=0.8,
                background=PANA_BG,
            )

    def _draw_sorny_arrow_button(self, draw, box, direction, large=False):
        x1, y1, x2, y2 = box
        draw.rectangle(box, fill=SORNY_PANEL, outline=SORNY_PANEL_EDGE, width=1)
        cx = (x1 + x2) // 2
        cy = (y1 + y2) // 2

        half_width = 13 if large else 10
        tip = 9 if large else 7
        base = 7 if large else 5

        if direction < 0:
            points = [
                (cx, cy - tip),
                (cx - half_width, cy + base),
                (cx + half_width, cy + base),
            ]
        else:
            points = [
                (cx, cy + tip),
                (cx - half_width, cy - base),
                (cx + half_width, cy - base),
            ]
        draw.polygon(points, fill=SORNY_TEXT)

    def _sorny_action_colour(self, action):
        """Use the established Basic Sony OSD palette for cylinder icons."""
        action = str(action or "").lower()

        # Top-level Personal Menu tiles. Keep these four visually distinct in
        # the classic Sony OSD palette; the same colours are used by their
        # low-opacity background glyphs.
        if action == "capture":
            # Classic Sony menu blue instead of cyan so the four Personal Menu
            # watermarks cover a broader retro OSD palette.
            return MENU_BLUE
        if action == "captures":
            # Darker mustard yellow gives the cassette watermark more contrast
            # against the pale Sorny tile while remaining recognisably yellow.
            return (190, 145, 28)
        if action == "system":
            return SONY_GREEN

        # Capture Source cylinder.
        if action == "dv_live":
            return SONY_CYAN
        if action == "dv_tape":
            return SONY_YELLOW
        if action == "set_analog":
            return SONY_MAGENTA
        if action == "analog_settings":
            return SONY_GREEN
        if action == "storage":
            return SONY_MAGENTA

        # System Settings / Network cylinder.
        if action == "device_status":
            return SONY_CYAN
        if action == "network":
            return SONY_GREEN
        if action == "network_status":
            return MENU_BLUE
        if action == "wifi_networks":
            return SONY_GREEN
        if action == "wifi_toggle":
            return SONY_MAGENTA
        if action == "network_refresh":
            return SONY_YELLOW
        if action == "interface":
            return SONY_MAGENTA
        if action in ("power", "shutdown"):
            return SONY_RED
        if action == "reboot":
            return SONY_YELLOW
        if action == "error_log":
            return SONY_YELLOW
        if action == "credits":
            return MENU_BLUE

        # Interface and other intermediate cylinder menus continue the same
        # small set of period Sony-style OSD colours.
        if action == "recalibrate":
            return SONY_CYAN
        if action == "font_size":
            return SONY_YELLOW
        if action in ("theme", "theme_default", "theme_sorny", "theme_panafonics", "theme_jayvc"):
            return SONY_MAGENTA
        if action.startswith("buzzer"):
            return SONY_GREEN

        if action in ("analog_modes", "analog_picture", "analog_caps"):
            return SONY_CYAN
        if action in ("analog_reset", "reset_picture", "refresh_analog"):
            return SONY_YELLOW

        if action == "storage_status":
            # Darker Sony blue keeps the large Status / free space watermark
            # visible against the pale Personal Menu tile without reading cyan.
            return SORNY_ACCENT_DARK
        if action == "storage_auto":
            return MENU_BLUE
        if action == "storage_internal":
            return SONY_CYAN
        if action == "storage_ssd":
            return SONY_GREEN

        if action == "file_info":
            return SONY_CYAN
        if action == "play_file":
            return SONY_GREEN
        if action == "delete_file":
            return SONY_RED

        return SORNY_ACCENT

    def _draw_sorny_category_glyph(
        self,
        draw,
        box,
        action,
        selected=False,
        _zoom_pass=False,
        _rgba=False,
    ):
        """Draw cylinder icons at 80% idle size and 120% focused size."""
        x1, y1, x2, y2 = box
        cx = (x1 + x2) // 2
        cy = (y1 + y2) // 2

        # Render every cylinder glyph to transparency before scaling. Idle
        # icons are reduced to 80%; the focused icon retains the existing 120%
        # treatment. The visual jump from idle to focus is therefore stronger
        # without changing icon spacing or painting over neighbouring entries.
        if not _zoom_pass:
            scratch_size = 32
            scratch = Image.new("RGBA", (scratch_size, scratch_size), (0, 0, 0, 0))
            scratch_draw = ImageDraw.Draw(scratch)
            self._draw_sorny_category_glyph(
                scratch_draw,
                (0, 0, scratch_size, scratch_size),
                action,
                selected=selected,
                _zoom_pass=True,
                _rgba=True,
            )
            scale = 1.20 if selected else 0.80
            scaled_size = max(1, int(round(scratch_size * scale)))
            resample = Image.Resampling.NEAREST if hasattr(Image, "Resampling") else Image.NEAREST
            scaled = scratch.resize((scaled_size, scaled_size), resample=resample)
            px = int(cx - scaled_size // 2)
            py = int(cy - scaled_size // 2)
            draw._image.paste(scaled, (px, py), scaled)
            return

        colour_rgb = self._sorny_action_colour(action)
        colour = tuple(colour_rgb) + (255,) if _rgba else colour_rgb
        action = str(action or "").lower()

        def camera():
            draw.rectangle((cx - 10, cy - 6, cx + 5, cy + 6), outline=colour, width=2)
            draw.ellipse((cx - 6, cy - 3, cx, cy + 3), outline=colour, width=1)
            draw.polygon(
                [(cx + 6, cy - 4), (cx + 12, cy - 7), (cx + 12, cy + 7), (cx + 6, cy + 4)],
                fill=colour,
            )

        def tape():
            draw.rectangle((cx - 11, cy - 7, cx + 11, cy + 7), outline=colour, width=2)
            draw.ellipse((cx - 8, cy - 4, cx - 2, cy + 2), outline=colour, width=1)
            draw.ellipse((cx + 2, cy - 4, cx + 8, cy + 2), outline=colour, width=1)
            draw.line((cx - 6, cy + 5, cx + 6, cy + 5), fill=colour, width=1)

        def analog_plug():
            draw.line((cx - 10, cy, cx + 2, cy), fill=colour, width=2)
            draw.rectangle((cx + 2, cy - 5, cx + 8, cy + 5), outline=colour, width=2)
            draw.line((cx + 8, cy - 3, cx + 12, cy - 3), fill=colour, width=1)
            draw.line((cx + 8, cy + 3, cx + 12, cy + 3), fill=colour, width=1)

        def sliders():
            for dx, knob_y in ((-7, -4), (0, 4), (7, -1)):
                draw.line((cx + dx, cy - 10, cx + dx, cy + 10), fill=colour, width=1)
                draw.rectangle(
                    (cx + dx - 3, cy + knob_y - 2, cx + dx + 3, cy + knob_y + 2),
                    fill=colour,
                )

        def drive():
            draw.rectangle((cx - 11, cy - 7, cx + 11, cy + 7), outline=colour, width=2)
            draw.line((cx - 8, cy + 3, cx + 8, cy + 3), fill=colour, width=1)
            draw.ellipse((cx + 5, cy - 4, cx + 8, cy - 1), fill=colour)

        def hdmi_play():
            # Display outline plus play symbol, making the Play on HDMI tile
            # read as an output/display action rather than generic playback.
            draw.rectangle((cx - 11, cy - 8, cx + 11, cy + 6), outline=colour, width=2)
            draw.line((cx - 5, cy + 10, cx + 5, cy + 10), fill=colour, width=2)
            draw.line((cx, cy + 6, cx, cy + 10), fill=colour, width=2)
            draw.polygon(
                [(cx - 3, cy - 5), (cx + 6, cy - 1), (cx - 3, cy + 3)],
                fill=colour,
            )

        def sd_card():
            draw.polygon(
                [(cx - 9, cy - 10), (cx + 4, cy - 10), (cx + 10, cy - 4),
                 (cx + 10, cy + 10), (cx - 9, cy + 10)],
                outline=colour,
            )
            draw.line((cx - 4, cy - 7, cx - 4, cy - 2), fill=colour, width=1)
            draw.line((cx, cy - 7, cx, cy - 2), fill=colour, width=1)
            draw.line((cx + 4, cy - 7, cx + 4, cy - 2), fill=colour, width=1)

        def usb():
            draw.line((cx, cy + 9, cx, cy - 6), fill=colour, width=2)
            draw.line((cx, cy - 1, cx - 7, cy - 6), fill=colour, width=1)
            draw.line((cx, cy - 1, cx + 7, cy - 6), fill=colour, width=1)
            draw.polygon([(cx, cy - 11), (cx - 3, cy - 5), (cx + 3, cy - 5)], fill=colour)
            draw.rectangle((cx - 9, cy - 9, cx - 6, cy - 6), fill=colour)
            draw.ellipse((cx + 5, cy - 9, cx + 9, cy - 5), outline=colour, width=1)

        def wifi():
            draw.arc((cx - 12, cy - 10, cx + 12, cy + 14), 205, 335, fill=colour, width=2)
            draw.arc((cx - 8, cy - 5, cx + 8, cy + 9), 205, 335, fill=colour, width=2)
            draw.ellipse((cx - 2, cy + 5, cx + 2, cy + 9), fill=colour)

        def connection():
            # Two linked nodes, visually distinct from the Wi-Fi radio icon.
            draw.ellipse((cx - 11, cy - 5, cx - 3, cy + 3), outline=colour, width=2)
            draw.ellipse((cx + 3, cy - 3, cx + 11, cy + 5), outline=colour, width=2)
            draw.line((cx - 3, cy - 1, cx + 3, cy + 1), fill=colour, width=2)
            draw.line((cx - 1, cy + 4, cx + 5, cy + 4), fill=colour, width=1)

        def gear():
            # Clearer cog silhouette for settings/system functions.
            tooth_spans = (
                (cx - 2, cy - 12, cx + 2, cy - 8),
                (cx - 2, cy + 8, cx + 2, cy + 12),
                (cx - 12, cy - 2, cx - 8, cy + 2),
                (cx + 8, cy - 2, cx + 12, cy + 2),
                (cx - 9, cy - 9, cx - 5, cy - 5),
                (cx + 5, cy - 9, cx + 9, cy - 5),
                (cx - 9, cy + 5, cx - 5, cy + 9),
                (cx + 5, cy + 5, cx + 9, cy + 9),
            )
            for tooth in tooth_spans:
                draw.rectangle(tooth, fill=colour)
            draw.ellipse((cx - 8, cy - 8, cx + 8, cy + 8), outline=colour, width=2)
            draw.ellipse((cx - 3, cy - 3, cx + 3, cy + 3), outline=colour, width=2)

        def power():
            draw.arc((cx - 9, cy - 8, cx + 9, cy + 10), 40, 320, fill=colour, width=2)
            draw.line((cx, cy - 11, cx, cy + 1), fill=colour, width=2)

        def info():
            draw.ellipse((cx - 9, cy - 9, cx + 9, cy + 9), outline=colour, width=2)
            draw.line((cx, cy - 2, cx, cy + 6), fill=colour, width=2)
            draw.ellipse((cx - 1, cy - 6, cx + 1, cy - 4), fill=colour)

        def document():
            draw.rectangle((cx - 8, cy - 10, cx + 8, cy + 10), outline=colour, width=2)
            for yy in (-5, 0, 5):
                draw.line((cx - 5, cy + yy, cx + 5, cy + yy), fill=colour, width=1)

        def person():
            draw.ellipse((cx - 4, cy - 9, cx + 4, cy - 1), outline=colour, width=2)
            draw.arc((cx - 9, cy, cx + 9, cy + 14), 180, 360, fill=colour, width=2)

        def crosshair():
            draw.ellipse((cx - 8, cy - 8, cx + 8, cy + 8), outline=colour, width=1)
            draw.line((cx - 12, cy, cx + 12, cy), fill=colour, width=1)
            draw.line((cx, cy - 12, cx, cy + 12), fill=colour, width=1)

        def text_icon():
            draw_text(draw, (cx - 9, cy - 8), "Aa", FONT_TINY, colour)

        def palette():
            draw.ellipse((cx - 10, cy - 8, cx + 10, cy + 8), outline=colour, width=2)
            for px, py in ((-4, -3), (2, -4), (5, 2)):
                draw.ellipse((cx + px - 1, cy + py - 1, cx + px + 1, cy + py + 1), fill=colour)

        def speaker():
            draw.rectangle((cx - 10, cy - 4, cx - 5, cy + 4), fill=colour)
            draw.polygon([(cx - 5, cy - 4), (cx + 2, cy - 9), (cx + 2, cy + 9), (cx - 5, cy + 4)], fill=colour)
            draw.arc((cx, cy - 8, cx + 12, cy + 8), 300, 60, fill=colour, width=2)

        def refresh():
            draw.arc((cx - 9, cy - 9, cx + 9, cy + 9), 30, 305, fill=colour, width=2)
            draw.polygon([(cx + 8, cy - 7), (cx + 12, cy - 7), (cx + 10, cy - 2)], fill=colour)

        def sun():
            draw.ellipse((cx - 5, cy - 5, cx + 5, cy + 5), outline=colour, width=2)
            for dx, dy in ((0, -11), (0, 11), (-11, 0), (11, 0), (-8, -8), (8, 8), (8, -8), (-8, 8)):
                draw.line((cx + dx * 0.65, cy + dy * 0.65, cx + dx, cy + dy), fill=colour, width=1)

        def auto():
            draw_text(draw, (cx - 6, cy - 8), "A", FONT_SMALL, colour)

        def fallback():
            draw.rectangle((cx - 9, cy - 8, cx + 9, cy + 8), outline=colour, width=2)
            draw.line((cx - 5, cy - 3, cx + 5, cy - 3), fill=colour, width=1)
            draw.line((cx - 5, cy + 2, cx + 5, cy + 2), fill=colour, width=1)

        # Specific action mapping keeps each major section visually distinct.
        # The four top-level Personal Menu entries deliberately reuse familiar
        # camcorder-era symbols so they remain recognisable even when rendered
        # as large translucent tile watermarks.
        if action == "capture":
            camera()
        elif action == "captures":
            tape()
        elif action == "system":
            gear()
        elif action == "dv_live":
            camera()
        elif action == "dv_tape":
            tape()
        elif action == "set_analog":
            analog_plug()
        elif action in ("analog_settings", "analog_picture") or action.startswith("analog_control::"):
            sliders()
        elif action == "analog_modes" or action.startswith("analog_mode::"):
            camera()
        elif action == "analog_caps":
            info()
        elif action in ("analog_reset", "reset_picture", "refresh_analog"):
            refresh()
        elif action in ("storage", "storage_status"):
            drive()
        elif action == "storage_auto":
            auto()
        elif action == "storage_internal":
            sd_card()
        elif action == "storage_ssd":
            usb()
        elif action == "device_status":
            info()
        elif action == "network_status":
            connection()
        elif action in ("network", "wifi_networks", "wifi_toggle"):
            wifi()
        elif action == "network_refresh":
            refresh()
        elif action == "interface":
            gear()
        elif action == "power" or action == "shutdown":
            power()
        elif action == "reboot":
            refresh()
        elif action == "error_log":
            document()
        elif action == "credits":
            person()
        elif action == "recalibrate":
            crosshair()
        elif action == "font_size":
            text_icon()
        elif action in ("theme", "theme_default", "theme_sorny", "theme_panafonics", "theme_jayvc"):
            palette()
        elif action.startswith("buzzer"):
            speaker()
        elif action == "file_info":
            info()
        elif action == "play_file":
            hdmi_play()
        elif action == "delete_file":
            draw.rectangle((cx - 7, cy - 6, cx + 7, cy + 9), outline=colour, width=2)
            draw.line((cx - 10, cy - 9, cx + 10, cy - 9), fill=colour, width=2)
        elif "picture" in action:
            sun()
        else:
            fallback()

    def _sorny_fit_font(self, text, bold=False, max_size=16, min_size=10, width=105):
        """Return a normal UI font small enough to fit one Sorny tile line."""
        path = SORNY_FONT_BOLD_PATH if bold else SORNY_FONT_NORMAL_PATH
        text = str(text)
        for size in range(int(max_size), int(min_size) - 1, -1):
            font = load_font(path, size)
            try:
                bbox = font.getbbox(text)
                text_width = bbox[2] - bbox[0]
            except Exception:
                text_width = len(text) * size
            if text_width <= width:
                return font
        return load_font(path, int(min_size))

    def _sorny_fit_multiline_font(
        self, lines, bold=False, max_size=16, min_size=10, width=105
    ):
        """Fit one font size across every line of a Sorny tile label."""
        path = SORNY_FONT_BOLD_PATH if bold else SORNY_FONT_NORMAL_PATH
        lines = tuple(str(line) for line in lines)
        for size in range(int(max_size), int(min_size) - 1, -1):
            font = load_font(path, size)
            fits = True
            for line in lines:
                try:
                    bbox = font.getbbox(line)
                    text_width = bbox[2] - bbox[0]
                except Exception:
                    text_width = len(line) * size
                if text_width > width:
                    fits = False
                    break
            if fits:
                return font
        return load_font(path, int(min_size))

    def _draw_sorny_block_background_glyph(self, draw, box, action, selected=False):
        """Draw a large faded retro glyph behind a top-level Personal Menu label."""
        action = str(action or "").lower()
        if action not in {
            "capture", "captures", "storage", "system",
            "storage_status", "play_file", "reboot", "shutdown",
        }:
            return

        # Draw the normal crisp category symbol once, then enlarge it with
        # nearest-neighbour scaling to preserve the period OSD/pixel character.
        scratch_size = 32
        scratch = Image.new("RGBA", (scratch_size, scratch_size), (0, 0, 0, 0))
        scratch_draw = ImageDraw.Draw(scratch)
        self._draw_sorny_category_glyph(
            scratch_draw,
            (0, 0, scratch_size, scratch_size),
            action,
            selected=False,
            _zoom_pass=True,
            _rgba=True,
        )

        # The watermark is intentionally broad rather than a small icon. Most
        # tiles remain subdued so the label stays dominant, but the dedicated
        # Power actions use stronger period Sony colour so yellow Reboot and red
        # Shut down read clearly instead of washing out to grey.
        if action in ("reboot", "shutdown"):
            opacity = 170 if selected else 195
        else:
            opacity = 92 if selected else 108
        alpha = scratch.getchannel("A").point(
            lambda value: (value * opacity) // 255
        )
        scratch.putalpha(alpha)

        x1, y1, x2, y2 = box
        tile_w = max(1, x2 - x1 + 1)
        tile_h = max(1, y2 - y1 + 1)

        # Scale beyond the tile height so the symbol fills most of the available
        # background. Render through a tile-sized transparent layer so any excess
        # is cleanly clipped to the button rather than bleeding into neighbours.
        glyph_size = max(58, min(tile_w - 8, int(round(tile_h * 1.30))))
        resample = Image.Resampling.NEAREST if hasattr(Image, "Resampling") else Image.NEAREST
        glyph = scratch.resize((glyph_size, glyph_size), resample=resample)

        tile_layer = Image.new("RGBA", (tile_w, tile_h), (0, 0, 0, 0))
        px = (tile_w - glyph_size) // 2
        py = (tile_h - glyph_size) // 2
        tile_layer.paste(glyph, (px, py), glyph)
        draw._image.paste(tile_layer, (x1, y1), tile_layer)

    def draw_sorny_blocks(self, draw):
        """Sony Personal-Menu-style 2x2 tiles, paged four items at a time."""
        menu = self.current_menu()
        boxes = self._sorny_block_boxes(menu)
        page_start = self._sorny_block_page_start()
        page_count = self._sorny_block_page_count(menu)
        page_index = min(page_count - 1, page_start // 4)

        draw.rectangle((0, HEADER_H, WIDTH, HEIGHT - FOOTER_H), fill=SORNY_BG)

        # Page chrome is only needed when there is actually another page.
        if page_count > 1:
            draw_text(
                draw,
                (14, 103),
                f"{page_index + 1}/{page_count}",
                SORNY_FONT_SMALL,
                SORNY_TEXT_DIM,
            )
            self._draw_sorny_arrow_button(draw, (12, 44, 58, 76), -1)
            self._draw_sorny_arrow_button(draw, (12, 145, 58, 177), 1)

        visible = menu[page_start:page_start + 4]
        for local_index, ((label, action), box) in enumerate(zip(visible, boxes)):
            actual_index = page_start + local_index
            selected = actual_index == self.selection
            x1, y1, x2, y2 = box

            draw.rectangle(
                (x1 + 3, y1 + 3, x2 + 3, y2 + 3),
                fill=SORNY_BG_DARK,
            )
            draw.rectangle(
                box,
                fill=SORNY_PANEL_BRIGHT if selected else SORNY_PANEL,
                outline=SORNY_ACCENT_BRIGHT if selected else SORNY_PANEL_EDGE,
                width=2 if selected else 1,
            )

            if selected:
                draw.rectangle((x1, y1, x1 + 5, y2), fill=SORNY_ACCENT)

            # Large translucent Sony-style watermarks are used on the four
            # main MENU tiles, plus the key Status / free space and Play on HDMI
            # actions requested in their respective block menus.
            show_background_glyph = (
                self.page == "menu"
                or (self.page == "storage" and action == "storage_status")
                or (self.page == "file_actions" and action == "play_file")
                or (self.page == "power" and action in ("reboot", "shutdown"))
            )
            if show_background_glyph:
                self._draw_sorny_block_background_glyph(
                    draw, box, action, selected=selected
                )

            words = str(label).split()
            if len(words) > 1 and len(str(label)) > 12:
                midpoint = max(1, len(words) // 2)
                lines = (" ".join(words[:midpoint]), " ".join(words[midpoint:]))
            else:
                lines = (str(label),)

            line_width = max(40, (x2 - x1) - 18)
            if len(lines) == 1:
                line_font = self._sorny_fit_font(
                    lines[0],
                    bold=True,
                    max_size=15,
                    min_size=10,
                    width=line_width,
                )
                centered_text(
                    draw,
                    (x1 + 8, y1 + 6, x2 - 6, y2 - 5),
                    lines[0],
                    line_font,
                    SORNY_TEXT,
                )
            else:
                # Keep all Personal Menu block labels bold all the time. The
                # moving accent bar already marks selection, so the label should
                # not change weight or point size when focus changes.
                multiline_font = self._sorny_fit_multiline_font(
                    lines,
                    bold=True,
                    max_size=14,
                    min_size=10,
                    width=line_width,
                )
                # Place the *visible glyph bounds* with one fixed gap instead
                # of independently centring each line. Different letters have
                # different font bboxes, which previously made CAPTURE / SOURCE
                # look farther apart than SYSTEM / SETTINGS despite identical
                # nominal line boxes.
                line_boxes = [multiline_font.getbbox(line) for line in lines]
                line_heights = [box_[3] - box_[1] for box_ in line_boxes]
                visible_gap = 13
                total_height = sum(line_heights) + visible_gap
                group_top = y1 + ((y2 - y1 + 1) - total_height) // 2
                text_left = x1 + 8
                text_right = x2 - 6
                text_cx = (text_left + text_right) // 2

                visible_top = group_top
                for line, bbox in zip(lines, line_boxes):
                    line_width_px = bbox[2] - bbox[0]
                    line_height_px = bbox[3] - bbox[1]
                    draw_x = text_cx - (line_width_px // 2) - bbox[0]
                    draw_y = visible_top - bbox[1]
                    draw_text(
                        draw,
                        (draw_x, draw_y),
                        line,
                        multiline_font,
                        SORNY_TEXT,
                    )
                    visible_top += line_height_px + visible_gap

    def draw_sorny_cylinder(self, draw):
        """Animated Sony-style category wheel with a curved left rail."""
        menu = self.current_menu()
        if not menu:
            return

        count = len(menu)
        anchor = self.selection if self.sorny_anim_anchor is None else self.sorny_anim_anchor
        shift = 0.0 if self.sorny_anim_anchor is None else self.sorny_anim_shift

        draw.rectangle((0, HEADER_H, WIDTH, HEIGHT - FOOTER_H), fill=SORNY_BG)

        # Left-side cylinder rail now matches the full width of the enlarged
        # navigation buttons, creating one continuous control column.
        rail_box = (0, 39, 74, 186)
        draw.ellipse((rail_box[0], 35, rail_box[2], 101), fill=SORNY_RAIL)
        draw.rectangle((rail_box[0], 67, rail_box[2], 158), fill=SORNY_RAIL)
        draw.ellipse((rail_box[0], 126, rail_box[2], 190), fill=SORNY_RAIL)
        draw.arc((-5, 35, 80, 190), 270, 90, fill=(92, 96, 102), width=1)

        # Dedicated Sony-like arrow controls on the rail.
        self._draw_sorny_arrow_button(draw, (0, 36, 74, 73), -1, large=True)
        self._draw_sorny_arrow_button(draw, (0, 151, 74, 188), 1, large=True)

        # Show nearby category glyphs down the centre of the rail. These roll
        # with the same continuous animation as the text items.
        glyph_items = []
        for index, (_label, action) in enumerate(menu):
            relative = self._sorny_relative(index, anchor, count) - shift
            if abs(relative) <= 1.8:
                glyph_items.append((abs(relative), relative, index, action))
        glyph_items.sort(reverse=True)

        for _distance, relative, index, action in glyph_items:
            # Keep adjacent icons inside the clear zone between the larger
            # arrow buttons. The focused icon gains its 20% zoom at y=112.
            icon_y = int(112 + (26.0 * relative))
            if 86 <= icon_y <= 138:
                self._draw_sorny_category_glyph(
                    draw,
                    (21, icon_y - 8, 53, icon_y + 8),
                    action,
                    selected=abs(relative) < 0.18,
                )

        items = []
        for index, (label, action) in enumerate(menu):
            relative = self._sorny_relative(index, anchor, count) - shift
            if abs(relative) <= 2.58:
                items.append((abs(relative), relative, index, str(label), action))

        # Far items first so the large centre selection panel sits on top.
        items.sort(reverse=True)

        for _distance, relative, index, label, _action in items:
            distance = min(2.5, abs(relative))
            scale = max(0.55, 1.0 - (0.18 * distance))
            cy = self._sorny_slot_y(relative)
            is_centre = distance < 0.18

            if is_centre:
                # The defining PC300 element: a large pale-aqua horizontal
                # highlight bar with dark text.
                x1, x2 = 80, WIDTH - 9
                item_h = 43
                y1 = int(cy - item_h / 2)
                y2 = y1 + item_h

                draw.rectangle(
                    (x1 + 3, y1 + 3, x2 + 2, y2 + 2),
                    fill=SORNY_BG_DARK,
                )
                draw.rectangle(
                    (x1, y1, x2, y2),
                    fill=SORNY_PANEL_BRIGHT,
                    outline=SORNY_ACCENT_BRIGHT,
                    width=1,
                )
                draw.rectangle((x1, y1, x1 + 4, y2), fill=SORNY_ACCENT)

                font = load_font(SORNY_FONT_BOLD_PATH, max(12, 16 + self.font_size_step))
                scrolling_text(
                    draw,
                    (x1 + 12, y1 + 2, x2 - 8, y2 - 2),
                    label,
                    font,
                    SORNY_TEXT,
                    speed=42.0,
                    hold=0.85,
                    background=SORNY_PANEL_BRIGHT,
                )
            else:
                # Receding labels stay simple and quiet, like Sony's list of
                # neighbouring items above/below the active strip.
                font_size = max(9, int((15 + self.font_size_step) * scale))
                font = load_font(SORNY_FONT_NORMAL_PATH, font_size)
                colour = SORNY_TEXT_DIM if distance < 1.75 else (111, 118, 122)
                x1 = 93 + int(9 * min(distance, 2.0))
                x2 = WIDTH - 16
                y1 = int(cy - 12)
                y2 = int(cy + 12)

                scrolling_text(
                    draw,
                    (x1, y1, x2, y2),
                    label,
                    font,
                    colour,
                    speed=28.0,
                    hold=0.9,
                    background=SORNY_BG,
                )

        # First-level cylinder screens use the lower-right area for a compact
        # explanation of the currently selected category/action.
        try:
            selected_action = menu[self.selection][1]
        except Exception:
            selected_action = ""
        description = self._sorny_first_level_description(selected_action)
        if description:
            # Join the description directly onto the selected centre item.
            # The selected item is centred at y=112 with a 43 px height, so
            # its lower edge lands at y=133. Match its left/right edges too.
            desc_box = (80, 133, WIDTH - 9, 181)
            draw.rectangle(
                desc_box,
                fill=SORNY_DESC_BG,
                outline=SORNY_DESC_BORDER,
                width=1,
            )
            lines = textwrap.wrap(description, width=31)[:2]
            y = 137
            for line in lines:
                draw_text(
                    draw,
                    (88, y),
                    line,
                    SORNY_FONT_TINY,
                    SORNY_DESC_TEXT,
                )
                y += 17

    def draw_default_menu(self, draw):
        menu = self.current_menu()
        self.ensure_selection_visible()

        sorny = self.ui_theme == "sorny"
        row_bg = SORNY_BG if sorny else BG
        selected_fill = SORNY_ACCENT_DARK if sorny else SELECTED
        row_text = SORNY_HEADER_TEXT if sorny else TEXT
        row_dim = SORNY_TEXT_DIM if sorny else TEXT_DIM
        row_accent = SORNY_ACCENT if sorny else ACCENT
        warning_colour = SORNY_WARNING if sorny else AMBER

        has_scroll_rail = len(menu) > self.MAX_VISIBLE_ROWS
        row_right = self.SCROLL_RAIL_X1 - 4 if has_scroll_rail else WIDTH - 7
        selected_arrow_x = row_right - 18
        label_chars = 26 if has_scroll_rail else 32

        visible = menu[self.menu_scroll:self.menu_scroll + self.MAX_VISIBLE_ROWS]
        y = self.MENU_TOP

        for visible_index, (label, _) in enumerate(visible):
            actual_index = self.menu_scroll + visible_index
            selected = actual_index == self.selection
            fill = selected_fill if selected else row_bg

            draw.rounded_rectangle(
                (7, y, row_right, y + self.ROW_HEIGHT - 4),
                radius=5,
                fill=fill,
            )

            if self.page == "error_log":
                scrolling_text(
                    draw,
                    (17, y + 3, row_right - 23, y + self.ROW_HEIGHT - 5),
                    label,
                    FONT_SMALL,
                    row_text,
                    speed=72.0,
                    hold=0.55,
                    background=fill,
                )
            elif self.page == "captures":
                action = visible[visible_index][1]
                text_right = selected_arrow_x - 4 if selected else row_right - 5
                if action == "capture_filter":
                    scrolling_text(
                        draw,
                        (17, y + 3, text_right, y + self.ROW_HEIGHT - 5),
                        label,
                        FONT_SMALL,
                        SORNY_ACCENT_BRIGHT if sorny else SONY_GREEN,
                        speed=48.0, hold=0.7, background=fill,
                    )
                else:
                    path = Path(action[6:]) if action.startswith("file::") else None
                    kind = self.recording_storage_kind(path) if path is not None else "SD"
                    badge_colour = SONY_CYAN if kind == "USB" else MENU_BLUE
                    badge_text = "USB" if kind == "USB" else "SD"
                    badge_font = FONT_TINY
                    bx1, bx2 = 14, 43
                    draw.rounded_rectangle(
                        (bx1, y + 7, bx2, y + self.ROW_HEIGHT - 11),
                        radius=3, outline=badge_colour, width=1,
                    )
                    bb = draw.textbbox((0, 0), badge_text, font=badge_font)
                    btw = bb[2] - bb[0]
                    bth = bb[3] - bb[1]
                    draw.text(
                        ((bx1 + bx2 - btw) // 2, y + 14 - (bth // 2) - bb[1]),
                        badge_text, font=badge_font, fill=badge_colour,
                    )
                    text_box = (48, y + 3, text_right, y + self.ROW_HEIGHT - 5)
                    if selected:
                        scrolling_text(
                            draw, text_box, label, FONT_SMALL, row_text,
                            speed=58.0, hold=0.7, background=fill,
                        )
                    else:
                        available_chars = max(8, 26 if has_scroll_rail else 34)
                        draw.text((48, y + 7), safe_text(label, available_chars), font=FONT_SMALL, fill=row_text)
            else:
                draw.text((17, y + 7), safe_text(label, label_chars), font=FONT_NORMAL, fill=row_text)

            if selected:
                draw.text((selected_arrow_x, y + 7), ">", font=FONT_BOLD, fill=row_accent)

            y += self.ROW_HEIGHT

        if has_scroll_rail:
            footer_top = HEIGHT - FOOTER_H
            rail_x1 = self.SCROLL_RAIL_X1 + 3
            rail_x2 = WIDTH - 4
            split_y = (self.MENU_TOP + footer_top) // 2
            can_up = self.selection > 0
            can_down = self.selection < len(menu) - 1
            up_active = self.scroll_feedback == -1
            down_active = self.scroll_feedback == 1
            inactive_rail = SORNY_BG_DARK if sorny else PANEL_ALT
            active_rail = SORNY_ACCENT_DARK if sorny else SONY_BLUE_DARK
            rail_outline = SORNY_ACCENT if sorny else None
            up_fill = active_rail if up_active else inactive_rail
            down_fill = active_rail if down_active else inactive_rail
            up_outline = (rail_outline if can_up else SORNY_RAIL) if sorny else (SONY_CYAN if can_up else BORDER)
            down_outline = (rail_outline if can_down else SORNY_RAIL) if sorny else (SONY_MAGENTA if can_down else BORDER)

            draw.rounded_rectangle(
                (rail_x1, self.MENU_TOP, rail_x2, split_y - 3),
                radius=6, fill=up_fill, outline=up_outline, width=2 if up_active else 1,
            )
            draw.rounded_rectangle(
                (rail_x1, split_y + 3, rail_x2, footer_top - 2),
                radius=6, fill=down_fill, outline=down_outline, width=2 if down_active else 1,
            )

            up_colour = (SORNY_ACCENT if can_up else SORNY_RAIL) if sorny else (SONY_CYAN if can_up else BORDER)
            down_colour = (SORNY_ACCENT if can_down else SORNY_RAIL) if sorny else (SONY_MAGENTA if can_down else BORDER)
            up_cx = (rail_x1 + rail_x2) // 2
            up_cy = (self.MENU_TOP + split_y - 3) // 2
            down_cx = up_cx
            down_cy = (split_y + 3 + footer_top - 2) // 2
            draw.polygon(
                [(up_cx, up_cy - 12), (up_cx - 13, up_cy + 8), (up_cx + 13, up_cy + 8)],
                fill=up_colour,
            )
            draw.polygon(
                [(down_cx, down_cy + 12), (down_cx - 13, down_cy - 8), (down_cx + 13, down_cy - 8)],
                fill=down_colour,
            )

        if self.message:
            draw.text((10, HEIGHT - FOOTER_H - 18), safe_text(self.message, 42), font=FONT_SMALL, fill=warning_colour)

    def draw_info(self, draw):
        """Shared structured renderer for status, overview and detail screens."""
        sorny = self.ui_theme == "sorny"
        jayvc = self.ui_theme == "jayvc" and not self.hdmi_playback_context()
        panafonics = self.ui_theme == "panafonics" and not self.hdmi_playback_context()

        if sorny:
            info_bg = SORNY_BG
            row_fill = SORNY_BG_DARK
            row_fill_alt = SORNY_RAIL
            row_outline = SORNY_RAIL
            label_colour = SORNY_ACCENT_BRIGHT
            info_text = SORNY_HEADER_TEXT
            dim_colour = SORNY_TEXT_DIM
            success_colour = SORNY_SUCCESS
            warning_colour = SORNY_WARNING
            danger_colour = SORNY_DANGER
        elif jayvc:
            info_bg = JAYVC_BG
            row_fill = JAYVC_BAR
            row_fill_alt = JAYVC_SELECT_DARK
            row_outline = JAYVC_BAR_EDGE
            label_colour = JAYVC_CYAN
            info_text = JAYVC_TEXT
            dim_colour = JAYVC_TEXT_DIM
            success_colour = JAYVC_GREEN
            warning_colour = JAYVC_WARNING
            danger_colour = JAYVC_RED
        elif panafonics:
            info_bg = PANA_BG
            row_fill = PANA_BG_DARK
            row_fill_alt = (62, 67, 69)
            row_outline = PANA_FRAME
            label_colour = PANA_CYAN
            info_text = PANA_TEXT
            dim_colour = PANA_TEXT_DIM
            success_colour = PANA_GREEN
            warning_colour = PANA_YELLOW
            danger_colour = SONY_RED
        else:
            info_bg = BG
            row_fill = PANEL_ALT
            row_fill_alt = PANEL
            row_outline = BORDER
            label_colour = SONY_CYAN
            info_text = TEXT
            dim_colour = TEXT_DIM
            success_colour = SONY_GREEN
            warning_colour = SONY_YELLOW
            danger_colour = SONY_RED

        # Explicitly paint the body so theme-specific pages do not inherit stale
        # pixels or an unrelated renderer's background.
        draw.rectangle((0, HEADER_H, WIDTH, HEIGHT - FOOTER_H), fill=info_bg)

        has_scroll_rail = len(self.info_lines) > self.INFO_VISIBLE_LINES
        row_right = self.SCROLL_RAIL_X1 - 5 if has_scroll_rail else WIDTH - 8
        visible = self.info_lines[
            self.info_scroll:self.info_scroll + self.INFO_VISIBLE_LINES
        ]
        credits = self.info_title == "CREDITS"

        # Keep identifiers aligned in one column. The width is calculated from
        # the visible rows, then capped so values always retain usable space.
        label_widths = []
        for line in visible:
            line_text = str(line)
            if ":" not in line_text:
                continue
            label = line_text.split(":", 1)[0].strip() + ":"
            label_font = FONT_SMALL if credits else FONT_BOLD
            try:
                bbox = draw.textbbox((0, 0), label, font=label_font)
                label_widths.append(bbox[2] - bbox[0])
            except Exception:
                pass

        content_width = max(80, row_right - 18)
        desired_label_width = max(label_widths, default=72) + 8
        label_column_width = int(clamp(desired_label_width, 72, min(116, content_width * 0.44)))

        def normalise_info_value(value):
            """Sentence-case human status phrases without altering IDs/paths."""
            raw = str(value or "").strip()
            lower = raw.lower()
            sentence_case_values = {
                "camera connected",
                "controller only",
                "not detected",
                "unavailable",
                "connected",
                "disconnected",
                "none",
                "none detected",
                "unknown",
                "offline",
                "missing",
                "mounted",
            }
            if lower in sentence_case_values:
                return raw[:1].upper() + raw[1:]
            if re.fullmatch(r"(?:connected|disconnected)\s+\d+%", lower):
                return raw[:1].upper() + raw[1:]
            return raw

        def status_colour(value):
            lower = str(value or "").strip().lower()
            if not lower:
                return dim_colour
            if any(token in lower for token in ("failed", "error", "failure")):
                return danger_colour
            if any(token in lower for token in (
                "not detected", "unavailable", "disconnected", "controller only",
                "missing", "none", "offline",
            )):
                return warning_colour
            if (
                lower == "ok"
                or lower == "connected"
                or lower.startswith("connected ")
                or lower.endswith(" connected")
                or lower == "mounted"
            ):
                return success_colour
            return info_text

        row_top = self.INFO_ROW_TOP
        row_step = self.INFO_ROW_STEP
        row_height = self.INFO_ROW_HEIGHT

        for visible_index, line in enumerate(visible):
            absolute_index = self.info_scroll + visible_index
            text = str(line)
            y1 = row_top + visible_index * row_step
            y2 = y1 + row_height
            fill = row_fill_alt if visible_index % 2 else row_fill

            if absolute_index in self.info_scroll_paused:
                row_phase_time = self.info_scroll_paused[absolute_index]
            else:
                row_phase_time = time.monotonic() - float(
                    self.info_scroll_time_offsets.get(absolute_index, 0.0)
                )

            draw.rounded_rectangle(
                (8, y1, row_right, y2),
                radius=4,
                fill=fill,
                outline=row_outline,
                width=1,
            )

            if ":" in text:
                label_text, value_text = text.split(":", 1)
                label_text = label_text.strip() + ":"
                value_text = normalise_info_value(value_text)

                label_font = FONT_SMALL if credits else FONT_BOLD
                value_font = FONT_SMALL if credits else FONT_NORMAL
                divider_x = 12 + label_column_width

                # A subtle divider makes label/value scanning much faster while
                # preserving almost all of the 320 px width for text.
                draw.line(
                    (divider_x, y1 + 3, divider_x, y2 - 3),
                    fill=row_outline,
                    width=1,
                )

                scrolling_text(
                    draw,
                    (13, y1 + 1, divider_x - 5, y2 - 1),
                    label_text,
                    label_font,
                    label_colour,
                    speed=42.0,
                    hold=0.8,
                    background=fill,
                    phase_time=row_phase_time,
                )

                if value_text:
                    scrolling_text(
                        draw,
                        (divider_x + 6, y1 + 1, row_right - 5, y2 - 1),
                        value_text,
                        value_font,
                        status_colour(value_text),
                        speed=48.0,
                        hold=0.8,
                        background=fill,
                        phase_time=row_phase_time,
                    )
            else:
                font = FONT_BOLD if credits and text.startswith("CamPi v") else FONT_NORMAL
                scrolling_text(
                    draw,
                    (13, y1 + 1, row_right - 5, y2 - 1),
                    text,
                    font,
                    info_text,
                    speed=48.0,
                    hold=0.8,
                    background=fill,
                    phase_time=row_phase_time,
                )

        if has_scroll_rail:
            footer_top = HEIGHT - FOOTER_H
            rail_x1 = self.SCROLL_RAIL_X1 + 3
            rail_x2 = WIDTH - 4
            split_y = (self.MENU_TOP + footer_top) // 2
            max_scroll = max(0, len(self.info_lines) - self.INFO_VISIBLE_LINES)
            can_up = self.info_scroll > 0
            can_down = self.info_scroll < max_scroll
            up_active = self.scroll_feedback == -1
            down_active = self.scroll_feedback == 1

            if sorny:
                inactive_rail = SORNY_BG_DARK
                active_rail = SORNY_ACCENT_DARK
                up_outline = SORNY_ACCENT if can_up else SORNY_RAIL
                down_outline = SORNY_ACCENT if can_down else SORNY_RAIL
                up_arrow = SORNY_ACCENT if can_up else SORNY_RAIL
                down_arrow = SORNY_ACCENT if can_down else SORNY_RAIL
            elif jayvc:
                inactive_rail = JAYVC_BAR
                active_rail = JAYVC_SELECT
                up_outline = JAYVC_FRAME_HI if can_up else JAYVC_BAR_EDGE
                down_outline = JAYVC_FRAME_HI if can_down else JAYVC_BAR_EDGE
                up_arrow = JAYVC_TEXT if can_up else JAYVC_TEXT_DIM
                down_arrow = JAYVC_TEXT if can_down else JAYVC_TEXT_DIM
            elif panafonics:
                inactive_rail = PANA_BG_DARK
                active_rail = (73, 80, 83)
                up_outline = PANA_CYAN if can_up else PANA_TEXT_DIM
                down_outline = PANA_CYAN if can_down else PANA_TEXT_DIM
                up_arrow = PANA_CYAN if can_up else PANA_TEXT_DIM
                down_arrow = PANA_CYAN if can_down else PANA_TEXT_DIM
            else:
                inactive_rail = PANEL_ALT
                active_rail = SONY_BLUE_DARK
                up_outline = SONY_CYAN if can_up else BORDER
                down_outline = SONY_MAGENTA if can_down else BORDER
                up_arrow = SONY_CYAN if can_up else BORDER
                down_arrow = SONY_MAGENTA if can_down else BORDER

            draw.rounded_rectangle(
                (rail_x1, self.MENU_TOP, rail_x2, split_y - 3),
                radius=6,
                fill=active_rail if up_active else inactive_rail,
                outline=up_outline,
                width=2 if up_active else 1,
            )
            draw.rounded_rectangle(
                (rail_x1, split_y + 3, rail_x2, footer_top - 2),
                radius=6,
                fill=active_rail if down_active else inactive_rail,
                outline=down_outline,
                width=2 if down_active else 1,
            )

            up_cx = (rail_x1 + rail_x2) // 2
            up_cy = (self.MENU_TOP + split_y - 3) // 2
            down_cx = up_cx
            down_cy = (split_y + 3 + footer_top - 2) // 2
            draw.polygon(
                [(up_cx, up_cy - 12), (up_cx - 13, up_cy + 8), (up_cx + 13, up_cy + 8)],
                fill=up_arrow,
            )
            draw.polygon(
                [(down_cx, down_cy + 12), (down_cx - 13, down_cy - 8), (down_cx + 13, down_cy - 8)],
                fill=down_arrow,
            )

    def draw_confirm(self, draw):
        jayvc = self.ui_theme == "jayvc" and not self.hdmi_playback_context()
        if jayvc:
            draw.rectangle((0, HEADER_H, WIDTH, HEIGHT - FOOTER_H), fill=JAYVC_BG)
            draw.rounded_rectangle((32, HEADER_H + 22, WIDTH - 32, HEIGHT - FOOTER_H - 16), radius=5, outline=JAYVC_FRAME_HI, width=1)
        y = HEADER_H + 28
        for line in self.confirm_lines[:4]:
            centered_text(draw, (8, y, WIDTH - 8, y + 28), safe_text(str(line), 38), FONT_NORMAL, JAYVC_TEXT if jayvc else TEXT)
            y += 30

    def draw_playback(self, draw):
        # JayVC deliberately falls back to the established Basic playback UI,
        # matching the Basic recordings/file-action presentation.
        jayvc = self.ui_theme == "jayvc" and not self.hdmi_playback_context()
        path = self.playback_file if self.playback_file is not None else self.selected_file
        filename = path.name if path is not None else "No file selected"
        centered_text(draw, (8, 34, WIDTH - 8, 53), safe_text(filename, 40), FONT_SMALL, JAYVC_TEXT if jayvc else TEXT)

        current = format_elapsed(max(0.0, self.playback_position))
        total = format_elapsed(max(0.0, self.playback_duration)) if self.playback_duration > 0 else "--:--:--"
        state = (
            "PAUSED"
            if self.playback_paused and self.playback_running()
            else ("PLAYING" if self.playback_running() else "STOPPED")
        )
        sorny = self.ui_theme == "sorny"
        accent_colour = SORNY_ACCENT_BRIGHT if sorny else (JAYVC_FRAME_HI if jayvc else SONY_CYAN)
        paused_colour = SORNY_WARNING if sorny else (JAYVC_WARNING if jayvc else SONY_MAGENTA)
        dim_colour = SORNY_TEXT_DIM if sorny else (JAYVC_TEXT_DIM if jayvc else TEXT_DIM)
        warning_colour = SORNY_WARNING if sorny else (JAYVC_WARNING if jayvc else AMBER)
        state_colour = (
            paused_colour
            if self.playback_paused
            else (accent_colour if self.playback_running() else dim_colour)
        )
        draw_text(draw, (12, 56), state, FONT_STATUS_TINY, state_colour, fontmode="1")

        time_text = f"{current} / {total}"
        bbox = draw.textbbox((0, 0), time_text, font=FONT_STATUS_TINY)
        right_w = bbox[2] - bbox[0]
        draw_text(
            draw,
            (WIDTH - 12 - right_w, 56),
            time_text,
            FONT_STATUS_TINY,
            accent_colour,
            fontmode="1",
        )

        if self.playback_source_fps > 0:
            frame_rate = float(self.playback_source_fps)
            height = int(self.playback_source_height or 0)

            if self.playback_interlaced:
                # Interlaced standards are conventionally identified by field
                # rate, e.g. NTSC DV = 480i59.94 and PAL DV = 576i50.
                field_rate = frame_rate * 2.0
                if height > 0:
                    source_label = f"{height}i{format_fps(field_rate)}"
                else:
                    source_label = f"{format_fps(field_rate)}i"
            elif self.playback_field_order == "progressive":
                if height > 0:
                    source_label = f"{height}p{format_fps(frame_rate)}"
                else:
                    source_label = f"{format_fps(frame_rate)}p"
            else:
                if height > 0:
                    source_label = f"{height}p? {format_fps(frame_rate)}fps"
                else:
                    source_label = f"{format_fps(frame_rate)}fps"

            timing = f"{source_label}  >  {compact_hdmi_mode_label(self.playback_output_mode)}"
            centered_text(draw, (8, 74, WIDTH - 8, 90), timing, FONT_TINY, accent_colour)

        # Large touch target around a thinner visual progress track.
        x1, y1, x2, y2 = self.playback_seek_bar_box()
        draw.rounded_rectangle((x1, y1, x2, y2), radius=7, fill=JAYVC_BAR if jayvc else PANEL_ALT, outline=JAYVC_FRAME if jayvc else BORDER)
        track_y1 = y1 + 9
        track_y2 = y2 - 8
        draw.rounded_rectangle((x1 + 7, track_y1, x2 - 7, track_y2), radius=4, fill=JAYVC_PANEL_DARK if jayvc else PANEL)

        fraction = 0.0
        if self.playback_duration > 0:
            fraction = clamp(self.playback_position / self.playback_duration, 0.0, 1.0)
        track_x1 = x1 + 7
        track_x2 = x2 - 7
        progress_x = int(track_x1 + (track_x2 - track_x1) * fraction)
        if progress_x > track_x1:
            draw.rounded_rectangle((track_x1, track_y1, progress_x, track_y2), radius=4, fill=SORNY_ACCENT if sorny else (JAYVC_FRAME_HI if jayvc else ACCENT))
        thumb_y = (track_y1 + track_y2) // 2
        draw.ellipse((progress_x - 7, thumb_y - 7, progress_x + 7, thumb_y + 7), fill=JAYVC_TEXT if jayvc else TEXT, outline=SORNY_ACCENT if sorny else (JAYVC_FRAME_HI if jayvc else ACCENT))

        boxes = self.playback_control_boxes()
        middle_label = "PLAY" if self.playback_paused or not self.playback_running() else "PAUSE"
        labels = ["-10 SEC", middle_label, "+10 SEC"]
        for index, (label, box) in enumerate(zip(labels, boxes)):
            focused = index == self.playback_focus or index == self.playback_feedback
            fill = (JAYVC_SELECT if focused else JAYVC_BAR) if jayvc else (SELECTED if focused else PANEL_ALT)
            outline = (SORNY_ACCENT if focused else SORNY_RAIL) if sorny else ((JAYVC_FRAME_HI if focused else JAYVC_FRAME) if jayvc else (ACCENT if focused else BORDER))
            draw.rounded_rectangle(box, radius=6, fill=fill, outline=outline, width=2 if focused else 1)
            centered_text(draw, box, label, FONT_BUTTON if index != 1 else FONT_SMALL, JAYVC_TEXT if jayvc else TEXT)

        if self.message:
            centered_text(draw, (8, 179, WIDTH - 8, 191), safe_text(self.message, 42), FONT_TINY, warning_colour)

    def draw_dv_tape(self, draw):
        sorny = self.ui_theme == "sorny"
        jayvc = self.ui_theme == "jayvc"
        accent_colour = SORNY_ACCENT_BRIGHT if sorny else (JAYVC_FRAME_HI if jayvc else SONY_CYAN)
        ready_colour = SORNY_SUCCESS if sorny else (JAYVC_GREEN if jayvc else SONY_YELLOW)
        warning_colour = SORNY_WARNING if sorny else (JAYVC_WARNING if jayvc else AMBER)
        danger_colour = SORNY_DANGER if sorny else (SONY_RED if jayvc else RED)
        if self.dv_deck_recording:
            state = "REC " + format_elapsed(time.monotonic() - self.record_started)
            state_colour = danger_colour
        elif self.dv_deck_record_pending:
            state = "STARTING REC..."
            state_colour = warning_colour
        else:
            state = self.dv_deck_transport or "READY"
            state_colour = accent_colour if self.dv_deck_running() else ready_colour

        centered_text(
            draw,
            (6, 35, WIDTH - 6, 58),
            safe_text(state, 34),
            FONT_STATUS_BOLD,
            state_colour,
            fontmode="1",
        )
        centered_text(
            draw,
            (6, 57, WIDTH - 6, 76),
            safe_text(f"TC {self.dv_deck_timecode}", 30),
            FONT_STATUS_SMALL,
            accent_colour,
            fontmode="1",
        )

        boxes = self.dv_tape_control_boxes()
        labels = ["REW", "PLAY/PAUSE", "FF", "STOP", "PLAY + REC"]
        for index, (label, box) in enumerate(zip(labels, boxes)):
            focused = index == self.dv_tape_focus or index == self.dv_tape_feedback
            if sorny:
                # Tape transport needs more contrast than the surrounding dark
                # Sony menu chrome. Idle controls use medium neutral grey;
                # focus lifts the control to the pale Sony panel treatment.
                fill = SORNY_PANEL if focused else SORNY_NEUTRAL
                outline = SORNY_ACCENT if focused else SORNY_PANEL_EDGE
                button_text = SORNY_TEXT if focused else SORNY_HEADER_TEXT
            elif jayvc:
                fill = JAYVC_SELECT if focused else JAYVC_BAR
                outline = JAYVC_FRAME_HI if focused else JAYVC_FRAME
                button_text = JAYVC_TEXT
            else:
                fill = SELECTED if focused else PANEL_ALT
                outline = ACCENT if focused else BORDER
                button_text = TEXT

            if index == 4 and self.dv_deck_recording:
                outline = danger_colour
            draw.rounded_rectangle(box, radius=6, fill=fill, outline=outline, width=2 if focused else 1)
            colour = danger_colour if index == 4 else button_text
            centered_text(draw, box, label, FONT_SMALL if index == 1 else FONT_BUTTON, colour)

        if self.message:
            centered_text(draw, (8, 178, WIDTH - 8, 191), safe_text(self.message, 40), FONT_TINY, warning_colour)

    def draw_analog_control(self, draw):
        sorny = self.ui_theme == "sorny"
        jayvc = self.ui_theme == "jayvc"
        accent_colour = SORNY_ACCENT_BRIGHT if sorny else (JAYVC_FRAME_HI if jayvc else ACCENT)
        warning_colour = SORNY_WARNING if sorny else (JAYVC_WARNING if jayvc else AMBER)
        control = self.find_analog_control(self.active_analog_control_name)
        if control is None:
            centered_text(draw, (10, 70, WIDTH - 10, 110), "Control unavailable", FONT_NORMAL, warning_colour)
            return

        centered_text(draw, (10, 52, WIDTH - 10, 82), safe_text(control["label"], 30), FONT_BOLD, JAYVC_TEXT if jayvc else TEXT)
        centered_text(draw, (10, 86, WIDTH - 10, 132), str(control["value"]), FONT_BIG, accent_colour)
        centered_text(
            draw,
            (10, 132, WIDTH - 10, 158),
            f"Range {control['min']} .. {control['max']}   Step {control['step']}",
            FONT_SMALL,
            JAYVC_TEXT_DIM if jayvc else TEXT_DIM,
        )
        centered_text(draw, (10, 158, WIDTH - 10, 181), "Use - / + to adjust", FONT_SMALL, JAYVC_TEXT_DIM if jayvc else TEXT_DIM)

    def draw_font_size(self, draw):
        sorny = self.ui_theme == "sorny"
        jayvc = self.ui_theme == "jayvc"
        accent_colour = SORNY_ACCENT_BRIGHT if sorny else (JAYVC_FRAME_HI if jayvc else SONY_CYAN)
        step = self.font_size_step
        index = step - FONT_SIZE_MIN + 1
        labels = ("XS", "SMALL", "MEDIUM", "LARGE")
        label = labels[step + 2]

        centered_text(
            draw,
            (10, 38, WIDTH - 10, 61),
            f"{label}   {index}/4   ({step:+d} pt)",
            FONT_BOLD,
            accent_colour,
        )

        draw.rounded_rectangle(
            (16, 67, WIDTH - 16, 174),
            radius=7,
            fill=JAYVC_BAR if jayvc else PANEL_ALT,
            outline=JAYVC_FRAME if jayvc else BORDER,
        )
        centered_text(
            draw,
            (24, 76, WIDTH - 24, 105),
            "Menu text  Aa123",
            FONT_NORMAL,
            JAYVC_TEXT if jayvc else TEXT,
        )
        centered_text(
            draw,
            (24, 108, 135, 146),
            "REC",
            FONT_STATUS_BOLD,
            SONY_RED,
            fontmode="1",
        )
        centered_text(
            draw,
            (128, 108, WIDTH - 24, 146),
            "00:12:34",
            FONT_STATUS_SMALL,
            accent_colour,
            fontmode="1",
        )
        centered_text(
            draw,
            (24, 145, WIDTH - 24, 164),
            "Use - / + to preview",
            FONT_TINY,
            JAYVC_TEXT_DIM if jayvc else TEXT_DIM,
        )
        scrolling_text(
            draw,
            (12, 169, WIDTH - 12, HEIGHT - FOOTER_H - 2),
            "Note: May not be effective on every theme.",
            FONT_TINY,
            JAYVC_TEXT_DIM if jayvc else TEXT_DIM,
            speed=32.0,
            hold=0.9,
            background=SORNY_BG if sorny else (JAYVC_BG if jayvc else BG),
        )

    def draw_footer(self, draw):
        top = HEIGHT - FOOTER_H
        # Genuine Sorny keeps a consistent footer treatment on every screen
        # except HOME, whose REC / SOURCE / MENU colours remain purpose-built.
        sorny_footer = self.ui_theme == "sorny" and self.page != "home"
        jayvc_footer = self.ui_theme == "jayvc" and not self.hdmi_playback_context()
        draw.rectangle(
            (0, top, WIDTH, HEIGHT),
            fill=SORNY_BG if sorny_footer else (JAYVC_BAR if jayvc_footer else PANEL),
        )

        labels = self.bottom_buttons()
        button_width = WIDTH // 3

        for i, label in enumerate(labels):
            if not label:
                continue

            x1 = i * button_width
            x2 = WIDTH if i == 2 else (i + 1) * button_width

            focused = False
            if self.footer_feedback == i:
                focused = True
            elif self.page in ("home", "confirm") and self.bottom_focus == i:
                focused = True
            elif self.page == "wifi_password":
                keys = self.keyboard_keys()
                focused = self.keyboard_index == len(keys) + i

            fill = SELECTED if focused else PANEL_ALT
            outline = ACCENT if focused else BORDER
            label_colour = TEXT
            display_label = label

            # Genuine Sorny footer colours follow the same semantic palette.
            if sorny_footer:
                if label in ("BACK", "CANCEL"):
                    fill = SORNY_NEUTRAL_BRIGHT if focused else SORNY_NEUTRAL
                    outline = SORNY_NEUTRAL_BRIGHT
                    label_colour = (18, 21, 24) if focused else (255, 255, 255)
                elif label == "HOME":
                    fill = SORNY_ACCENT if focused else SORNY_ACCENT_DARK
                    outline = SORNY_ACCENT_BRIGHT
                    label_colour = (255, 255, 255)
                elif label in ("YES", "JOIN", "DONE"):
                    fill = SORNY_SUCCESS if focused else SORNY_SUCCESS_DARK
                    outline = SORNY_SUCCESS
                    label_colour = (15, 28, 20) if focused else SORNY_SUCCESS
                elif label == "STOP":
                    fill = SORNY_DANGER if focused else SORNY_DANGER_DARK
                    outline = SORNY_DANGER
                    label_colour = (255, 255, 255) if focused else SORNY_DANGER
                else:
                    fill = SORNY_ACCENT if focused else SORNY_ACCENT_DARK
                    outline = SORNY_ACCENT_BRIGHT if focused else SORNY_ACCENT
                    label_colour = (18, 29, 40) if focused else SORNY_ACCENT_BRIGHT

            if jayvc_footer:
                fill = JAYVC_SELECT if focused else JAYVC_BAR
                outline = JAYVC_FRAME_HI if focused else JAYVC_BAR_EDGE
                label_colour = JAYVC_TEXT

            # Persistent HOME/MENU navigation uses the dedicated menu-blue
            # family so it remains distinct from capture and source controls.
            if not sorny_footer and not jayvc_footer and label == "MENU":
                if self.ui_theme == "sorny":
                    fill = SORNY_ACCENT if focused else SORNY_ACCENT_DARK
                    outline = SORNY_ACCENT_BRIGHT if focused else SORNY_ACCENT
                    label_colour = (255, 255, 255)
                else:
                    fill = MENU_BLUE if focused else MENU_BLUE_DARK
                    outline = SONY_CYAN if focused else MENU_BLUE
                    label_colour = TEXT
            elif not sorny_footer and not jayvc_footer and label == "HOME":
                fill = SONY_BLUE if focused else SONY_BLUE_DARK
                outline = SONY_CYAN if focused else SONY_BLUE
                label_colour = TEXT

            # Sony's period title palette also included white and green. Use
            # those remaining colours for the generic navigation actions so
            # they do not duplicate REC/SOURCE or the cyan/magenta scroll rail.
            if not sorny_footer and not jayvc_footer and label == "BACK":
                fill = (52, 56, 62) if focused else PANEL_ALT
                outline = (255, 255, 255)
                label_colour = (255, 255, 255)
            elif not sorny_footer and not jayvc_footer and label == "SELECT":
                fill = SONY_GREEN if focused else SONY_GREEN_DARK
                outline = SONY_GREEN
                label_colour = (8, 11, 16) if focused else SONY_GREEN

            # HOME transport button uses Sony-style state colours. REC is red
            # while idle. During active recording, STOP flips to white with
            # Sony red text and border so the active state is unmistakable.
            if self.page == "home" and i == 0:
                if self.recording:
                    fill = (255, 255, 255)
                    outline = SONY_RED
                    label_colour = SONY_RED
                else:
                    fill = SONY_RED_DARK if not focused else SELECTED
                    outline = SONY_RED
                    label_colour = SONY_RED

            # SOURCE uses Sony yellow so the three HOME actions have distinct
            # Sony-style functional colours: REC/STOP, SOURCE and MENU.
            if self.page == "home" and i == 1:
                if self.ui_theme == "sorny":
                    fill = SORNY_WARNING if focused else SORNY_WARNING_DARK
                    outline = SORNY_WARNING
                    label_colour = (26, 22, 13) if focused else SORNY_WARNING
                else:
                    fill = SONY_YELLOW_DARK if not focused else SELECTED
                    outline = SONY_YELLOW
                    label_colour = SONY_YELLOW

            if jayvc_footer and self.page == "home" and i == 0:
                if self.recording:
                    fill = JAYVC_PANEL
                    outline = SONY_RED
                    label_colour = JAYVC_TEXT_DARK
                else:
                    fill = JAYVC_BAR
                    outline = JAYVC_FRAME_HI if focused else JAYVC_BAR_EDGE
                    label_colour = JAYVC_TEXT

            if jayvc_footer and self.page == "home" and i == 1:
                fill = JAYVC_SELECT if focused else JAYVC_BAR
                outline = JAYVC_WARNING if focused else JAYVC_BAR_EDGE
                label_colour = JAYVC_TEXT

            if jayvc_footer and label in ("HOME", "MENU", "BACK", "SELECT", "STOP") and self.page != "home":
                fill = JAYVC_SELECT if focused else JAYVC_BAR
                outline = JAYVC_FRAME_HI if focused else JAYVC_BAR_EDGE
                label_colour = JAYVC_TEXT

            active_record_button = self.page == "home" and i == 0 and self.recording
            draw.rectangle(
                (x1 + 2, top + 2, x2 - 2, HEIGHT - 2),
                fill=fill,
                outline=outline,
                width=2 if (focused or active_record_button) else 1,
            )
            if sorny_footer:
                # Use the same button font family as the front HOME screen,
                # retaining the crisp 1-bit Sorny footer rendering.
                centered_text_noaa(
                    draw,
                    (x1 + 2, top + 2, x2 - 2, HEIGHT - 2),
                    display_label,
                    FONT_BUTTON,
                    label_colour,
                )
            else:
                centered_text(
                    draw,
                    (x1 + 2, top + 2, x2 - 2, HEIGHT - 2),
                    display_label,
                    FONT_BUTTON,
                    label_colour,
                )

        # Tape Deck intentionally has no centre footer button. Use that space
        # for a two-line storage readout instead of squeezing it above BACK.
        if self.page == "dv_tape":
            storage_name, _, free_gb = self.storage()
            display_name = storage_display_name(storage_name)
            free_text = "UNAVAILABLE" if free_gb is None else f"{free_gb:.1f} GB FREE"
            x1 = button_width + 3
            x2 = (button_width * 2) - 3
            storage_colour = SORNY_ACCENT_BRIGHT if self.ui_theme == "sorny" else (JAYVC_FRAME_HI if self.ui_theme == "jayvc" else SONY_CYAN)
            storage_dim = SORNY_TEXT_DIM if self.ui_theme == "sorny" else (JAYVC_TEXT_DIM if self.ui_theme == "jayvc" else TEXT_DIM)
            centered_text(draw, (x1, top + 3, x2, top + 23), display_name, FONT_TINY, storage_colour)
            centered_text(draw, (x1, top + 22, x2, HEIGHT - 3), free_text, FONT_TINY, storage_dim)

    def render(self):
        self.update_message()
        jayvc_body = self.ui_theme == "jayvc" and not self.hdmi_playback_context()
        image = Image.new("RGB", (WIDTH, HEIGHT), SORNY_BG if self.ui_theme == "sorny" else (JAYVC_BG if jayvc_body else BG))
        draw = ImageDraw.Draw(image)
        draw.fontmode = "L"

        self.draw_header(draw)

        if self.page == "home":
            self.draw_home(draw)
        elif self.page == "info":
            self.draw_info(draw)
        elif self.page == "confirm":
            self.draw_confirm(draw)
        elif self.page == "dv_tape":
            self.draw_dv_tape(draw)
        elif self.page == "playback":
            self.draw_playback(draw)
        elif self.page == "analog_control":
            self.draw_analog_control(draw)
        elif self.page == "font_size":
            self.draw_font_size(draw)
        elif self.page == "wifi_password":
            self.draw_wifi_password(draw)
        else:
            self.draw_menu(draw)

        # Some full-screen/menu renderers may paint close to the top boundary.
        # Re-overlay the global header on every non-HOME screen so the CamPi
        # title/status bar remains clearly visible throughout the UI.
        if self.page != "home":
            self.draw_header(draw)

        self.draw_footer(draw)
        self.device.display(image)


# ============================================================
# DISPLAY INITIALISATION
# ============================================================

def create_display():
    serial = spi(
        port=SPI_PORT,
        device=LCD_SPI_DEVICE,
        gpio_DC=LCD_DC,
        gpio_RST=LCD_RST,
        bus_speed_hz=LCD_SPI_SPEED,
    )
    return ili9341(serial, width=WIDTH, height=HEIGHT, rotate=LCD_ROTATION)


# ============================================================
# MAIN
# ============================================================

def main():
    ensure_app_dirs()
    device = create_display()

    touch = None
    calibration = None

    try:
        touch = XPT2046(bus=SPI_PORT, device=TOUCH_SPI_DEVICE, speed=TOUCH_SPI_SPEED)
        calibration = load_touch_calibration()
        if calibration is None:
            calibration = calibrate_touch(device, touch)
    except Exception as exc:
        print("Touch unavailable:", exc)
        if touch is not None:
            touch.close()
        touch = None
        calibration = None

    ui = TapelessUI(
        device,
        touch=touch,
        calibration=calibration,
    )
    # HDMI remains untouched at startup. It is acquired only when the user
    # enters Recordings / playback.

    # Do not treat an asserted XPT2046 IRQ during panel/application startup as
    # a real tap.  v1.08 removed the HDMI startup holder, which made the main
    # input loop begin almost immediately; some cheap XPT2046 boards briefly
    # report TOUCH while settling.  We now require a clean released state
    # before touch input is armed.
    touch_armed = touch is None or calibration is None
    touch_release_since = None

    # Resolve taps on RELEASE rather than on initial contact. This prevents a
    # scroll gesture from prematurely activating the item it started on.
    touch_session = False
    touch_start = None
    touch_last = None
    touch_scroll_anchor_y = None
    touch_gesture_moved = False
    touch_swipe_consumed = False

    def read_touch_point():
        """Get a stable translated point without losing very short taps."""
        samples = []
        for attempt in range(TOUCH_SAMPLE_RETRIES):
            raw_point = touch.raw_point()
            if raw_point is not None:
                try:
                    samples.append(translate_touch(raw_point, calibration))
                except Exception:
                    pass
            if samples and attempt >= 1:
                break
            if attempt < TOUCH_SAMPLE_RETRIES - 1:
                time.sleep(TOUCH_SAMPLE_RETRY_DELAY)

        if not samples:
            return None
        if len(samples) == 1:
            return samples[0]

        # Average a couple of valid readings to reduce resistive-panel jitter
        # without making stylus input feel sluggish.
        x = int(round(sum(point[0] for point in samples) / len(samples)))
        y = int(round(sum(point[1] for point in samples) / len(samples)))
        return x, y

    try:
        while True:
            ui.poll_capture()
            now = time.monotonic()

            if touch is not None and calibration is not None:
                touching = touch.is_touched()

                if not touch_armed:
                    # Arm only after the controller has reported RELEASED for
                    # at least 120 ms. This prevents one bad startup sample
                    # from being translated into a footer/menu tap.
                    if touching:
                        touch_release_since = None
                    else:
                        if touch_release_since is None:
                            touch_release_since = now
                        elif now - touch_release_since >= TOUCH_STARTUP_RELEASE_SECONDS:
                            touch_armed = True
                else:
                    if touching:
                        point = read_touch_point()

                        if not touch_session:
                            touch_session = True
                            touch_start = point
                            touch_last = point
                            touch_scroll_anchor_y = point[1] if point is not None else None
                            touch_gesture_moved = False
                            touch_swipe_consumed = False
                        elif point is not None:
                            touch_last = point

                            if touch_start is not None:
                                dx = point[0] - touch_start[0]
                                dy = point[1] - touch_start[1]
                                if (dx * dx + dy * dy) >= (TOUCH_GESTURE_THRESHOLD_PX * TOUCH_GESTURE_THRESHOLD_PX):
                                    touch_gesture_moved = True

                            # Genuine Sorny cylinder menus move exactly one
                            # entry per finger gesture. This is intentionally
                            # conservative for the resistive touchscreen.
                            if (
                                ui.sorny_cylinder_enabled()
                                and touch_scroll_anchor_y is not None
                                and not touch_swipe_consumed
                            ):
                                delta_y = point[1] - touch_scroll_anchor_y
                                if abs(delta_y) >= TOUCH_SWIPE_STEP_PX:
                                    direction = 1 if delta_y < 0 else -1
                                    ui.animate_sorny_move(direction)
                                    touch_swipe_consumed = True
                                    touch_gesture_moved = True
                    else:
                        if touch_session:
                            # Commit immediately on the first clean RELEASE
                            # sample. The loop runs at ~20 Hz, so waiting for
                            # an additional release interval made fast taps feel
                            # like they needed a second press.
                            if not touch_gesture_moved and touch_last is not None:
                                ui.tap(touch_last[0], touch_last[1])

                            touch_session = False
                            touch_start = None
                            touch_last = None
                            touch_scroll_anchor_y = None
                            touch_gesture_moved = False
                            touch_swipe_consumed = False

            ui.render()
            time.sleep(TOUCH_LOOP_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        pass

    finally:
        if ui.recording:
            ui.stop_recording()
        ui._stop_dv_stream_probe()
        ui.close_dv_deck(show_message=False)
        ui.stop_playback(show_message=False, restore_holding=False)
        ui._stop_hdmi_holding()
        ui.buzzer.close()
        if touch is not None:
            touch.close()


if __name__ == "__main__":
    main()

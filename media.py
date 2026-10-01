"""File types, browser previews and thumbnails for PhotoCull.

Browsers can show JPEG/PNG/WebP/GIF directly. Everything else is turned into a
JPEG preview here and cached under %LOCALAPPDATA%\\PhotoCull:
  * HEIC/HEIF (phones) - decoded with pillow-heif when it is installed;
  * raw files with no JPEG next to them - the camera's embedded JPEG preview;
  * videos - a frame grabbed with ffmpeg (thumbnails only; playback is native).
"""
import hashlib
import io
import os
import re
import shutil
import subprocess

from PIL import Image, ImageOps

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    HAVE_HEIF = True
except ImportError:
    HAVE_HEIF = False

NATIVE = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif"}
HEIF = {".heic", ".heif"}
RAW = {".nef", ".nrw", ".dng", ".cr2", ".cr3", ".arw", ".raf", ".orf", ".rw2", ".pef", ".srw"}
VIDEO = {".mov", ".mp4", ".m4v", ".3gp", ".avi", ".mkv", ".mts", ".webm"}
COMPANION = {".xmp", ".aae"}  # edit sidecars deleted along with their photo
IMAGES = NATIVE | HEIF | RAW
MEDIA = IMAGES | VIDEO

CONTENT_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                 ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
                 ".avif": "image/avif", ".mov": "video/quicktime", ".mp4": "video/mp4",
                 ".m4v": "video/mp4", ".3gp": "video/3gpp", ".webm": "video/webm",
                 ".mkv": "video/x-matroska", ".avi": "video/x-msvideo", ".mts": "video/mp2t"}

CACHE_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "PhotoCull")
THUMB_PX = 320
FFMPEG = shutil.which("ffmpeg")


def ext(name):
    return os.path.splitext(name)[1].lower()


def group_key(name):
    """Base name shared by every file belonging to one shot.

    IMG_1234.HEIC, IMG_1234.MOV (Live Photo), IMG_1234.AAE and IMG_1234.JPG.xmp
    all map to 'img_1234'.
    """
    base, e = os.path.splitext(name)
    if e.lower() in COMPANION and ext(base) in MEDIA:
        base = os.path.splitext(base)[0]
    return base.lower()


def natural_key(s):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s.lower())]


def pick_display(files):
    """(file, render) used to show a group: render is native/heif/raw/video."""
    for kinds, render in ((NATIVE, "native"), (HEIF, "heif"), (RAW, "raw"), (VIDEO, "video")):
        for f in files:
            if ext(f) in kinds:
                return f, render
    return None, None


# ---------------------------------------------------------------- caching

def _cache_path(kind, path, suffix=".jpg"):
    st = os.stat(path)
    key = hashlib.sha1(f"{os.path.abspath(path)}|{st.st_size}|{kind}".encode()).hexdigest()
    return os.path.join(CACHE_DIR, kind, key + suffix)


def _cached(kind, path, build):
    cached = _cache_path(kind, path)
    try:
        with open(cached, "rb") as f:
            return f.read()
    except OSError:
        pass
    data = build()
    os.makedirs(os.path.dirname(cached), exist_ok=True)
    with open(cached + ".tmp", "wb") as f:
        f.write(data)
    os.replace(cached + ".tmp", cached)
    return data


def _to_jpeg(im, quality=88):
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=quality)
    return buf.getvalue()


# ---------------------------------------------------------------- raw previews

def _jpeg_end(data, start):
    """End offset of the baseline/progressive JPEG at start, or None."""
    i, n, displayable = start + 2, len(data), False
    while i + 4 <= n and data[i] == 0xFF:
        m = data[i + 1]
        if m == 0xFF:
            i += 1
            continue
        if m == 0xD8 or m == 0x01 or 0xD0 <= m <= 0xD7:
            i += 2
            continue
        length = int.from_bytes(data[i + 2:i + 4], "big")
        if m in (0xC0, 0xC1, 0xC2):
            displayable = True
        elif 0xC3 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
            return None  # lossless/arithmetic JPEG: raw sensor data, not a preview
        if m == 0xDA:
            end = data.find(b"\xff\xd9", i + 2 + length)
            return end + 2 if end > 0 and displayable else None
        i += 2 + length
    return None


def embedded_jpeg(path):
    """Largest displayable JPEG embedded in a raw file, as bytes."""
    with open(path, "rb") as f:
        data = f.read()
    best = (0, 0)
    pos = data.find(b"\xff\xd8\xff")
    while pos >= 0:
        end = _jpeg_end(data, pos)
        if end and end - pos > best[1] - best[0]:
            best = (pos, end)
        pos = data.find(b"\xff\xd8\xff", (end or pos + 3))
    if best[1] - best[0] < 10_000:
        raise ValueError(f"no embedded preview in {os.path.basename(path)}")
    jpeg = data[best[0]:best[1]]
    # Embedded previews usually lack the orientation tag; take it from the raw.
    try:
        with Image.open(path) as raw:
            orientation = raw.getexif().get(0x0112, 1)
    except Exception:
        orientation = 1
    if orientation in (3, 6, 8):
        with Image.open(io.BytesIO(jpeg)) as im:
            T = Image.Transpose
            im = im.transpose({3: T.ROTATE_180, 6: T.ROTATE_270, 8: T.ROTATE_90}[orientation])
            jpeg = _to_jpeg(im, 90)
    return jpeg


# ---------------------------------------------------------------- public

def preview(path, render):
    """(bytes, content_type) for showing a non-native image in the browser."""
    if render == "heif":
        if not HAVE_HEIF:
            raise LookupError("HEIC preview needs pillow-heif")

        def build():
            with Image.open(path) as im:
                return _to_jpeg(ImageOps.exif_transpose(im), 90)
        return _cached("heif", path, build), "image/jpeg"
    if render == "raw":
        return _cached("raw", path, lambda: embedded_jpeg(path)), "image/jpeg"
    raise ValueError(render)


def _video_frame(path):
    if not FFMPEG:
        raise LookupError("video thumbnails need ffmpeg")
    for seek in ("1", "0"):
        r = subprocess.run([FFMPEG, "-v", "error", "-ss", seek, "-i", path, "-frames:v", "1",
                            "-vf", f"scale={THUMB_PX * 2}:-2", "-f", "image2", "-c:v", "mjpeg", "-"],
                           capture_output=True, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.stdout:
            return r.stdout
    raise ValueError(f"no frame from {os.path.basename(path)}")


def thumbnail(path, render):
    def build():
        if render == "video":
            src = io.BytesIO(_video_frame(path))
        elif render == "raw":
            src = io.BytesIO(preview(path, "raw")[0])
        elif render == "heif" and not HAVE_HEIF:
            raise LookupError("HEIC preview needs pillow-heif")
        else:
            src = path
        with Image.open(src) as im:
            im.draft("RGB", (THUMB_PX * 2, THUMB_PX * 2))  # fast DCT downscale
            im = ImageOps.exif_transpose(im)
            im.thumbnail((THUMB_PX, THUMB_PX))
            return _to_jpeg(im, 80)
    return _cached(f"thumb{THUMB_PX}", path, build)

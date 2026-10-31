"""Keeper - fast rating, labelling and culling of photo folders.

Usage:  python keeper.py [folder]
Opens a browser UI. Without a folder it reopens the last one used (or the
Pictures folder); the page's Open folder button switches to another.

Files sharing a base name are one item (NEF+JPG, HEIC+MOV Live Photo, ...).
Nikon items keep ratings and labels where NX Studio reads them (see
xmp_rating.py); everything else, and all rejects, live in
<folder>/.keeper.json. Deleted items (every file in the group plus NX Studio
sidecars) go to the Recycle Bin.
"""
import ctypes
import json
import os
import re
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import webbrowser
import zlib
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote

from PIL import Image, ExifTags

import media as M
import xmp_rating as X

# Bundled files (keeper.html) live in PyInstaller's unpack folder when
# running as Keeper.exe, and next to this script otherwise.
HERE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
VERSION = "1.2.0"  # release.py tags and publishes this
STATE_FILE = ".keeper.json"
LEGACY_STATE_FILE = ".photocull.json"  # written by the app before it was renamed
# Remembers the last folder. Kept apart from the thumbnail cache in
# %LOCALAPPDATA%\Keeper, which the README says can be deleted at any time.
SETTINGS_FILE = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Keeper", "settings.json")
NIKON_RAW = {".nef", ".nrw"}
NIKON_NAME = re.compile(r"^(DSC_|_DSC|DSCN)\d", re.I)


# ---------------------------------------------------------------- catalogue

def _is_nikon_jpeg(path):
    """Out-of-camera Nikon JPEG (no NEF) - NX Studio reads its in-file XMP."""
    try:
        with open(path, "rb") as f:
            head = f.read(X._HEAD)
    except OSError:
        return False
    b = X._packet_bounds(head)
    return bool(b) and b"NIKON" in head[b[0]:b[1]]


class Catalogue:
    def __init__(self, folder, quick=False):
        # quick: for sidebar counts. Reads only Keeper's and NX Studio's own
        # files, never the photos (in-camera ratings), which on a big folder
        # is the difference between half a second and half a minute.
        self.folder = folder
        self.quick = quick
        self.lock = threading.Lock()
        self.times = {}  # key -> capture time, filled by capture_times()
        self.times_lock = threading.Lock()
        self.items = {}   # key -> dict
        self.order = []
        self.state = {"rejects": [], "ratings": {}, "labels": {}}
        self._load_state()
        self.scan()

    # -- state file: rejects for everything, ratings/labels for non-Nikon items
    def _load_state(self):
        # Folders culled before the rename have .photocull.json; read it until
        # the first save replaces it with .keeper.json.
        for name in (STATE_FILE, LEGACY_STATE_FILE):
            try:
                with open(os.path.join(self.folder, name), encoding="utf-8") as f:
                    self.state.update(json.load(f))
                break
            except (OSError, ValueError):
                continue
        self.rejects = {r.lower() for r in self.state["rejects"]}

    def _save_state(self):
        self.state["rejects"] = sorted(self.rejects)
        path = os.path.join(self.folder, STATE_FILE)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=1, sort_keys=True)
        os.replace(tmp, path)
        legacy = os.path.join(self.folder, LEGACY_STATE_FILE)
        if os.path.exists(legacy):
            os.remove(legacy)  # its contents now live in .keeper.json

    def scan(self):
        groups = {}
        for name in os.listdir(self.folder):
            e = M.ext(name)
            if (e in M.MEDIA or e in M.COMPANION) and os.path.isfile(os.path.join(self.folder, name)):
                groups.setdefault(M.group_key(name), []).append(name)
        has_nksc = os.path.isdir(os.path.join(self.folder, X.SIDECAR_DIR))
        items = {}
        for key, files in groups.items():
            display, render = M.pick_display(files)
            if not display:
                continue  # only companions left (e.g. a stray .xmp)
            files.sort(key=lambda n: (M.ext(n) not in M.RAW, M.ext(n) in M.COMPANION, n))
            images = [n for n in files if M.ext(n) in M.IMAGES]
            nikon = bool(images) and (
                any(M.ext(n) in NIKON_RAW for n in images)
                or (has_nksc and bool(X.sidecars_for(self.folder, images)))
                or bool(not self.quick and NIKON_NAME.match(display)
                        and _is_nikon_jpeg(os.path.join(self.folder, display))))
            rating, label = 0, None
            if nikon:
                try:
                    rating, label = X.read_pair(self.folder, images, camera=not self.quick)
                except Exception:
                    pass
            else:
                rating = self.state["ratings"].get(key, 0)
                label = self.state["labels"].get(key)
            items[key] = {"key": key, "name": os.path.splitext(display)[0], "files": files,
                          "images": images, "display": display, "render": render,
                          "nikon": nikon, "rating": rating, "label": label,
                          "viewable": render != "heif" or M.HAVE_HEIF}
        with self.lock:
            self.items = items
            self.order = sorted(items, key=lambda k: M.natural_key(items[k]["name"]))
            self.rejects &= set(items)

    def listing(self):
        hidden = ("images",)
        with self.lock:
            return [dict({k: v for k, v in self.items[b].items() if k not in hidden},
                         reject=b in self.rejects) for b in self.order]

    def update(self, key, **changes):
        """Set rating= and/or label= for an item."""
        item = self.items[key]
        if "rating" in changes and not 0 <= changes["rating"] <= 5:
            raise ValueError("rating must be 0-5")
        if changes.get("label") not in (None, *X.LABELS):
            raise ValueError("unknown label")
        with self.lock:
            if item["nikon"]:
                state = {"rating": item["rating"], "label": item["label"], **changes}
                X.write_pair(self.folder, item["images"], state["rating"], state["label"],
                             gps=self._gps_version(item))
            else:
                for field, store in (("rating", "ratings"), ("label", "labels")):
                    if field in changes:
                        if changes[field]:
                            self.state[store][key] = changes[field]
                        else:
                            self.state[store].pop(key, None)
                self._save_state()
            item.update(changes)

    def _gps_version(self, item):
        """EXIF GPSVersionID, which NX Studio copies into its sidecars."""
        for n in item["images"]:
            if M.ext(n) in M.NATIVE:
                try:
                    with Image.open(os.path.join(self.folder, n)) as im:
                        v = im.getexif().get_ifd(0x8825).get(0)
                    return bytes(v) if v else None
                except Exception:
                    return None
        return None

    def set_reject(self, key, reject):
        if key not in self.items:
            raise KeyError(key)
        with self.lock:
            (self.rejects.add if reject else self.rejects.discard)(key)
            self._save_state()

    def capture_times(self):
        """{key: Date Taken with sub-seconds, or None} for every item, for
        grouping bursts. Read once per folder (a few seconds for 1,000
        photos), in parallel, after the page has loaded."""
        with self.times_lock:
            with self.lock:
                todo = [self.items[k] for k in self.order if k not in self.times]
            paths = [None if i["render"] == "video" else os.path.join(self.folder, i["display"]) for i in todo]
            with ThreadPoolExecutor(8) as ex:
                for item, t in zip(todo, ex.map(lambda p: p and date_taken(p, subsec=True), paths)):
                    self.times[item["key"]] = t
            return dict(self.times)

    def fix_dates(self, apply=False):
        """Set each JPEG's Date Modified to its EXIF Date Taken.

        Only the file system timestamp changes; the file's contents are never
        touched. Date Taken has no time zone, so it's read as this PC's local
        time, which is how Windows shows it. A JPEG without one borrows it from
        its raw file (an NX Studio export of DSC_1234.NEF, say), if any.
        Returns {"change": [...], "same": n, "missing": [...], "failed": [...]}.
        """
        out = {"change": [], "same": 0, "missing": [], "failed": []}
        with self.lock:
            items = [self.items[k] for k in self.order]
        for item in items:
            jpegs = [n for n in item["files"] if M.ext(n) in (".jpg", ".jpeg")]
            if not jpegs:
                continue
            raws = [n for n in item["files"] if M.ext(n) in M.RAW]
            for name in jpegs:
                path = os.path.join(self.folder, name)
                taken = date_taken(path)
                for raw in raws:
                    taken = taken or date_taken(os.path.join(self.folder, raw))
                if taken is None:
                    out["missing"].append(name)
                    continue
                try:
                    st = os.stat(path)
                    if abs(st.st_mtime - taken) < 1:
                        out["same"] += 1
                        continue
                    if apply:
                        os.utime(path, (st.st_atime, taken))  # keep the access time
                    out["change"].append(name)
                except OSError:
                    out["failed"].append(name)
        return out

    def files_to_delete(self, key):
        item = self.items[key]
        paths = [os.path.join(self.folder, n) for n in item["files"]]
        paths += X.sidecars_for(self.folder, item["files"])
        return [p for p in paths if os.path.exists(p)]

    def delete_rejects(self):
        deleted, failed = [], []
        for key in sorted(self.rejects):
            if recycle(self.files_to_delete(key)):
                deleted.append(key)
            else:
                failed.append(key)
        with self.lock:
            for key in deleted:
                self.rejects.discard(key)
                self.items.pop(key, None)
                self.state["ratings"].pop(key, None)
                self.state["labels"].pop(key, None)
            self.order = [b for b in self.order if b in self.items]
            self._save_state()
        return deleted, failed


# ---------------------------------------------------------------- recycle bin

class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
                ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]


FO_DELETE = 3
FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 0x4, 0x10, 0x40, 0x400


def recycle(paths):
    """Send files to the Recycle Bin. Returns True if all are gone."""
    if not paths:
        return True
    op = SHFILEOPSTRUCTW()
    op.wFunc = FO_DELETE
    op.pFrom = "\0".join(os.path.abspath(p) for p in paths) + "\0\0"
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    return rc == 0 and not op.fAnyOperationsAborted and not any(map(os.path.exists, paths))


# ---------------------------------------------------------------- exif

_TAGS = {v: k for k, v in ExifTags.TAGS.items()}


def exif_info(path):
    with Image.open(path) as im:
        exif = im.getexif()
        sub = exif.get_ifd(0x8769)

    def g(name):
        return sub.get(_TAGS[name], exif.get(_TAGS[name]))
    out = {}
    t = g("ExposureTime")
    if t:
        t = float(t)
        out["shutter"] = f"1/{round(1 / t)}s" if t < 1 else f"{t:g}s"
    if g("FNumber"):
        out["aperture"] = f"f/{float(g('FNumber')):g}"
    if g("ISOSpeedRatings"):
        out["iso"] = f"ISO {g('ISOSpeedRatings')}"
    if g("FocalLength"):
        out["focal"] = f"{float(g('FocalLength')):g}mm"
    if g("DateTimeOriginal"):
        out["date"] = str(g("DateTimeOriginal"))
    if exif.get(_TAGS["Model"]):
        out["camera"] = str(exif.get(_TAGS["Model"])).strip("\0 ")
    return out


def _raw_exif_dates(path):
    """{tag: value} of the Exif date tags in a TIFF-based raw (NEF, DNG, ARW...),
    which Pillow can't read. Only the first 256 KB is read."""
    with open(path, "rb") as f:
        d = f.read(1 << 18)
    bo = {b"II": "little", b"MM": "big"}.get(d[:2])
    if not bo:
        return {}
    u = lambda o, n: int.from_bytes(d[o:o + n], bo)

    def entries(off):
        for i in range(u(off, 2) if off + 2 <= len(d) else 0):
            e = off + 2 + 12 * i
            if e + 12 <= len(d):
                yield u(e, 2), u(e + 2, 2), u(e + 4, 4), e + 8  # tag, type, count, value at
    exif = next((u(v, 4) for t, _, _, v in entries(u(4, 4)) if t == 0x8769), 0)
    out = {}
    for t, typ, n, v in entries(exif) if exif else ():
        if t in _DATE_TAGS and typ == 2:  # ASCII
            at = v if n <= 4 else u(v, 4)
            out[t] = d[at:at + n].decode("ascii", "replace")
    return out


# DateTimeOriginal and DateTimeDigitized, each with its SubSecTime tag.
_DATE_TAGS = {0x9003: 0x9291, 0x9004: 0x9292}
_DATE_TAGS.update({v: None for v in list(_DATE_TAGS.values())})


def date_taken(path, subsec=False):
    """EXIF Date Taken (DateTimeOriginal, else DateTimeDigitized) as a local
    timestamp, or None; with subsec, to the fraction of a second the camera
    recorded (needed to tell burst frames apart). Only the header is read."""
    try:
        if M.ext(path) in M.RAW:
            sub = _raw_exif_dates(path)
        else:
            with Image.open(path) as im:
                sub = im.getexif().get_ifd(0x8769)
        for tag in (0x9003, 0x9004):
            v = str(sub.get(tag) or "").strip("\0 ")
            if v and not v.startswith("0000"):
                t = time.mktime(time.strptime(v[:19], "%Y:%m:%d %H:%M:%S"))
                frac = "".join(c for c in str(sub.get(_DATE_TAGS[tag]) or "") if c.isdigit())
                return t + float("0." + frac) if subsec and frac else t
    except Exception:
        pass
    return None


# ---------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    cat: Catalogue = None  # replaced when another folder is opened
    media_url = ""
    notice = ""  # shown once by the page, e.g. "last folder isn't available"
    # Keep-alive: the browser closes connections, not us. On some PCs (seen
    # with antivirus network filtering) a connection the server closes first
    # loses the tail of a large response, so photos arrived truncated. Every
    # response must therefore carry a Content-Length.
    protocol_version = "HTTP/1.1"
    timeout = 60  # drop idle keep-alive connections so threads don't pile up

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json", cache=False):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, ctype):
        """Stream a file, honouring Range requests (needed for video seeking)."""
        size = os.path.getsize(path)
        start, end = 0, size - 1
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "max-age=3600")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(1 << 20, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        arg = parse_qs(query).get("path", [""])[0]
        try:
            if path == "/":
                with open(os.path.join(HERE, "keeper.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if path == "/api/folders":  # ?path=; default: the open folder's parent
                return self._send(200, folder_tree(arg or os.path.dirname(self.cat.folder)))
            if path == "/api/folder-stats":
                if not os.path.isdir(arg):
                    return self._send(404, {"error": "no such folder"})
                return self._send(200, folder_stats(arg))
            if path == "/api/recent":
                return self._send(200, recent_folders())
            if path == "/api/times":
                return self._send(200, self.cat.capture_times())
            if path == "/api/list":
                cat = self.cat
                # Media URLs carry a per-folder prefix so the browser's cached
                # /thumb/dsc_1234 from one folder isn't reused for another.
                media = f"{self.media_url}/f{zlib.crc32(cat.folder.lower().encode()):08x}"
                notice, Handler.notice = Handler.notice, ""
                return self._send(200, {"folder": cat.folder, "media": media, "notice": notice,
                                        "heif": M.HAVE_HEIF, "items": cat.listing()})
            path = re.sub(r"^/f[0-9a-f]{8}(?=/)", "", path)
            kind, _, key = path.lstrip("/").partition("/")
            if kind in ("img", "thumb", "info", "video"):
                item = self.cat.items[unquote(key)]
                p = os.path.join(self.cat.folder, item["display"])
                render = item["render"]
                if kind == "thumb":
                    return self._send(200, M.thumbnail(p, render), "image/jpeg", cache=True)
                if kind == "info":
                    return self._send(200, exif_info(p) if render == "native" else {}, cache=True)
                if kind == "video" or render == "native":
                    return self._send_file(p, M.CONTENT_TYPES.get(M.ext(p), "application/octet-stream"))
                data, ctype = M.preview(p, render)
                return self._send(200, data, ctype, cache=True)
            self._send(404, {"error": "not found"})
        except KeyError:
            self._send(404, {"error": "not found"})
        except (ConnectionError, BrokenPipeError):
            pass
        except Exception as e:
            try:
                self._send(415 if isinstance(e, LookupError) else 500, {"error": str(e)})
            except (ConnectionError, BrokenPipeError):
                pass

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(n) or b"{}")
            if self.path == "/api/rate":
                self.cat.update(data["key"], rating=int(data["rating"]))
                return self._send(200, {"ok": True})
            if self.path == "/api/label":
                self.cat.update(data["key"], label=data["label"] or None)
                return self._send(200, {"ok": True})
            if self.path == "/api/reject":
                self.cat.set_reject(data["key"], bool(data["reject"]))
                return self._send(200, {"ok": True})
            if self.path == "/api/delete-rejects":
                deleted, failed = self.cat.delete_rejects()
                return self._send(200, {"deleted": deleted, "failed": failed})
            if self.path == "/api/fix-dates":
                return self._send(200, self.cat.fix_dates(apply=bool(data.get("apply"))))
            if self.path == "/api/open":  # a folder picked in the sidebar
                if not os.path.isdir(data.get("path") or ""):
                    return self._send(404, {"error": "That folder isn't available"})
                open_folder(data["path"])
                return self._send(200, {"changed": True, "folder": self.cat.folder})
            if self.path == "/api/open-folder":
                if not PICKING.acquire(blocking=False):
                    return self._send(409, {"error": "The folder picker is already open"})
                try:
                    folder = pick_folder(self.cat.folder)
                finally:
                    PICKING.release()
                if not folder:
                    return self._send(200, {"changed": False})
                open_folder(folder)
                return self._send(200, {"changed": True, "folder": self.cat.folder})
            self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})


DEFAULT_PORT = 47615


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # The grid requests dozens of thumbnails at once; the default backlog of 5
    # makes Windows reset the overflow connections.
    request_queue_size = 128


class ExclusiveServer(Server):
    # On Windows, SO_REUSEADDR (Python's default here) lets a second process
    # bind a port that's already in use; without it, a second Keeper's bind fails.
    allow_reuse_address = False


def start_main_server(port):
    """Bind the page/API server.

    Uses a fixed port by default because the browser keeps the page's saved
    preferences (view, filmstrip, grid size...) per address, port included; a
    random port each launch would forget them. Falls back to any free port if
    that one is taken (e.g. a second Keeper window).
    """
    if port is not None:
        return ExclusiveServer(("127.0.0.1", port), Handler)
    try:
        return ExclusiveServer(("127.0.0.1", DEFAULT_PORT), Handler)
    except OSError:
        return ExclusiveServer(("127.0.0.1", 0), Handler)


PICKING = threading.Lock()  # one folder picker at a time


def pick_folder(start=None):
    """Windows folder picker; returns "" if cancelled. Safe to call from a
    request thread: the Tk root is created and destroyed on that thread."""
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)  # in front of the browser
    folder = filedialog.askdirectory(parent=root, title="Choose a folder of photos",
                                     initialdir=start or None, mustexist=True)
    root.destroy()
    return os.path.normpath(folder) if folder else ""


def load_settings():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(**changes):
    s = {**load_settings(), **changes}
    try:
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=1)
        os.replace(tmp, SETTINGS_FILE)
    except OSError as e:
        print(f"Couldn't save settings: {e}")


def default_folder():
    """The user's Pictures folder (wherever Windows has it, e.g. OneDrive)."""
    buf = ctypes.create_unicode_buffer(260)
    if ctypes.windll.shell32.SHGetFolderPathW(None, 0x27, None, 0, buf) == 0 and os.path.isdir(buf.value):  # CSIDL_MYPICTURES
        return buf.value
    return os.path.expanduser("~")


RECENT_MAX = 10


def open_folder(folder):
    """Scan folder, serve it from now on, and remember it for next time."""
    folder = os.path.abspath(folder)
    print(f"Scanning {folder} ...")
    Handler.cat = Catalogue(folder)
    Handler.notice = ""
    recent = [f for f in load_settings().get("recent", []) if f.lower() != folder.lower()]
    save_settings(last_folder=folder, recent=[folder, *recent][:RECENT_MAX])
    _STATS.pop(folder.lower(), None)  # it's about to change; counted live while open
    items = Handler.cat.items.values()
    nikon = sum(i["nikon"] for i in items)
    print(f"{len(items)} items ({nikon} Nikon, {len(items) - nikon} other).")
    if not M.HAVE_HEIF and any(i["render"] == "heif" for i in items):
        print("Note: HEIC photos can be culled but not previewed until pillow-heif is installed.")


# ---------------------------------------------------------------- folder sidebar

_SKIP_DIRS = {X.SIDECAR_DIR.lower(), "$recycle.bin", "system volume information"}
_STATS = {}  # folder.lower() -> (signature, stats); for folders other than the open one


def subfolders(folder):
    """[(name, path)] of folder's visible subfolders, in natural order."""
    out = []
    try:
        with os.scandir(folder) as it:
            for e in it:
                if (e.name.startswith((".", "$")) or e.name.lower() in _SKIP_DIRS
                        or not e.is_dir(follow_symlinks=False)):
                    continue
                try:
                    if e.stat().st_file_attributes & 0x6:  # hidden or system
                        continue
                except (OSError, AttributeError):
                    pass
                out.append((e.name, e.path))
    except OSError:
        pass
    return sorted(out, key=lambda t: M.natural_key(t[0]))


def folder_tree(folder):
    """One level of the sidebar tree: folder's subfolders, and its parent."""
    folder = os.path.abspath(folder)
    parent = os.path.dirname(folder)
    return {"path": folder, "name": os.path.basename(folder) or folder,
            "parent": parent if parent != folder else None,
            "children": [{"name": n, "path": p, "has_children": bool(subfolders(p))}
                         for n, p in subfolders(folder)]}


def counts(items):
    """Sidebar numbers: everything, still to sort (no stars, label or
    reject), and rejected."""
    return {"items": len(items),
            "todo": sum(1 for i in items if not (i["rating"] or i["label"] or i["reject"])),
            "rejected": sum(1 for i in items if i["reject"])}


def folder_stats(folder):
    """counts() for any folder. The open folder is counted live; others are
    scanned (read-only) and cached until their files or decisions change."""
    folder = os.path.abspath(folder)
    cat = Handler.cat
    if cat and cat.folder.lower() == folder.lower():
        return counts(cat.listing())
    sig = []
    for p in (folder, os.path.join(folder, X.SIDECAR_DIR), os.path.join(folder, STATE_FILE)):
        try:
            sig.append(os.stat(p).st_mtime_ns)
        except OSError:
            sig.append(None)
    hit = _STATS.get(folder.lower())
    if hit and hit[0] == sig:
        return hit[1]
    stats = counts(Catalogue(folder, quick=True).listing())
    _STATS[folder.lower()] = (sig, stats)
    return stats


def recent_folders():
    current = Handler.cat.folder.lower() if Handler.cat else ""
    return [{"path": f, "name": os.path.basename(f) or f, "exists": os.path.isdir(f)}
            for f in load_settings().get("recent", []) if f.lower() != current]


def startup_folder(args):
    """Folder from the command line, else the last one used, else Pictures.
    Returns (folder, notice to show in the page)."""
    if args:
        if os.path.isdir(args[0]):
            return args[0], ""
        sys.exit(f"Folder not found: {args[0]}")
    last = load_settings().get("last_folder")
    if last and os.path.isdir(last):
        return last, ""
    pictures = default_folder()
    if last:
        return pictures, f"The last folder ({last}) isn't available, so Keeper opened {pictures}. Use Open folder to pick another."
    return pictures, "Use Open folder to pick a folder of photos."


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    folder, notice = startup_folder(args)
    open_folder(folder)
    Handler.notice = notice
    port = next((int(a.split("=", 1)[1]) for a in sys.argv if a.startswith("--port=")), None)
    server = start_main_server(port)
    # Photos are served from a second port: browsers allow only ~6 connections
    # per host, and big image downloads must never queue up rating saves.
    media = Server(("127.0.0.1", 0), Handler)
    Handler.media_url = f"http://127.0.0.1:{media.server_port}"
    threading.Thread(target=media.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"Keeper {VERSION} running at {url}")
    print("Close this window to quit.")
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    server.serve_forever()


if __name__ == "__main__":
    main()

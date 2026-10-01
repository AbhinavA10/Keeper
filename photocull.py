"""PhotoCull - fast rating, labelling and culling of photo folders.

Usage:  python photocull.py [folder]
Opens a browser UI.

Files sharing a base name are one item (NEF+JPG, HEIC+MOV Live Photo, ...).
Nikon items keep ratings and labels where NX Studio reads them (see
xmp_rating.py); everything else, and all rejects, live in
<folder>/.photocull.json. Deleted items (every file in the group plus NX Studio
sidecars) go to the Recycle Bin.
"""
import ctypes
import json
import os
import re
import sys
import threading
import webbrowser
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

from PIL import Image, ExifTags

import media as M
import xmp_rating as X

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = ".photocull.json"
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
    def __init__(self, folder):
        self.folder = folder
        self.lock = threading.Lock()
        self.items = {}   # key -> dict
        self.order = []
        self.state = {"rejects": [], "ratings": {}, "labels": {}}
        self._load_state()
        self.scan()

    # -- state file: rejects for everything, ratings/labels for non-Nikon items
    def _load_state(self):
        try:
            with open(os.path.join(self.folder, STATE_FILE), encoding="utf-8") as f:
                self.state.update(json.load(f))
        except (OSError, ValueError):
            pass
        self.rejects = {r.lower() for r in self.state["rejects"]}

    def _save_state(self):
        self.state["rejects"] = sorted(self.rejects)
        path = os.path.join(self.folder, STATE_FILE)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=1, sort_keys=True)
        os.replace(tmp, path)

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
                or bool(NIKON_NAME.match(display) and _is_nikon_jpeg(os.path.join(self.folder, display))))
            rating, label = 0, None
            if nikon:
                try:
                    rating, label = X.read_pair(self.folder, images)
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
                X.write_pair(self.folder, item["images"], **changes)
            else:
                for field, store in (("rating", "ratings"), ("label", "labels")):
                    if field in changes:
                        if changes[field]:
                            self.state[store][key] = changes[field]
                        else:
                            self.state[store].pop(key, None)
                self._save_state()
            item.update(changes)

    def set_reject(self, key, reject):
        if key not in self.items:
            raise KeyError(key)
        with self.lock:
            (self.rejects.add if reject else self.rejects.discard)(key)
            self._save_state()

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


# ---------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    cat: Catalogue = None
    media_url = ""

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
        path = self.path.split("?")[0]
        try:
            if path == "/":
                with open(os.path.join(HERE, "photocull.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if path == "/api/list":
                return self._send(200, {"folder": self.cat.folder, "media": self.media_url,
                                        "heif": M.HAVE_HEIF, "items": self.cat.listing()})
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
            self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})


def pick_folder():
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    folder = filedialog.askdirectory(title="Choose a folder of photos")
    root.destroy()
    return folder


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    folder = args[0] if args else pick_folder()
    if not folder or not os.path.isdir(folder):
        sys.exit("No folder chosen.")
    folder = os.path.abspath(folder)
    print(f"Scanning {folder} ...")
    Handler.cat = Catalogue(folder)
    port = next((int(a.split("=", 1)[1]) for a in sys.argv if a.startswith("--port=")), 0)
    ThreadingHTTPServer.daemon_threads = True
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    # Photos are served from a second port: browsers allow only ~6 connections
    # per host, and big image downloads must never queue up rating saves.
    media = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Handler.media_url = f"http://127.0.0.1:{media.server_port}"
    threading.Thread(target=media.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/"
    items = Handler.cat.items.values()
    nikon = sum(i["nikon"] for i in items)
    print(f"{len(items)} items ({nikon} Nikon, {len(items) - nikon} other). PhotoCull running at {url}")
    if not M.HAVE_HEIF and any(i["render"] == "heif" for i in items):
        print("Note: HEIC photos can be culled but not previewed until pillow-heif is installed.")
    print("Close this window to quit.")
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    server.serve_forever()


if __name__ == "__main__":
    main()

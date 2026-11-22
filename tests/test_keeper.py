"""Folder scanning, where decisions are stored, Fix dates, folder memory and the API."""
import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from unittest import mock

import keeper as K
import xmp_rating as X
from helpers import fingerprints, make_jpeg, make_nef, touch

TAKEN = "2026:08:24 11:48:06"
TAKEN_TS = time.mktime(time.strptime(TAKEN, "%Y:%m:%d %H:%M:%S"))


class Folder(unittest.TestCase):
    """A temp folder with a Nikon pair, phone photos, a video and stray files."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        d = self.dir = self._tmp.name
        make_nef(os.path.join(d, "DSC_0001.NEF"), TAKEN)
        make_jpeg(os.path.join(d, "DSC_0001.JPG"), TAKEN)
        touch(os.path.join(d, "IMG_0002.HEIC"))
        touch(os.path.join(d, "IMG_0002.MOV"))       # Live Photo
        make_jpeg(os.path.join(d, "IMG_0003.JPG"))
        touch(os.path.join(d, "IMG_0003.AAE"))       # iPhone edit file
        touch(os.path.join(d, "clip.mp4"))
        touch(os.path.join(d, "orphan.xmp"))         # companion with no photo
        touch(os.path.join(d, "notes.txt"))
        self.photos = fingerprints(d)
        self.cat = K.Catalogue(d)

    def tearDown(self):
        self._tmp.cleanup()

    def assertPhotosUntouched(self):
        now = {n: f for n, f in fingerprints(self.dir).items() if n in self.photos}
        self.assertEqual(now, self.photos)

    def state(self):
        with open(os.path.join(self.dir, K.STATE_FILE), encoding="utf-8") as f:
            return json.load(f)


class Scan(Folder):
    def test_groups_files_by_shot(self):
        files = {k: it["files"] for k, it in self.cat.items.items()}
        self.assertEqual(files, {
            "dsc_0001": ["DSC_0001.NEF", "DSC_0001.JPG"],
            "img_0002": ["IMG_0002.HEIC", "IMG_0002.MOV"],
            "img_0003": ["IMG_0003.JPG", "IMG_0003.AAE"],
            "clip": ["clip.mp4"]})
        self.assertTrue(self.cat.items["dsc_0001"]["nikon"])
        self.assertFalse(self.cat.items["img_0003"]["nikon"])

    def test_scanning_writes_nothing(self):
        self.assertPhotosUntouched()
        self.assertEqual(set(os.listdir(self.dir)), set(self.photos))


class Decisions(Folder):
    def test_nikon_ratings_go_to_sidecars(self):
        self.cat.update("dsc_0001", rating=4)
        self.cat.update("dsc_0001", label="Blue")
        for n in ("DSC_0001.NEF", "DSC_0001.JPG"):
            self.assertEqual(X.read_sidecar(X.sidecar_path(self.dir, n)), (4, "Blue"))
        self.assertFalse(os.path.exists(os.path.join(self.dir, K.STATE_FILE)))
        self.assertPhotosUntouched()

    def test_other_ratings_go_to_the_state_file(self):
        self.cat.update("img_0003", rating=2, label="Red")
        self.assertEqual(self.state()["ratings"], {"img_0003": 2})
        self.assertEqual(self.state()["labels"], {"img_0003": "Red"})
        self.cat.update("img_0003", label=None)
        self.assertEqual(self.state()["labels"], {})
        self.assertFalse(os.path.exists(os.path.join(self.dir, X.SIDECAR_DIR)))
        self.assertPhotosUntouched()

    def test_decisions_survive_a_restart(self):
        self.cat.update("dsc_0001", rating=5, label="Red")
        self.cat.update("img_0003", rating=3)
        self.cat.set_reject("clip", True)
        again = {it["key"]: it for it in K.Catalogue(self.dir).listing()}
        self.assertEqual((again["dsc_0001"]["rating"], again["dsc_0001"]["label"]), (5, "Red"))
        self.assertEqual(again["img_0003"]["rating"], 3)
        self.assertTrue(again["clip"]["reject"])
        self.assertFalse(again["img_0003"]["reject"])

    def test_rejects_never_reach_nx_studio(self):
        self.cat.set_reject("dsc_0001", True)
        self.assertEqual(self.state()["rejects"], ["dsc_0001"])
        self.assertFalse(os.path.exists(os.path.join(self.dir, X.SIDECAR_DIR)))
        self.assertPhotosUntouched()

    def test_bad_values_are_refused(self):
        with self.assertRaises(ValueError):
            self.cat.update("img_0003", rating=9)
        with self.assertRaises(ValueError):
            self.cat.update("dsc_0001", label="Teal")

    def test_reads_the_old_photocull_file_then_replaces_it(self):
        legacy = os.path.join(self.dir, K.LEGACY_STATE_FILE)
        with open(legacy, "w", encoding="utf-8") as f:
            json.dump({"rejects": ["clip"], "ratings": {"img_0003": 4}, "labels": {}}, f)
        cat = K.Catalogue(self.dir)
        self.assertEqual(cat.items["img_0003"]["rating"], 4)
        self.assertIn("clip", cat.rejects)
        cat.set_reject("img_0002", True)
        self.assertFalse(os.path.exists(legacy))
        self.assertEqual(self.state()["rejects"], ["clip", "img_0002"])
        self.assertEqual(self.state()["ratings"], {"img_0003": 4})

    def test_delete_takes_every_file_of_the_shot_and_its_sidecars(self):
        self.cat.update("dsc_0001", rating=1)
        names = sorted(os.path.relpath(p, self.dir) for p in self.cat.files_to_delete("dsc_0001"))
        self.assertEqual(names, sorted(["DSC_0001.NEF", "DSC_0001.JPG",
                                        os.path.join("NKSC_PARAM", "DSC_0001.NEF.nksc"),
                                        os.path.join("NKSC_PARAM", "DSC_0001.JPG.nksc")]))
        self.assertEqual(sorted(os.path.basename(p) for p in self.cat.files_to_delete("img_0003")),
                         ["IMG_0003.AAE", "IMG_0003.JPG"])


class FixDates(Folder):
    def setUp(self):
        super().setUp()
        make_jpeg(os.path.join(self.dir, "DSC_0004.JPG"))      # export without EXIF...
        make_nef(os.path.join(self.dir, "DSC_0004.NEF"), "2026:08:25 07:00:00")  # ...but its NEF has it
        self.cat = K.Catalogue(self.dir)
        self.photos = fingerprints(self.dir)

    def test_reads_date_taken_from_jpeg_and_raw(self):
        self.assertEqual(K.date_taken(os.path.join(self.dir, "DSC_0001.JPG")), TAKEN_TS)
        self.assertEqual(K.date_taken(os.path.join(self.dir, "DSC_0001.NEF")), TAKEN_TS)
        self.assertIsNone(K.date_taken(os.path.join(self.dir, "IMG_0003.JPG")))
        self.assertIsNone(K.date_taken(os.path.join(self.dir, "notes.txt")))

    def test_dry_run_changes_nothing(self):
        out = self.cat.fix_dates()
        self.assertEqual(sorted(out["change"]), ["DSC_0001.JPG", "DSC_0004.JPG"])
        self.assertEqual(out["missing"], ["IMG_0003.JPG"])
        self.assertPhotosUntouched()

    def test_sets_only_the_modified_time(self):
        self.cat.fix_dates(apply=True)
        now = fingerprints(self.dir)
        for name, (sha, size, _) in self.photos.items():
            self.assertEqual(now[name][:2], (sha, size), f"{name} contents changed")
        self.assertEqual(os.path.getmtime(os.path.join(self.dir, "DSC_0001.JPG")), TAKEN_TS)
        self.assertEqual(os.path.getmtime(os.path.join(self.dir, "DSC_0004.JPG")),
                         time.mktime(time.strptime("2026:08:25 07:00:00", "%Y:%m:%d %H:%M:%S")))
        # Only JPEGs with a date: the NEFs and the undated JPEG keep their times.
        for name in ("DSC_0001.NEF", "DSC_0004.NEF", "IMG_0003.JPG"):
            self.assertEqual(now[name], self.photos[name], name)
        again = self.cat.fix_dates()
        self.assertEqual((again["change"], again["same"]), ([], 2))


class CaptureTimes(Folder):
    def test_sub_seconds_for_bursts(self):
        p = make_jpeg(os.path.join(self.dir, "DSC_0009.JPG"), TAKEN, subsec="37")
        self.assertAlmostEqual(K.date_taken(p, subsec=True), TAKEN_TS + .37, places=3)
        self.assertEqual(K.date_taken(p), TAKEN_TS)  # Fix dates keeps whole seconds

    def test_every_item_gets_a_time_or_none(self):
        make_jpeg(os.path.join(self.dir, "DSC_0002.JPG"), TAKEN, subsec="5")
        cat = K.Catalogue(self.dir)
        times = cat.capture_times()
        self.assertEqual(set(times), set(cat.items))
        self.assertEqual(times["dsc_0001"], TAKEN_TS)
        self.assertAlmostEqual(times["dsc_0002"], TAKEN_TS + .5)
        self.assertIsNone(times["clip"])       # videos aren't read
        self.assertIsNone(times["img_0003"])   # no Date Taken
        self.assertPhotosUntouched()


@unittest.skipUnless(K.M.FFMPEG and K.M.FFPROBE, "needs ffmpeg")
class RotateVideo(unittest.TestCase):
    """Rotating rewrites the video's header only and keeps its dates."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.path = os.path.join(self.dir, "IMG_0100.MOV")
        subprocess.run([K.M.FFMPEG, "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=2:size=320x180:rate=15",
                        "-f", "lavfi", "-i", "sine=duration=2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                        "-metadata", "creation_time=2026-07-01T09:30:00Z", self.path], check=True)
        old = 1_600_000_000_000_000_000  # Sep 2020, in ns
        K.set_file_times(self.path, old - 10**12, old + 10**9, old)
        self.original = fingerprints(self.dir)["IMG_0100.MOV"]
        K.set_file_times(self.path, old - 10**12, old + 10**9, old)  # again: reading it updated Accessed
        self.st = os.stat(self.path)
        self.orig_path = os.path.join(self.dir, "IMG_0100_original.MOV")
        self.binned = []
        def fake_recycle(paths):  # keep the real Recycle Bin out of tests
            self.binned += [os.path.basename(p) for p in paths]
            for p in paths:
                os.remove(p)
            return True
        patch = mock.patch.object(K, "recycle", fake_recycle)
        patch.start()
        self.addCleanup(patch.stop)
        self.cat = K.Catalogue(self.dir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_rotates_losslessly_and_keeps_every_date(self):
        out = self.cat.rotate_video("img_0100", 90)
        self.assertEqual(out["rotation"], -90)  # 90° clockwise = -90 counter-clockwise
        st = os.stat(self.path)  # before anything reads the file (reading updates Accessed)
        self.assertEqual((st.st_mtime_ns, st.st_atime_ns, st.st_ctime_ns),
                         (self.st.st_mtime_ns, self.st.st_atime_ns, self.st.st_ctime_ns))
        # The original is kept beside it, untouched (renamed, same contents and dates).
        self.assertEqual(sorted(os.listdir(self.dir)), ["IMG_0100.MOV", "IMG_0100_original.MOV"])
        self.assertEqual(fingerprints(self.dir)["IMG_0100_original.MOV"], self.original)
        self.assertEqual(self.binned, [])
        self.assertEqual((out["rotated"], out["files"]), (True, ["IMG_0100.MOV", "IMG_0100_original.MOV"]))
        info = K.M.video_info(self.path)
        self.assertEqual((info["rotation"], info["streams"]), (-90, ["video", "audio"]))
        r = subprocess.run([K.M.FFPROBE, "-v", "error", "-show_entries", "format_tags=creation_time",
                            "-of", "default=nw=1:nk=1", self.path], capture_output=True, text=True)
        self.assertTrue(r.stdout.startswith("2026-07-01T09:30:00"))

    def test_the_pair_is_one_item_after_a_rescan(self):
        self.cat.rotate_video("img_0100", 90)
        item = K.Catalogue(self.dir).items["img_0100"]
        self.assertEqual((item["display"], item["files"], item["rotated"]),
                         ("IMG_0100.MOV", ["IMG_0100.MOV", "IMG_0100_original.MOV"], True))
        self.assertEqual(len(K.Catalogue(self.dir).items), 1)

    def test_turns_add_up_and_turning_back_restores_the_original(self):
        self.assertEqual(self.cat.rotate_video("img_0100", 90)["rotation"], -90)
        self.assertEqual(self.cat.rotate_video("img_0100", 90)["rotation"], 180)
        self.assertEqual(fingerprints(self.dir)["IMG_0100_original.MOV"], self.original)  # still the first one
        self.assertEqual(self.cat.rotate_video("img_0100", -90)["rotation"], -90)
        out = self.cat.rotate_video("img_0100", -90)
        self.assertEqual((out["rotation"], out["ver"], out["rotated"]), (0, 4, False))
        self.assertEqual(os.listdir(self.dir), ["IMG_0100.MOV"])
        self.assertEqual(fingerprints(self.dir)["IMG_0100.MOV"], self.original)  # the original itself is back
        self.assertEqual(self.binned, ["IMG_0100.MOV"])  # the rotated copy, to the Recycle Bin

    def test_only_video_originals_are_grouped(self):
        self.assertEqual(K.M.group_key("IMG_1_original.MP4"), "img_1")
        self.assertEqual(K.M.group_key("IMG_1_original.JPG"), "img_1_original")  # a photo named that is its own item

    def test_refuses_what_it_cannot_rotate(self):
        make_jpeg(os.path.join(self.dir, "IMG_0101.JPG"))
        touch(os.path.join(self.dir, "clip.mkv"))
        cat = K.Catalogue(self.dir)
        for key, deg in (("img_0101", 90), ("clip", 90), ("img_0100", 45)):
            with self.subTest(key=key, deg=deg), self.assertRaises(ValueError):
                cat.rotate_video(key, deg)
        self.assertEqual(self.binned, [])

    def test_a_failed_rewrite_leaves_the_original(self):
        before = fingerprints(self.dir)
        with mock.patch.object(K.M, "write_rotated", side_effect=ValueError("boom")), self.assertRaises(ValueError):
            self.cat.rotate_video("img_0100", 90)
        self.assertEqual(fingerprints(self.dir), before)
        self.assertEqual(self.binned, [])


class FolderMemory(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.photos = os.path.join(self._tmp.name, "photos")
        self.pictures = os.path.join(self._tmp.name, "Pictures")
        os.makedirs(self.photos)
        os.makedirs(self.pictures)
        patches = [mock.patch.object(K, "SETTINGS_FILE", os.path.join(self._tmp.name, "Keeper", "settings.json")),
                   mock.patch.object(K, "default_folder", lambda: self.pictures)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def test_first_run_opens_pictures(self):
        folder, notice = K.startup_folder([])
        self.assertEqual(folder, self.pictures)
        self.assertIn("Open folder", notice)

    def test_reopens_the_last_folder(self):
        K.open_folder(self.photos)
        self.assertEqual(K.startup_folder([]), (self.photos, ""))

    def test_command_line_wins(self):
        K.save_settings(last_folder=self.photos)
        self.assertEqual(K.startup_folder([self.pictures]), (self.pictures, ""))

    def test_missing_last_folder_falls_back_with_a_notice(self):
        K.save_settings(last_folder=os.path.join(self._tmp.name, "unplugged"))
        folder, notice = K.startup_folder([])
        self.assertEqual(folder, self.pictures)
        self.assertIn("unplugged", notice)


class Sidebar(Folder):
    def setUp(self):
        super().setUp()
        for sub in ("Trip 10", "Trip 2", ".hidden", "$RECYCLE.BIN", os.path.join("Trip 2", "day1")):
            os.makedirs(os.path.join(self.dir, sub))
        patches = [mock.patch.object(K, "SETTINGS_FILE", os.path.join(self.dir, ".settings.json")),
                   mock.patch.object(K.Handler, "cat", None)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        K._STATS.clear()

    def test_lists_visible_subfolders_in_natural_order(self):
        self.cat.update("dsc_0001", rating=1)  # creates NKSC_PARAM, which must not be listed
        t = K.folder_tree(self.dir)
        self.assertEqual([(c["name"], c["has_children"]) for c in t["children"]],
                         [("Trip 2", True), ("Trip 10", False)])
        self.assertEqual(t["parent"], os.path.dirname(self.dir))

    def test_counts_what_is_left_to_sort(self):
        self.cat.update("dsc_0001", rating=3)
        self.cat.update("img_0003", label="Blue")
        self.cat.set_reject("clip", True)
        self.assertEqual(K.folder_stats(self.dir), {"items": 4, "todo": 1, "rejected": 1})
        self.assertEqual(K.folder_stats(os.path.join(self.dir, "Trip 2")), {"items": 0, "todo": 0, "rejected": 0})
        self.assertPhotosUntouched()

    def test_cached_counts_follow_new_decisions(self):
        self.assertEqual(K.folder_stats(self.dir)["todo"], 4)
        self.cat.set_reject("img_0002", True)
        self.assertEqual(K.folder_stats(self.dir)["todo"], 3)

    def test_quick_scan_skips_in_camera_ratings(self):
        make_jpeg(os.path.join(self.dir, "DSC_0005.JPG"), xmp_rating=4)
        make_nef(os.path.join(self.dir, "DSC_0005.NEF"))
        self.assertEqual(K.Catalogue(self.dir).items["dsc_0005"]["rating"], 4)
        self.assertEqual(K.Catalogue(self.dir, quick=True).items["dsc_0005"]["rating"], 0)

    def test_recent_folders_most_recent_first(self):
        trip2, trip10 = os.path.join(self.dir, "Trip 2"), os.path.join(self.dir, "Trip 10")
        for f in (trip2, trip10, self.dir, trip2):
            K.open_folder(f)
        self.assertEqual(K.load_settings()["recent"], [trip2, self.dir, trip10])
        self.assertEqual([r["path"] for r in K.recent_folders()], [self.dir, trip10])  # not the open one
        os.rmdir(trip10)
        self.assertFalse(K.recent_folders()[1]["exists"])


class Api(Folder):
    """The HTTP endpoints the page uses, against a real server."""

    def setUp(self):
        super().setUp()
        for patch in (mock.patch.multiple(K.Handler, cat=self.cat, media_url="", notice=""),
                      mock.patch.object(K.M, "CACHE_DIR", os.path.join(self.dir, ".cache"))):
            patch.start()
            self.addCleanup(patch.stop)
        self.server = K.Server(("127.0.0.1", 0), K.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return r.status, r.headers.get("Content-Type"), r.read()

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)

    def test_list_and_folder_prefixed_media(self):
        _, _, body = self.get("/api/list")
        j = json.loads(body)
        self.assertEqual(len(j["items"]), 4)
        self.assertRegex(j["media"], r"/f[0-9a-f]{8}$")
        status, ctype, data = self.get(j["media"] + "/thumb/dsc_0001")
        self.assertEqual((status, ctype), (200, "image/jpeg"))
        self.assertTrue(data.startswith(b"\xff\xd8"))

    def test_rate_label_reject(self):
        self.assertEqual(self.post("/api/rate", {"key": "dsc_0001", "rating": 3}), {"ok": True})
        self.assertEqual(self.post("/api/label", {"key": "dsc_0001", "label": "Blue"}), {"ok": True})
        self.assertEqual(self.post("/api/reject", {"key": "img_0003", "reject": True}), {"ok": True})
        self.assertEqual(X.read_pair(self.dir, ["DSC_0001.NEF", "DSC_0001.JPG"]), (3, "Blue"))
        self.assertEqual(self.state()["rejects"], ["img_0003"])
        self.assertPhotosUntouched()

    def test_sidebar_endpoints(self):
        sub = os.path.join(self.dir, "Trip")
        os.makedirs(sub)
        make_jpeg(os.path.join(sub, "IMG_9.JPG"))
        _, _, body = self.get("/api/folders?path=" + urllib.parse.quote(self.dir))
        self.assertEqual([c["name"] for c in json.loads(body)["children"]], ["Trip"])
        _, _, body = self.get("/api/folder-stats?path=" + urllib.parse.quote(sub))
        self.assertEqual(json.loads(body), {"items": 1, "todo": 1, "rejected": 0})
        with mock.patch.object(K, "SETTINGS_FILE", os.path.join(self.dir, ".settings.json")):
            self.assertEqual(self.post("/api/open", {"path": sub})["folder"], sub)
            self.assertEqual(K.Handler.cat.folder, sub)
            with self.assertRaises(urllib.error.HTTPError) as e:
                self.post("/api/open", {"path": os.path.join(self.dir, "nope")})
            self.assertEqual(e.exception.code, 404)

    def test_capture_times_endpoint(self):
        _, _, body = self.get("/api/times")
        self.assertEqual(json.loads(body)["dsc_0001"], TAKEN_TS)

    def test_dropped_connections_are_not_reported(self):
        # Seeking a video drops the connection mid-download; that's routine.
        for err, printed in ((ConnectionResetError(10054, "reset"), False), (ValueError("bug"), True)):
            with self.subTest(err=type(err).__name__), mock.patch("sys.stderr", new_callable=io.StringIO) as out:
                try:
                    raise err
                except Exception:
                    self.server.handle_error(None, ("127.0.0.1", 1))
                self.assertEqual(bool(out.getvalue()), printed)

    def test_bad_rating_is_an_error_not_a_write(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.post("/api/rate", {"key": "dsc_0001", "rating": 7})
        self.assertEqual(e.exception.code, 500)
        self.assertFalse(os.path.exists(os.path.join(self.dir, X.SIDECAR_DIR)))


if __name__ == "__main__":
    unittest.main()

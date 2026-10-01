# PhotoCull

Fast keyboard culling for photo folders — Nikon NEF+JPG (NX Studio compatible),
phone photos and videos.

**Run:** double-click `PhotoCull.bat` and pick a folder, or drag a folder onto it.
It opens in your browser; close the console window to quit.

| Key | Action |
|---|---|
| ← → / Space | Previous / next |
| X | Toggle reject |
| 1–5, 0 | Set / clear stars |
| R | Toggle red label |
| Z or click | Zoom 100% |
| P | Play / pause video |
| Ctrl+Z | Undo |
| F | Cycle filter (unrated, rejected, red, 3★+, photos, videos …) |
| A | Toggle auto-advance |
| Del | Review & move rejected items to the Recycle Bin |

## What counts as one item

Files with the same base name are grouped and deleted together:
`DSC_1234.NEF + .JPG`, `IMG_1234.HEIC + .MOV` (Live Photo), `IMG_1234.JPG + .AAE`,
`photo.DNG + photo.JPG`, `photo.JPG.xmp`. A video with no matching photo is its own item.

Previews: JPEG/PNG/WebP/GIF directly; raw files without a JPEG use the camera's
embedded preview; videos play in the browser (thumbnails via ffmpeg).
HEIC needs one extra package:

```
%USERPROFILE%\miniconda3\envs\photoframe\python.exe -m pip install pillow-heif
```

## Where ratings, labels and rejects are stored

* **Nikon items** (NEF/NRW, anything with an NX Studio sidecar, out-of-camera
  Nikon JPEGs): ratings and labels are written where NX Studio reads them, per file —
  the XMP packet in its `NKSC_PARAM\*.nksc` sidecar if it has one, otherwise the
  camera's XMP inside the file (edited in place; the file size never changes).
  Label = `photoshop:Urgency` + `xmp:Label`, exactly as NX Studio writes it.
* **Everything else** (phone photos, videos): ratings and labels are kept in
  `.photocull.json` in the folder — the files themselves are never modified.
* **Rejects** are always kept in `.photocull.json`.

Close NX Studio (or refresh its folder view) while culling so it doesn't
overwrite ratings from its cache.

Needs the `photoframe` conda env (Python + Pillow). Previews and thumbnails are
cached in `%LOCALAPPDATA%\PhotoCull`.

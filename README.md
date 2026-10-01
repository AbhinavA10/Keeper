# PhotoCull

Fast keyboard culling for photo folders: Nikon NEF+JPG pairs (NX Studio
compatible), phone photos and videos. Rate, label and reject photos in your
browser, then send the rejects to the Recycle Bin in one go.

PhotoCull never modifies your photo or video files. It only reads them, and
deleting rejects to the Recycle Bin is the only change it ever makes to them.

## Quick start: portable exe (nothing to install)

`PhotoCull.exe` is a single file with Python and everything else built in.
Copy it anywhere (Desktop, USB stick, another PC) and:

* **double-click it** and choose a photo folder, or
* **drag a folder onto it**.

A console window opens, scans the folder, and opens PhotoCull in your default
browser. Keep the console window open while you work; **close it to quit**.

Requirements on the machine: **Windows 10 or 11** and a modern browser
(Chrome, Edge or Firefox). Optionally **[ffmpeg](https://ffmpeg.org/download.html)**
on the `PATH` for video thumbnails; videos still play without it.

A ready-built exe is committed at `dist\PhotoCull.exe`; to make a fresh one,
[build it](#build-the-portable-exe) (a minute or so). The first launch takes a few seconds while Windows
unpacks it; Windows SmartScreen may ask you to confirm the first time because
the exe isn't code-signed (*More info → Run anyway*).

## Install from source (for running or building)

You need **Anaconda or Miniconda**. Clone the repo and create the `photocull`
conda environment from `environment.yml` (Python, Pillow, pillow-heif for
iPhone HEIC photos, and PyInstaller for building the exe):

```bash
git clone git@github.com:AbhinavA10/nx-sorting.git
```

```bash
cd nx-sorting
```

```bash
conda env create -f environment.yml
```

After `environment.yml` changes, update the environment with:

```bash
conda env update -f environment.yml --prune
```

### Run from source

* **Double-click `PhotoCull.bat`** and choose a photo folder, or
* **drag a folder onto `PhotoCull.bat`**.

`PhotoCull.bat` uses the `photocull` conda environment (from Miniconda or
Anaconda in your user folder). To use a different Python, set
`PHOTOCULL_PYTHON` to its `python.exe`.

### Build the portable exe

```bash
conda run -n photocull pyinstaller photocull.spec --noconfirm --clean
```

The result is `dist\PhotoCull.exe` (about 20 MB), built from `photocull.spec`
with the icon in `assets\`. Rebuild it after changing any `.py` or `.html` file.
`dist\PhotoCull.exe` is committed so a ready-built copy is always in the repo;
commit it again after rebuilding. (`build\` is ignored.)

## Command line

The exe and the script take the same arguments:

```bash
PhotoCull.exe "M:\all\Pics-2sort-AB\101D7500"
```

```bash
conda run -n photocull python photocull.py "M:\all\Pics-2sort-AB\101D7500"
```

| Option | Effect |
|---|---|
| *(no folder)* | Opens a folder picker |
| `--port=8765` | Use a fixed port instead of a random free one |
| `--no-browser` | Don't open the browser automatically (the URL is printed) |

## Using it

There are two views. **Single view** shows one large photo; press **T** or the
*Filmstrip* button to show a strip of thumbnails underneath, where the current
photo is lifted and outlined in blue. **Grid view**
(press **G** or the *Grid* button) fills the window with thumbnails; drag the
slider in the header to resize them, and click a photo to open it. Rating,
labelling and rejecting work in both views.

Before culling a folder in PhotoCull, **close NX Studio** (or refresh its folder
view afterwards). Otherwise NX Studio can overwrite the new ratings from its cache.


| Key | Action |
|---|---|
| G | Switch between grid and single-photo view |
| Enter / click | Grid: open the selected photo in single view |
| Esc | Single view: back to the grid |
| T | Single view: show / hide the filmstrip |
| ← → / Space / Backspace | Previous / next (↑ ↓ move by rows in the grid) |
| X | Toggle reject |
| 1–5, 0 | Set / clear stars |
| S (or R) | Toggle social media: shareable, no people (stored as the Red label in NX Studio) |
| Z or click | Zoom to 100% |
| P | Play / pause video |
| Ctrl+Z | Undo |
| F | Cycle filter (unrated, rejected, social media, 3★+, photos, videos …) |
| A | Toggle auto-advance to the next photo after rating or rejecting |
| Home / End | First / last |
| Del | Review and move rejected items to the Recycle Bin |
| ? | Show these shortcuts |

The header shows **Saving… / Saved**. Wait for *Saved* before closing the
browser tab; it warns you if a save is still in progress.

### What counts as one item

Files with the same base name are one item and are deleted together:
`DSC_1234.NEF + .JPG`, `IMG_1234.HEIC + .MOV` (Live Photo), `IMG_1234.JPG + .AAE`,
`photo.DNG + photo.JPG`, `photo.JPG.xmp`. A video with no matching photo is its own item.

Previews: JPEG/PNG/WebP/GIF are shown directly. Raw files without a JPEG use
the camera's embedded preview. Videos play in the browser; some codecs (e.g.
HEVC in Chrome) can't play there, but they can still be culled.

### Where ratings, labels and rejects are stored

* **Nikon items** (NEF/NRW, anything that already has an NX Studio sidecar,
  out-of-camera Nikon JPEGs): ratings and labels go into NX Studio sidecars in
  `NKSC_PARAM\` next to the photos, one per file (`DSC_1234.NEF.nksc`,
  `DSC_1234.JPG.nksc`). Existing sidecars are updated; missing ones (and the
  folder) are created in NX Studio's own format. NX Studio 1.9 shows them as
  normal ratings and colour labels.
* **Everything else** (phone photos, videos): ratings and labels are kept in
  `.photocull.json` in the photo folder.
* **Rejects** are always kept in `.photocull.json`, so they never appear in NX Studio.

## Troubleshooting

| Problem | Fix |
|---|---|
| `Could not find the "photocull" conda environment` | Run `conda env create -f environment.yml`, or use `PhotoCull.exe`. |
| `conda env create` fails with `CERTIFICATE_VERIFY_FAILED` | Antivirus or a proxy is inspecting HTTPS. Run `set CONDA_SSL_VERIFY=truststore` in the same terminal first, so conda uses the Windows certificate store. |
| `conda env create` fails with `HTTP 429` / "requires conda client registration" | Anaconda's `defaults` channel is blocked on your network. `environment.yml` already uses only conda-forge; make sure you're using the repo's copy. |
| Windows or antivirus blocks `PhotoCull.exe` | Unsigned PyInstaller exes are sometimes flagged. Allow it, or run from source with `PhotoCull.bat`. |
| HEIC photos show "No preview" | Running from an old environment: `conda env update -f environment.yml --prune`. |
| Video thumbnails are blank | Install ffmpeg and make sure `ffmpeg` runs from a new terminal. |
| Pages sometimes take a few seconds to load, or show "Failed to fetch" | Antivirus web protection (e.g. AVG/Avast Web Shield) can scan PhotoCull's local traffic and stall or reset it. PhotoCull retries automatically; if it keeps happening, add an exception for `127.0.0.1` in the antivirus settings. |
| A rejected item fails to delete | The file is open in another program (e.g. NX Studio). Close it and press **Del** again. |
| Ratings don't show in NX Studio | Refresh the folder in NX Studio, or close and reopen it. |

Previews and thumbnails are cached in `%LOCALAPPDATA%\PhotoCull`. It's safe
to delete that folder at any time; it is rebuilt as needed.

## Project layout

| File | Purpose |
|---|---|
| `PhotoCull.bat` | Runs the app from source with the `photocull` conda environment |
| `photocull.py` | Local web server, folder scanning, grouping, Recycle Bin deletes |
| `photocull.html` | The browser UI |
| `xmp_rating.py` | Reading and writing NX Studio sidecars (`.nksc`) |
| `media.py` | File types, previews (HEIC, raw, video) and thumbnails |
| `environment.yml` | Conda environment (runtime + build tools) |
| `photocull.spec`, `assets\photocull.ico` | PyInstaller build of `PhotoCull.exe` and its icon |

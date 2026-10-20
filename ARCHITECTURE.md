# snapsort architecture

Python + system PySide6 (pacman, so Qt picks up kimageformats for HEIC/AVIF), stdlib,
`jpegtran` and `ffmpeg` as subprocesses. Do not declare PySide6 in pyproject: a pip wheel
bundles its own Qt without the system image plugins.

## Modules

| module | Qt? | role |
|---|---|---|
| `exif.py` | no | JPEG segment parser, IFD0 orientation, orientation algebra, metadata splice |
| `store.py` | no | listing, natural sort, `.snapsort.json`, `Filter`, rounds, move + undo |
| `imaging.py` | core/gui | `decode_scaled`, `Loader` (thread pools, LRU, thumb cache), rotate/flip, save edits |
| `views.py` | widgets | single view (image/video/GIF), grid model + delegate, help overlay |
| `editor.py` | widgets | edit mode: pen, box, arrow, text, pixelate, blur; patch-based undo |
| `app.py` | widgets | `MainWindow`, keymap (one list drives shortcuts and help), filter bar, dialogs |

`exif` and `store` never import Qt.

## Data

`<folder>/.snapsort.json` — keys are bare filenames, so the file travels with the folder.

```json
{"version": 1, "round": 2, "files": {"IMG_1.jpg": {"tags": ["fav", "keep"], "round": 2}}}
```

Written atomically (temp + fsync + `os.replace`) on every change. A corrupt file is renamed
to `.snapsort.json.bad-<ts>`, never overwritten. Tag slot names (keys 1–9) live in
`QSettings("snapsort")`; files store tag names, so renaming a slot never rewrites data.

Thumbnails: `~/.cache/snapsort/thumbs/<sha1(abspath, mtime_ns, size)>.jpg`, 256 px.

## Rounds

Rounds reuse tags: `K`/`X` set the tags `keep`/`reject`. `N` advances every kept file in the
current view to `round = r+1` (undecided: user picks keep or reject), clears keep/reject in
the view and sets the filter to `round ≥ r+1`. Earlier rounds are visible by lowering the
spinbox. Undo restores a snapshot.

## Rotation and EXIF orientation

Display orientation is modelled as dihedral element (k, f) = "mirror horizontally if f, then
rotate k·90° CW". `compose(old, op)` = op ∘ old, using `M R = R⁻¹ M`. The 8×4 table is
tested against an independent grid oracle and against Qt's own autoTransform.

JPEG rotate order: (1) patch the 2-byte EXIF Orientation in place — lossless, instant;
(2) `jpegtran -perfect -copy all`; (3) re-encode q95 with `splice_metadata`.
Other formats re-encode via Qt (metadata lost; GIF and animations refused).

`splice_metadata` keeps Exif (orientation → 1, IFD1 thumbnail zeroed so a censored region
does not survive in the embedded preview), XMP (orientation → 1), ICC, IPTC; drops MPF.

## Threading

UI thread owns widgets, caches, the tag store and all file moves. A 3-thread decode pool and
a thumbnail pool only emit `(path, QImage)` signals. Measured: decoding six 48 MP JPEGs in
the pool leaves the UI timer gap at one tick (16.7 ms) — shiboken releases the GIL.
Navigation clears queued preloads and requests current (+1..+4, −1..−2) by priority.

## Safety

Edits overwrite the original (user's choice); undo (Ctrl+Z, last 20 actions) keeps original
bytes in memory for the session. Delete goes to the freedesktop trash, never `unlink`.
Moves never overwrite: collisions become `name (1).ext`.

## Not built (add when needed)

1:1 zoom/pan, grid multi-select as move source, folder watching, recursive folders, redo,
thumbnail cache eviction, RAW.

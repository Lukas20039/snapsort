# snapsort

Fast image/video culler for Linux: flip through a folder, tag, narrow down in rounds,
move the keepers into a subfolder. Rotate/flip, censor, pen, text, box, arrow.

## Install (Arch)

```bash
sudo pacman -S --needed pyside6 libheif qt6-multimedia-ffmpeg qt6-imageformats kimageformats libjpeg-turbo ffmpeg python-pipx
pipx install --system-site-packages --python /usr/bin/python3 -e ~/dev/snapsort
install -Dm644 ~/dev/snapsort/snapsort.desktop ~/.local/share/applications/snapsort.desktop
```

Run: `snapsort [folder-or-file]`

## Culling 50 photos down to 10

1. `snapsort ~/Pictures/trip` → grid. `Enter` opens the first image.
2. `K` keep / `X` reject (jumps to the next). Arrow keys just look.
3. `N` finishes the round: only the kept ones remain (`round ≥ 1`).
4. Repeat until ~10 are left. `M` moves exactly what is shown into a subfolder.
5. `Ctrl+Z` undoes a round, move, rotate, edit or trash.

Tags: `F` favourite, `1`–`9` your own tags (`Ctrl+T` renames them).
Filter: `Ctrl+F` / `Ctrl+1…9` cycle off → only → without, or click chips in the filter bar.
`?` / `F1` shows all keys.

Tags live in `<folder>/.snapsort.json` and travel with the folder.

**Edits overwrite the original.** JPEG rotate/flip is lossless (EXIF orientation);
other edits re-encode at quality 95 and keep EXIF/ICC/XMP. Undo works until you quit.

## Tests

```bash
QT_QPA_PLATFORM=offscreen /usr/bin/python3 -m pytest -q
```

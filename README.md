# SpaceScan

Read-only disk usage viewer (SpaceSniffer-style treemap). Nothing is ever deleted or modified.

## Easiest way (Windows)

1. Open https://github.com/alext0525/random-/releases/tag/latest and download `SpaceScan.exe`.
2. Double-click it. No window appears; your browser opens with the app.
   (Windows may warn "protected your PC": click **More info** > **Run anyway**.)
3. Pick a drive, press **Scan**. Click a block or list row to zoom in; use the breadcrumbs to go back.
4. Close the browser tab to quit the app.

## Features

- Results update live while scanning; **Stop** keeps what was found so far.
- Finished scans are remembered while the app is open: pressing Scan on the same folder reopens it
  instantly. **Rescan** redoes it.
- Folders Windows won't let it read are skipped and counted (⚠).
- **Show hidden & system items** toggle (no rescan needed).
- OneDrive "online-only" files live in the cloud and use no disk space. They are excluded from sizes by
  default and shown as "☁ X in cloud". Tick **Include cloud-only files** to add them to the map (hatched).
- For a whole drive, a grey block shows used space that couldn't be read (pagefile, protected system files).

## Run from source

Needs Python 3.8+ (no packages): `python spacescan.py` (`--no-browser` skips opening the browser).

## Notes

- Sizes are file sizes, not disk-cluster usage, so totals can differ slightly from Explorer.
- Symlinks/junctions are not followed.
- Per folder, the 300 biggest files are shown; the rest are grouped as "smaller files".

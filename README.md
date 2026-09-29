# SpaceScan

Read-only disk usage viewer (SpaceSniffer-style treemap). Nothing is ever deleted or modified.

## Run (Windows)

1. Install Python 3.8+ from https://python.org (tick "Add Python to PATH").
2. In this folder, run: `python spacescan.py`
3. Your browser opens. Pick a drive or paste a folder path, press **Scan**.
4. Click a block or a list row to zoom into a folder. Use the breadcrumbs to go back.

No packages to install. Everything stays on your machine (server binds to 127.0.0.1).

## Notes

- Sizes are file sizes, not disk-cluster usage, so totals can differ slightly from Explorer.
- Folders you lack permission for are skipped.
- Symlinks/junctions are not followed.
- Per folder, the 300 biggest files are shown; the rest are grouped as "smaller files".
- `python spacescan.py 9000` uses a different port; `--no-browser` skips opening the browser.

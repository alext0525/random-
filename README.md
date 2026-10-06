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

---

# CrashDoctor

After your laptop crashes, freezes or reboots by itself, run CrashDoctor. It finds the last unexpected
shutdown, pulls the Windows event logs from the minutes before it, and asks Claude why it happened.
You can then ask follow-up questions in the same window.

## Use it (Windows)

1. Download `CrashDoctor.exe` from https://github.com/alext0525/random-/releases/tag/crashdoctor-latest
2. Double-click it after the crash (once you've rebooted). No window appears; your browser opens with the app.
3. It shows the crash it found (or pick "something just happened" / a time) and how far back to look (default 10 min).
4. First time: paste an Anthropic API key (https://console.anthropic.com/settings/keys). "Remember on this PC"
   saves it to `%APPDATA%\CrashDoctor\api_key.txt`. Or set `ANTHROPIC_API_KEY`.
5. Press **Diagnose**. Claude's answer streams in; ask follow-ups underneath. Close the tab to quit.

Run it as administrator to also list crash dump files.

## What gets sent

System + Application errors/warnings, thermal and low-memory logs, all System events from the last
5 minutes before the crash, the shutdown/blue-screen records, crash dump file names, and nearby Windows
Error Reporting reports. Everything is saved to `%USERPROFILE%\CrashDoctor\crash-<time>.md` (with Claude's
diagnosis appended), and you can expand "Logs that were sent" in the app to see exactly what went out.

## From source

`pip install anthropic`, then `python crashdoctor.py` (`--no-browser` skips opening the browser).

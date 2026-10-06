#!/usr/bin/env python3
"""CrashDoctor: grab Windows event logs from before your last crash and ask Claude why it happened.

Run: python crashdoctor.py            (interactive)
     python crashdoctor.py --minutes 60 --now --dry-run
"""
import argparse
import getpass
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta

MODEL = "claude-opus-5-5"
APP_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "CrashDoctor")
KEY_FILE = os.path.join(APP_DIR, "api_key.txt")
REPORT_DIR = os.path.join(os.path.expanduser("~"), "CrashDoctor")
TS_FMT = "%Y-%m-%dT%H:%M:%S"
MAX_MSG = 700          # chars per event message
MAX_LINES = 1500       # grouped error/warning lines sent to Claude
TAIL_MINUTES = 5       # all-level System events for the last few minutes before the end

PS_COMMON = r"""
$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
function Ev($e, $xml) {
  $o = [ordered]@{ t = $e.TimeCreated.ToString('yyyy-MM-dd HH:mm:ss.fff'); log = $e.LogName;
    lvl = $e.LevelDisplayName; src = $e.ProviderName; id = $e.Id; msg = $e.Message }
  if ($xml) { $o.xml = $e.ToXml() }
  [pscustomobject]$o
}
function T($d) { $d.ToString('yyyy-MM-ddTHH:mm:ss') }
"""

# Finds the most recent unexpected shutdown (Kernel-Power 41 / EventLog 6008) and the last
# thing logged before the machine went down.
PS_DETECT = PS_COMMON + r"""
$now = Get-Date
$out = [ordered]@{ now = (T $now); crash = $null }
$marks = @(Get-WinEvent -FilterHashtable @{LogName='System'; Id=41,6008; StartTime=$now.AddDays(-__DAYS__)} -MaxEvents 5)
if ($marks.Count -gt 0) {
  $k = $marks[0]
  $boot = Get-WinEvent -FilterHashtable @{LogName='System'; ProviderName='Microsoft-Windows-Kernel-General'; Id=12; EndTime=$k.TimeCreated} -MaxEvents 1
  $bootT = if ($boot) { $boot.TimeCreated } else { $k.TimeCreated }
  $last = Get-WinEvent -FilterHashtable @{LogName='System'; EndTime=$bootT.AddSeconds(-1)} -MaxEvents 1
  $endT = if ($last) { $last.TimeCreated.AddSeconds(5) } else { $bootT }
  $markers = @(Get-WinEvent -FilterHashtable @{LogName='System'; Id=41,6008,1001; StartTime=$bootT.AddMinutes(-1); EndTime=$bootT.AddMinutes(30)} -MaxEvents 10 | % { Ev $_ $true })
  $out.crash = [ordered]@{ reboot = (T $bootT); end = (T $endT); markers = $markers }
}
$out | ConvertTo-Json -Depth 6 -Compress
"""

PS_COLLECT = PS_COMMON + r"""
$start = [datetime]'__START__'; $end = [datetime]'__END__'
$out = [ordered]@{}
$os = Get-CimInstance Win32_OperatingSystem
$cs = Get-CimInstance Win32_ComputerSystem
$out.system = [ordered]@{
  os = "$($os.Caption) $($os.Version) (build $($os.BuildNumber))"
  model = "$($cs.Manufacturer) $($cs.Model)"
  ram_gb = [math]::Round($cs.TotalPhysicalMemory / 1GB, 1)
  cpu = (Get-CimInstance Win32_Processor | Select-Object -First 1).Name
  gpu = (Get-CimInstance Win32_VideoController | % { "$($_.Name) (driver $($_.DriverVersion))" }) -join '; '
  bios = (Get-CimInstance Win32_BIOS).SMBIOSBIOSVersion
  last_boot = (T $os.LastBootUpTime)
}
$ev = @()
foreach ($log in 'System', 'Application') {
  $ev += @(Get-WinEvent -FilterHashtable @{LogName=$log; Level=1,2,3; StartTime=$start; EndTime=$end} -MaxEvents 4000 | % { Ev $_ $false })
}
foreach ($log in 'Microsoft-Windows-Kernel-Power/Thermal-Operational', 'Microsoft-Windows-Resource-Exhaustion-Detector/Operational') {
  $ev += @(Get-WinEvent -FilterHashtable @{LogName=$log; StartTime=$start; EndTime=$end} -MaxEvents 200 | % { Ev $_ $false })
}
$out.events = $ev
$out.tail = @(Get-WinEvent -FilterHashtable @{LogName='System'; StartTime=$end.AddMinutes(-__TAIL__); EndTime=$end} -MaxEvents 400 |
  ? { $_.Level -eq 0 -or $_.Level -ge 4 } | % { Ev $_ $false })
$out.dumps = @(Get-ChildItem "$env:SystemRoot\Minidump\*.dmp", "$env:SystemRoot\MEMORY.DMP" |
  Sort-Object LastWriteTime -Descending | Select-Object -First 5 |
  % { "$($_.FullName)  $(T $_.LastWriteTime)  $([math]::Round($_.Length / 1MB, 1)) MB" })
$wer = @(Get-ChildItem "$env:ProgramData\Microsoft\Windows\WER\ReportArchive", "$env:ProgramData\Microsoft\Windows\WER\ReportQueue", "$env:LOCALAPPDATA\Microsoft\Windows\WER\ReportArchive" -Recurse -Filter Report.wer |
  ? { $_.LastWriteTime -ge $start.AddMinutes(-5) -and $_.LastWriteTime -le $end.AddHours(1) } |
  Sort-Object LastWriteTime -Descending | Select-Object -First 5)
$out.wer = @($wer | % { "== $($_.Directory.Name) ($(T $_.LastWriteTime))`n" + ((Get-Content $_.FullName -TotalCount 60) -join "`n") })
$out | ConvertTo-Json -Depth 6 -Compress
"""

SYSTEM_PROMPT = """You are an expert Windows reliability engineer. The user's laptop crashed, froze, \
or rebooted unexpectedly, and you are given Windows event logs and crash artifacts from the time \
window before it. Work out the most likely cause.

Reply in this structure, concise and plain-language:
1. **Most likely cause** - one or two sentences, with your confidence (high/medium/low).
2. **Evidence** - the specific log lines (time, source, event ID) that support it, and what they mean.
3. **Other possibilities** - ranked, each with what would confirm or rule it out.
4. **What to do** - concrete, ordered steps the user can take on their own (driver/BIOS updates, \
settings, hardware tests, commands to run).

Notes for reading the logs: Kernel-Power 41 with BugcheckCode 0 usually means power loss, a hard \
hang, or a forced power-off rather than a blue screen; a non-zero BugcheckCode is a BSOD stop code \
(give its name). Many Windows warnings are routine noise (DCOM 10016, DistributedCOM, some \
Service Control Manager timeouts) - say so rather than blaming them. If the logs don't show a \
clear cause, say that honestly and explain what to capture next time (e.g. enable minidumps, \
run WinDbg !analyze -v on a dump)."""


def run_ps(script):
    """Run a PowerShell script and parse its JSON output."""
    if os.name != "nt":
        sys.exit("CrashDoctor reads Windows event logs, so it has to run on Windows.")
    fd, path = tempfile.mkstemp(suffix=".ps1")
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig") as f:
            f.write(script)
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", path],
            capture_output=True, timeout=600)
    finally:
        os.remove(path)
    text = r.stdout.decode("utf-8", "replace").strip()
    if not text:
        sys.exit("PowerShell returned nothing:\n" + r.stderr.decode("utf-8", "replace"))
    return json.loads(text)


def as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def clean(msg):
    return re.sub(r"\s+", " ", msg or "").strip()[:MAX_MSG]


def detect_crash(days):
    return run_ps(PS_DETECT.replace("__DAYS__", str(int(days))))


def collect(start, end):
    data = run_ps(PS_COLLECT.replace("__START__", start.strftime(TS_FMT))
                  .replace("__END__", end.strftime(TS_FMT))
                  .replace("__TAIL__", str(TAIL_MINUTES)))
    for k in ("events", "tail", "dumps", "wer"):
        data[k] = as_list(data.get(k))
    return data


def group_events(events):
    """Collapse repeats of the same (log, source, id, message) into one line with a count."""
    groups = {}
    for e in events:
        k = (e.get("log"), e.get("src"), e.get("id"), clean(e.get("msg")))
        g = groups.get(k)
        if g:
            g["n"] += 1
            g["first"] = min(g["first"], e["t"])
            g["last"] = max(g["last"], e["t"])
        else:
            groups[k] = {"e": e, "msg": k[3], "n": 1, "first": e["t"], "last": e["t"]}
    out = sorted(groups.values(), key=lambda g: g["last"])
    if len(out) > MAX_LINES:  # keep the most recent groups (closest to the crash)
        out = out[-MAX_LINES:]
    return sorted(out, key=lambda g: g["first"])


def fmt_event(e, msg=None, extra=""):
    return f"[{e['t']}] {e.get('log')} {e.get('lvl') or ''} {e.get('src')}/{e.get('id')}{extra}: {msg if msg is not None else clean(e.get('msg'))}"


def build_report(info, crash, start, end):
    lines = [f"# Logs from {start:%Y-%m-%d %H:%M:%S} to {end:%Y-%m-%d %H:%M:%S} (local time)", ""]
    lines.append("## Machine")
    lines += [f"- {k}: {v}" for k, v in (info.get("system") or {}).items()]
    lines.append("")
    if crash:
        lines.append(f"## Unexpected shutdown: last log entry ~{crash['end']}, rebooted at {crash['reboot']}")
        for m in as_list(crash.get("markers")):
            lines.append(fmt_event(m))
            lines.append(re.sub(r"\s+", " ", m.get("xml") or "")[:3000])
        lines.append("")
    else:
        lines += ["## No reboot after this window (live freeze / app crash / user-chosen window)", ""]

    groups = group_events(info["events"])
    lines.append(f"## Errors and warnings ({len(info['events'])} events, {len(groups)} unique)")
    for g in groups:
        extra = f" (x{g['n']}, first {g['first']}, last {g['last']})" if g["n"] > 1 else ""
        lines.append(fmt_event(g["e"], g["msg"], extra))
    lines.append("")
    lines.append(f"## All System events in the last {TAIL_MINUTES} minutes (info level)")
    lines += [fmt_event(e) for e in sorted(info["tail"], key=lambda e: e["t"])]
    lines.append("")
    lines.append("## Crash dumps on disk")
    lines += info["dumps"] or ["(none found - may need admin rights, or dumps are disabled)"]
    lines.append("")
    lines.append("## Windows Error Reporting reports near this time")
    lines += info["wer"] or ["(none)"]
    return "\n".join(lines)


def get_api_key():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    try:
        with open(KEY_FILE, encoding="utf-8") as f:
            key = f.read().strip()
        if key:
            return key
    except OSError:
        pass
    print("\nNeeds an Anthropic API key (get one at https://console.anthropic.com/settings/keys).")
    key = getpass.getpass("Paste API key (hidden): ").strip()
    if not key:
        sys.exit("No key given.")
    if ask("Save it for next time?", True):
        os.makedirs(APP_DIR, exist_ok=True)
        with open(KEY_FILE, "w", encoding="utf-8") as f:
            f.write(key)
        print(f"Saved to {KEY_FILE}")
    return key


def ask(q, default=True):
    a = input(f"{q} [{'Y/n' if default else 'y/N'}] ").strip().lower()
    return default if not a else a.startswith("y")


def ask_claude(client, messages, model):
    """Stream one reply to the console and return the final message."""
    import anthropic
    try:
        with client.beta.messages.stream(
            model=model,
            max_tokens=32000,
            system=SYSTEM_PROMPT,
            messages=messages,
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",  # retry on another model if the request is declined
        ) as stream:
            for text in stream.text_stream:
                print(text, end="", flush=True)
            msg = stream.get_final_message()
    except anthropic.AuthenticationError:
        sys.exit(f"\nThe API key was rejected. Fix it in {KEY_FILE} or ANTHROPIC_API_KEY.")
    except anthropic.RateLimitError:
        sys.exit("\nRate limited by the API. Try again in a minute.")
    except anthropic.APIStatusError as e:
        sys.exit(f"\nAPI error {e.status_code}: {e.message}")
    except anthropic.APIConnectionError:
        sys.exit("\nCouldn't reach the Anthropic API. Check your internet connection.")
    print()
    if msg.stop_reason == "refusal":
        print("(Claude declined to answer this one.)")
    return msg


def main():
    p = argparse.ArgumentParser(description="Send the logs from before your last crash to Claude.")
    p.add_argument("--minutes", type=int, help="how much log history to send (default 30)")
    p.add_argument("--now", action="store_true", help="use the window ending now (skip crash detection)")
    p.add_argument("--end", help='custom window end, e.g. "2026-10-05 22:15"')
    p.add_argument("--days", type=int, default=7, help="how far back to look for a crash (default 7)")
    p.add_argument("--dry-run", action="store_true", help="collect and save logs only, don't send")
    p.add_argument("--model", default=MODEL)
    p.add_argument("--yes", action="store_true", help="don't ask questions")
    a = p.parse_args()
    interactive = not a.yes and sys.stdin.isatty()

    print("CrashDoctor - finding out why your PC crashed\n")
    crash = None
    if a.end:
        end = datetime.strptime(a.end.replace("T", " "), "%Y-%m-%d %H:%M")
    elif a.now:
        end = datetime.now()
    else:
        print(f"Looking for an unexpected shutdown in the last {a.days} days...")
        d = detect_crash(a.days)
        end = datetime.strptime(d["now"], TS_FMT)
        crash = d.get("crash")
        if crash:
            print(f"Found one: last log entry around {crash['end'].replace('T', ' ')}, "
                  f"rebooted at {crash['reboot'].replace('T', ' ')}.")
            if not interactive or ask("Analyze that crash?", True):
                end = datetime.strptime(crash["end"], TS_FMT)
            else:
                crash = None
                print("OK, using the logs up to now instead.")
        else:
            print("No unexpected shutdown found, so using the logs up to now "
                  "(good for freezes or app crashes without a reboot).")

    minutes = a.minutes
    if minutes is None:
        minutes = 30
        if interactive:
            v = input("How many minutes of logs before that? [30] ").strip()
            minutes = int(v) if v.isdigit() and int(v) > 0 else 30
    start = end - timedelta(minutes=minutes)

    print(f"Collecting logs from {start:%H:%M:%S} to {end:%H:%M:%S}...")
    report = build_report(collect(start, end), crash, start, end)
    os.makedirs(REPORT_DIR, exist_ok=True)
    path = os.path.join(REPORT_DIR, f"crash-{end:%Y%m%d-%H%M%S}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"Saved the collected logs to {path} ({len(report) // 1000} KB).")
    if a.dry_run:
        return
    if interactive and not ask("Send them to Claude for analysis?", True):
        return

    import anthropic
    client = anthropic.Anthropic(api_key=get_api_key())
    messages = [{"role": "user", "content": "My Windows laptop crashed. Why? Here are the logs:\n\n" + report}]
    print(f"\nAsking Claude ({a.model})... this can take a minute.\n")
    with open(path, "a", encoding="utf-8") as log:
        log.write("\n\n---\n# Claude's diagnosis\n\n")
        while True:
            msg = ask_claude(client, messages, a.model)
            reply = "".join(b.text for b in msg.content if b.type == "text")
            log.write(reply + "\n")
            log.flush()
            messages.append({"role": "assistant", "content": msg.content})
            if not interactive:
                break
            q = input("\nFollow-up question (Enter to quit): ").strip()
            if not q:
                break
            log.write(f"\n## Q: {q}\n\n")
            messages.append({"role": "user", "content": q})
            print()
    print(f"\nDiagnosis saved to {path}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
    except SystemExit as e:
        if e.code not in (None, 0):
            print(e.code)
    if getattr(sys, "frozen", False):  # double-clicked exe: keep the window open
        input("\nPress Enter to close.")

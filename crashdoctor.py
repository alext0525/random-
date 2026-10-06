#!/usr/bin/env python3
"""CrashDoctor: grab Windows event logs from before your last crash and ask Claude why it happened.

Run: python crashdoctor.py   (opens in your browser; close the tab to quit)
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

MODEL = "claude-opus-5-5"
PORT = 8775
BYE_GRACE = 6      # tab said goodbye and nothing came back (a refresh pings again at once)
IDLE_LIMIT = 180   # no ping at all
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
        raise RuntimeError("CrashDoctor reads Windows event logs, so it has to run on Windows.")
    fd, path = tempfile.mkstemp(suffix=".ps1")
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig") as f:
            f.write(script)
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", path],
            capture_output=True, timeout=600, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    finally:
        os.remove(path)
    text = r.stdout.decode("utf-8", "replace").strip()
    if not text:
        raise RuntimeError("PowerShell returned nothing: " + r.stderr.decode("utf-8", "replace")[:500])
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


def saved_key():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    try:
        with open(KEY_FILE, encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def save_key(key):
    os.makedirs(APP_DIR, exist_ok=True)
    with open(KEY_FILE, "w", encoding="utf-8") as f:
        f.write(key)


class Session:
    """One diagnosis: collected logs plus the conversation with Claude."""

    def __init__(self):
        self.state = "collecting"   # collecting | asking | done | error
        self.error = None
        self.turns = []             # [{"role": "user"|"assistant", "text": str}]
        self.partial = ""
        self.report = ""
        self.path = None
        self.messages = []          # API messages, assistant turns kept as full content blocks
        self.lock = threading.Lock()

    def view(self):
        with self.lock:
            return {"state": self.state, "error": self.error, "turns": self.turns, "partial": self.partial,
                    "report": self.report, "path": self.path}


state = {"session": None, "key": None}
lifecycle = {"ping": None, "bye": None, "start": time.time()}


def run_session(s, end, minutes, crash):
    try:
        start = end - timedelta(minutes=minutes)
        report = build_report(collect(start, end), crash, start, end)
        os.makedirs(REPORT_DIR, exist_ok=True)
        path = os.path.join(REPORT_DIR, f"crash-{end:%Y%m%d-%H%M%S}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(report + "\n\n---\n# Claude's diagnosis\n\n")
        with s.lock:
            s.report, s.path = report, path
            s.messages = [{"role": "user", "content": "My Windows laptop crashed. Why? Here are the logs:\n\n" + report}]
        ask_claude(s)
    except Exception as e:  # shown in the page
        with s.lock:
            s.state, s.error = "error", str(e)


def ask_claude(s):
    """Stream one reply into the session."""
    import anthropic
    with s.lock:
        s.state, s.partial = "asking", ""
    try:
        client = anthropic.Anthropic(api_key=state["key"])
        with client.beta.messages.stream(
            model=MODEL,
            max_tokens=32000,
            system=SYSTEM_PROMPT,
            messages=s.messages,
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",  # retry on another model if the request is declined
        ) as stream:
            for text in stream.text_stream:
                with s.lock:
                    s.partial += text
            msg = stream.get_final_message()
    except anthropic.AuthenticationError:
        return fail(s, "The API key was rejected. Enter a valid key and try again.", key=True)
    except anthropic.RateLimitError:
        return fail(s, "Rate limited by the API. Try again in a minute.")
    except anthropic.APIStatusError as e:
        return fail(s, f"API error {e.status_code}: {e.message}")
    except anthropic.APIConnectionError:
        return fail(s, "Couldn't reach the Anthropic API. Check your internet connection.")
    reply = "".join(b.text for b in msg.content if b.type == "text")
    if msg.stop_reason == "refusal":
        reply += "\n\n_(Claude declined to answer this one.)_"
    with open(s.path, "a", encoding="utf-8") as f:
        f.write(reply + "\n")
    with s.lock:
        s.messages.append({"role": "assistant", "content": msg.content})
        s.turns.append({"role": "assistant", "text": reply})
        s.partial, s.state = "", "done"


def fail(s, message, key=False):
    if key:
        state["key"] = None
    with s.lock:
        s.state, s.error, s.partial = "error", message, ""
        if s.messages and s.messages[-1]["role"] == "user" and len(s.messages) > 1:
            s.messages.pop()  # let the follow-up be asked again
            s.turns.pop()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
            with open(os.path.join(base, "crashdoctor.html"), "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/detect":
            try:
                d = detect_crash(7)
            except Exception as e:
                return self.send_json({"error": str(e), "has_key": bool(state["key"])})
            d["has_key"] = bool(state["key"])
            self.send_json(d)
        elif path == "/api/session":
            s = state["session"]
            self.send_json(s.view() if s else {"state": None})
        elif path == "/api/open-folder":
            os.makedirs(REPORT_DIR, exist_ok=True)
            if hasattr(os, "startfile"):
                os.startfile(REPORT_DIR)
            self.send_json({"ok": True})
        elif path == "/api/ping":
            lifecycle["ping"] = time.time()
            lifecycle["bye"] = None
            self.send_json({"ok": True})
        elif path == "/api/bye":
            lifecycle["bye"] = time.time()
            self.send_json({"ok": True})
        else:
            self.send_json({"error": "Not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/bye":
            return self.do_GET()
        b = self.body()
        s = state["session"]
        if path == "/api/key":
            key = (b.get("key") or "").strip()
            if not key:
                return self.send_json({"error": "Paste your API key first."}, 400)
            state["key"] = key
            if b.get("remember"):
                save_key(key)
            self.send_json({"ok": True})
        elif path == "/api/analyze":
            if not state["key"]:
                return self.send_json({"error": "Enter your API key first."}, 400)
            if s and s.state in ("collecting", "asking"):
                return self.send_json({"error": "Already working on one."}, 409)
            try:
                end = datetime.strptime(b["end"][:16], "%Y-%m-%dT%H:%M") if b.get("end") else datetime.now()
                minutes = max(1, min(int(b.get("minutes") or 10), 24 * 60))
            except (ValueError, TypeError):
                return self.send_json({"error": "Bad time or minutes."}, 400)
            if b.get("crash"):  # the detected crash: keep its exact second
                end = datetime.strptime(b["crash"]["end"], TS_FMT)
            s = state["session"] = Session()
            threading.Thread(target=run_session, args=(s, end, minutes, b.get("crash")), daemon=True).start()
            self.send_json({"ok": True})
        elif path == "/api/ask":
            q = (b.get("q") or "").strip()
            ready = s and s.state in ("done", "error") and s.messages and s.messages[-1]["role"] == "assistant"
            if not ready or not q:
                return self.send_json({"error": "Nothing to ask about yet."}, 400)
            if not state["key"]:
                return self.send_json({"error": "Enter your API key first (New diagnosis)."}, 400)
            with s.lock:
                s.messages.append({"role": "user", "content": q})
                s.turns.append({"role": "user", "text": q})
                s.error = None
            with open(s.path, "a", encoding="utf-8") as f:
                f.write(f"\n## Q: {q}\n\n")
            threading.Thread(target=ask_claude, args=(s,), daemon=True).start()
            self.send_json({"ok": True})
        else:
            self.send_json({"error": "Not found"}, 404)


def watchdog():
    while True:
        time.sleep(1)
        now = time.time()
        last = lifecycle["ping"] or lifecycle["start"]
        bye = lifecycle["bye"]
        busy = state["session"] and state["session"].state in ("collecting", "asking")
        if not busy and ((bye and now - bye > BYE_GRACE) or now - last > IDLE_LIMIT):
            os._exit(0)


def main():
    state["key"] = saved_key()
    port = PORT
    while True:
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            port += 1
    url = f"http://127.0.0.1:{port}/"
    print(f"CrashDoctor is running at {url} - close the browser tab to quit.")
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    if "--no-exit" not in sys.argv:
        threading.Thread(target=watchdog, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""SpaceScan: read-only disk usage treemap. Run: python spacescan.py"""
import json
import os
import shutil
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PORT = 8765
MAX_FILES_PER_DIR = 300  # keep the biggest files per folder, lump the rest
MAX_CACHED_SCANS = 6

# Windows file attributes
ATTR_HIDDEN = 0x2
ATTR_SYSTEM = 0x4
ATTR_REPARSE = 0x400
ATTR_OFFLINE = 0x1000
ATTR_RECALL_ON_OPEN = 0x40000
ATTR_RECALL_ON_DATA_ACCESS = 0x400000  # OneDrive "online-only" placeholder
CLOUD_MASK = ATTR_OFFLINE | ATTR_RECALL_ON_OPEN | ATTR_RECALL_ON_DATA_ACCESS


def attrs_of(st):
    return getattr(st, "st_file_attributes", 0)


def is_junction(path):
    f = getattr(os.path, "isjunction", None)
    return bool(f and f(path))


class Node:
    """l/c = local/cloud-only bytes; hl/hc = the part of those inside hidden items."""
    __slots__ = ("name", "path", "dir", "l", "c", "hl", "hc", "cnt", "dn",
                 "hidden", "children", "parent", "done")

    def __init__(self, name, path, is_dir, parent=None, hidden=False):
        self.name, self.path, self.dir, self.parent, self.hidden = name, path, is_dir, parent, hidden
        self.l = self.c = self.hl = self.hc = self.cnt = self.dn = 0
        self.children = []
        self.done = not is_dir


class Job:
    def __init__(self, path):
        self.path = path
        self.root = Node(path, path, True)
        self.index = {key(path): self.root}
        self.cancel = threading.Event()
        self.running = True
        self.stopped = False
        self.files = 0
        self.dirs = 0
        self.denied = 0
        self.started = time.time()
        self.finished = None
        self.last_used = time.time()


def key(p):
    return os.path.normcase(os.path.abspath(p))


# The exe has no console window, so the app quits itself once the browser tab is closed.
lifecycle = {"ping": None, "bye": None, "start": time.time()}
BYE_GRACE = 6      # tab said goodbye and nothing came back (a page refresh pings again at once)
IDLE_LIMIT = 180   # no ping at all (background tabs get throttled, so this is generous)


def watchdog():
    while True:
        time.sleep(1)
        now = time.time()
        last = lifecycle["ping"] or lifecycle["start"]
        bye = lifecycle["bye"]
        if (bye and now - bye > BYE_GRACE) or now - last > IDLE_LIMIT:
            os._exit(0)


jobs = {}
jobs_lock = threading.Lock()


def propagate(node, l, c, hl, hc, cnt):
    n = node
    while n:
        n.l += l
        n.c += c
        n.hl += hl
        n.hc += hc
        n.cnt += cnt
        if n.hidden:
            hl, hc = l, c
        n = n.parent


def scan_dir(job, node):
    if job.cancel.is_set():
        return
    try:
        with os.scandir(node.path) as it:
            entries = list(it)
    except OSError:
        n = node
        while n:
            n.dn += 1
            n = n.parent
        job.denied += 1
        node.done = True
        return
    job.dirs += 1
    L = C = HL = HC = 0
    files, subdirs = [], []
    for e in entries:
        try:
            if e.is_symlink():
                continue
            is_dir = e.is_dir(follow_symlinks=False)
            st = e.stat(follow_symlinks=False)
        except OSError:
            continue
        a = attrs_of(st)
        hidden = bool(a & (ATTR_HIDDEN | ATTR_SYSTEM)) if os.name == "nt" else e.name.startswith(".")
        if is_dir:
            if a & ATTR_REPARSE and is_junction(e.path):
                continue  # e.g. "Documents and Settings" -> would double count
            child = Node(e.name, e.path, True, node, hidden)
            job.index[key(e.path)] = child
            node.children.append(child)
            subdirs.append(child)
        elif e.is_file(follow_symlinks=False):
            cloud = bool(a & CLOUD_MASK)
            f = Node(e.name, e.path, False, node, hidden)
            f.cnt = 1
            if cloud:
                f.c = st.st_size
            else:
                f.l = st.st_size
            if hidden:
                f.hl, f.hc = f.l, f.c
            files.append(f)
            L += f.l
            C += f.c
            HL += f.hl
            HC += f.hc
    job.files += len(files)
    propagate(node, L, C, HL, HC, len(files))
    files.sort(key=lambda f: -(f.l + f.c))
    node.children.extend(files[:MAX_FILES_PER_DIR])
    rest = files[MAX_FILES_PER_DIR:]
    if rest:
        agg = Node(f"({len(rest)} smaller files)", None, False, node)
        agg.cnt = len(rest)
        agg.l, agg.c = sum(f.l for f in rest), sum(f.c for f in rest)
        agg.hl, agg.hc = sum(f.hl for f in rest), sum(f.hc for f in rest)
        node.children.append(agg)
    for child in subdirs:
        scan_dir(job, child)
    node.done = True


def run_job(job):
    try:
        scan_dir(job, job.root)
    finally:
        job.running = False
        job.finished = time.time()
        job.stopped = job.cancel.is_set()


def start_job(path, force):
    k = key(path)
    with jobs_lock:
        old = jobs.get(k)
        if old and not force and (old.running or not old.stopped):
            old.last_used = time.time()
            return old, True
        if old:
            old.cancel.set()
        job = Job(os.path.abspath(path))
        jobs[k] = job
        while len(jobs) > MAX_CACHED_SCANS:
            victim = min((j for j in jobs.values() if not j.running and j is not job),
                         key=lambda j: j.last_used, default=None)
            if not victim:
                break
            jobs.pop(key(victim.path))
    threading.Thread(target=run_job, args=(job,), daemon=True).start()
    return job, False


def eff(n, show_hidden):
    """(local, cloud) bytes of node under the hidden-items setting."""
    if n.hidden and not show_hidden:
        return 0, 0
    if show_hidden:
        return n.l, n.c
    return n.l - n.hl, n.c - n.hc


def child_view(n, show_hidden, show_cloud):
    l, c = eff(n, show_hidden)
    return {"name": n.name, "path": n.path, "dir": n.dir, "size": l + (c if show_cloud else 0),
            "local": l, "cloud": c, "denied": n.dn, "hidden": n.hidden,
            "busy": n.dir and not n.done}


def disk_info(job):
    if os.path.dirname(job.path) != job.path and not (os.name == "nt" and len(job.path) <= 3):
        return None
    try:
        u = shutil.disk_usage(job.path)
        return {"total": u.total, "used": u.total - u.free}
    except OSError:
        return None


def view(job, node, show_hidden, show_cloud):
    v = child_view(node, show_hidden, show_cloud)
    kids = [child_view(c, show_hidden, show_cloud) for c in list(node.children)]
    kids = [k for k in kids if k["size"] > 0 or k["cloud"] > 0 or k["denied"]]
    if node is job.root and not job.running and not job.stopped:
        d = disk_info(job)
        if d:
            gap = d["used"] - job.root.l
            if gap > d["used"] * 0.005:
                kids.append({"name": "Not readable / system (pagefile, protected files, etc.)",
                             "path": None, "dir": False, "size": gap, "local": gap, "cloud": 0,
                             "denied": 0, "hidden": False, "busy": False, "special": True})
    kids.sort(key=lambda k: (-k["size"], -k["cloud"]))
    v["children"] = kids
    v["count"] = node.cnt
    return v


def drives():
    if os.name == "nt":
        return [f"{d}:\\" for d in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if os.path.exists(f"{d}:\\")]
    return ["/", os.path.expanduser("~")]


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

    def get_job(self, q):
        with jobs_lock:
            job = jobs.get(key(q.get("scan", [""])[0] or "."))
        if job:
            job.last_used = time.time()
        return job

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        if url.path == "/":
            base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
            with open(os.path.join(base, "index.html"), "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/api/drives":
            self.send_json({"drives": drives(), "home": os.path.expanduser("~")})
        elif url.path == "/api/scan":
            path = os.path.abspath(q.get("path", [""])[0])
            if not os.path.isdir(path):
                return self.send_json({"error": "Not a folder"}, 400)
            job, reused = start_job(path, q.get("force", ["0"])[0] == "1")
            self.send_json({"ok": True, "scan": job.path, "reused": reused})
        elif url.path == "/api/status":
            job = self.get_job(q)
            if not job:
                return self.send_json({"error": "No scan"}, 404)
            self.send_json({"running": job.running, "stopped": job.stopped, "files": job.files,
                            "dirs": job.dirs, "denied": job.denied, "finished": job.finished,
                            "now": time.time(), "disk": disk_info(job)})
        elif url.path == "/api/ping":
            lifecycle["ping"] = time.time()
            lifecycle["bye"] = None
            self.send_json({"ok": True})
        elif url.path == "/api/bye":
            lifecycle["bye"] = time.time()
            self.send_json({"ok": True})
        elif url.path == "/api/stop":
            job = self.get_job(q)
            if job:
                job.cancel.set()
            self.send_json({"ok": True})
        elif url.path == "/api/node":
            job = self.get_job(q)
            if not job:
                return self.send_json({"error": "No scan"}, 404)
            node = job.index.get(key(q.get("path", [job.path])[0]))
            if not node:
                return self.send_json({"error": "Not found"}, 404)
            self.send_json(view(job, node, q.get("hidden", ["0"])[0] == "1",
                                q.get("cloud", ["0"])[0] == "1"))
        else:
            self.send_json({"error": "Not found"}, 404)


Handler.do_POST = Handler.do_GET  # sendBeacon from the page uses POST


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else PORT
    while True:
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            port += 1
    url = f"http://127.0.0.1:{port}/"
    print(f"SpaceScan is running at {url}")
    print("Your browser should open. Close the browser tab to quit SpaceScan.")
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

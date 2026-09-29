#!/usr/bin/env python3
"""SpaceScan: read-only disk usage treemap. Run: python spacescan.py"""
import json
import os
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PORT = 8765
MAX_FILES_PER_DIR = 300  # keep the biggest files per folder, lump the rest

state = {"running": False, "path": None, "root": None, "files": 0, "error": None}
lock = threading.Lock()


def scan_dir(path, cancel):
    """Return node dict: {name, path, size, dir, count, children}."""
    node = {"name": os.path.basename(path) or path, "path": path, "size": 0,
            "dir": True, "count": 0, "children": []}
    files = []
    try:
        with os.scandir(path) as it:
            entries = list(it)
    except (PermissionError, OSError):
        return node
    for e in entries:
        if cancel.is_set():
            break
        try:
            if e.is_symlink():
                continue
            if e.is_dir(follow_symlinks=False):
                child = scan_dir(e.path, cancel)
                node["children"].append(child)
                node["size"] += child["size"]
                node["count"] += child["count"]
            elif e.is_file(follow_symlinks=False):
                size = e.stat(follow_symlinks=False).st_size
                files.append({"name": e.name, "path": e.path, "size": size,
                              "dir": False, "count": 1})
                node["size"] += size
                node["count"] += 1
                with lock:
                    state["files"] += 1
        except OSError:
            continue
    files.sort(key=lambda f: -f["size"])
    keep, rest = files[:MAX_FILES_PER_DIR], files[MAX_FILES_PER_DIR:]
    node["children"].extend(keep)
    if rest:
        node["children"].append({"name": f"({len(rest)} smaller files)", "path": None,
                                 "size": sum(f["size"] for f in rest), "dir": False,
                                 "count": len(rest)})
    return node


cancel_event = threading.Event()


def run_scan(path):
    root = scan_dir(path, cancel_event)
    with lock:
        state["root"] = root
        state["running"] = False


def find_node(node, path):
    if node["path"] == path:
        return node
    if node["dir"] and path.startswith(node["path"]):
        for c in node["children"]:
            if c["dir"]:
                found = find_node(c, path)
                if found:
                    return found
    return None


def view(node):
    """Node with children flattened one level (grandchildren not sent)."""
    return {**{k: v for k, v in node.items() if k != "children"},
            "children": [{k: v for k, v in c.items() if k != "children"}
                         for c in sorted(node["children"], key=lambda c: -c["size"])]}


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

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        if url.path == "/":
            base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
            page = os.path.join(base, "index.html")
            with open(page, "rb") as f:
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
            with lock:
                if state["running"]:
                    cancel_event.set()
            cancel_event.clear()
            with lock:
                state.update(running=True, path=path, root=None, files=0, error=None)
            threading.Thread(target=run_scan, args=(path,), daemon=True).start()
            self.send_json({"ok": True})
        elif url.path == "/api/status":
            with lock:
                self.send_json({"running": state["running"], "files": state["files"],
                                "path": state["path"]})
        elif url.path == "/api/node":
            with lock:
                root = state["root"]
            if not root:
                return self.send_json({"error": "No scan yet"}, 400)
            path = q.get("path", [root["path"]])[0]
            node = find_node(root, path)
            if not node:
                return self.send_json({"error": "Not found"}, 404)
            self.send_json(view(node))
        else:
            self.send_json({"error": "Not found"}, 404)


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
    print("Your browser should open. Close this window to quit SpaceScan.")
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

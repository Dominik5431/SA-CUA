#!/usr/bin/env python3
"""Minimal log server bundled into the 10 custom-built website containers.

Writes POST /logs/write bodies directly to security.log and exposes a
lightweight in-memory GET /logs/elements endpoint returning the latest
element map posted by reveal.js (action: 'element_map') for coordinate-
based element targeting.

The WebArena GitLab proxy uses an OpenResty/Lua endpoint instead — see
environments/webarena/gitlab-setup/nginx.conf.
"""
import http.server
import socketserver
import json
import os

LOG_FILE = '/usr/share/nginx/html/logs/security.log'
LAST_ELEMENT_MAP = {"elements": []}

def load_last_element_map_from_file() -> dict:
    """Read the last element_map entry from LOG_FILE (best-effort)."""
    try:
        if not os.path.exists(LOG_FILE):
            return {"elements": []}
        with open(LOG_FILE, 'rb') as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            read_size = min(size, 256 * 1024)
            f.seek(-read_size, os.SEEK_END)
            chunk = f.read(read_size).decode('utf-8', errors='ignore')
        lines = [ln for ln in chunk.splitlines() if ln.strip()]
        for ln in reversed(lines):
            try:
                obj = json.loads(ln)
                if isinstance(obj, dict) and obj.get('action') == 'element_map' and isinstance(obj.get('elements'), list):
                    return {"timestamp": obj.get("timestamp"), "elements": obj.get("elements", [])}
            except Exception:
                continue
    except Exception:
        pass
    return {"elements": []}

class LogHandler(http.server.SimpleHTTPRequestHandler):
    def _send_json(self, status: int, payload: dict):
        self.send_response(status)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode('utf-8'))

    def do_GET(self):
        if self.path == '/logs/elements' or self.path == '/logs/elements/':
            payload = LAST_ELEMENT_MAP
            if not payload.get("elements"):
                payload = load_last_element_map_from_file()
            self._send_json(200, payload)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == '/logs/write' or self.path == '/logs/write/':
            content_length = int(self.headers.get('Content-Length', 0))
            data = self.rfile.read(content_length)
            
            os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
            
            try:
                if content_length > 0:
                    decoded_data = data.decode('utf-8').strip()
                    if decoded_data:
                        try:
                            obj = json.loads(decoded_data)
                            if isinstance(obj, dict) and obj.get('action') == 'element_map' and isinstance(obj.get('elements'), list):
                                global LAST_ELEMENT_MAP
                                LAST_ELEMENT_MAP = {"timestamp": obj.get("timestamp"), "elements": obj.get("elements", [])}
                        except Exception:
                            pass
                        with open(LOG_FILE, 'a') as f:
                            f.write(decoded_data + '\n')
                        print(f"LOG WRITTEN: {len(data)} bytes", file=__import__('sys').stderr)
            except Exception as e:
                print(f"ERROR writing log: {e}", file=__import__('sys').stderr)
            
            self._send_json(200, {"status": "ok"})
        else:
            print(f"404: {self.path}", file=__import__('sys').stderr)
            self.send_response(404)
            self.end_headers()
    
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, GET, OPTIONS')
        self.end_headers()
    
    def log_message(self, *args):
        pass

if __name__ == '__main__':
    PORT = 8889
    with socketserver.TCPServer(("", PORT), LogHandler) as httpd:
        httpd.serve_forever()


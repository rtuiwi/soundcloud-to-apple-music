#!/usr/bin/env python3
"""Run the local SoundCloud → Music selection interface."""
from __future__ import annotations

import argparse
import hmac
import json
import secrets
import signal
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from core import BASE, MusicLibrary


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port=0, library=None):
        self.library = library or MusicLibrary()
        self.token = secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", port), Handler)
        self.origin = "http://127.0.0.1:" + str(self.server_port)


class Handler(BaseHTTPRequestHandler):
    server_version = "SoundCloudMusic/1"

    def log_message(self, format, *args):
        pass

    def _headers(self, status, content_type, size):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' https: data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()

    def _json(self, payload, status=200):
        content = json.dumps(payload, ensure_ascii=False).encode()
        self._headers(status, "application/json; charset=utf-8", len(content))
        self.wfile.write(content)

    def _local_request(self):
        if self.headers.get("Host") != "127.0.0.1:" + str(self.server.server_port):
            self._json({"error": "Неверный адрес локального сервера."}, 403)
            return False
        origin = self.headers.get("Origin")
        if origin and origin != self.server.origin:
            self._json({"error": "Запросы разрешены только из локального интерфейса."}, 403)
            return False
        if self.headers.get("Sec-Fetch-Site") in ("cross-site",):
            self._json({"error": "Внешний запрос отклонён."}, 403)
            return False
        return True

    def _authorized(self):
        if not self._local_request():
            return False
        expected = "Bearer " + self.server.token
        actual = self.headers.get("Authorization", "")
        if not hmac.compare_digest(actual, expected):
            self._json({"error": "Откройте интерфейс из запущенного инструмента."}, 401)
            return False
        return True

    def do_GET(self):
        path = urlsplit(self.path).path
        if path.startswith("/api/"):
            if not self._authorized():
                return
            if path == "/api/state":
                self._json(self.server.library.state())
            else:
                self._json({"error": "Адрес не найден."}, 404)
            return
        if not self._local_request():
            return
        if path == "/favicon.ico":
            self._headers(204, "image/x-icon", 0)
            return
        files = {"/": ("index.html", "text/html; charset=utf-8"),
                 "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                 "/styles.css": ("styles.css", "text/css; charset=utf-8")}
        if path not in files:
            self._json({"error": "Адрес не найден."}, 404)
            return
        name, content_type = files[path]
        try:
            content = (BASE / "web" / name).read_text()
            content = content.replace("__SESSION_TOKEN__", self.server.token).encode()
        except OSError:
            self._json({"error": "Файлы интерфейса не найдены."}, 503)
            return
        self._headers(200, content_type, len(content))
        self.wfile.write(content)

    def do_POST(self):
        if not self._authorized():
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 128 * 1024:
                self._json({"error": "Слишком большой запрос."}, 413)
                return
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                self._json({"error": "Нужен запрос JSON."}, 415)
                return
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("Неверный формат запроса.")
            path = urlsplit(self.path).path
            library = self.server.library
            if path == "/api/load":
                library.load(payload.get("url"))
            elif path == "/api/transfer":
                library.transfer(payload.get("ids"), payload.get("playlist", "Из SoundCloud"), payload.get("import", True))
            elif path == "/api/cancel":
                library.cancel()
            elif path == "/api/reveal":
                library.reveal()
            elif path == "/api/choose-cookies":
                library.choose_cookies()
            else:
                self._json({"error": "Адрес не найден."}, 404)
                return
            self._json({"ok": True})
        except (ValueError, TypeError) as error:
            self._json({"error": str(error)[:1000]}, 400)
        except Exception as error:
            self._json({"error": str(error)[:1000]}, 500)


def main():
    parser = argparse.ArgumentParser(description="SoundCloud → Музыка: локальный выбор и импорт треков")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    arguments = parser.parse_args()
    if not 0 <= arguments.port <= 65535:
        parser.error("Порт должен быть от 0 до 65535.")
    server = LocalServer(arguments.port)
    print("Локальный интерфейс: " + server.origin, flush=True)
    if not arguments.no_browser:
        webbrowser.open(server.origin)
    def stop(signum, frame):
        server.library.cancel()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        server.library.cancel()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

"""AI Radio: dependency-free, loopback-only application server."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import logging
from logging.handlers import RotatingFileHandler
import mimetypes
import os
from pathlib import Path
import re
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from urllib.parse import unquote, urlsplit, parse_qs
import uuid
import webbrowser

from library import Library, VERSION, EXTENSIONS
from local_auth import LocalAuth, save_session, remove_session
from radio_services import RadioServices

APP_DIR = Path(__file__).resolve().parent
MAX_JSON = 64 * 1024
MAX_UPLOAD = 250 * 1024 * 1024
LOG = logging.getLogger("ai-radio")
MIME = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
        ".ogg": "audio/ogg", ".flac": "audio/flac", ".aac": "audio/aac",
        ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".html": "text/html; charset=utf-8"}


class RadioHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"

    def __init__(self, address, data_dir):
        self.library = Library(data_dir)
        self.services = RadioServices(self.library.data_dir)
        self.dialog_lock = threading.Lock()
        self.talk_slots = threading.BoundedSemaphore(2)
        self.auth = LocalAuth()
        super().__init__(address, Handler)


def make_server(host="127.0.0.1", port=8765, data_dir=None):
    if host != "127.0.0.1":
        raise ValueError("このアプリは127.0.0.1でのみ起動できます。")
    return RadioHTTPServer((host, port), data_dir or APP_DIR / "data")


class Handler(BaseHTTPRequestHandler):
    server_version = "AIRadio"
    protocol_version = "HTTP/1.1"

    def version_string(self):
        return self.server_version

    def setup(self):
        super().setup()
        self.connection.settimeout(45)

    def log_message(self, fmt, *args):
        # Do not log arbitrary local filenames, feed URLs, or request bodies.
        LOG.info("HTTP %s", args[1] if len(args) > 1 else "request")

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' https://www.youtube.com https://s.ytimg.com; style-src 'self'; img-src 'self' data: https://i.ytimg.com; frame-src https://www.youtube.com https://www.youtube-nocookie.com; media-src 'self' blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        super().end_headers()

    @property
    def port(self):
        return self.server.server_address[1]

    def allowed_hosts(self):
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}

    def guard(self):
        if any(len(self.headers.get_all(name, [])) > 1 for name in ("Host", "Origin", "Authorization", "Sec-Fetch-Site")):
            self.respond(400, {"error": "重複したリクエストヘッダーです。"})
            return False
        host = self.headers.get("Host", "")
        if host not in self.allowed_hosts():
            self.respond(403, {"error": "ローカルの正しいURLから開いてください。"})
            return False
        origin = self.headers.get("Origin")
        # Reject cross-origin reads too, including requests from arbitrary websites.
        if origin is not None and origin != "http://" + host:
            self.respond(403, {"error": "別サイトからのアクセスは許可されていません。"})
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self.respond(403, {"error": "別サイトからのアクセスは許可されていません。"})
            return False
        try:
            parsed = urlsplit(self.path)
            path = unquote(parsed.path)
        except ValueError:
            self.respond(400, {"error": "URLが不正です。"})
            return False
        if path.startswith("/api/") and path != "/api/health":
            authorized = self.server.auth.accepts(self.headers.get("Authorization"))
            if not authorized and self.command in ("GET", "HEAD"):
                authorized = self.server.auth.accepts_media(path, parsed.query)
            if not authorized:
                self.respond(401, {"error": "起動用ファイルからラジオを開き直してください。"})
                return False
        return True

    def respond(self, status, value):
        data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if self.command != "HEAD":
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def read_body_length(self, maximum):
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("Transfer-Encodingには対応していません。")
        if len(self.headers.get_all("Content-Length", [])) != 1:
            raise ValueError("Content-Lengthは1つだけ指定してください。")
        raw = self.headers.get("Content-Length", "")
        if not raw.isascii() or not raw.isdigit():
            raise ValueError("Content-Lengthが必要です。")
        size = int(raw)
        if size > maximum:
            raise OverflowError("ファイルまたはデータが大きすぎます。1ファイル250MBまでです。")
        return size

    def read_json(self):
        if self.headers.get_content_type() != "application/json":
            raise ValueError("Content-Type: application/json が必要です。")
        length = self.read_body_length(MAX_JSON)
        data = self.rfile.read(length)
        if len(data) != length:
            raise ValueError("データを最後まで読み込めませんでした。")
        try:
            value = json.loads(data.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError("不正な数値です。")))
        except RecursionError as exc:
            raise ValueError("JSONの入れ子が深すぎます。") from exc
        if not isinstance(value, dict):
            raise ValueError("JSONオブジェクトを指定してください。")
        return value

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self.guard():
            return
        try:
            path = unquote(urlsplit(self.path).path)
            lib = self.server.library
            if path == "/api/health":
                self.respond(200, {"app": "ai-radio", "version": VERSION, "auth_instance": self.server.auth.instance})
            elif path == "/api/state":
                self.respond(200, {"version": VERSION, "settings": lib.settings, "tracks": lib.tracks(), "warning": getattr(lib, "settings_warning", "")})
            elif path == "/api/status":
                self.respond(200, self.server.services.status(lib.settings))
            elif path.startswith("/api/media/"):
                tid = path.removeprefix("/api/media/")
                if not re.fullmatch(r"[a-f0-9]{32}", tid):
                    raise KeyError("曲が見つかりません。")
                self.send_file(lib.media_path(tid))
            elif path.startswith("/api/audio/"):
                name = path.removeprefix("/api/audio/")
                if not re.fullmatch(r"[a-f0-9-]{16,64}\.wav", name):
                    raise KeyError("トーク音声が見つかりません。")
                self.send_file(lib.data_dir / "audio" / name)
            else:
                mapping = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/style.css": "style.css", "/favicon.svg": "favicon.svg", "/static/app.js": "app.js", "/static/style.css": "style.css", "/static/favicon.svg": "favicon.svg"}
                if path not in mapping:
                    raise KeyError("ページが見つかりません。")
                self.send_file(APP_DIR / "static" / mapping[path])
        except (KeyError, FileNotFoundError) as exc:
            self.respond(404, {"error": str(exc).strip("'")})
        except (ValueError, UnicodeError) as exc:
            self.respond(400, {"error": str(exc)})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            LOG.exception("GET failed")
            self.respond(500, {"error": "処理に失敗しました。data/server.logを確認してください。"})

    def send_file(self, path):
        with Path(path).open("rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            start, end, status = 0, size - 1, 200
            requested = self.headers.get("Range")
            if requested:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested.strip())
                valid = bool(match) and size > 0
                if valid:
                    left, right = match.groups()
                    valid = bool(left or right)
                    if left:
                        start = int(left)
                        end = min(int(right), size - 1) if right else size - 1
                    elif right:
                        suffix = int(right)
                        start = max(0, size - suffix)
                        valid = suffix > 0
                    valid = valid and 0 <= start <= end and start < size
                if not valid:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206
            length = max(0, end - start + 1)
            self.send_response(status)
            self.send_header("Content-Type", MIME.get(Path(path).suffix.lower(), mimetypes.guess_type(str(path))[0] or "application/octet-stream"))
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-store")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command == "HEAD":
                return
            stream.seek(start)
            while length:
                chunk = stream.read(min(length, 128 * 1024))
                if not chunk:
                    break
                self.wfile.write(chunk)
                length -= len(chunk)

    def do_POST(self):
        if not self.guard():
            return
        try:
            parsed = urlsplit(self.path)
            path = unquote(parsed.path)
            if path == "/api/library/upload":
                self.upload(parsed)
                return
            data = self.read_json()
            lib = self.server.library
            if path == "/api/settings":
                self.respond(200, {"settings": lib.update_settings(data)})
            elif path == "/api/playback-url":
                self.respond(200, {"url": self.server.auth.media_url(data.get("path"))})
            elif path == "/api/library/folder":
                self.respond(200, lib.import_folder(data.get("path")))
            elif path == "/api/library/demo":
                added = 0
                for filename, title, tags in (
                    ("01_Morning_Window.wav", "朝の窓辺 / Morning Window", "穏やか, 朝"),
                    ("02_Evening_Walk.wav", "夕暮れの散歩 / Evening Walk", "穏やか, 夕方"),
                ):
                    track, fresh = lib.add_file(APP_DIR / "sample_music" / filename)
                    if fresh:
                        added += 1
                        lib.update_track({"id": track["id"], "title": title, "artist": "よりみちラジオ（デモ音源）", "tags": tags,
                                          "comment": "動作確認用に作成した、14秒のオリジナル曲です。"})
                self.respond(200, {"tracks": lib.tracks(), "added": added, "warnings": []})
            elif path == "/api/library/pick-folder":
                self.respond(200, {"path": self.pick_folder()})
            elif path == "/api/tracks/update":
                self.respond(200, {"track": lib.update_track(data)})
            elif path == "/api/tracks/remove":
                lib.remove(data.get("id", ""))
                self.respond(200, {"ok": True})
            elif path == "/api/library/clear":
                lib.clear()
                self.respond(200, {"ok": True})
            elif path == "/api/youtube/import":
                self.respond(200, lib.import_youtube(data.get("url"), data.get("video_ids")))
            elif path in ("/api/talk/prepare", "/api/talk/fallback"):
                category = data.get("category")
                if category is not None and category not in ("weather", "news", "music", "chat"):
                    raise ValueError("話題の種類が不正です。")
                track, next_track = data.get("track") or {}, data.get("next_track") or {}
                if not isinstance(track, dict) or not isinstance(next_track, dict):
                    raise ValueError("曲情報が不正です。")
                for entry in (track, next_track):
                    if any(k in entry and not isinstance(entry[k], str) for k in ("title", "artist", "tags", "comment")):
                        raise ValueError("曲情報は文字列で指定してください。")
                if path == "/api/talk/fallback":
                    self.respond(200, self.server.services.prepare_fallback(lib.settings, track, next_track))
                    return
                if not self.server.talk_slots.acquire(blocking=False):
                    self.respond(429, {"error": "トークを準備中です。少し待ってから操作してください。"})
                    return
                try:
                    result = self.server.services.prepare_talk(lib.settings, track, next_track, category)
                finally:
                    self.server.talk_slots.release()
                self.respond(200, result)
            elif path == "/api/shutdown":
                self.respond(200, {"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self.respond(404, {"error": "APIが見つかりません。"})
        except (ValueError, UnicodeError, TypeError) as exc:
            self.respond(400, {"error": str(exc)})
        except OverflowError as exc:
            self.respond(413, {"error": str(exc)})
        except (KeyError, FileNotFoundError) as exc:
            self.respond(404, {"error": str(exc).strip("'")})
        except PermissionError:
            self.respond(403, {"error": "このフォルダまたはファイルにアクセスできません。"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except socket.timeout:
            self.respond(408, {"error": "読み込みがタイムアウトしました。"})
        except Exception:
            LOG.exception("POST failed")
            self.respond(500, {"error": "処理に失敗しました。data/server.logを確認してください。"})

    def upload(self, parsed):
        length = self.read_body_length(MAX_UPLOAD)
        if length == 0:
            raise ValueError("空のファイルは登録できません。")
        name = parse_qs(parsed.query).get("name", [""])[0].replace("\\", "/").rsplit("/", 1)[-1]
        ext = Path(name).suffix.lower()
        if ext not in EXTENSIONS:
            raise ValueError("MP3・WAV・OGG・M4A・FLAC・AACを選択してください。")
        lib = self.server.library
        destination = lib.data_dir / "uploads" / (uuid.uuid4().hex + ext)
        try:
            with destination.open("xb") as stream:
                remaining = length
                while remaining:
                    chunk = self.rfile.read(min(128 * 1024, remaining))
                    if not chunk:
                        raise ValueError("ファイルを最後まで受信できませんでした。")
                    stream.write(chunk)
                    remaining -= len(chunk)
            track, _ = lib.add_file(destination)
            if track["title"] == destination.stem:
                track = lib.update_track({"id": track["id"], "title": Path(name).stem})
            self.respond(200, {"track": track})
        except BaseException:
            destination.unlink(missing_ok=True)
            raise

    def pick_folder(self):
        if not self.server.dialog_lock.acquire(blocking=False):
            raise ValueError("フォルダ選択画面がすでに開いています。")
        try:
            import tkinter
            from tkinter import filedialog
            root = tkinter.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            try:
                selected = filedialog.askdirectory(title="音楽フォルダを選択", parent=root, mustexist=True)
            finally:
                root.destroy()
            return selected or None
        except ImportError as exc:
            raise ValueError("フォルダのパスを直接入力するか、ファイル追加を使ってください。") from exc
        finally:
            self.server.dialog_lock.release()


def main():
    parser = argparse.ArgumentParser(description="無料で使う、あなたのAIラジオ")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=APP_DIR / "data")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    args.data_dir.mkdir(parents=True, exist_ok=True)
    LOG.setLevel(logging.INFO)
    log_handler = RotatingFileHandler(args.data_dir / "server.log", maxBytes=1024 * 1024, backupCount=2, encoding="utf-8")
    log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOG.addHandler(log_handler)
    try:
        server = make_server(port=args.port, data_dir=args.data_dir)
        save_session(args.data_dir, server.server_address[1], server.auth.token, server.auth.instance)
    except OSError as exc:
        if "server" in locals():
            server.server_close()
        LOG.error("Cannot start server: %s", exc)
        print(f"起動できません。ポート{args.port}が使われていないか確認してください。", flush=True)
        return 1
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    pid_path = args.data_dir / "server.pid"
    pid_path.write_text(str(os.getpid()), encoding="ascii")
    print(f"AI Radio {VERSION}: {url}", flush=True)
    LOG.info("Started on port %s", server.server_address[1])
    if not args.no_browser:
        webbrowser.open(server.auth.browser_url(server.server_address[1]))
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        remove_session(args.data_dir, server.server_address[1], server.auth.instance)
        try:
            if pid_path.read_text(encoding="ascii") == str(os.getpid()):
                pid_path.unlink(missing_ok=True)
        except OSError:
            pass
        LOG.info("Stopped")
        log_handler.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

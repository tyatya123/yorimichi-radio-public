"""Local library and validated settings. Standard library only."""
from __future__ import annotations

import copy
from contextlib import contextmanager
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
from urllib.parse import urlsplit, parse_qs
import uuid

VERSION = "0.2.2"
EXTENSIONS = {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac"}
DEFAULT_SETTINGS = {
    "city": "東京", "latitude": 35.6812, "longitude": 139.7671,
    "timezone": "Asia/Tokyo", "talk_seconds": 30,
    "topic_weights": {"weather": 15, "news": 30, "music": 25, "chat": 30},
    "news_feeds": ["https://rss.itmedia.co.jp/rss/2.0/news_bursts.xml"],
    "voicevox_url": "http://127.0.0.1:50021", "speaker_id": 3,
    "speech_rate": 1.0, "browser_voice": "", "llm_enabled": False,
    "ollama_url": "http://127.0.0.1:11434", "ollama_model": "",
    "persona": "落ち着いた親しみやすいDJ", "shuffle": True,
    "music_volume": 0.8, "talk_volume": 0.85,
}


def clean_text(value, limit=300):
    if not isinstance(value, str):
        raise ValueError("文字列を指定してください。")
    return "".join(c for c in value if c >= " " or c == "\n").strip()[:limit]


def validate_track_id(value):
    """Reject JSON containers and non-text IDs before reaching SQLite."""
    if not isinstance(value, str):
        raise ValueError("曲IDは文字列で指定してください。")
    return value


def local_url(value):
    value = clean_text(value, 300).rstrip("/")
    parsed = urlsplit(value)
    try:
        loopback = parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname or "").is_loopback
    except ValueError:
        loopback = False
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("接続先のポートが不正です。") from exc
    if (parsed.scheme != "http" or not loopback or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
        raise ValueError("接続先はこのPCの http://127.0.0.1:ポート番号 を指定してください。")
    return value


def validate_settings(patch, base=None):
    if not isinstance(patch, dict):
        raise ValueError("設定はオブジェクトで指定してください。")
    unknown = set(patch) - set(DEFAULT_SETTINGS)
    if unknown:
        raise ValueError("不明な設定項目: " + ", ".join(sorted(unknown)))
    result = copy.deepcopy(base if base is not None else DEFAULT_SETTINGS)
    for key, value in patch.items():
        if key in ("shuffle", "llm_enabled"):
            if not isinstance(value, bool):
                raise ValueError(f"{key} はON/OFFで指定してください。")
        elif key in ("latitude", "longitude", "music_volume", "talk_volume", "speech_rate"):
            bounds = {"latitude": (-90, 90), "longitude": (-180, 180),
                      "music_volume": (0, 1), "talk_volume": (0, 1), "speech_rate": (0.5, 2)}
            lo, hi = bounds[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not lo <= value <= hi:
                raise ValueError(f"{key} は {lo}〜{hi} の数値を指定してください。")
        elif key in ("talk_seconds", "speaker_id"):
            lo, hi = (10, 90) if key == "talk_seconds" else (0, 100000)
            if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
                raise ValueError(f"{key} は {lo}〜{hi} の整数を指定してください。")
        elif key == "topic_weights":
            if not isinstance(value, dict) or set(value) != {"weather", "news", "music", "chat"}:
                raise ValueError("話題の割合は天気・ニュース・曲紹介・雑談を指定してください。")
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 100 for v in value.values()):
                raise ValueError("話題の割合は0〜100の数値を指定してください。")
            if sum(value.values()) <= 0:
                raise ValueError("話題を1つ以上有効にしてください。")
        elif key == "news_feeds":
            if not isinstance(value, list) or len(value) > 5:
                raise ValueError("RSSは5件以内で指定してください。")
            feeds = []
            for feed in value:
                feed = clean_text(feed, 2048)
                p = urlsplit(feed)
                if p.scheme != "https" or not p.hostname or p.username or p.password or p.fragment:
                    raise ValueError("RSSは公開されているHTTPSのURLを指定してください。")
                feeds.append(feed)
            value = list(dict.fromkeys(feeds))
        elif key in ("voicevox_url", "ollama_url"):
            value = local_url(value)
        elif key == "timezone":
            if value != "Asia/Tokyo":
                raise ValueError("現在のバージョンのタイムゾーンはAsia/Tokyoです。")
        else:
            value = clean_text(value, 600 if key == "persona" else 160)
            if key == "city" and not value:
                raise ValueError("地域名を入力してください。")
        result[key] = value
    return result


def _id3_text(payload):
    if not payload:
        return ""
    encodings = {0: "latin-1", 1: "utf-16", 2: "utf-16-be", 3: "utf-8"}
    return payload[1:].decode(encodings.get(payload[0], "utf-8"), errors="replace").strip("\x00 \r\n")[:300]


def _synchsafe(data):
    n = 0
    for b in data:
        n = (n << 7) | (b & 127)
    return n


def read_metadata(path):
    """Read common ID3v2.3/v2.4/v1 tags without loading an entire MP3."""
    title, artist = path.stem, ""
    if path.suffix.lower() != ".mp3":
        return title, artist
    try:
        with path.open("rb") as stream:
            header = stream.read(10)
            if header[:3] == b"ID3" and header[3] in (3, 4):
                data = stream.read(min(_synchsafe(header[6:10]), 1024 * 1024))
                pos = 0
                if header[5] & 0x40 and len(data) >= 4:
                    extended = _synchsafe(data[:4]) if header[3] == 4 else int.from_bytes(data[:4], "big") + 4
                    pos = extended
                for _ in range(500):
                    if pos + 10 > len(data):
                        break
                    frame = data[pos:pos + 4]
                    size = _synchsafe(data[pos+4:pos+8]) if header[3] == 4 else int.from_bytes(data[pos+4:pos+8], "big")
                    if not size or not re.fullmatch(b"[A-Z0-9]{4}", frame) or pos + 10 + size > len(data):
                        break
                    payload = data[pos+10:pos+10+size]
                    if frame == b"TIT2":
                        title = _id3_text(payload) or title
                    elif frame == b"TPE1":
                        artist = _id3_text(payload)
                    pos += 10 + size
            if not artist or title == path.stem:
                stream.seek(max(0, path.stat().st_size - 128))
                legacy = stream.read(128)
                if legacy[:3] == b"TAG":
                    if title == path.stem:
                        title = legacy[3:33].rstrip(b"\x00 ").decode("cp932", "replace") or title
                    artist = artist or legacy[33:63].rstrip(b"\x00 ").decode("cp932", "replace")
    except (OSError, ValueError, UnicodeError):
        pass
    return title, artist


class Library:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "uploads").mkdir(exist_ok=True)
        (self.data_dir / "audio").mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.db_path = self.data_dir / "library.sqlite3"
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS tracks (
                id TEXT PRIMARY KEY, path TEXT UNIQUE, source TEXT NOT NULL,
                title TEXT NOT NULL, artist TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '', comment TEXT NOT NULL DEFAULT '',
                video_id TEXT NOT NULL DEFAULT '', added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
        self.settings_path = self.data_dir / "settings.json"
        self._settings = copy.deepcopy(DEFAULT_SETTINGS)
        if self.settings_path.exists():
            try:
                self._settings = validate_settings(json.loads(self.settings_path.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError):
                # Keep damaged settings for inspection rather than silently overwriting them.
                self.settings_warning = "設定ファイルを読み込めなかったため初期設定で起動しました。"

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @property
    def settings(self):
        with self.lock:
            return copy.deepcopy(self._settings)

    def update_settings(self, patch):
        with self.lock:
            new = validate_settings(patch, self._settings)
            temp = self.settings_path.with_suffix(".tmp")
            temp.write_text(json.dumps(new, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
            os.replace(temp, self.settings_path)
            self._settings = new
            return self.settings

    def tracks(self):
        with self.connect() as db:
            return [self.public_track(row) for row in db.execute("SELECT * FROM tracks ORDER BY added_at, rowid")]

    def public_track(self, row):
        result = dict(row)
        path = result.pop("path", None)
        result["filename"] = Path(path).name if path and result["source"] == "local" else ""
        result["extension"] = Path(path).suffix.lower() if path and result["source"] == "local" else ""
        result["url"] = "/api/media/" + result["id"] if result["source"] == "local" else "https://www.youtube.com/watch?v=" + result["video_id"]
        return result

    def track(self, track_id):
        track_id = validate_track_id(track_id)
        with self.connect() as db:
            row = db.execute("SELECT * FROM tracks WHERE id=?", (track_id,)).fetchone()
        if row is None:
            raise KeyError("曲が見つかりません。")
        return self.public_track(row)

    def media_path(self, track_id):
        track_id = validate_track_id(track_id)
        with self.connect() as db:
            row = db.execute("SELECT path, source FROM tracks WHERE id=?", (track_id,)).fetchone()
        if row is None or row["source"] != "local":
            raise KeyError("曲が見つかりません。")
        path = Path(row["path"])
        if not path.is_file():
            raise FileNotFoundError("音楽ファイルが移動または削除されています。再登録してください。")
        return path

    def add_file(self, path, title=None):
        path = Path(path).expanduser().resolve(strict=True)
        if not path.is_file() or path.suffix.lower() not in EXTENSIONS:
            raise ValueError("対応していない音楽ファイルです。")
        file_title, artist = read_metadata(path)
        with self.lock, self.connect() as db:
            row = db.execute("SELECT * FROM tracks WHERE path=?", (str(path),)).fetchone()
            if row:
                return self.public_track(row), False
            tid = uuid.uuid4().hex
            db.execute("INSERT INTO tracks(id,path,source,title,artist) VALUES(?,?,?,?,?)", (tid, str(path), "local", title or file_title, artist))
            row = db.execute("SELECT * FROM tracks WHERE id=?", (tid,)).fetchone()
            return self.public_track(row), True

    def import_folder(self, folder):
        if not isinstance(folder, str) or not folder.strip():
            raise ValueError("音楽フォルダのパスを入力してください。")
        root = Path(folder).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError("フォルダを指定してください。")
        added = 0
        warnings = []
        count = 0
        # os.walk does not follow directory symlinks; never modify source files.
        for directory, _, names in os.walk(root, followlinks=False):
            for name in sorted(names):
                path = Path(directory) / name
                if path.suffix.lower() not in EXTENSIONS:
                    continue
                count += 1
                if count > 5000:
                    warnings.append("1回の登録は5000曲までです。フォルダを分けて追加してください。")
                    return {"tracks": self.tracks(), "added": added, "warnings": warnings}
                try:
                    _, fresh = self.add_file(path)
                    added += int(fresh)
                except (OSError, ValueError):
                    warnings.append(f"読み込めませんでした: {name}")
        return {"tracks": self.tracks(), "added": added, "warnings": warnings[:30]}

    def update_track(self, patch):
        if not isinstance(patch, dict):
            raise ValueError("曲情報はオブジェクトで指定してください。")
        tid = patch.get("id")
        self.track(tid)
        values = {key: clean_text(patch[key], 1000 if key == "comment" else 300)
                  for key in ("title", "artist", "tags", "comment") if key in patch}
        if "title" in values and not values["title"]:
            raise ValueError("曲名を入力してください。")
        if values:
            with self.connect() as db:
                db.execute("UPDATE tracks SET " + ",".join(f"{key}=?" for key in values) + " WHERE id=?", (*values.values(), tid))
        return self.track(tid)

    def remove(self, tid):
        tid = validate_track_id(tid)
        with self.connect() as db:
            db.execute("DELETE FROM tracks WHERE id=?", (tid,))

    def clear(self):
        with self.connect() as db:
            db.execute("DELETE FROM tracks")

    def import_youtube(self, url, video_ids=None):
        if not isinstance(url, str):
            raise ValueError("YouTube URLを指定してください。")
        parsed = urlsplit(url.strip())
        if parsed.scheme != "https" or parsed.hostname not in ("www.youtube.com", "youtube.com", "m.youtube.com", "youtu.be") or parsed.username or parsed.password:
            raise ValueError("https://www.youtube.com/ または https://youtu.be/ のURLを指定してください。")
        query = parse_qs(parsed.query)
        single = parsed.path.lstrip("/") if parsed.hostname == "youtu.be" else query.get("v", [""])[0]
        if video_ids is None:
            video_ids = [single] if single else []
        if not isinstance(video_ids, list) or not video_ids or len(video_ids) > 500:
            raise ValueError("再生リストの曲情報がまだありません。画面で読み込みを完了するか、動画単体のURLを追加してください。")
        if any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", v) for v in video_ids):
            raise ValueError("YouTube動画IDが不正です。")
        added = 0
        with self.lock, self.connect() as db:
            for vid in dict.fromkeys(video_ids):
                dbkey = "youtube:" + vid
                if db.execute("SELECT id FROM tracks WHERE path=?", (dbkey,)).fetchone():
                    continue
                db.execute("INSERT INTO tracks(id,path,source,title,video_id) VALUES(?,?,?,?,?)", (uuid.uuid4().hex, dbkey, "youtube", "YouTube " + vid, vid))
                added += 1
        return {"tracks": self.tracks(), "added": added, "warnings": ["YouTube連携はベータ版です。動画の埋め込み制限や広告、自動再生制限があります。"]}

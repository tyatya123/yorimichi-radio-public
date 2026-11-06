"""Exercise the real local HTTP boundary without external services or media tools."""
from __future__ import annotations

import http.client
import io
import json
from pathlib import Path
from unittest.mock import patch
import struct
import sys
import tempfile
import threading
import unittest
from urllib.parse import quote
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


def wav_bytes() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"".join(struct.pack("<h", i % 1000) for i in range(800)))
    return output.getvalue()


class HTTPServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ai-radio-http-")
        self.data_dir = Path(self.temp.name) / "data"
        self.server = app.make_server(host="127.0.0.1", port=0, data_dir=self.data_dir)
        self.port = self.server.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.02), daemon=True
        )
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def request(self, method, path, payload=None, headers=None, raw=None, authenticate=True):
        request_headers = {"Connection": "close"}
        if authenticate:
            request_headers["Authorization"] = "Bearer " + self.server.auth.token
        body = raw
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        if headers:
            request_headers.update(headers)
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            status, result_headers = response.status, dict(response.getheaders())
            result_body = response.read()
            if "application/json" in result_headers.get("Content-Type", "") and result_body:
                result_body = json.loads(result_body)
            return status, result_headers, result_body
        finally:
            connection.close()

    def upload(self):
        content = wav_bytes()
        status, _, result = self.request(
            "POST", "/api/library/upload?name=" + quote("試聴用.wav"),
            headers={"Content-Type": "application/octet-stream", "Origin": self.origin},
            raw=content,
        )
        self.assertIn(status, (200, 201), result)
        return result["track"], content

    def test_health_identifies_this_application(self):
        status, _, body = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["app"], "ai-radio")
        self.assertTrue(body["version"])

    def test_state_starts_with_settings_and_empty_library(self):
        status, _, body = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertIsInstance(body["settings"], dict)
        self.assertEqual(body["tracks"], [])

    def test_frontend_static_assets_are_served_at_html_urls(self):
        for path, expected_type in (("/static/style.css", "text/css"), ("/static/app.js", "text/javascript")):
            with self.subTest(path=path):
                status, headers, body = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertIn(expected_type, headers["Content-Type"])
                self.assertTrue(body)

    def test_demo_music_can_be_registered_without_user_files(self):
        status, _, body = self.request("POST", "/api/library/demo", {})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["added"], 2)
        self.assertEqual(len(body["tracks"]), 2)
        for track in body["tracks"]:
            status, headers, body = self.request("HEAD", "/api/media/" + track["id"])
            self.assertEqual(status, 200)
            self.assertIn("audio/wav", headers["Content-Type"])

    def test_foreign_host_is_rejected(self):
        status, _, _ = self.request("GET", "/api/health", headers={"Host": "evil.example"})
        self.assertIn(status, (400, 403))

    def test_foreign_origin_cannot_change_settings(self):
        status, _, _ = self.request(
            "POST", "/api/settings", {"city": "侵入テスト"},
            headers={"Origin": "https://evil.example"},
        )
        self.assertIn(status, (400, 403))
        _, _, state = self.request("GET", "/api/state")
        self.assertNotEqual(state["settings"]["city"], "侵入テスト")

    def test_same_origin_can_save_settings(self):
        status, _, body = self.request(
            "POST", "/api/settings", {"city": "大阪", "latitude": 34.69, "longitude": 135.50},
            headers={"Origin": self.origin},
        )
        self.assertEqual(status, 200, body)
        _, _, state = self.request("GET", "/api/state")
        self.assertEqual(state["settings"]["city"], "大阪")

    def test_invalid_coordinate_is_rejected_without_partial_update(self):
        status, _, _ = self.request("POST", "/api/settings", {"city": "invalid", "latitude": 500})
        self.assertEqual(status, 400)
        _, _, state = self.request("GET", "/api/state")
        self.assertNotEqual(state["settings"]["city"], "invalid")

    def test_json_booleans_cannot_be_used_as_numeric_coordinates(self):
        status, _, _ = self.request("POST", "/api/settings", {"latitude": True})
        self.assertEqual(status, 400)

    def test_string_cannot_be_used_as_boolean_setting(self):
        status, _, _ = self.request("POST", "/api/settings", {"llm_enabled": "false"})
        self.assertEqual(status, 400)

    def test_all_zero_topic_weights_are_rejected(self):
        status, _, _ = self.request(
            "POST", "/api/settings",
            {"topic_weights": {"weather": 0, "news": 0, "music": 0, "chat": 0}},
        )
        self.assertEqual(status, 400)

    def test_external_synthesis_endpoint_is_rejected(self):
        status, _, _ = self.request(
            "POST", "/api/settings", {"voicevox_url": "https://example.com"}
        )
        self.assertEqual(status, 400)

    def test_malformed_json_is_rejected(self):
        status, _, _ = self.request(
            "POST", "/api/settings", raw=b'{"city":', headers={"Content-Type": "application/json"}
        )
        self.assertEqual(status, 400)

    def test_unsupported_upload_extension_is_rejected(self):
        status, _, _ = self.request(
            "POST", "/api/library/upload?name=payload.exe",
            raw=b"MZ", headers={"Content-Type": "application/octet-stream"},
        )
        self.assertEqual(status, 400)

    def test_upload_returns_track_and_exact_media_bytes(self):
        track, content = self.upload()
        self.assertEqual(track["source"], "local")
        status, headers, body = self.request("GET", "/api/media/" + track["id"])
        self.assertEqual(status, 200)
        self.assertEqual(body, content)
        self.assertEqual(int(headers["Content-Length"]), len(content))
        self.assertEqual(headers["Accept-Ranges"], "bytes")

    def test_media_head_has_length_without_body(self):
        track, content = self.upload()
        status, headers, body = self.request("HEAD", "/api/media/" + track["id"])
        self.assertEqual(status, 200)
        self.assertEqual(int(headers["Content-Length"]), len(content))
        self.assertEqual(body, b"")

    def test_media_byte_range(self):
        track, content = self.upload()
        status, headers, body = self.request(
            "GET", "/api/media/" + track["id"], headers={"Range": "bytes=3-10"}
        )
        self.assertEqual(status, 206)
        self.assertEqual(body, content[3:11])
        self.assertEqual(headers["Content-Range"], f"bytes 3-10/{len(content)}")

    def test_media_suffix_range(self):
        track, content = self.upload()
        status, headers, body = self.request(
            "GET", "/api/media/" + track["id"], headers={"Range": "bytes=-9"}
        )
        self.assertEqual(status, 206)
        self.assertEqual(body, content[-9:])
        self.assertEqual(headers["Content-Range"], f"bytes {len(content)-9}-{len(content)-1}/{len(content)}")

    def test_media_open_ended_range(self):
        track, content = self.upload()
        status, _, body = self.request(
            "GET", "/api/media/" + track["id"], headers={"Range": "bytes=32-"}
        )
        self.assertEqual(status, 206)
        self.assertEqual(body, content[32:])

    def test_unsatisfiable_range_returns_416(self):
        track, content = self.upload()
        status, headers, _ = self.request(
            "GET", "/api/media/" + track["id"], headers={"Range": "bytes=999999-"}
        )
        self.assertEqual(status, 416)
        self.assertEqual(headers["Content-Range"], f"bytes */{len(content)}")

    def test_invalid_and_multiple_ranges_are_rejected(self):
        track, content = self.upload()
        for value in ("bytes=8-3", "bytes=0-1,4-5", "bytes=-0", "bytes=-", "items=0-9"):
            with self.subTest(range=value):
                status, headers, _ = self.request(
                    "GET", "/api/media/" + track["id"], headers={"Range": value}
                )
                self.assertEqual(status, 416)
                self.assertEqual(headers["Content-Range"], f"bytes */{len(content)}")

    def test_static_and_media_path_traversal_cannot_read_outside_files(self):
        secret = Path(self.temp.name) / "private.txt"
        secret.write_text("private-marker-446", encoding="utf-8")
        for path in ("/../private.txt", "/%2e%2e/private.txt", "/api/media/../private.txt", "/api/audio/../private.txt"):
            with self.subTest(path=path):
                status, _, body = self.request("GET", path)
                self.assertIn(status, (400, 403, 404))
                self.assertNotIn(b"private-marker-446", body if isinstance(body, bytes) else json.dumps(body).encode())

    def test_edit_and_remove_only_unregister_media(self):
        track, _ = self.upload()
        status, _, body = self.request(
            "POST", "/api/tracks/update",
            {"id": track["id"], "title": "朝の曲", "artist": "テスト歌手", "tags": "爽やか,朝", "comment": "好きな曲"},
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["track"]["title"], "朝の曲")
        before = list((self.data_dir / "uploads").rglob("*"))
        self.assertTrue(any(path.is_file() for path in before))
        status, _, _ = self.request("POST", "/api/tracks/remove", {"id": track["id"]})
        self.assertEqual(status, 200)
        _, _, state = self.request("GET", "/api/state")
        self.assertEqual(state["tracks"], [])
        self.assertTrue(all(path.exists() for path in before))

    def test_folder_import_references_original_and_clear_preserves_it(self):
        music_dir = Path(self.temp.name) / "music"
        music_dir.mkdir()
        original = music_dir / "original.wav"
        original.write_bytes(wav_bytes())
        status, _, result = self.request("POST", "/api/library/folder", {"path": str(music_dir)})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["added"], 1)
        self.assertTrue(result["tracks"])
        status, _, _ = self.request("POST", "/api/library/clear", {})
        self.assertEqual(status, 200)
        self.assertTrue(original.exists())

    def test_youtube_url_validation_rejects_foreign_host(self):
        status, _, _ = self.request(
            "POST", "/api/youtube/import", {"url": "https://evil.example/watch?v=dQw4w9WgXcQ"}
        )
        self.assertEqual(status, 400)

    def test_native_clients_without_auth_cannot_read_or_change_data(self):
        for method, path, data in (("GET", "/api/state", None), ("GET", "/api/status", None),
                                   ("POST", "/api/settings", {"city": "unauthorized"}),
                                   ("POST", "/api/library/folder", {"path": self.temp.name}),
                                   ("POST", "/api/shutdown", {})):
            with self.subTest(path=path):
                status, _, _ = self.request(method, path, data, authenticate=False)
                self.assertEqual(status, 401)
        self.assertNotEqual(self.server.library.settings["city"], "unauthorized")

    def test_public_health_and_shell_never_contain_secret(self):
        for path in ("/api/health", "/", "/static/app.js"):
            with self.subTest(path=path):
                status, _, body = self.request("GET", path, authenticate=False)
                self.assertEqual(status, 200)
                if isinstance(body, dict):
                    body = json.dumps(body).encode()
                self.assertNotIn(self.server.auth.token.encode(), body)

    def test_forged_auth_or_cookie_is_not_accepted(self):
        for headers in ({"Authorization": "Bearer " + "a" * 43},
                        {"Authorization": "Bearer \xff"},
                        {"Cookie": "session=" + self.server.auth.token}):
            status, _, _ = self.request("GET", "/api/state", headers=headers, authenticate=False)
            self.assertEqual(status, 401)

    def test_signed_media_supports_range_but_cannot_authorize_api_or_other_track(self):
        track, content = self.upload()
        path = "/api/media/" + track["id"]
        self.assertEqual(self.request("GET", path, authenticate=False)[0], 401)
        status, _, signed = self.request("POST", "/api/playback-url", {"path": path})
        self.assertEqual(status, 200)
        url = signed["url"]
        self.assertNotIn(self.server.auth.token, url)
        status, _, body = self.request("GET", url, headers={"Range": "bytes=0-3"}, authenticate=False)
        self.assertEqual(status, 206)
        self.assertEqual(body, content[:4])
        self.assertEqual(self.request("HEAD", url, authenticate=False)[0], 200)
        query = url.split("?", 1)[1]
        self.assertEqual(self.request("GET", "/api/state?" + query, authenticate=False)[0], 401)
        self.assertEqual(self.request("GET", "/api/media/" + "f" * 32 + "?" + query, authenticate=False)[0], 401)

    def test_expired_and_previous_launch_signatures_are_rejected(self):
        track, _ = self.upload()
        path = "/api/media/" + track["id"]
        with patch("local_auth.time.time", return_value=100):
            url = self.server.auth.media_url(path)
        self.assertEqual(self.request("GET", url, authenticate=False)[0], 401)
        url = self.server.auth.media_url(path)
        self.server.auth = app.LocalAuth()
        self.assertEqual(self.request("GET", url, authenticate=False)[0], 401)

    def test_playback_url_only_accepts_local_canonical_media_paths(self):
        for path in ("https://example.com/a.mp3", "/api/state", "/api/media/../x", "/api/audio/x.wav", None):
            with self.subTest(path=path):
                self.assertEqual(self.request("POST", "/api/playback-url", {"path": path})[0], 400)

    def test_hardened_headers_and_invalid_id_types(self):
        for path in ("/", "/api/health", "/not-found"):
            _, headers, _ = self.request("GET", path)
            self.assertEqual(headers["Server"], "AIRadio")
            self.assertIn("form-action 'self'", headers["Content-Security-Policy"])
            self.assertNotIn("unsafe-inline", headers["Content-Security-Policy"])
        for path in ("/api/tracks/update", "/api/tracks/remove"):
            self.assertEqual(self.request("POST", path, {"id": []})[0], 400)

    def test_ambiguous_content_length_and_deep_json_are_rejected(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.putrequest("POST", "/api/settings")
            connection.putheader("Authorization", "Bearer " + self.server.auth.token)
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", "2")
            connection.putheader("Content-Length", "3")
            connection.endheaders(b"{}")
            response = connection.getresponse()
            self.assertEqual(response.status, 400)
            response.read()
        finally:
            connection.close()
        status, _, _ = self.request("POST", "/api/settings", raw=b"{}",
            headers={"Content-Type": "application/json", "Content-Length": "2, 2"})
        self.assertEqual(status, 400)
        status, _, _ = self.request("POST", "/api/settings", raw=b'{"city":' + b'[' * 2000 + b'0' + b']' * 2000 + b'}',
            headers={"Content-Type": "application/json"})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()

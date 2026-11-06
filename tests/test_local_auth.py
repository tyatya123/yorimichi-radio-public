"""Local session protection and narrowly scoped playback capabilities."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from local_auth import LocalAuth, load_session, save_session, remove_session, session_path


class LocalAuthTests(unittest.TestCase):
    def test_session_roundtrip_and_owner_scoping(self):
        with tempfile.TemporaryDirectory() as data:
            auth = LocalAuth()
            save_session(data, 9876, auth.token, auth.instance)
            saved = load_session(data, 9876)
            self.assertEqual(saved["token"], auth.token)
            raw = session_path(data, 9876).read_bytes()
            self.assertNotIn(auth.token.encode(), raw)
            if os.name == "nt":
                self.assertEqual(json.loads(raw)["protection"], "windows-current-user-dpapi")
            else:
                self.assertEqual(session_path(data, 9876).stat().st_mode & 0o777, 0o600)
            remove_session(data, 9876, "different-instance")
            self.assertTrue(session_path(data, 9876).exists())
            remove_session(data, 9876, auth.instance)
            self.assertFalse(session_path(data, 9876).exists())

    def test_modified_session_does_not_load(self):
        with tempfile.TemporaryDirectory() as data:
            auth = LocalAuth()
            save_session(data, 9876, auth.token, auth.instance)
            path = session_path(data, 9876)
            envelope = json.loads(path.read_text())
            envelope["payload"] = "a" * 32
            path.write_text(json.dumps(envelope), encoding="utf-8")
            with self.assertRaises((OSError, ValueError, UnicodeError)):
                load_session(data, 9876)

    def test_tokens_are_per_launch_and_exact(self):
        first, second = LocalAuth(), LocalAuth()
        self.assertTrue(first.accepts("Bearer " + first.token))
        self.assertFalse(second.accepts("Bearer " + first.token))
        for value in (None, first.token, "Bearer \uffff", "Bearer " + first.token + "x"):
            self.assertFalse(first.accepts(value))

    def test_signature_binds_path_expiry_and_launch(self):
        auth = LocalAuth()
        path = "/api/media/" + "a" * 32
        with patch("local_auth.time.time", return_value=100):
            url = auth.media_url(path)
            query = url.split("?", 1)[1]
            self.assertTrue(auth.accepts_media(path, query))
            self.assertFalse(auth.accepts_media("/api/media/" + "b" * 32, query))
            self.assertFalse(LocalAuth().accepts_media(path, query))
            self.assertFalse(auth.accepts_media(path, query + "&expires=500"))
            self.assertFalse(auth.accepts_media(path, query.replace("expires=43300", "expires=43301")))
        with patch("local_auth.time.time", return_value=43301):
            self.assertFalse(auth.accepts_media(path, query))

    def test_plaintext_windows_session_is_never_accepted(self):
        if os.name != "nt":
            self.skipTest("Windows DPAPI policy")
        with tempfile.TemporaryDirectory() as data:
            path = session_path(data, 9876)
            path.write_text(json.dumps({"protection": "owner-only", "payload": "e30="}))
            with self.assertRaises(ValueError):
                load_session(data, 9876)


if __name__ == "__main__":
    unittest.main()

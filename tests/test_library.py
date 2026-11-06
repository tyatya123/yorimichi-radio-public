"""Regression coverage for malformed JSON IDs and existing library behavior."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from library import Library, validate_settings, read_metadata


class LibraryTests(unittest.TestCase):
    def test_unicode_id3_and_edit_do_not_modify_original(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            text = b"\x03" + "日本語の曲".encode("utf-8")
            frame = b"TIT2" + len(text).to_bytes(4, "big") + b"\0\0" + text
            size = bytes((len(frame) >> shift) & 127 for shift in (21, 14, 7, 0))
            original = b"ID3\x03\0\0" + size + frame + b"\xff\xfb\x90\0" * 100
            path = root / "original.mp3"
            path.write_bytes(original)
            self.assertEqual(read_metadata(path)[0], "日本語の曲")
            lib = Library(root / "data")
            first, fresh = lib.add_file(path)
            self.assertTrue(fresh)
            second, fresh = lib.add_file(path)
            self.assertFalse(fresh)
            self.assertEqual(first["id"], second["id"])
            lib.update_track({"id": first["id"], "tags": "爽やか", "comment": "お気に入り"})
            self.assertEqual(Library(root / "data").tracks()[0]["tags"], "爽やか")
            lib.clear()
            self.assertEqual(path.read_bytes(), original)

    def test_settings_persist_and_invalid_patch_is_atomic(self):
        with tempfile.TemporaryDirectory() as temp:
            lib = Library(temp)
            lib.update_settings({"city": "福岡", "latitude": 33.59, "longitude": 130.4})
            with self.assertRaises(ValueError):
                lib.update_settings({"city": "invalid", "latitude": float("nan")})
            self.assertEqual(Library(temp).settings["city"], "福岡")

    def test_zero_weights_and_unknown_keys_are_rejected(self):
        for patch in ({"topic_weights": {"weather": 0, "news": 0, "music": 0, "chat": 0}}, {"mystery": True}, {"shuffle": "false"}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                validate_settings(patch)

    def test_youtube_dedup_and_persistence(self):
        with tempfile.TemporaryDirectory() as temp:
            lib = Library(temp)
            one = lib.import_youtube("https://youtu.be/M7lc1UVf-VE")
            self.assertEqual(one["added"], 1)
            self.assertEqual(lib.import_youtube("https://www.youtube.com/watch?v=M7lc1UVf-VE")["added"], 0)
            self.assertEqual(Library(temp).tracks()[0]["video_id"], "M7lc1UVf-VE")


class TrackIdValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ai-radio-library-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.library = Library(self.root / "data")
        self.invalid_ids = (None, [], {}, 123, 1.5, True, b"bytes")

    def assert_invalid_ids_never_reach_sqlite(self, operation):
        for value in self.invalid_ids:
            with self.subTest(value=value):
                with patch.object(self.library, "connect", side_effect=AssertionError("invalid ID reached SQLite")):
                    with self.assertRaisesRegex(ValueError, "曲ID"):
                        operation(value)

    def test_track_rejects_non_string_id_before_sqlite(self):
        self.assert_invalid_ids_never_reach_sqlite(self.library.track)

    def test_media_rejects_non_string_id_before_sqlite(self):
        self.assert_invalid_ids_never_reach_sqlite(self.library.media_path)

    def test_remove_rejects_non_string_id_before_sqlite(self):
        self.assert_invalid_ids_never_reach_sqlite(self.library.remove)

    def test_update_rejects_non_string_id_before_sqlite(self):
        self.assert_invalid_ids_never_reach_sqlite(
            lambda value: self.library.update_track({"id": value, "title": "unchanged"})
        )

    def test_update_rejects_non_object_payload_and_missing_id(self):
        for payload in (None, [], "track", 12, {}):
            with self.subTest(payload=payload):
                with patch.object(self.library, "connect", side_effect=AssertionError("invalid payload reached SQLite")):
                    with self.assertRaises(ValueError):
                        self.library.update_track(payload)

    def test_unknown_string_ids_keep_not_found_and_delete_noop_behavior(self):
        for value in ("", "unknown", "' OR 1=1 --"):
            with self.subTest(value=value):
                with self.assertRaises(KeyError):
                    self.library.track(value)
                with self.assertRaises(KeyError):
                    self.library.media_path(value)
                with self.assertRaises(KeyError):
                    self.library.update_track({"id": value, "title": "unchanged"})
                self.library.remove(value)
        self.assertEqual(self.library.tracks(), [])

    def test_valid_id_edit_media_and_unregister_preserve_original(self):
        original = self.root / "original.wav"
        with wave.open(str(original), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(8000)
            audio.writeframes(b"\x00\x00" * 8)
        before = original.read_bytes()
        track, fresh = self.library.add_file(original)
        self.assertTrue(fresh)
        tid = track["id"]
        self.assertEqual(self.library.track(tid)["id"], tid)
        self.assertEqual(self.library.media_path(tid), original.resolve())
        updated = self.library.update_track({"id": tid, "title": "曲名", "comment": "メモ"})
        self.assertEqual(updated["title"], "曲名")
        self.assertEqual(updated["comment"], "メモ")
        self.library.remove(tid)
        self.assertEqual(self.library.tracks(), [])
        self.assertEqual(original.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()

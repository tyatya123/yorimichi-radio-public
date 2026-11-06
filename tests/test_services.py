"""Offline service tests plus a real loopback HTTP integration stub."""
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import re
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import urllib.error
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import radio_services as services


def wav_bytes():
    result = io.BytesIO()
    with wave.open(result, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24000)
        output.writeframes(b"\0\0" * 2400)
    return result.getvalue()


class StubHandler(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.requests.append(("GET", self.path, None))
        if self.path == "/speakers":
            self.send_data([{"name": "テスト音声", "styles": [{"id": 3, "name": "ノーマル"}]}])
        elif self.path == "/api/tags":
            self.send_data({"models": [{"name": "test:local"}, {"name": "test:cloud", "remote_host": "example.com"}]})
        else:
            self.send_error(404)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        body = json.loads(raw) if raw else None
        self.requests.append(("POST", self.path, body))
        if self.path.startswith("/audio_query?"):
            self.send_data({"accent_phrases": [], "speedScale": 1.0, "outputSamplingRate": 24000})
        elif self.path.startswith("/synthesis?"):
            self.send_data(wav_bytes())
        elif self.path == "/api/chat":
            user = body["messages"][1]["content"]
            topic = re.search(r"テーマ：([^。\n]+)", user).group(1)
            self.send_data({"message": {"role": "assistant", "content": f"{topic}について考えてみるのもよさそうです。ゆっくりと音楽を楽しみましょう。"}})
        else:
            self.send_error(404)

    def send_data(self, data):
        binary = isinstance(data, bytes)
        payload = data if binary else json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav" if binary else "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class URLSafetyTests(unittest.TestCase):
    def test_only_loopback_root_services(self):
        self.assertEqual(services._loopback_url("http://localhost:50021/"), "http://127.0.0.1:50021")
        self.assertEqual(services._loopback_url("http://[::1]:11434"), "http://[::1]:11434")
        for url in ["http://192.168.1.2:50021", "http://example.com", "file:///x", "http://a@localhost",
                    "http://localhost/api", "http://localhost?x=1", "http://localhost\n", "http://127.0.0.1#fragment"]:
            with self.subTest(url=url), self.assertRaises(services.ServiceError):
                services._loopback_url(url)

    def test_private_or_mixed_dns_is_rejected(self):
        def addresses(*ips):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in ips]
        for ips in [("127.0.0.1",), ("10.0.0.1",), ("169.254.169.254",), ("8.8.8.8", "192.168.1.2")]:
            with self.subTest(ips=ips), patch.object(services.socket, "getaddrinfo", return_value=addresses(*ips)):
                with self.assertRaises(services.ServiceError):
                    services._public_address("https://example.com/feed")
        with patch.object(services.socket, "getaddrinfo", return_value=addresses("8.8.8.8")):
            self.assertEqual(services._public_address("https://example.com/feed"), "8.8.8.8")
        for url in ["http://example.com/feed", "https://example.com:8443/feed", "ftp://example.com"]:
            with self.assertRaises(services.ServiceError):
                services._public_address(url)

    def test_dns_pinning_preserves_tls_hostname(self):
        context = Mock()
        context.verify_mode = services.ssl.CERT_REQUIRED
        context.check_hostname = True
        connection = services._PinnedHTTPSConnection("example.com", pinned_ip="8.8.8.8", context=context, timeout=3)
        sock = Mock()
        with patch.object(services.socket, "create_connection", return_value=sock) as create:
            connection.connect()
        create.assert_called_once_with(("8.8.8.8", 443), 3, None)
        context.wrap_socket.assert_called_once_with(sock, server_hostname="example.com")

    def test_redirect_to_private_address_is_rejected_before_connect(self):
        error = urllib.error.HTTPError("https://example.com/feed", 302, "redirect", {"Location": "https://127.0.0.1/admin"}, io.BytesIO())
        opener = Mock()
        opener.open.side_effect = error
        def resolve(host, *_args, **_kwargs):
            address = "8.8.8.8" if host == "example.com" else "127.0.0.1"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]
        with patch.object(services.socket, "getaddrinfo", side_effect=resolve), patch.object(services.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(services.ServiceError):
                services._fetch("https://example.com/feed")
        self.assertEqual(opener.open.call_count, 1)

    def test_response_size_limit(self):
        response = Mock()
        response.headers = {"Content-Length": "99999999"}
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        with patch.object(services.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(services.ServiceError):
                services._fetch("http://127.0.0.1:50021/speakers", scope="local", limit=100)
        response.read.assert_not_called()


class FeedTests(unittest.TestCase):
    def test_rss_atom_and_missing_dates(self):
        rss = b'''<rss><channel><title>Source</title><item><title>Headline &amp; more</title>
        <link>https://example.com/a</link><pubDate>Thu, 05 Nov 2026 09:00:00 GMT</pubDate></item>
        <item><title>No date</title></item></channel></rss>'''
        items = services._parse_feed(rss, "https://example.com/rss")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["headline"], "Headline & more")
        self.assertEqual(items[0]["publisher"], "Source")
        atom = b'''<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom Source</title>
        <entry><title>Atom title</title><link rel="alternate" href="/post"/>
        <published>2026-11-05T09:00:00Z</published></entry></feed>'''
        items = services._parse_feed(atom, "https://example.com/rss")
        self.assertEqual(items[0]["url"], "https://example.com/post")

    def test_dtd_rejected_and_unsafe_links_are_not_displayed(self):
        with self.assertRaises(services.ServiceError):
            services._parse_feed(b'<!DOCTYPE a [<!ENTITY e "evil">]><rss/>', "https://example.com/rss")
        self.assertEqual(services._source_link("javascript:alert(1)", "https://example.com/rss"), "https://example.com/rss")
        self.assertEqual(services._source_link("http://127.0.0.1/secrets", "https://example.com/rss"), "https://example.com/rss")

    def test_clean_preserves_normal_text_entities_and_incomplete_markup(self):
        self.assertEqual(services._clean("  A <b>見出し</b> &amp; updates\n > old <unfinished  "),
                         "A 見出し & updates > old <unfinished")
        self.assertEqual(services._clean("before <<nested>after"), "before after")

    def test_large_unmatched_tags_are_bounded_in_an_isolated_process(self):
        # One-MiB input used to take quadratic CPU time. A short subprocess
        # deadline prevents a future regression from hanging the test runner.
        script = '''import sys
sys.path.insert(0, sys.argv[1])
import radio_services as service
payload = ('<rss><channel><title>Source</title><item><title><![CDATA[' + '<' * (1024 * 1024 - 512) +
           ']]></title><pubDate>Fri, 06 Nov 2026 00:00:00 +0900</pubDate></item></channel></rss>').encode()
assert len(payload) < 1024 * 1024
entry = service._parse_feed(payload, 'https://example.com/rss')[0]
assert entry['headline'] == '<' * 500
assert service._clean('ordinary ' * (1024 * 128), 6000).startswith('ordinary')
print('bounded-ok')
'''
        result = subprocess.run([sys.executable, "-c", script, str(Path(services.__file__).parent)],
                                capture_output=True, text=True, timeout=3, check=True)
        self.assertEqual(result.stdout.strip(), "bounded-ok")

    def test_dtd_rejected_by_parser_in_utf8_and_utf16(self):
        declarations = [
            '<!DOCTYPE rss [<!ENTITY sample "must-not-expand">]>',
            '<!DOCTYPE rss [<!ENTITY sample SYSTEM "file:///nonexistent-security-probe">]>',
            '<!DOCTYPE rss SYSTEM "https://example.invalid/external.dtd">',
        ]
        for encoding, xml_encoding in (("utf-8", "UTF-8"), ("utf-8-sig", "UTF-8"),
                                       ("utf-16", "UTF-16"), ("utf-16-le", "UTF-16"), ("utf-16-be", "UTF-16")):
            for declaration in declarations:
                xml = f'<?xml version="1.0" encoding="{xml_encoding}"?>{declaration}<rss><channel><title>Source</title></channel></rss>'
                with self.subTest(encoding=encoding, declaration=declaration), self.assertRaisesRegex(services.ServiceError, "DTD"):
                    services._parse_feed(xml.encode(encoding), "https://example.com/rss")

    def test_utf16_rss_and_atom_without_dtd_keep_normal_display(self):
        for encoding in ("utf-16", "utf-16-le", "utf-16-be"):
            for atom in (False, True):
                if atom:
                    body = ('<feed xmlns="http://www.w3.org/2005/Atom"><title>配信元</title><entry>'
                            '<title><![CDATA[新しい <b>見出し</b> &amp; 情報]]></title><link href="/story"/>'
                            '<published>2026-11-06T00:00:00+09:00</published></entry></feed>')
                else:
                    body = ('<rss><channel><title>配信元</title><item><title><![CDATA[新しい <b>見出し</b> &amp; 情報]]></title>'
                            '<link>https://example.com/story</link><pubDate>Fri, 06 Nov 2026 00:00:00 +0900</pubDate>'
                            '</item></channel></rss>')
                # A declaration-like string in a comment is not an actual DTD.
                xml = '<?xml version="1.0" encoding="UTF-16"?><!-- <!DOCTYPE example> -->' + body
                with self.subTest(encoding=encoding, atom=atom):
                    result = services._parse_feed(xml.encode(encoding), "https://example.com/rss")
                    self.assertEqual(result[0]["headline"], "新しい 見出し & 情報")
                    self.assertEqual(result[0]["publisher"], "配信元")
                    self.assertEqual(result[0]["url"], "https://example.com/story")


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.radio = services.RadioServices(Path(self.temp.name))
        self.settings = {"timezone": "Asia/Tokyo", "voicevox_url": "http://127.0.0.1:50021", "llm_enabled": False}

    def prepare_without_audio(self, category, settings=None):
        with patch.object(self.radio, "_synthesize", side_effect=services.ServiceError("offline")):
            return self.radio.prepare_talk(settings or self.settings, {"title": "前の曲"}, {"title": "次の曲"}, category)

    def test_offline_chat_and_history_survive_restart(self):
        first = self.prepare_without_audio("chat")
        second = self.prepare_without_audio("chat")
        self.assertNotEqual(first["text"], second["text"])
        self.assertEqual(first["engine"], "browser")
        self.assertEqual(first["generation_engine"], "template")
        self.assertEqual(first["model"], "")
        self.assertIsNone(first["audio_url"])
        self.assertIn("ブラウザ", first["warning"])
        other = services.RadioServices(Path(self.temp.name))
        self.assertEqual(len(other._history), 2)

    def test_missing_weather_falls_back_without_claiming_live_conditions(self):
        with patch.object(services, "_json_fetch", side_effect=services.ServiceError("offline")):
            result = self.prepare_without_audio("weather")
        self.assertEqual(result["category"], "chat")
        self.assertEqual(result["sources"], [])
        self.assertIn("切り替え", result["warning"])

    def test_music_uses_only_registered_metadata(self):
        text, facts = self.radio._music({"title": "Title", "artist": "Artist", "tags": ["夜向け"], "comment": "お気に入り"}, {"title": "Next"})
        self.assertIn("あなたのメモ", text)
        self.assertIn("お気に入り", text)
        self.assertIn("Next", text)
        self.assertEqual(facts["user_tags"], ["夜向け"])
        self.assertNotIn("メロディー", text)

    def test_weather_requires_todays_date_and_recent_timestamp(self):
        now = datetime.now(services._tz("Asia/Tokyo"))
        data = {"current": {"time": now.isoformat(), "temperature_2m": 18.2, "weather_code": 3},
                "daily": {"time": [now.date().isoformat()], "temperature_2m_max": [21], "temperature_2m_min": [14],
                          "precipitation_probability_max": [40]}}
        with patch.object(services, "_json_fetch", return_value=data):
            text, sources, _facts = self.radio._weather(self.settings)
        self.assertIn("18.2度", text)
        self.assertIn("40パーセント", text)
        self.assertEqual(sources[0]["url"], "https://open-meteo.com/")
        data["daily"]["time"] = [(now.date() - timedelta(days=1)).isoformat()]
        with self.assertRaises(services.ServiceError):
            self.radio._weather(self.settings)

    def test_news_only_today_never_llm_and_never_repeats(self):
        now = datetime.now(timezone.utc)
        old = now - timedelta(days=3)
        rss = (f'<rss><channel><title>配信元</title><item><title>本日の見出し</title><link>https://example.com/a</link>'
               f'<pubDate>{now.isoformat()}</pubDate></item><item><title>古い見出し</title>'
               f'<pubDate>{old.isoformat()}</pubDate></item></channel></rss>').encode("utf-8")
        settings = dict(self.settings, news_feeds=["https://example.com/rss"], llm_enabled=True)
        with patch.object(services, "_fetch", return_value=rss), patch.object(self.radio, "_rewrite", side_effect=lambda _s, _c, draft, _f: draft) as rewrite:
            first = self.prepare_without_audio("news", settings)
            second = self.prepare_without_audio("news", settings)
        self.assertEqual(first["category"], "news")
        self.assertIn("本日の見出し", first["text"])
        self.assertNotIn("古い見出し", first["text"])
        self.assertEqual(first["sources"][0]["url"], "https://example.com/a")
        # Fallback chat can use a model, but the headline was never rewritten.
        self.assertEqual(rewrite.call_count, 1)
        self.assertEqual(rewrite.call_args.args[1], "chat")
        self.assertEqual(second["category"], "chat")
        restored = services.RadioServices(Path(self.temp.name))
        self.assertEqual(len(restored._news_seen), 1)

    def test_invalid_model_response_preserves_template(self):
        settings = dict(self.settings, llm_enabled=True, ollama_model="local")
        with patch.object(self.radio, "_rewrite", side_effect=services.ServiceError("invalid")):
            result = self.prepare_without_audio("chat", settings)
        self.assertIn("定型トーク", result["warning"])
        self.assertEqual(result["generation_engine"], "template")

    def test_production_music_never_calls_llm_even_when_enabled(self):
        settings = dict(self.settings, llm_enabled=True, ollama_model="local")
        with patch.object(self.radio, "_rewrite") as generate:
            result = self.prepare_without_audio("music", settings)
        generate.assert_not_called()
        self.assertEqual(result["generation_engine"], "template")
        self.assertIn("前の曲", result["text"])
        self.assertIn("次の曲", result["text"])

    def test_busy_model_falls_back_quickly_without_a_second_generation(self):
        settings = dict(self.settings, llm_enabled=True, ollama_model="local")
        self.radio._llm_slot.acquire()
        try:
            started = time.monotonic()
            with patch.object(self.radio, "_rewrite_serial") as generate:
                result = self.prepare_without_audio("chat", settings)
            self.assertLess(time.monotonic() - started, 0.8)
            generate.assert_not_called()
            self.assertEqual(result["generation_engine"], "template")
        finally:
            self.radio._llm_slot.release()

    def test_fallback_cache_hits_are_offline_and_ignore_stale_track_names(self):
        def synthesize(_settings, _text, talk_id, **_kwargs):
            (self.radio.audio_dir / f"{talk_id}.wav").write_bytes(wav_bytes())
        with patch.object(self.radio, "_synthesize", side_effect=synthesize) as synth:
            first = self.radio.prepare_fallback(self.settings, {"title": "stale title"}, {"title": "stale next"})
            started = time.monotonic()
            with patch.object(services, "_fetch", side_effect=AssertionError("cache must not contact network")):
                second = self.radio.prepare_fallback(self.settings, {"title": "another title"}, {})
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertEqual(synth.call_count, 1)
            self.assertEqual(first["audio_url"], second["audio_url"])
            self.assertEqual(second["engine"], "voicevox")
            self.assertNotIn("stale", second["text"])
            self.assertEqual(second["generation_engine"], "template")
            self.radio.prepare_fallback(dict(self.settings, speaker_id=4))
            self.assertEqual(synth.call_count, 2)

    def test_fallback_busy_warmup_returns_browser_without_waiting(self):
        self.radio._fallback_lock.acquire()
        try:
            with patch.object(self.radio, "_synthesize") as synth:
                result = self.radio.prepare_fallback(self.settings)
            synth.assert_not_called()
            self.assertEqual(result["engine"], "browser")
        finally:
            self.radio._fallback_lock.release()

    def test_sentence_shortening_does_not_cut_a_sentence_in_half(self):
        text = "気分に合わせて曲を選ぶ時間も楽しそうですね。" * 5
        fitted = self.radio._fit_script(text, 65)
        self.assertLessEqual(len(fitted), 65)
        self.assertTrue(fitted.endswith("。"))
        self.assertTrue(text.startswith(fitted))

    def test_incomplete_reasoning_never_becomes_speech(self):
        with self.assertRaises(services.ServiceError):
            self.radio._script_text("<think>この原稿をどのように作るか考える途中です。")
        self.assertEqual(self.radio._script_text("<think>内部の検討</think>曲の合間にひと休み。"), "曲の合間にひと休み。")

    def test_private_rewrite_cannot_generate_music(self):
        with patch.object(services, "_json_fetch") as fetch:
            with self.assertRaises(services.ServiceError):
                self.radio._rewrite(dict(self.settings, ollama_model="test:local"), "music", "曲名", {})
        fetch.assert_not_called()

    def test_chat_uses_plain_topic_prompt_without_previous_scripts(self):
        self.radio._history = [{"category": "chat", "chat_id": "窓辺",
                                "text": "夕方の風が窓をかすめます。前回だけの長い文章をここには含めます。"}]
        responses = [{"models": [{"name": "test:local"}]},
                     {"response": "紙にひとこと書き留めるなら、今日は何を選びますか。気になる言葉を残すのも楽しそうですね。"}]
        with patch.object(services, "_json_fetch", side_effect=responses) as fetch:
            text = self.radio._rewrite(dict(self.settings, ollama_model="test:local"), "chat", "手書きについてのヒント", {"topic": "手書き"})
        body = fetch.call_args.kwargs["body"]
        self.assertTrue(fetch.call_args.args[0].endswith("/api/chat"))
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertIn("です・ます調", body["messages"][0]["content"])
        prompt = body["messages"][1]["content"]
        self.assertIn("テーマ：手書き", prompt)
        self.assertNotIn("前回だけの長い文章", prompt)
        self.assertNotIn("recent", prompt)
        self.assertIn("紙", text)

    def test_wrong_topic_or_unstated_current_time_is_not_accepted(self):
        self.assertFalse(self.radio._on_topic("今は夕方です。曲名を考えてみましょう。", "chat", "曲名", []))
        self.assertFalse(self.radio._on_topic("飲み物を用意するのもいいですね。", "chat", "手書き", []))
        self.assertTrue(self.radio._on_topic("もし朝に曲名を考えるなら、どんな名前が浮かぶでしょう。", "chat", "曲名", []))

    def test_chat_rejects_diaries_but_allows_general_conditional_scenes(self):
        for text in ["昨日、お茶を飲んだ。飲み物の話です。", "今朝、飲み物を買いました。",
                     "私が飲み物を選びました。", "今は雨です。飲み物を用意しましょう。"]:
            with self.subTest(text=text):
                self.assertFalse(self.radio._on_topic(text, "chat", "飲み物"))
        self.assertTrue(self.radio._on_topic("冬の夜に、温かい紅茶を飲むと、心も体もほっとします。ゆっくりと味わうのもいいですね。", "chat", "飲み物"))
        self.assertTrue(self.radio._on_topic("飲み物を一つ選ぶなら、どのカップを使いますか。お気に入りで過ごすのも楽しそうですね。", "chat", "飲み物"))

    def test_chat_does_not_introduce_an_invented_presenter(self):
        for text in ["おはようございます、みきです。温かい紅茶もいいですね。",
                     "こんにちは。飲み物の時間です。", "こんばんは。紅茶はいかがですか。",
                     "みきです。飲み物を用意しましょう。", "DJのみきです。紅茶もいいですね。"]:
            with self.subTest(text=text):
                self.assertFalse(self.radio._on_topic(text, "chat", "飲み物"))
        self.assertTrue(self.radio._on_topic("温かい紅茶を選ぶなら、お気に入りのカップも使いたいですね。", "chat", "飲み物"))

    def test_reject_cloud_before_generation(self):
        with patch.object(services, "_json_fetch") as network:
            with self.assertRaises(services.ServiceError):
                self.radio._rewrite(dict(self.settings, ollama_model="model:cloud"), "chat", "原稿", {})
        network.assert_not_called()

    def test_audio_pruning_keeps_fifty_and_does_not_delete_unrelated_files(self):
        for index in range(55):
            (self.radio.audio_dir / f"{index:032x}.wav").write_bytes(b"test")
        unrelated = self.radio.audio_dir / "my-recording.wav"
        unrelated.write_bytes(b"mine")
        self.radio._prune_audio()
        self.assertTrue(unrelated.exists())
        self.assertLessEqual(len(list(self.radio.audio_dir.glob("*.wav"))), 51)


class LoopbackIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.radio = services.RadioServices(Path(self.temp.name))
        base = f"http://127.0.0.1:{self.server.server_port}"
        self.settings = {"voicevox_url": base, "ollama_url": base, "ollama_model": "test:local",
                         "speaker_id": 3, "speech_rate": 1.2, "talk_seconds": 30, "llm_enabled": True}
        StubHandler.requests.clear()

    def test_status_generation_and_valid_wav_through_http(self):
        status = self.radio.status(self.settings)
        self.assertTrue(status["voicevox"]["ok"])
        self.assertEqual(status["voicevox"]["speakers"][0]["id"], 3)
        self.assertEqual(status["ollama"]["models"], ["test:local"])
        result = self.radio.prepare_talk(self.settings, {}, {}, "chat")
        self.assertEqual(result["engine"], "voicevox")
        self.assertEqual(result["generation_engine"], "ollama")
        self.assertEqual(result["model"], "test:local")
        self.assertTrue(result["text"].endswith("。"))
        self.assertTrue((self.radio.audio_dir / f"{result['id']}.wav").exists())
        self.assertEqual(result["audio_url"], f"/api/audio/{result['id']}.wav")
        synthesis = next(body for method, path, body in StubHandler.requests if path.startswith("/synthesis?"))
        self.assertEqual(synthesis["speedScale"], 1.2)
        generation = next(body for method, path, body in StubHandler.requests if path == "/api/chat")
        self.assertIn("命令", generation["messages"][0]["content"])
        self.assertIn("音源を実際に聴いた感想", generation["messages"][0]["content"])
        self.assertFalse(generation["stream"])
        self.assertFalse(generation["think"])
        self.assertEqual(generation["options"]["num_ctx"], 2048)
        self.assertEqual(generation["keep_alive"], "3m")

    def test_fallback_warmup_and_cached_return_through_http(self):
        first = self.radio.prepare_fallback(self.settings)
        self.assertEqual(first["engine"], "voicevox")
        self.assertFalse(any(path == "/api/chat" for _method, path, _body in StubHandler.requests))
        calls = len(StubHandler.requests)
        second = self.radio.prepare_fallback(self.settings)
        self.assertEqual(len(StubHandler.requests), calls)
        self.assertEqual(first["audio_url"], second["audio_url"])


if __name__ == "__main__":
    unittest.main()

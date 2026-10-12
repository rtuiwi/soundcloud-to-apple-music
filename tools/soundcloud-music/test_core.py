"""Offline checks: never download audio or launch Music."""
import http.client
import json
import subprocess
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from core import Cancelled, MusicLibrary, normalize_profile
from server import LocalServer


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.library = MusicLibrary(self.temporary.name)
        # A loaded collection has a source URL; public builds start with an empty profile.
        self.library.profile = "https://soundcloud.com/artist/likes"
        self.track = {"id": "123", "url": "https://soundcloud.com/artist/track", "title": "Трек",
                      "artist": "Artist", "duration": None, "artwork": None, "status": "ready", "error": None}
        self.library.tracks = [self.track]
        self.library.sources = {"123": self.track["url"]}

    def tearDown(self):
        self.library.cancel()
        self.temporary.cleanup()

    def wait_job(self):
        deadline = time.monotonic() + 3
        while self.library.state()["job"]["active"] and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(self.library.state()["job"]["active"])

    def test_profile_rejects_remote_hosts_and_normalizes_tracking(self):
        self.assertEqual(normalize_profile("https://soundcloud.com/user-1/?utm_source=x"), "https://soundcloud.com/user-1/likes")
        for url in ("https://soundcloud.com.evil/hello", "https://soundcloud.com@evil/hello", "http://127.0.0.1/a", "https://soundcloud.com:123/a", "https://soundcloud.com/a/../b"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                normalize_profile(url)

    def test_forged_ids_are_not_downloadable(self):
        with self.assertRaises(ValueError):
            self.library.transfer(["999"], "Playlist", True)

    def test_playlist_entries_keep_rich_metadata_and_access_policy(self):
        from yt_dlp import YoutubeDL as RealDownloader
        class FixtureDownloader(RealDownloader):
            def extract_info(self, url, download=False, ie_key=None):
                if "/sets/" not in url:
                    return {"title": "Likes", "entries": [{"id": "456", "ie_key": "SoundcloudSet", "url": "https://soundcloud.com/artist/sets/album"}]}
                extractor = self.get_info_extractor(ie_key)
                extractor._CLIENT_ID = "fixture"
                extractor._call_api = lambda *args, **kwargs: []
                return extractor._extract_set({"id": 456, "title": "Playlist", "tracks": [
                    {"id": 123, "title": "Actual title", "duration": 191632, "permalink_url": "https://soundcloud.com/artist/actual-title", "policy": "BLOCK", "user": {"username": "Actual artist"}},
                    {"id": 124, "permalink_url": "https://soundcloud.com/artist/unknown-song"}]})
        with patch("yt_dlp.YoutubeDL", FixtureDownloader):
            self.library.load("https://soundcloud.com/artist/likes")
            self.wait_job()
        self.assertEqual(len(self.library.tracks), 2, self.library.job["message"])
        self.assertEqual(self.library.tracks[0]["title"], "Actual title")
        self.assertEqual(self.library.tracks[0]["artist"], "Actual artist")
        self.assertAlmostEqual(self.library.tracks[0]["duration"], 191.632)
        self.assertEqual(self.library.tracks[0]["status"], "unavailable")
        self.assertEqual(self.library.tracks[1]["title"], "unknown song")
        self.assertEqual(self.library.tracks[1]["artist"], "artist")
        self.assertEqual(self.library.tracks[1]["status"], "ready")

    def test_import_retry_reuses_file_and_persists_success(self):
        self.library.download_dir.mkdir()
        audio = self.library.download_dir / "123.mp3"
        audio.write_bytes(b"offline fixture")
        self.library.manifest["123"] = {"path": str(audio), "imported": False}
        with patch.object(self.library, "_download", side_effect=AssertionError("should reuse")), patch.object(self.library, "_import", side_effect=RuntimeError("denied")):
            self.library.transfer(["123"], "Playlist", True)
            self.wait_job()
        self.assertEqual(self.track["status"], "error")
        self.assertIn("Не удалось", self.library.job["message"])
        self.assertTrue(audio.is_file())
        with patch.object(self.library, "_download", side_effect=AssertionError("should reuse")), patch.object(self.library, "_import") as importer:
            self.library.transfer(["123"], 'Playlist "quoted"', True)
            self.wait_job()
            importer.assert_called_once_with(audio, 'Playlist "quoted"', self.track["url"])
        restored = MusicLibrary(self.temporary.name)
        self.assertTrue(restored.manifest["123"]["imported"])
        self.assertEqual(restored.tracks[0]["status"], "imported")
        with patch.object(restored, "_import", side_effect=AssertionError("duplicate")), patch.object(restored, "_download", side_effect=AssertionError("duplicate download")):
            restored.transfer(["123"], "Playlist", True)
            deadline = time.monotonic() + 3
            while restored.job["active"] and time.monotonic() < deadline:
                time.sleep(.01)
        self.assertFalse(restored.job["active"])
        self.assertEqual(restored.job["results"][0]["status"], "already_imported")

    def test_manifest_cannot_reuse_file_outside_downloads(self):
        outside = Path(self.temporary.name) / "private.mp3"
        outside.write_bytes(b"private")
        self.assertIsNone(self.library._record_file({"path": str(outside)}))

    def test_preview_formats_filtered_and_metadata_updated(self):
        owner = self
        class FakeDownloader:
            def __init__(self, options):
                self.options = options
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def extract_info(self, source, download=False):
                return {"id": "123", "title": "Rich title", "artists": ["Rich artist"], "duration": 123,
                        "formats": [{"format_id": "hls_mp3_preview"}, {"format_id": "http_mp3_full"}]}
            def process_ie_result(self, info, download):
                owner.assertEqual([item["format_id"] for item in info["formats"]], ["http_mp3_full"])
                path = Path(self.options["outtmpl"].replace("%(ext)s", "mp3"))
                path.write_bytes(b"offline fixture")
                self.options["progress_hooks"][0]({"status": "finished", "filename": str(path)})
                return info
        def fake_ffmpeg(command):
            Path(command[-1]).write_bytes(b"tagged fixture")
        with patch.dict("sys.modules", {"yt_dlp": types.SimpleNamespace(YoutubeDL=FakeDownloader)}), patch("core.ffmpeg_path", return_value="ffmpeg"), patch.object(self.library, "_thermal_guard"), patch.object(self.library, "_run", side_effect=fake_ffmpeg):
            path = self.library._download(self.track, self.track["url"])
        self.assertTrue(path.is_file())
        self.assertEqual(self.track["artist"], "Rich artist")
        self.assertEqual(self.track["duration"], 123)

    def test_music_requires_positive_import_confirmation(self):
        for output in ("", "0\n", "missing value\n", "OK:\n", "OK:garbage\n", "OK:1234\n"):
            with self.subTest(output=output), patch("core.sys.platform", "darwin"), patch("core.subprocess.run", return_value=subprocess.CompletedProcess([], 0, output, "")) as run:
                with self.assertRaises(RuntimeError):
                    self.library._import(Path('/tmp/a "quoted".mp3'), 'x "playlist"')
        self.assertEqual(run.call_args.args[0][-3:], ['/tmp/a "quoted".mp3', 'x "playlist"', ""])

    def download_fixture(self, suffix, events, options_seen):
        class FixtureDownloader:
            def __init__(self, options):
                self.options = options
                options_seen.update(options)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def extract_info(self, source, download=False):
                return {"id": "123", "formats": [{"format_id": "http_full"}]}
            def process_ie_result(self, info, download):
                events.append("network")
                raw = Path(self.options["outtmpl"].replace("%(ext)s", suffix))
                raw.write_bytes(b"downloaded fixture")
                self.options["progress_hooks"][0]({"status": "finished", "filename": str(raw)})
                return info
        return types.SimpleNamespace(YoutubeDL=FixtureDownloader)

    def test_compatible_audio_copy_is_not_blocked_by_thermal_guard(self):
        events, options_seen = [], {}
        def copy(command):
            events.append("copy")
            self.assertEqual(command[command.index("-c:a") + 1], "copy")
            Path(command[-1]).write_bytes(b"copied fixture")
        with patch.dict("sys.modules", {"yt_dlp": self.download_fixture("m4a", events, options_seen)}), patch("core.ffmpeg_path", return_value="ffmpeg"), patch.object(self.library, "_thermal_guard", side_effect=AssertionError("fair must not block network/copy")) as guard, patch.object(self.library, "_run", side_effect=copy):
            audio = self.library._download(self.track, self.track["url"])
        self.assertTrue(audio.is_file())
        self.assertEqual(events, ["network", "copy"])
        guard.assert_not_called()
        self.assertEqual(options_seen["concurrent_fragment_downloads"], 1)
        self.assertEqual(options_seen["ratelimit"], 2 * 1024 * 1024)

    def test_reencoding_is_guarded_before_and_after_and_single_threaded(self):
        events, options_seen = [], {}
        def guard():
            events.append("guard")
        def encode(command):
            self.assertEqual(events, ["network", "guard"])
            self.assertEqual(command[command.index("-c:a") + 1], "libmp3lame")
            self.assertEqual(command[command.index("-threads") + 1], "1")
            events.append("encode")
            Path(command[-1]).write_bytes(b"encoded fixture")
        with patch.dict("sys.modules", {"yt_dlp": self.download_fixture("webm", events, options_seen)}), patch("core.ffmpeg_path", return_value="ffmpeg"), patch.object(self.library, "_thermal_guard", side_effect=guard), patch.object(self.library, "_run", side_effect=encode):
            audio = self.library._download(self.track, self.track["url"])
        self.assertTrue(audio.is_file())
        self.assertEqual(events, ["network", "guard", "encode", "guard"])
        events.clear()
        with patch.dict("sys.modules", {"yt_dlp": self.download_fixture("webm", events, options_seen)}), patch("core.ffmpeg_path", return_value="ffmpeg"), patch.object(self.library, "_thermal_guard", side_effect=Cancelled()), patch.object(self.library, "_run", side_effect=AssertionError("encoder cannot start while guard waits")) as encoder:
            with self.assertRaises(Cancelled):
                self.library._download(self.track, self.track["url"])
        self.assertEqual(events, ["network"])
        encoder.assert_not_called()

    def test_music_accepts_persistent_id_acknowledgement(self):
        result = subprocess.CompletedProcess([], 0, "OK:0123456789ABCDEF\n", "")
        with patch("core.sys.platform", "darwin"), patch("core.subprocess.run", return_value=result) as run:
            acknowledgement = self.library._import(Path('/tmp/a "quoted".mp3'), 'x "playlist"', self.track["url"])
        self.assertEqual(acknowledgement, "OK:0123456789ABCDEF")
        command = run.call_args.args[0]
        self.assertEqual(command[-3:], ['/tmp/a "quoted".mp3', 'x "playlist"', self.track["url"]])

    def test_music_auth_error_is_distinguished_from_import_failure(self):
        for detail, expected, forbidden in (("execution error (-1743)", "Автоматизация", None),
                                             ("Музыка не добавила аудиофайл. (-2700)", "Файл сохранён", "Автоматизация")):
            with self.subTest(detail=detail), patch("core.sys.platform", "darwin"), patch("core.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", detail)):
                with self.assertRaises(RuntimeError) as raised:
                    self.library._import(Path('/tmp/a.mp3'), 'Playlist')
                self.assertIn(expected, str(raised.exception))
                if forbidden:
                    self.assertNotIn(forbidden, str(raised.exception))

    def test_drm_download_is_unavailable_and_never_imported(self):
        with patch.object(self.library, "_download", side_effect=RuntimeError("This song is DRM protected")), patch.object(self.library, "_import", side_effect=AssertionError("DRM must not import")):
            self.library.transfer(["123"], "Playlist", True)
            self.wait_job()
        self.assertEqual(self.track["status"], "unavailable")
        self.assertIn("защищён DRM", self.track["error"])
        self.assertEqual(self.library.job["results"][0]["status"], "unavailable")
        self.assertNotIn("123", self.library.manifest)

    def test_preview_only_never_downloads_audio(self):
        class PreviewDownloader:
            def __init__(self, options):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def extract_info(self, source, download=False):
                return {"id": "123", "formats": [{"format_id": "hls_mp3_128_preview"}]}
            def process_ie_result(self, *args, **kwargs):
                raise AssertionError("A preview must never download")
        with patch.dict("sys.modules", {"yt_dlp": types.SimpleNamespace(YoutubeDL=PreviewDownloader)}), patch("core.ffmpeg_path", return_value="ffmpeg"), patch.object(self.library, "_thermal_guard"):
            with self.assertRaisesRegex(ValueError, "превью"):
                self.library._download(self.track, self.track["url"])

    def test_cancel_before_import_keeps_download_for_retry(self):
        self.library.download_dir.mkdir()
        audio = self.library.download_dir / "123.mp3"
        audio.write_bytes(b"offline fixture")
        def finished_download(*args):
            self.library.manifest["123"] = {"path": str(audio), "imported": False}
            self.library._save()
            self.library.cancel()
            return audio
        with patch.object(self.library, "_download", side_effect=finished_download), patch.object(self.library, "_import", side_effect=AssertionError("cancelled")):
            self.library.transfer(["123"], "Playlist", True)
            self.wait_job()
        self.assertTrue(self.library.job["cancelled"])
        self.assertEqual(self.track["status"], "downloaded")
        self.assertTrue(audio.is_file())

    def test_cookies_only_copy_soundcloud_and_preserve_source(self):
        selected = Path(self.temporary.name) / "exported.txt"
        content = "# Netscape HTTP Cookie File\n.google.com\tTRUE\t/\tTRUE\t0\tsecret\tgoogle\n#HttpOnly_.soundcloud.com\tTRUE\t/\tTRUE\t0\toauth\tsoundcloud\n"
        selected.write_text(content)
        result = subprocess.CompletedProcess([], 0, str(selected) + "\n", "")
        with patch("core.sys.platform", "darwin"), patch("core.subprocess.run", return_value=result):
            self.library.choose_cookies()
        copied = Path(self.library.cookies)
        self.assertNotIn("google", copied.read_text())
        self.assertIn("soundcloud", copied.read_text())
        self.assertEqual(copied.stat().st_mode & 0o777, 0o600)
        self.assertEqual(selected.read_text(), content)


class ServerTests(unittest.TestCase):
    def test_api_requires_token_and_local_host_origin(self):
        with tempfile.TemporaryDirectory() as directory:
            server = LocalServer(library=MusicLibrary(directory))
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            def request(headers=None, method="GET", body=None):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                connection.request(method, "/api/state" if method == "GET" else "/api/cancel", body=body, headers=headers or {})
                result = connection.getresponse()
                status, payload = result.status, json.loads(result.read())
                connection.close()
                return status, payload
            try:
                self.assertEqual(request()[0], 401)
                authorized = {"Authorization": "Bearer " + server.token}
                self.assertEqual(request(authorized)[0], 200)
                self.assertEqual(request({**authorized, "Origin": "https://evil.example"})[0], 403)
                self.assertEqual(request({**authorized, "Host": "evil.example"})[0], 403)
                self.assertEqual(request({**authorized, "Content-Type": "application/json", "Origin": server.origin}, "POST", "{}")[0], 200)
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=3)


if __name__ == "__main__":
    unittest.main()

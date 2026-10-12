"""Offline cover regressions; no network or Music library writes."""
import struct
import subprocess
import tempfile
import time
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch, MagicMock

from artwork import MAX_ARTWORK, apply_music_cover, checked_url, embed_cover, fetch_cover, image_type
from core import MusicLibrary


def png_fixture():
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + chunk(b"IEND", b"")


class ArtworkTests(unittest.TestCase):
    def test_music_cover_requires_positive_matching_track_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            cover = Path(directory) / "cover.png"
            cover.write_bytes(png_fixture())
            successful = subprocess.CompletedProcess([], 0, "ART:0123456789ABCDEF\n", "")
            with patch("artwork.subprocess.run", return_value=successful) as run, patch("artwork.time.sleep") as sleep:
                self.assertEqual(apply_music_cover(cover, "Playlist", "https://soundcloud.com/a/b", "0123456789ABCDEF"), "0123456789ABCDEF")
                run.assert_called_once()
                sleep.assert_not_called()
                self.assertEqual(run.call_args.args[0][-4:], ["Playlist", "https://soundcloud.com/a/b", "0123456789ABCDEF", "png"])
            for acknowledgement in ("", "ART:", "ART:FFFFFFFFFFFFFFFF"):
                with self.subTest(acknowledgement=acknowledgement), patch("artwork.subprocess.run", return_value=subprocess.CompletedProcess([], 0, acknowledgement, "")), patch("artwork.time.sleep") as sleep:
                    with self.assertRaises(RuntimeError):
                        apply_music_cover(cover, "Playlist", "https://soundcloud.com/a/b", "0123456789ABCDEF")
                    sleep.assert_not_called()

    def test_music_busy_retries_with_bounded_pauses_and_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            cover = Path(directory) / "cover.png"
            cover.write_bytes(png_fixture())
            busy = subprocess.CompletedProcess([], 1, "", "execution error: File some object is busy. (-47)")
            successful = subprocess.CompletedProcess([], 0, "ART:0123456789ABCDEF\n", "")
            with patch("artwork.subprocess.run", side_effect=[busy, busy, busy, successful]) as run, patch("artwork.time.sleep") as sleep:
                self.assertEqual(apply_music_cover(cover, "Playlist", "https://soundcloud.com/a/b", "0123456789ABCDEF"), "0123456789ABCDEF")
                self.assertEqual(run.call_count, 4)
                self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4, 6])
            with patch("artwork.subprocess.run", return_value=busy) as run, patch("artwork.time.sleep") as sleep:
                with self.assertRaisesRegex(RuntimeError, "ещё обрабатывает файл") as error:
                    apply_music_cover(cover, "Playlist", "https://soundcloud.com/a/b")
                self.assertEqual(run.call_count, 4)
                self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4, 6])
                self.assertNotIn("-47", str(error.exception))

    def test_music_permanent_errors_are_not_retried_and_have_friendly_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            cover = Path(directory) / "cover.png"
            cover.write_bytes(png_fixture())
            for detail, message in (("execution error (-2700)", "не подтвердила обложку"),
                                     ("execution error (-1743)", "Автоматизация")):
                with self.subTest(detail=detail), patch("artwork.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", detail)) as run, patch("artwork.time.sleep") as sleep:
                    with self.assertRaisesRegex(RuntimeError, message) as error:
                        apply_music_cover(cover, "Playlist", "https://soundcloud.com/a/b")
                    run.assert_called_once()
                    sleep.assert_not_called()
                    self.assertNotIn("execution error", str(error.exception))

    def test_rejects_non_soundcloud_redirect_targets(self):
        for url in ("http://i1.sndcdn.com/x.jpg", "https://sndcdn.com.evil/x.jpg", "https://i1.sndcdn.com@evil/x.jpg", "https://127.0.0.1/x.jpg", "https://i1.sndcdn.com:99/x.jpg"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                checked_url(url)
        self.assertEqual(checked_url("https://i1.sndcdn.com/x-large.jpg"), "https://i1.sndcdn.com/x-large.jpg")

    def test_png_validation_rejects_corruption_and_truncation(self):
        valid = png_fixture()
        self.assertEqual(image_type(valid), "png")
        for data in (valid[:-1], valid[:40] + b"broken" + valid[46:], b"<html>not a cover</html>", b"\xff\xd8not a jpeg\xff\xd9"):
            with self.subTest(data=data[:10]), self.assertRaises(ValueError):
                image_type(data)

    def test_download_is_bounded_before_any_cache_write(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = "https://i1.sndcdn.com/x-t500x500.jpg"
        response.headers = {"Content-Length": str(MAX_ARTWORK + 1)}
        opener = MagicMock()
        opener.open.return_value = response
        with tempfile.TemporaryDirectory() as directory, patch("artwork.urllib.request.build_opener", return_value=opener):
            with self.assertRaisesRegex(ValueError, "15 МБ"):
                fetch_cover("https://i1.sndcdn.com/x-large.jpg", "123", directory)
            response.read.assert_not_called()
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_download_sniffs_format_and_reuses_cache(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = "https://i1.sndcdn.com/x-t500x500.jpg"
        response.headers = {}
        response.read.return_value = png_fixture()
        opener = MagicMock()
        opener.open.return_value = response
        with tempfile.TemporaryDirectory() as directory, patch("artwork.urllib.request.build_opener", return_value=opener):
            cover = fetch_cover("https://i1.sndcdn.com/x-large.jpg", "123", directory)
            self.assertEqual(cover.name, "123.png")
            self.assertEqual(cover.read_bytes(), png_fixture())
            self.assertEqual(fetch_cover("https://i1.sndcdn.com/x-large.jpg", "123", directory), cover)
            opener.open.assert_called_once()
            response.read.assert_called_once_with(MAX_ARTWORK + 1)

    def test_bad_image_or_tag_failure_does_not_modify_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "audio.m4a"
            audio.write_bytes(b"original audio bytes")
            cover = Path(directory) / "cover.jpg"
            cover.write_bytes(b"\xff\xd8invalid\xff\xd9")
            with self.assertRaises(ValueError):
                embed_cover(audio, cover)
            cover.write_bytes(png_fixture())
            with self.assertRaises(Exception):
                embed_cover(audio, cover)
            self.assertEqual(audio.read_bytes(), b"original audio bytes")
            self.assertFalse(list(Path(directory).glob(".artwork-*")))

    def test_mp3_embedding_preserves_audio_and_url_comment(self):
        from mutagen.id3 import ID3, COMM
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "audio.mp3"
            payload = b"audio payload remains unchanged"
            audio.write_bytes(payload)
            tags = ID3()
            tags.add(COMM(encoding=3, lang="eng", desc="", text=["https://soundcloud.com/a/b"]))
            tags.save(audio, v2_version=3)
            cover = Path(directory) / "cover.png"
            cover.write_bytes(png_fixture())
            self.assertTrue(embed_cover(audio, cover))
            updated = ID3(audio)
            self.assertEqual(updated.getall("APIC")[0].data, png_fixture())
            self.assertEqual(updated.getall("APIC")[0].mime, "image/png")
            self.assertEqual(updated.getall("COMM")[0].text, ["https://soundcloud.com/a/b"])
            self.assertTrue(audio.read_bytes().endswith(payload))

    def test_existing_import_updates_cover_without_reimport(self):
        with tempfile.TemporaryDirectory() as directory:
            library = MusicLibrary(directory)
            library.download_dir.mkdir()
            audio = library.download_dir / "123.mp3"
            audio.write_bytes(b"fixture")
            cover = Path(directory) / "cover.png"
            cover.write_bytes(png_fixture())
            source = "https://soundcloud.com/a/b"
            library.tracks = [{"id": "123", "title": "Track", "artist": "Artist", "url": source, "artwork": "https://i1.sndcdn.com/x.jpg", "status": "imported", "error": None}]
            library.sources = {"123": source}
            library.manifest["123"] = {"path": str(audio), "imported": True, "playlist": "Playlist", "music_persistent_id": "0123456789ABCDEF"}
            with patch("artwork.fetch_cover", return_value=cover), patch("artwork.embed_cover", return_value=True), patch("artwork.apply_music_cover", side_effect=RuntimeError("Cover failed")), patch.object(library, "_import", side_effect=AssertionError("duplicate import")), patch.object(library, "_download", side_effect=AssertionError("duplicate download")):
                library.transfer(["123"], "Playlist", True)
                deadline = time.monotonic() + 3
                while library.job["active"] and time.monotonic() < deadline:
                    time.sleep(.01)
            self.assertFalse(library.job["active"])
            self.assertTrue(library.manifest["123"]["imported"])
            self.assertEqual(library.tracks[0]["status"], "imported")
            self.assertEqual(library.job["results"][0]["status"], "already_imported")
            self.assertEqual(library.job["results"][0]["artwork_warning"], "Cover failed")
            self.assertTrue(MusicLibrary(directory).manifest["123"]["imported"])

    def test_new_import_is_persisted_before_cover_failure_and_cover_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            library = MusicLibrary(directory)
            library.download_dir.mkdir()
            audio = library.download_dir / "123.mp3"
            audio.write_bytes(b"fixture")
            artwork_directory = library.data_dir / "artwork"
            artwork_directory.mkdir()
            cover = artwork_directory / "123.png"
            cover.write_bytes(png_fixture())
            source = "https://soundcloud.com/a/b"
            library.tracks = [{"id": "123", "title": "Track", "artist": "Artist", "url": source, "artwork": "https://i1.sndcdn.com/x.jpg", "status": "downloaded", "error": None}]
            library.sources = {"123": source}
            library.manifest["123"] = {"path": str(audio), "imported": False}
            def fail_cover(*args):
                self.assertTrue(MusicLibrary(directory).manifest["123"]["imported"])
                raise RuntimeError("Cover failed")
            def wait():
                deadline = time.monotonic() + 3
                while library.job["active"] and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertFalse(library.job["active"])
            with patch("artwork.fetch_cover", return_value=cover), patch("artwork.embed_cover", return_value=True), patch("artwork.apply_music_cover", side_effect=fail_cover), patch.object(library, "_import", return_value="OK:0123456789ABCDEF") as importer:
                library.transfer(["123"], "Playlist", True)
                wait()
                importer.assert_called_once_with(audio, "Playlist", source)
            self.assertEqual(library.tracks[0]["status"], "imported")
            self.assertEqual(library.job["results"][0]["status"], "imported")
            with patch("artwork.apply_music_cover", return_value="0123456789ABCDEF") as setter, patch.object(library, "_import", side_effect=AssertionError("duplicate import")):
                library.transfer(["123"], "Playlist", True)
                wait()
                setter.assert_called_once_with(cover, "Playlist", source, "0123456789ABCDEF")
            self.assertTrue(library.manifest["123"]["music_artwork"])
            self.assertTrue(library.tracks[0]["music_artwork"])
            self.assertIsNone(library.tracks[0]["artwork_error"])
            self.assertEqual(library.tracks[0]["artwork_status"], "added")


if __name__ == "__main__":
    unittest.main()

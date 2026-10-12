"""Local SoundCloud selection/download service. No work happens on import."""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent.parent
DEFAULT_PROFILE = ""
SUPPORTED = {".mp3", ".m4a", ".aac", ".wav", ".aiff", ".aif"}


class Cancelled(Exception):
    pass


def normalize_profile(value):
    if not isinstance(value, str) or len(value) > 2000:
        raise ValueError("Укажите ссылку на профиль, лайки или плейлист SoundCloud.")
    value = value.strip()
    if value and "://" not in value:
        value = "https://" + value
    parsed = urlsplit(value)
    if (parsed.scheme not in ("http", "https") or parsed.hostname not in
            ("soundcloud.com", "www.soundcloud.com") or parsed.username or parsed.password
            or parsed.port not in (None, 80, 443)):
        raise ValueError("Разрешены только ссылки soundcloud.com.")
    parts = [part for part in parsed.path.split("/") if part]
    if not parts or any(not re.fullmatch(r"[A-Za-z0-9_-]+", part) for part in parts):
        raise ValueError("Нужна ссылка на конкретный профиль или плейлист.")
    if len(parts) == 1:
        parts.append("likes")
    elif not (len(parts) == 2 and parts[1] in ("likes", "sets") or
              len(parts) == 3 and parts[1] == "sets"):
        raise ValueError("Откройте профиль, его /likes или плейлист /sets/название.")
    return urlunsplit(("https", "soundcloud.com", "/" + "/".join(parts), "", ""))


def ffmpeg_path():
    return shutil.which("ffmpeg") or ("/opt/homebrew/bin/ffmpeg" if
                                      Path("/opt/homebrew/bin/ffmpeg").is_file() else None)


def safe_filename(value):
    return re.sub(r"[^\w\- .()]", "_", value, flags=re.UNICODE).strip(" .")[:100] or "track"


def unavailable(info):
    if info.get("has_drm") is True:
        return "Трек защищён DRM: сохранить его аудиофайл нельзя."
    if info.get("deleted") is True or info.get("state") in ("deleted", "failed"):
        return "Трек удалён или недоступен на SoundCloud."
    if info.get("is_preview") or info.get("policy") in ("SNIP", "BLOCK") or info.get("availability") in ("premium_only", "subscriber_only", "needs_auth", "private"):
        return "Полный трек недоступен: требуется доступ владельца или подписка. Превью пропущено."
    return None


def fallback_labels(source, identifier):
    parsed = urlsplit(source)
    parts = parsed.path.strip("/").split("/")
    if parsed.hostname in ("soundcloud.com", "www.soundcloud.com") and len(parts) == 2:
        return (unquote(parts[1]).replace("-", " ").replace("_", " "),
                unquote(parts[0]).replace("-", " ").replace("_", " "))
    return "Трек SoundCloud #" + identifier, "SoundCloud"


def preview_format(item):
    return (bool(item.get("is_preview")) or "preview" in str(item.get("format_id", "")).lower()
            or "preview" in str(item.get("format_note", "")).lower()
            or "/preview/" in str(item.get("url", "")))


class MusicLibrary:
    def __init__(self, data_dir=None):
        self.data_dir = Path(data_dir or BASE / "data").resolve()
        self.download_dir = self.data_dir / "downloads"
        self.manifest_path = self.data_dir / "manifest.json"
        self.collection_path = self.data_dir / "collection.json"
        self.lock = threading.RLock()
        self.cancel_event = threading.Event()
        self.cookies = None
        self.tracks = []
        self.sources = {}
        self.profile = DEFAULT_PROFILE
        self.profile_name = None
        self.playlist = "Из SoundCloud"
        self.job = self._idle()
        self.manifest = {}
        try:
            saved = json.loads(self.manifest_path.read_text())
            if isinstance(saved, dict):
                self.manifest = saved
        except (OSError, ValueError):
            pass
        try:
            saved = json.loads(self.collection_path.read_text())
            self.profile = normalize_profile(saved.get("profile", DEFAULT_PROFILE))
            self.profile_name = saved.get("profile_name")
            for item in saved.get("tracks", []):
                identifier = str(item.get("id", ""))
                if not re.fullmatch(r"[0-9]+", identifier):
                    continue
                source = self._source({"url": saved.get("sources", {}).get(identifier)}, identifier)
                record = self.manifest.get(identifier, {})
                item["status"] = "imported" if record.get("imported") else "downloaded" if self._record_file(record) else "unavailable" if item.get("status") == "unavailable" else "ready"
                if item["status"] != "unavailable":
                    item["error"] = None
                self.sources[identifier] = source
                self.tracks.append(item)
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        cookie_path = self.data_dir / "cookies.txt"
        if cookie_path.is_file():
            self.cookies = str(cookie_path)

    @staticmethod
    def _idle():
        return {"kind": "idle", "active": False, "total": 0, "completed": 0,
                "current": None, "message": "Готово к работе", "results": [], "cancelled": False}

    def state(self):
        with self.lock:
            return copy.deepcopy({"tracks": self.tracks, "job": self.job, "profile": self.profile,
                                  "profile_name": self.profile_name,
                                  "playlist": self.playlist, "download_dir": str(self.download_dir),
                                  "cookies": bool(self.cookies),
                                  "dependencies": {"yt_dlp": importlib.util.find_spec("yt_dlp") is not None,
                                                   "ffmpeg": ffmpeg_path() is not None,
                                                   "music": sys.platform == "darwin" and Path("/System/Applications/Music.app").exists()}})

    def _start(self, kind, work, total=0):
        with self.lock:
            if self.job["active"]:
                raise ValueError("Дождитесь окончания текущей операции или остановите её.")
            self.cancel_event.clear()
            self.job = {"kind": kind, "active": True, "total": total, "completed": 0,
                        "current": None, "message": "Начинаем…", "results": [], "cancelled": False}
            threading.Thread(target=self._worker, args=(work,), daemon=True).start()

    def _worker(self, work):
        try:
            work()
            with self.lock:
                if self.job["kind"] == "loading":
                    self.job["message"] = f"Найдено треков: {len(self.tracks)}"
        except Cancelled:
            with self.lock:
                self.job["cancelled"] = True
            self._message("Остановлено. Уже сохранённые файлы доступны для повторного импорта.")
        except Exception as error:
            self._message(str(error)[:1000])
        finally:
            try:
                self._save_collection()
            except OSError:
                pass
            with self.lock:
                self.job["active"] = False
                self.job["current"] = None

    def _message(self, message):
        with self.lock:
            self.job["message"] = message

    def _check_cancel(self):
        if self.cancel_event.is_set():
            raise Cancelled()

    def _pause(self, seconds):
        if self.cancel_event.wait(seconds):
            raise Cancelled()

    def cancel(self):
        self.cancel_event.set()

    def _options(self):
        result = {"quiet": True, "no_warnings": True, "socket_timeout": 20,
                  "retries": 2, "noplaylist": True, "logger": _QuietLogger()}
        if self.cookies:
            result["cookiefile"] = self.cookies
        return result

    def load(self, url):
        normalized = normalize_profile(url)
        def work():
            from yt_dlp import YoutubeDL
            from yt_dlp.extractor.soundcloud import SoundcloudSetIE, SoundcloudUserIE
            class RichSoundcloudUserIE(SoundcloudUserIE):
                """Reuse metadata already returned by likes pages; no per-track API requests."""
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, **kwargs)
                    self.rich_tracks = {}

                def _call_api(self, *args, **kwargs):
                    response = super()._call_api(*args, **kwargs)
                    if isinstance(response, dict):
                        for entry in response.get("collection") or []:
                            if not isinstance(entry, dict):
                                continue
                            candidate = entry.get("track") or entry
                            if (isinstance(candidate, dict) and candidate.get("id") and
                                    (entry.get("track") is not None or candidate.get("kind") == "track") and not candidate.get("tracks")):
                                self.rich_tracks[str(candidate["id"])] = candidate
                    return response

                def _entries(self, *args, **kwargs):
                    for entry in super()._entries(*args, **kwargs):
                        if entry and (raw := self.rich_tracks.pop(str(entry.get("id")), None)):
                            # Flat metadata extraction still raises for BLOCK;
                            # retain metadata and expose the real policy in UI.
                            details = self._extract_info_dict({**raw, "policy": "ALLOW"}, extract_flat=True)
                            details.pop("__post_extractor", None)
                            details.pop("formats", None)
                            yield {**entry, **details, "policy": raw.get("policy")}
                        else:
                            yield entry

                def _extract_thumbnails(self, info):
                    # The normal extractor HEAD-checks original artwork for every
                    # track. List previews use already-known artwork with no extra
                    # requests, keeping a large likes collection quick and cool.
                    artwork = info.get("artwork_url") or (info.get("user") or {}).get("avatar_url")
                    if not isinstance(artwork, str) or urlsplit(artwork).scheme != "https":
                        return None
                    artwork = re.sub(self._IMAGE_REPL_RE, "-t300x300.jpg", artwork)
                    return [{"url": artwork, "width": 300, "height": 300}]

            class RichSoundcloudSetIE(SoundcloudSetIE):
                _extract_thumbnails = RichSoundcloudUserIE._extract_thumbnails

                def _extract_set(self, playlist, token=None):
                    # The standard set extractor intentionally outputs only URLs.
                    # Restore titles, artists, duration and availability from the
                    # response it already fetched, requesting id-only stubs in
                    # batches rather than resolving every audio stream.
                    result = super()._extract_set(playlist, token)
                    raw_tracks = {str(item["id"]): item for item in playlist.get("tracks") or []
                                  if isinstance(item, dict) and item.get("id")}
                    missing = [identifier for identifier, item in raw_tracks.items() if not item.get("title")]
                    for offset in range(0, len(missing), 50):
                        self_library._check_cancel()
                        query = {"ids": ",".join(missing[offset:offset + 50])}
                        if token:
                            query.update({"playlistId": str(playlist["id"]), "playlistSecretToken": token})
                        fetched = self._call_api(self._API_V2_BASE + "tracks", str(playlist["id"]),
                                                 "Reading playlist track metadata", query=query,
                                                 headers=self._HEADERS, fatal=False)
                        for item in fetched if isinstance(fetched, list) else []:
                            if isinstance(item, dict) and item.get("id"):
                                raw_tracks[str(item["id"])] = item
                    for entry in result.get("entries") or []:
                        raw = raw_tracks.get(str(entry.get("id")))
                        if raw:
                            details = self._extract_info_dict({**raw, "policy": "ALLOW"}, extract_flat=True)
                            details.pop("__post_extractor", None)
                            details.pop("formats", None)
                            entry.update({key: value for key, value in details.items() if value is not None})
                            entry["policy"] = raw.get("policy")
                            entry["state"] = raw.get("state")
                            entry["deleted"] = raw.get("deleted")
                    return result
            self_library = self
            with self.lock:
                self.profile, self.tracks, self.sources, self.profile_name = normalized, [], {}, None
            self._message("Читаем сохранённые треки SoundCloud…")
            options = self._options()
            options.update({"extract_flat": "in_playlist", "lazy_playlist": True, "noplaylist": False})
            with YoutubeDL(options) as downloader:
                downloader.add_info_extractor(RichSoundcloudUserIE())
                downloader.add_info_extractor(RichSoundcloudSetIE())
                extractor_key = RichSoundcloudUserIE.ie_key() if len(urlsplit(normalized).path.strip("/").split("/")) == 2 else RichSoundcloudSetIE.ie_key()
                collection = downloader.extract_info(normalized, download=False, ie_key=extractor_key)
                if not collection:
                    raise ValueError("SoundCloud не вернул список. Проверьте ссылку и доступ к профилю.")
                with self.lock:
                    self.profile_name = collection.get("title")
                def entries(container, depth=0):
                    for item in container.get("entries") or []:
                        self._check_cancel()
                        if not item:
                            continue
                        if item.get("entries") is not None:
                            yield from entries(item, depth + 1)
                        elif (item.get("ie_key") == "SoundcloudSet" or "/sets/" in str(item.get("url", ""))) and depth < 3:
                            nested = downloader.extract_info(item.get("url"), download=False, ie_key=RichSoundcloudSetIE.ie_key())
                            if nested:
                                yield from entries(nested, depth + 1)
                        else:
                            yield item
                for item in entries(collection):
                    self._check_cancel()
                    if not item:
                        continue
                    identifier = str(item.get("id") or "")
                    if not re.fullmatch(r"[0-9]+", identifier):
                        continue
                    source = self._source(item, identifier)
                    if source is None:
                        continue
                    error = unavailable(item)
                    record = self.manifest.get(identifier, {})
                    status = "unavailable" if error else ("imported" if record.get("imported") else
                              "downloaded" if self._record_file(record) else "ready")
                    fallback_title, fallback_artist = fallback_labels(source, identifier)
                    track = {"id": identifier, "url": item.get("webpage_url") or source,
                             "title": item.get("title") or fallback_title,
                             "artist": item.get("artist") or ", ".join(item.get("artists") or []) or item.get("uploader") or fallback_artist,
                             "duration": item.get("duration"), "artwork": self._artwork(item),
                             "status": status, "error": error}
                    with self.lock:
                        if identifier in self.sources:
                            continue
                        self.sources[identifier] = source
                        self.tracks.append(track)
                        self.job["completed"] = len(self.tracks)
                        self.job["total"] = len(self.tracks)
                    self._message(f"Найдено треков: {len(self.tracks)}")
                    if len(self.tracks) % 25 == 0:
                        self._save_collection()
            if not self.tracks:
                raise ValueError("Список пуст. Войдите через cookies, если ваши сохранённые треки закрыты.")
        self._start("loading", work)

    @staticmethod
    def _source(item, identifier):
        for key in ("webpage_url", "permalink_url", "url"):
            value = item.get(key)
            if not isinstance(value, str):
                continue
            parsed = urlsplit(value)
            if (parsed.scheme == "https" and parsed.hostname in
                    ("soundcloud.com", "www.soundcloud.com", "api.soundcloud.com", "api-v2.soundcloud.com")
                    and not parsed.username and not parsed.password and parsed.port is None):
                if parsed.hostname in ("soundcloud.com", "www.soundcloud.com"):
                    parts = parsed.path.strip("/").split("/")
                    if len(parts) != 2 or parts[1] in ("sets", "likes", "tracks", "reposts", "albums", "spotlight"):
                        return None
                elif not re.fullmatch(r"/tracks/[0-9]+/?", parsed.path):
                    return None
                return value
        return "https://api.soundcloud.com/tracks/" + identifier

    @staticmethod
    def _artwork(item):
        value = item.get("thumbnail") or next((x.get("url") for x in reversed(item.get("thumbnails") or []) if x.get("url")), None)
        if isinstance(value, str) and urlsplit(value).scheme == "https":
            return value
        return None

    def transfer(self, ids, playlist, should_import):
        if not isinstance(ids, list) or not ids or len(ids) > 5000 or any(not isinstance(i, str) for i in ids):
            raise ValueError("Выберите хотя бы один трек.")
        ids = list(dict.fromkeys(ids))
        if not isinstance(playlist, str) or not playlist.strip() or len(playlist) > 200 or "\x00" in playlist:
            raise ValueError("Укажите название плейлиста (до 200 символов).")
        if not isinstance(should_import, bool):
            raise ValueError("Неверный параметр импорта.")
        with self.lock:
            if self.job["active"]:
                raise ValueError("Дождитесь окончания текущей операции или остановите её.")
            if any(identifier not in self.sources for identifier in ids):
                raise ValueError("Неизвестный трек. Загрузите список снова.")
            self.playlist = playlist.strip()
            destination_playlist = self.playlist
        def work():
            for index, identifier in enumerate(ids):
                self._check_cancel()
                track = next(t for t in self.tracks if t["id"] == identifier)
                with self.lock:
                    self.job["current"] = identifier
                try:
                    record = self.manifest.get(identifier, {})
                    if record.get("imported") and should_import:
                        self._message("Добавляем обложку: " + track["title"])
                        self._prepare_artwork(track, record, self._record_file(record), destination_playlist, True)
                        self._track(identifier, "imported", None)
                        outcome = "already_imported"
                    else:
                        file_path = self._record_file(record)
                        if not file_path:
                            if track["status"] == "unavailable":
                                raise ValueError(track["error"])
                            self._track(identifier, "downloading", None)
                            file_path = self._download(track, self.sources[identifier])
                            record = self.manifest.get(identifier, {"path": str(file_path), "title": track["title"], "artist": track["artist"], "imported": False})
                            self.manifest[identifier] = record
                            self._save()
                        self._prepare_artwork(track, record, file_path, destination_playlist, False)
                        self._track(identifier, "downloaded", None)
                        if should_import:
                            self._check_cancel()
                            self._track(identifier, "importing", None)
                            acknowledgement = self._import(file_path, destination_playlist, self.sources[identifier])
                            record.update({"imported": True, "playlist": destination_playlist})
                            if isinstance(acknowledgement, str) and re.fullmatch(r"OK:[0-9A-Fa-f]{16}", acknowledgement):
                                record["music_persistent_id"] = acknowledgement[3:]
                            # Persist the audio import before attempting optional artwork.
                            self._save()
                            self._prepare_artwork(track, record, file_path, destination_playlist, True)
                            self._track(identifier, "imported", None)
                        outcome = "imported" if should_import else "downloaded"
                    with self.lock:
                        result = {"id": identifier, "status": outcome}
                        if track.get("artwork_error"):
                            result["artwork_warning"] = track["artwork_error"]
                        self.job["results"].append(result)
                except Cancelled:
                    self._track(identifier, "downloaded" if self._record_file(self.manifest.get(identifier, {})) else "ready", None)
                    raise
                except Exception as error:
                    if self.cancel_event.is_set():
                        self._track(identifier, "downloaded" if self._record_file(self.manifest.get(identifier, {})) else "ready", None)
                        raise Cancelled() from error
                    message = str(error)[:1000]
                    is_unavailable = track["status"] == "unavailable"
                    if re.search(r"\bDRM\b|digital rights management", message, re.IGNORECASE):
                        is_unavailable = True
                        message = "Трек защищён DRM: сохранить его аудиофайл нельзя. Выберите другой трек."
                    status = "unavailable" if is_unavailable else "error"
                    self._track(identifier, status, message)
                    with self.lock:
                        self.job["results"].append({"id": identifier, "status": status, "error": message})
                with self.lock:
                    self.job["completed"] = index + 1
                if index + 1 < len(ids):
                    self._pause(3)
            errors = sum(r["status"] == "error" for r in self.job["results"])
            blocked = sum(r["status"] == "unavailable" for r in self.job["results"])
            message = f"Завершено. Не удалось обработать треков: {errors}. Их можно повторить." if errors else "Готово"
            if blocked:
                message += f" Недоступно на SoundCloud: {blocked}."
            artwork_warnings = sum(bool(r.get("artwork_warning")) for r in self.job["results"])
            if artwork_warnings:
                message += f" Обложки не добавлены для {artwork_warnings} треков; аудио сохранено."
            self._message(message)
        self._start("transfer", work, len(ids))

    def _track(self, identifier, status, error):
        with self.lock:
            for track in self.tracks:
                if track["id"] == identifier:
                    track.update({"status": status, "error": error})

    def _record_file(self, record):
        try:
            path = Path(record.get("path", "")).resolve()
            if path.is_relative_to(self.download_dir) and path.is_file() and path.stat().st_size:
                return path
        except (OSError, TypeError, ValueError):
            pass
        return None

    def _prepare_artwork(self, track, record, audio, playlist, should_import):
        """Artwork failures must never undo or misreport a successful audio import."""
        from artwork import apply_music_cover, embed_cover, fetch_cover, image_type, MAX_ARTWORK
        self._check_cancel()
        artwork_directory = self.data_dir / "artwork"
        cover = None
        warning = None
        try:
            cached = Path(record.get("artwork_file", "")).resolve()
            if cached.is_relative_to(artwork_directory) and cached.is_file() and cached.stat().st_size <= MAX_ARTWORK:
                image_type(cached.read_bytes())
                cover = cached
            elif track.get("artwork"):
                cover = fetch_cover(track["artwork"], track["id"], artwork_directory)
            if cover:
                record["artwork_file"] = str(cover)
                if audio and not record.get("artwork_embedded"):
                    try:
                        record["artwork_embedded"] = embed_cover(audio, cover)
                    except Exception as error:
                        warning = "Обложка сохранена, но не записана в аудиофайл: " + str(error)[:250]
                if should_import and record.get("imported") and not record.get("music_artwork"):
                    try:
                        music_id = apply_music_cover(cover, record.get("playlist") or playlist,
                                                     self.sources[track["id"]], record.get("music_persistent_id"))
                        record.update({"music_artwork": True, "music_persistent_id": music_id})
                    except Exception as error:
                        warning = str(error)[:500]
        except Exception as error:
            warning = "Не удалось получить обложку: " + str(error)[:350]
        record["artwork_error"] = warning
        with self.lock:
            track.update({"artwork_error": warning, "music_artwork": bool(record.get("music_artwork")),
                          "artwork_status": "error" if warning else "added" if record.get("music_artwork") else "embedded" if record.get("artwork_embedded") else "saved" if cover else "missing"})
        self._save()

    def _save(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.manifest_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.manifest, ensure_ascii=False, indent=2))
        os.chmod(temporary, 0o600)
        temporary.replace(self.manifest_path)

    def _save_collection(self):
        with self.lock:
            value = copy.deepcopy({"profile": self.profile, "profile_name": self.profile_name, "tracks": self.tracks, "sources": self.sources})
        self.data_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.collection_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
        os.chmod(temporary, 0o600)
        temporary.replace(self.collection_path)

    def _thermal_guard(self):
        """The launcher runs on the host; never use a sandbox reading as evidence."""
        while True:
            self._check_cancel()
            try:
                completed = subprocess.run([sys.executable, str(ROOT / "tools/laptop-load/thermal.py")],
                                           capture_output=True, text=True, timeout=8, check=True)
                label = json.loads(completed.stdout).get("thermal_label", "unknown")
            except (OSError, ValueError, subprocess.SubprocessError):
                label = "unknown"
            if label == "nominal":
                return
            if label not in ("fair", "serious", "critical"):
                self._message("Тепловая нагрузка недоступна. Осторожная пауза 90 секунд…")
                self._pause(90)
                return
            self._message(f"Тепловая нагрузка macOS: {label}. Ждём nominal…")
            self._pause(20)

    def _download(self, track, source):
        from yt_dlp import YoutubeDL
        executable = ffmpeg_path()
        if not executable:
            raise ValueError("Для сохранения музыки нужен ffmpeg. Установите его и перезапустите инструмент.")
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self._message("Скачиваем: " + track["title"])
        prefix = self.download_dir / (track["id"] + " - " + safe_filename(track["title"]))
        result_paths = []
        def progress(update):
            self._check_cancel()
            if update.get("status") == "finished":
                result_paths.append(update.get("filename"))
        options = self._options()
        options.update({"format": "bestaudio/best", "outtmpl": str(prefix) + ".%(ext)s",
                        "progress_hooks": [progress], "overwrites": False,
                        "concurrent_fragment_downloads": 1, "ratelimit": 2 * 1024 * 1024})
        with YoutubeDL(options) as downloader:
            info = downloader.extract_info(source, download=False)
            self._check_cancel()
            if info and (info.get("_type") in ("playlist", "multi_video") or "entries" in info or str(info.get("id")) != track["id"]):
                raise ValueError("Вместо выбранного трека SoundCloud вернул плейлист или другой объект.")
            if not info or unavailable(info):
                raise ValueError(unavailable(info or {}) or "Полный трек недоступен.")
            formats = info.get("formats") or [info]
            full_formats = [item for item in formats if not preview_format(item)]
            if not full_formats:
                raise ValueError("SoundCloud отдаёт только превью. Короткий фрагмент не будет импортирован.")
            if info.get("formats"):
                info["formats"] = full_formats
                info.pop("requested_formats", None)
                info.pop("requested_downloads", None)
            artist = info.get("artist") or ", ".join(info.get("artists") or []) or info.get("uploader") or track["artist"]
            with self.lock:
                track.update({"title": info.get("title") or track["title"], "artist": artist,
                              "duration": info.get("duration") or track["duration"],
                              "artwork": self._artwork(info) or track["artwork"]})
            info = downloader.process_ie_result(info, download=True)
            raw = Path(result_paths[-1] if result_paths else downloader.prepare_filename(info))
        self._check_cancel()
        if not raw.is_file() or not raw.stat().st_size:
            raise ValueError("SoundCloud не вернул аудиофайл.")
        suffix = ".m4a" if raw.suffix.lower() == ".aac" else raw.suffix.lower() if raw.suffix.lower() in SUPPORTED else ".mp3"
        final = prefix.with_name(prefix.name + suffix)
        temporary = prefix.with_name(prefix.name + ".metadata" + suffix)
        command = [executable, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(raw),
                   "-map", "0:a:0", "-vn", "-metadata", "title=" + track["title"],
                   "-metadata", "artist=" + track["artist"], "-metadata", "comment=" + track["url"]]
        reencode = raw.suffix.lower() not in SUPPORTED
        command += ["-c:a", "libmp3lame", "-q:a", "0", "-threads", "1"] if reencode else ["-c:a", "copy"]
        if suffix == ".mp3":
            command += ["-id3v2_version", "3"]
        command.append(str(temporary))
        # Network transfers and metadata remuxing are light; guard actual encoding.
        if reencode:
            self._thermal_guard()
        self._message("Подготавливаем файл для Музыки: " + track["title"])
        try:
            self._run(command)
            temporary.replace(final)
            self.manifest[track["id"]] = {"path": str(final), "title": track["title"], "artist": track["artist"], "imported": False}
            self._prepare_artwork(track, self.manifest[track["id"]], final, self.playlist, False)
            self._save()
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        finally:
            if reencode:
                self._thermal_guard()
        if raw != final:
            raw.unlink(missing_ok=True)
        return final

    def _run(self, command):
        # File output avoids a full stderr pipe blocking a long subprocess.
        import tempfile
        with tempfile.TemporaryFile(mode="w+") as errors:
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=errors)
            try:
                while process.poll() is None:
                    self._pause(0.2)
                if process.returncode:
                    errors.seek(0)
                    raise RuntimeError(errors.read()[-1200:] or "Не удалось подготовить аудиофайл.")
            except BaseException:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                raise

    def _import(self, path, playlist, source=None):
        if sys.platform != "darwin":
            raise ValueError("Импорт в приложение Музыка доступен только на macOS.")
        script = '''on run argv
set audioFile to (POSIX file (item 1 of argv)) as alias
set playlistName to item 2 of argv
set sourceURL to item 3 of argv
tell application "Music"
if not (exists user playlist playlistName) then
make new user playlist with properties {name:playlistName}
end if
set destinationPlaylist to user playlist playlistName
if sourceURL is not "" then
set existingTracks to (every file track of destinationPlaylist whose comment is sourceURL)
if (count existingTracks) > 0 then return my acknowledge(item 1 of existingTracks)
end if
set importedTracks to add {audioFile} to destinationPlaylist
return my acknowledge(importedTracks)
end tell
end run

on acknowledge(importedTracks)
if importedTracks is missing value then error "Музыка не подтвердила добавление трека."
if class of importedTracks is list then
if (count importedTracks) is 0 then error "Музыка не добавила аудиофайл."
set importedTrack to item 1 of importedTracks
else
set importedTrack to importedTracks
end if
tell application "Music"
if not (exists importedTrack) then error "Музыка не подтвердила добавление трека."
set trackID to persistent ID of importedTrack
end tell
if trackID is missing value or trackID is "" then error "Музыка не подтвердила идентификатор трека."
return "OK:" & trackID
end acknowledge'''
        # File/playlist/source are argv, never interpolated into AppleScript source.
        completed = subprocess.run(["/usr/bin/osascript", "-e", script, str(path), playlist, source or ""],
                                   capture_output=True, text=True, timeout=120)
        if completed.returncode:
            detail = completed.stderr[-500:]
            if "(-1743)" in detail:
                raise RuntimeError("macOS запретила управление Музыкой. Разрешите его в настройках macOS: Конфиденциальность и безопасность → Автоматизация. " + detail)
            raise RuntimeError("Музыка не подтвердила импорт. Файл сохранён. " + detail)
        confirmation = completed.stdout.strip()
        if not re.fullmatch(r"OK:[0-9A-Fa-f]{16}", confirmation):
            raise RuntimeError("Музыка не подтвердила добавление трека. Файл сохранён; повторите импорт.")
        return confirmation

    def choose_cookies(self):
        if sys.platform != "darwin":
            raise ValueError("Выбор файла доступен на macOS.")
        if self.job["active"]:
            raise ValueError("Остановите текущую операцию перед выбором cookies.")
        script = 'POSIX path of (choose file with prompt "Выберите cookies.txt в формате Netscape для SoundCloud")'
        result = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, text=True, timeout=120)
        if result.returncode:
            if "(-128)" in result.stderr:
                return
            raise ValueError("Не удалось выбрать файл cookies.")
        path = Path(result.stdout.strip()).resolve()
        if not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
            raise ValueError("Выберите обычный файл cookies.txt (до 20 МБ).")
        content = path.read_text(errors="replace")
        first = content[:200]
        if "Netscape HTTP Cookie File" not in first and "HTTP Cookie File" not in first:
            raise ValueError("Нужен файл cookies.txt в формате Netscape.")
        selected = []
        for line in content.splitlines():
            fields = line.removeprefix("#HttpOnly_").split("\t")
            domain = fields[0].lstrip(".").lower()
            if len(fields) == 7 and (domain == "soundcloud.com" or domain.endswith(".soundcloud.com")):
                selected.append(line)
        if not selected:
            raise ValueError("В файле нет cookies SoundCloud.")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        cookie_path = self.data_dir / "cookies.txt"
        descriptor = os.open(cookie_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as target:
            target.write("# Netscape HTTP Cookie File\n" + "\n".join(selected) + "\n")
        os.chmod(cookie_path, 0o600)
        self.cookies = str(cookie_path)

    def reveal(self):
        self.download_dir.mkdir(parents=True, exist_ok=True)
        if sys.platform == "darwin":
            subprocess.Popen(["/usr/bin/open", str(self.download_dir)])


class _QuietLogger:
    def debug(self, message):
        pass

    def warning(self, message):
        pass

    def error(self, message):
        pass

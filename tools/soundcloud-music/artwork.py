"""Bounded SoundCloud cover downloads and atomic audio metadata updates."""
import os
import re
import shutil
import struct
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zlib
from pathlib import Path
from urllib.parse import urlsplit

MAX_ARTWORK = 15 * 1024 * 1024


def checked_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or
            not (parsed.hostname == "sndcdn.com" or parsed.hostname.endswith(".sndcdn.com")) or
            parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise ValueError("Обложка должна находиться на HTTPS-сервере SoundCloud.")
    return url


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        checked_url(newurl)
        return super().redirect_request(request, response, code, message, headers, newurl)


def image_type(data):
    """Reject HTML, malformed headers and truncated images before touching audio."""
    if not data or len(data) > MAX_ARTWORK:
        raise ValueError("Обложка пустая или превышает 15 МБ.")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        position, has_image, has_header = 8, False, False
        while position + 12 <= len(data):
            length = struct.unpack(">I", data[position:position + 4])[0]
            kind = data[position + 4:position + 8]
            end = position + 12 + length
            if end > len(data):
                break
            payload = data[position + 8:end - 4]
            checksum = struct.unpack(">I", data[end - 4:end])[0]
            if zlib.crc32(kind + payload) & 0xffffffff != checksum:
                break
            if not has_header:
                if kind != b"IHDR" or length != 13:
                    break
                width, height = struct.unpack(">II", payload[:8])
                if not (0 < width <= 8192 and 0 < height <= 8192):
                    break
                has_header = True
            if kind == b"IDAT" and length:
                has_image = True
            if kind == b"IEND" and length == 0 and has_image and end == len(data):
                return "png"
            position = end
    elif data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9"):
        position, has_dimensions = 2, False
        while position + 4 <= len(data):
            if data[position] != 255:
                break
            while position < len(data) and data[position] == 255:
                position += 1
            if position + 3 > len(data):
                break
            marker = data[position]
            position += 1
            if marker == 0xda:
                if has_dimensions and position + 2 < len(data):
                    length = struct.unpack(">H", data[position:position + 2])[0]
                    if length >= 2 and position + length < len(data) - 2:
                        return "jpg"
                break
            length = struct.unpack(">H", data[position:position + 2])[0]
            if length < 2 or position + length > len(data):
                break
            if marker in (0xc0, 0xc1, 0xc2):
                if length < 8:
                    break
                height, width = struct.unpack(">HH", data[position + 3:position + 7])
                has_dimensions = 0 < width <= 8192 and 0 < height <= 8192
            position += length
    raise ValueError("SoundCloud не вернул целую обложку JPEG или PNG.")


def fetch_cover(url, identifier, directory):
    if not re.fullmatch(r"[0-9]+", str(identifier)):
        raise ValueError("Неверный идентификатор обложки.")
    checked_url(url)
    directory = Path(directory)
    for extension in ("jpg", "png"):
        cached = directory / (str(identifier) + "." + extension)
        if cached.is_file() and cached.stat().st_size <= MAX_ARTWORK:
            if image_type(cached.read_bytes()) == extension:
                return cached
    # SoundCloud publishes this standard 500px variant of its known thumbnail.
    larger = re.sub(r"-(?:large|t\d+x\d+)\.(jpg|png)(?=\?|$)", r"-t500x500.\1", url)
    opener = urllib.request.build_opener(SafeRedirect())
    candidates = list(dict.fromkeys((larger, url)))
    for index, candidate in enumerate(candidates):
        try:
            request = urllib.request.Request(candidate, headers={"User-Agent": "SoundCloud-Music-local/1"})
            with opener.open(request, timeout=20) as response:
                checked_url(response.geturl())
                length = response.headers.get("Content-Length")
                if length and int(length) > MAX_ARTWORK:
                    raise ValueError("Обложка превышает 15 МБ.")
                data = response.read(MAX_ARTWORK + 1)
            extension = image_type(data)
            break
        except urllib.error.HTTPError as error:
            if error.code != 404 or index + 1 == len(candidates):
                raise
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (str(identifier) + "." + extension)
    with tempfile.NamedTemporaryFile(dir=directory, prefix=".cover-", delete=False) as temporary:
        temporary.write(data)
        temporary_path = Path(temporary.name)
    try:
        os.chmod(temporary_path, 0o600)
        temporary_path.replace(target)
    finally:
        temporary_path.unlink(missing_ok=True)
    return target


def embed_cover(audio, cover):
    audio, cover = Path(audio), Path(cover)
    if cover.stat().st_size > MAX_ARTWORK:
        raise ValueError("Обложка превышает 15 МБ.")
    data = cover.read_bytes()
    kind = image_type(data)
    if audio.suffix.lower() not in (".m4a", ".mp3"):
        return False
    with tempfile.NamedTemporaryFile(dir=audio.parent, prefix=".artwork-", suffix=audio.suffix, delete=False) as output:
        temporary = Path(output.name)
    try:
        shutil.copyfile(audio, temporary)
        if audio.suffix.lower() == ".m4a":
            from mutagen.mp4 import MP4, MP4Cover
            tagged = MP4(temporary)
            if tagged.tags is None:
                tagged.add_tags()
            tagged["covr"] = [MP4Cover(data, imageformat=MP4Cover.FORMAT_JPEG if kind == "jpg" else MP4Cover.FORMAT_PNG)]
            tagged.save()
        else:
            from mutagen.id3 import ID3, ID3NoHeaderError, APIC
            try:
                tagged = ID3(temporary)
            except ID3NoHeaderError:
                tagged = ID3()
            tagged.delall("APIC")
            tagged.add(APIC(encoding=3, mime="image/jpeg" if kind == "jpg" else "image/png", type=3, desc="Cover", data=data))
            tagged.save(temporary, v2_version=3)
        os.chmod(temporary, audio.stat().st_mode & 0o777)
        temporary.replace(audio)
    finally:
        temporary.unlink(missing_ok=True)
    return True


MUSIC_ARTWORK_SCRIPT = '''on run argv
set coverFile to (POSIX file (item 1 of argv)) as alias
set playlistName to item 2 of argv
set sourceURL to item 3 of argv
set trackID to item 4 of argv
set imageType to item 5 of argv
if imageType is "png" then
set imageData to read coverFile as «class PNGf»
else
set imageData to read coverFile as «class JPEG»
end if
tell application "Music"
if not (exists user playlist playlistName) then error "Плейлист для обложки не найден."
set destinationPlaylist to user playlist playlistName
if trackID is not "" then
set matches to (every file track of destinationPlaylist whose persistent ID is trackID and comment is sourceURL)
else
set matches to (every file track of destinationPlaylist whose comment is sourceURL)
end if
if (count matches) is not 1 then error "Нельзя однозначно определить трек для обложки."
set targetTrack to item 1 of matches
if (count artworks of targetTrack) > 0 then
set existingData to raw data of artwork 1 of targetTrack
if existingData is not missing value then return "ART:" & persistent ID of targetTrack
end if
set data of artwork 1 of targetTrack to imageData
if (count artworks of targetTrack) is 0 then error "Музыка не подтвердила обложку."
set storedData to raw data of artwork 1 of targetTrack
if storedData is missing value then error "Музыка не подтвердила данные обложки."
return "ART:" & persistent ID of targetTrack
end tell
end run'''


def apply_music_cover(cover, playlist, source, persistent_id=None):
    if not source:
        raise ValueError("Нет точной ссылки SoundCloud для обновления обложки.")
    kind = image_type(Path(cover).read_bytes())
    command = ["/usr/bin/osascript", "-e", MUSIC_ARTWORK_SCRIPT,
               str(cover), playlist, source, persistent_id or "", kind]
    for attempt in range(4):
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
        confirmation = result.stdout.strip()
        if (result.returncode == 0 and re.fullmatch(r"ART:[0-9A-Fa-f]{16}", confirmation)
                and (not persistent_id or confirmation[4:].upper() == persistent_id.upper())):
            return confirmation[4:]
        if result.returncode and re.search(r"\(-47\)", result.stderr):
            # Music may still be copying/tagging a newly added file.
            if attempt < 3:
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError("«Музыка» ещё обрабатывает файл. Повтори добавление обложки после завершения переноса.")
        if "(-1743)" in result.stderr:
            raise RuntimeError("Разреши управление «Музыкой» в настройках macOS: Конфиденциальность и безопасность → Автоматизация.")
        raise RuntimeError("Аудио добавлено; «Музыка» не подтвердила обложку. Её можно добавить повторно после завершения переноса.")

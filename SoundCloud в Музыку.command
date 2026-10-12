#!/bin/zsh
set -eu
cd "${0:A:h}/tools/soundcloud-music"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
if ! command -v python3 >/dev/null 2>&1; then
  print 'Для запуска нужен Python 3.10 или новее.'
  read -k 1 '?Нажми любую клавишу, чтобы закрыть…'
  exit 1
fi
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
  print 'Нужен Python 3.10 или новее. Установи актуальный Python с python.org и запусти снова.'
  read -k 1 '?Нажми любую клавишу, чтобы закрыть…'
  exit 1
fi
if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
fi
if ! .venv/bin/python -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
  print 'Окружение .venv создано старым Python. Переименуй папку .venv и запусти снова; папку data сохрани.'
  read -k 1 '?Нажми любую клавишу, чтобы закрыть…'
  exit 1
fi
if ! .venv/bin/python -c 'import yt_dlp, curl_cffi, mutagen' >/dev/null 2>&1; then
  print 'Подготавливаем компоненты SoundCloud внутри папки инструмента…'
  if ! .venv/bin/python -m pip install --no-cache-dir -r requirements.txt; then
    print 'Не удалось скачать компоненты. Проверь интернет и запусти ещё раз.'
    read -k 1 '?Нажми любую клавишу, чтобы закрыть…'
    exit 1
  fi
fi
if ! command -v ffmpeg >/dev/null 2>&1; then
  print 'Для подготовки аудио нужен ffmpeg. Инструкция по установке — в папке инструмента.'
  read -k 1 '?Нажми любую клавишу, чтобы закрыть…'
  exit 1
fi
print 'SoundCloud → Музыка'
print 'Список и выбор треков откроются в браузере.'
print 'Чтобы закрыть инструмент, нажми Control+C в этом окне.'
exec .venv/bin/python server.py

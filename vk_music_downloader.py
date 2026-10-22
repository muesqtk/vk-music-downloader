#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VK Music Downloader
GUI-приложение для скачивания своей музыки из VK через официальное API (audio.get).

Запуск:      python vk_music_downloader.py
Зависимости: requests (обязательно), mutagen (опционально, теги ID3),
             ffmpeg (опционально, если VK отдаёт HLS/DASH-плейлисты вместо mp3)
"""

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor, as_completed
from tkinter import filedialog, messagebox, ttk

try:
    import requests
except ImportError:  # pragma: no cover
    raise SystemExit("Не установлена библиотека requests. Выполните: pip install requests")

API_URL = "https://api.vk.com/method/"
API_VERSION = "5.131"
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".vk_music_downloader.json")
PAGE_SIZE = 100


# ---------------------------------------------------------------------------
# VK API
# ---------------------------------------------------------------------------

class VkError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def describe_error(err):
    hints = {
        5: "Неверный или просроченный токен — получите новый.",
        6: "Слишком много запросов к VK, подождите минуту и попробуйте снова.",
        15: "Доступ запрещён: у токена нет прав на аудио.",
        100: "Неверные параметры запроса.",
        200: "Доступ к аудио запрещён для этого токена/приложения.",
        201: "Доступ к аудио запрещён (требуется авторизация).",
        203: "Аудио недоступно для этого токена.",
    }
    hint = hints.get(err.code)
    if hint:
        return f"{hint}\n\nКод VK: {err.code}\n{err}"
    return f"Ошибка VK (код {err.code}): {err}"


def vk_api(method, token, **params):
    params["access_token"] = token
    params["v"] = API_VERSION
    try:
        resp = requests.post(API_URL + method, data=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        raise VkError(-1, f"Сетевая ошибка: {exc}") from exc
    except ValueError as exc:
        raise VkError(-1, "VK вернул ответ, который не удалось разобрать.") from exc

    if "error" in data:
        err = data["error"]
        raise VkError(err.get("error_code", -1), err.get("error_msg", "Неизвестная ошибка"))
    if "response" not in data:
        raise VkError(-1, "Пустой ответ VK.")
    return data["response"]


def _normalize_track(item):
    album = item.get("album")
    if isinstance(album, dict):
        album_title = album.get("title") or ""
    else:
        album_title = ""
    track = {
        "id": item.get("id"),
        "owner_id": item.get("owner_id"),
        "artist": (item.get("artist") or "").strip() or "Неизвестный исполнитель",
        "title": (item.get("title") or "").strip() or "Без названия",
        "duration": int(item.get("duration") or 0),
        "album": album_title,
        "url": item.get("url") or "",
    }
    track["iid"] = f'{track["owner_id"]}_{track["id"]}'
    return track


def _fetch_audio_pages(token, base_params, on_page=None):
    """
    Загружает все страницы audio.get.

    VK может вернуть меньше PAGE_SIZE элементов, даже если треки ещё остались
    (часть позиций он пропускает), поэтому останавливаемся только когда страница
    пустая или offset дошёл до count.
    """
    tracks = []
    seen = set()
    offset = 0
    total = 0
    pages = 0

    while True:
        params = dict(base_params)
        params.update(count=PAGE_SIZE, offset=offset)
        resp = vk_api("audio.get", token, **params)

        if isinstance(resp, dict):
            items = resp.get("items") or []
            total = int(resp.get("count") or offset + len(items))
        else:
            items = resp or []
            total = offset + len(items)

        if not items:
            break

        for item in items:
            track = _normalize_track(item)
            if track["iid"] in seen:
                continue
            seen.add(track["iid"])
            tracks.append(track)

        offset += len(items)
        pages += 1
        if on_page:
            on_page(len(tracks), total)
        if offset >= total or pages > 500:
            break

    return tracks


def fetch_tracks(token, on_page=None):
    """Загружает всю фонотеку текущего пользователя."""
    return _fetch_audio_pages(token, {}, on_page=on_page)


def fetch_playlists(token, on_page=None):
    """Загружает плейлисты текущего пользователя."""
    users = vk_api("users.get", token)
    if not isinstance(users, list) or not users:
        raise VkError(-1, "Не удалось определить ID пользователя")
    user_id = users[0].get("id")

    playlists = []
    offset = 0
    total = 0

    while True:
        resp = vk_api(
            "audio.getPlaylists", token, owner_id=user_id, count=100, offset=offset
        )
        if isinstance(resp, dict):
            items = resp.get("items") or []
            total = int(resp.get("count") or 0)
        else:
            items = resp or []
            total = offset + len(items)

        if not items:
            break

        for item in items:
            playlists.append({
                "id": item.get("id"),
                "owner_id": item.get("owner_id"),
                "title": (item.get("title") or "").strip() or "Без названия",
                "count": int(item.get("count") or 0),
                "access_key": item.get("access_key") or "",
                "description": (item.get("description") or "").strip(),
            })

        offset += len(items)
        if on_page:
            on_page(len(playlists), total)
        if offset >= total:
            break

    return playlists


def fetch_playlist_tracks(token, playlist, on_page=None):
    """Загружает треки одного плейлиста."""
    params = {
        "owner_id": playlist["owner_id"],
        "playlist_id": playlist["id"],
    }
    if playlist.get("access_key"):
        params["access_key"] = playlist["access_key"]
    return _fetch_audio_pages(token, params, on_page=on_page)


def refetch_url(token, track):
    """Пробует получить свежую ссылку на файл (старые ссылки VK быстро истекают)."""
    try:
        resp = vk_api("audio.getById", token, audios=f'{track["owner_id"]}_{track["id"]}')
    except VkError:
        return ""
    if isinstance(resp, list) and resp:
        return (resp[0] or {}).get("url") or ""
    return ""


# ---------------------------------------------------------------------------
# Скачивание
# ---------------------------------------------------------------------------

class DownloadCancelled(Exception):
    pass


class NoAudioAccess(RuntimeError):
    """VK вместо аудиофайла прислал заглушку, HTML или JSON."""


class _HlsPlaylist(Exception):
    pass


MIN_AUDIO_SIZE = 16 * 1024


def _remove_file(path):
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _preview(head):
    text = head[:160].decode("utf-8", "replace")
    return " ".join(text.split())


def _is_audio_head(head):
    if head.startswith((b"ID3", b"fLaC", b"OggS")):
        return True
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return True
    return False


def _is_manifest(head):
    return (
        head.lstrip(b"\xef\xbb\xbf \r\n\t").startswith(b"#EXTM3U")
        or b"<MPD" in head[:512]
    )


def _mutagen_readable(path):
    try:
        from mutagen import File as MutagenFile
    except ImportError:
        return False
    try:
        return MutagenFile(path) is not None
    except Exception:
        return False


def _validate_audio(path, head, content_type, size, url):
    if "audio_api_unavailable" in (url or ""):
        raise NoAudioAccess(
            "VK вернул заглушку audio_api_unavailable — у токена нет доступа к этому треку"
        )
    ctype = (content_type or "не указан").split(";")[0].strip()
    if size < MIN_AUDIO_SIZE:
        raise NoAudioAccess(
            f"получено {fmt_size(size)} вместо музыки (Content-Type: {ctype}). "
            f"Похоже, VK прислал заглушку или страницу ошибки. "
            f"Начало ответа: {_preview(head)!r}"
        )
    if _is_audio_head(head) or _mutagen_readable(path):
        return
    raise NoAudioAccess(
        f"VK вернул не аудиофайл ({fmt_size(size)}, Content-Type: {ctype}). "
        f"Начало ответа: {_preview(head)!r}"
    )


def _download_hls(url, dest, cancel, on_progress):
    """Собирает трек из HLS/DASH-манифеста через ffmpeg (если VK отдал m3u8/mpd)."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise NoAudioAccess(
            "VK отдал плейлист (m3u8/mpd) вместо mp3, а ffmpeg не найден. "
            "Установите ffmpeg и добавьте его в PATH — тогда программа соберёт трек сама"
        )

    part = dest + ".part.mp3"
    log_path = dest + ".ffmpeg.log"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    cmd = [
        ffmpeg, "-y", "-loglevel", "error", "-i", url,
        "-vn", "-c:a", "libmp3lame", "-b:a", "192k", "-f", "mp3", part,
    ]
    try:
        with open(log_path, "wb") as err_fh:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=err_fh, creationflags=flags
            )
            while True:
                try:
                    proc.wait(timeout=0.25)
                    break
                except subprocess.TimeoutExpired:
                    if cancel.is_set():
                        proc.kill()
                        proc.wait()
                        raise DownloadCancelled()
                    if on_progress:
                        on_progress(None)

        error_text = ""
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
                error_text = fh.read().strip()
        except OSError:
            pass

        if proc.returncode != 0 or not os.path.exists(part):
            raise NoAudioAccess(
                "ffmpeg не смог собрать трек из плейлиста"
                + (f": {error_text[-300:]}" if error_text else "")
            )

        with open(part, "rb") as fh:
            head = fh.read(256)
        _validate_audio(part, head, "audio/mpeg", os.path.getsize(part), "")
        os.replace(part, dest)
    except Exception:
        _remove_file(part)
        raise
    finally:
        _remove_file(log_path)


def _stream_to_file(url, dest, cancel, on_progress):
    part = dest + ".part"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept": "*/*",
        "Referer": "https://vk.com/",
    }
    try:
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    except OSError:
        pass

    try:
        with requests.get(url, stream=True, timeout=60, headers=headers) as resp:
            resp.raise_for_status()
            content_type = resp.headers.get("Content-Type", "")
            length = int(resp.headers.get("Content-Length") or 0)
            head = b""
            done = 0
            with open(part, "wb") as fh:
                for chunk in resp.iter_content(64 * 1024):
                    if cancel.is_set():
                        raise DownloadCancelled()
                    if not chunk:
                        continue
                    if not head:
                        head = chunk[:512]
                        if _is_manifest(head):
                            raise _HlsPlaylist()
                    fh.write(chunk)
                    done += len(chunk)
                    if on_progress:
                        on_progress(done / length if length else None)

        size = os.path.getsize(part)
        _validate_audio(part, head, content_type, size, url)
        os.replace(part, dest)
    except _HlsPlaylist:
        _remove_file(part)
        _download_hls(url, dest, cancel, on_progress)
    except Exception:
        _remove_file(part)
        raise


def download_track(track, dest, cancel, on_progress, token):
    url = track.get("url") or ""
    if not url and token:
        url = refetch_url(token, track)
    if not url:
        raise NoAudioAccess("VK не выдал ссылку на файл — у токена нет доступа к аудио")
    if "audio_api_unavailable" in url:
        raise NoAudioAccess(
            "VK вернул заглушку audio_api_unavailable — у токена нет доступа к аудио"
        )

    problem = None
    try:
        _stream_to_file(url, dest, cancel, on_progress)
        return
    except DownloadCancelled:
        raise
    except NoAudioAccess as exc:
        problem = exc
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status not in (403, 410) or not token:
            raise RuntimeError(f"сервер вернул HTTP {status}" if status else str(exc))
        problem = exc
    except requests.RequestException as exc:
        raise RuntimeError(f"сетевая ошибка: {exc}")

    # ссылка могла устареть — пробуем получить новую и один раз повторить
    new_url = refetch_url(token, track) if token else ""
    if not new_url or new_url == url or "audio_api_unavailable" in new_url:
        if isinstance(problem, NoAudioAccess):
            raise problem
        raise RuntimeError("VK не выдал рабочую ссылку на файл")
    try:
        _stream_to_file(new_url, dest, cancel, on_progress)
    except requests.RequestException as exc:
        raise RuntimeError(f"сетевая ошибка: {exc}")


def write_tags(path, track):
    """Прописывает ID3-теги, если установлен mutagen."""
    try:
        from mutagen.id3 import ID3, TALB, TIT2, TPE1
    except ImportError:
        return
    try:
        try:
            tags = ID3(path)
        except Exception:
            tags = ID3()
        tags.delall("TIT2")
        tags.add(TIT2(encoding=3, text=track["title"]))
        tags.delall("TPE1")
        tags.add(TPE1(encoding=3, text=track["artist"]))
        if track.get("album"):
            tags.delall("TALB")
            tags.add(TALB(encoding=3, text=track["album"]))
        tags.save(path)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def fmt_duration(seconds):
    seconds = max(0, int(seconds or 0))
    return f"{seconds // 60}:{seconds % 60:02d}"


def fmt_size(size):
    value = float(size)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if value < 1024 or unit == "ГБ":
            return f"{value:.0f} {unit}" if unit == "Б" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} ГБ"


def safe_filename(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        return "track"
    if name.split(".")[0].upper() in _WIN_RESERVED:
        name = "_" + name
    return name[:150]


def format_filename(track, template):
    values = {
        "artist": track["artist"],
        "title": track["title"],
        "album": track.get("album") or "",
        "id": str(track.get("id") or ""),
        "owner_id": str(track.get("owner_id") or ""),
        "duration": fmt_duration(track.get("duration")),
    }
    name = template
    for key, value in values.items():
        name = name.replace("{" + key + "}", value)
    return safe_filename(name)


def unique_path(folder, filename, reserved):
    base, ext = os.path.splitext(filename)
    candidate = os.path.join(folder, filename)
    counter = 1
    while candidate in reserved:
        counter += 1
        candidate = os.path.join(folder, f"{base} ({counter}){ext}")
    reserved.add(candidate)
    return candidate


def existing_is_audio(path):
    """Проверяет, что уже лежащий на диске файл — музыка, а не заглушка/страница ошибки."""
    try:
        if os.path.getsize(path) < MIN_AUDIO_SIZE:
            return False
        with open(path, "rb") as fh:
            head = fh.read(256)
    except OSError:
        return False
    return _is_audio_head(head) or _mutagen_readable(path)


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("VK Music Downloader")
        self.geometry("1000x720")
        self.minsize(860, 600)

        self.cfg = self._load_config()
        self.tracks = []
        self.by_iid = {}
        self.checked = set()
        self.sort_col = None
        self.sort_reverse = False
        self.events = queue.Queue()
        self.worker = None
        self.cancel_event = threading.Event()
        self.total = 0
        self.loading = False
        self.playlist_dialog = None

        self.token_var = tk.StringVar(value=self.cfg.get("token", ""))
        default_folder = os.path.join(os.path.expanduser("~"), "Music", "VK")
        self.folder_var = tk.StringVar(value=self.cfg.get("folder") or default_folder)
        self.template_var = tk.StringVar(value=self.cfg.get("template") or "{artist} - {title}")
        self.search_var = tk.StringVar()
        self.remember_var = tk.BooleanVar(value=bool(self.cfg.get("remember")))
        self.skip_var = tk.BooleanVar(value=True)
        self.show_token_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Готов к работе")
        try:
            default_workers = int(self.cfg.get("workers") or 4)
        except (TypeError, ValueError):
            default_workers = 4
        self.workers_var = tk.IntVar(value=max(1, min(8, default_workers)))

        self._build_ui()
        self.search_var.trace_add("write", lambda *_: self._refresh_tree())
        self.after(120, self._poll_events)

    # -- построение интерфейса ------------------------------------------------

    def _build_ui(self):
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure(".", font=("Segoe UI", 9))
        style.configure("Treeview", rowheight=24)
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))
        style.configure("Accent.TButton", font=("Segoe UI", 9, "bold"))

        outer = ttk.Frame(self, padding=10)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        # 1. Доступ
        access = ttk.LabelFrame(outer, text=" 1. Доступ к VK ", padding=10)
        access.grid(row=0, column=0, sticky="ew")
        access.columnconfigure(1, weight=1)

        ttk.Label(access, text="Токен:").grid(row=0, column=0, sticky="w")
        self.token_entry = ttk.Entry(access, textvariable=self.token_var, show="•")
        self.token_entry.grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Checkbutton(
            access, text="Показать", variable=self.show_token_var,
            command=self._toggle_token_visibility,
        ).grid(row=0, column=2)
        self.btn_load = ttk.Button(
            access, text="Загрузить мою музыку", style="Accent.TButton",
            command=self._load_tracks,
        )
        self.btn_load.grid(row=0, column=3, padx=(8, 0))
        self.btn_playlists = ttk.Button(
            access, text="Плейлисты…", command=self._show_playlists
        )
        self.btn_playlists.grid(row=0, column=4, padx=(8, 0))
        ttk.Button(access, text="Как получить токен", command=self._show_token_help).grid(
            row=0, column=5, padx=(8, 0)
        )

        ttk.Checkbutton(
            access, text="Запомнить токен на этом компьютере", variable=self.remember_var
        ).grid(row=1, column=1, sticky="w", padx=8, pady=(6, 0))
        ttk.Label(
            access, text="Загружается ваша фонотека или выбранный плейлист.",
            foreground="#666",
        ).grid(row=1, column=3, columnspan=3, sticky="e", pady=(6, 0))

        # 2. Панель инструментов
        bar = ttk.Frame(outer)
        bar.grid(row=1, column=0, sticky="ew", pady=(10, 6))
        bar.columnconfigure(1, weight=1)

        ttk.Label(bar, text="Поиск:").grid(row=0, column=0, sticky="w")
        ttk.Entry(bar, textvariable=self.search_var).grid(row=0, column=1, sticky="ew", padx=(6, 10))
        ttk.Button(bar, text="Отметить все", command=lambda: self._set_all(True)).grid(row=0, column=2, padx=2)
        ttk.Button(bar, text="Снять все", command=lambda: self._set_all(False)).grid(row=0, column=3, padx=2)

        self.btn_dl_checked = ttk.Button(
            bar, text="Скачать отмеченные (0)", command=self._download_checked
        )
        self.btn_dl_checked.grid(row=0, column=4, padx=(10, 2))
        self.btn_dl_all = ttk.Button(bar, text="Скачать всё", command=self._download_all)
        self.btn_dl_all.grid(row=0, column=5, padx=2)
        self.btn_stop = ttk.Button(bar, text="Стоп", command=self._stop, state="disabled")
        self.btn_stop.grid(row=0, column=6, padx=(2, 0))

        # 3. Таблица треков
        tree_frame = ttk.Frame(outer)
        tree_frame.grid(row=2, column=0, sticky="nsew")
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)

        columns = ("chk", "num", "artist", "title", "dur", "status")
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="extended")
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=vsb.set)

        widths = {"chk": 44, "num": 52, "artist": 220, "title": 340, "dur": 70, "status": 110}
        titles = {
            "chk": "✓", "num": "№", "artist": "Исполнитель",
            "title": "Название", "dur": "Время", "status": "Статус",
        }
        for col in columns:
            kwargs = {}
            if col in ("artist", "title", "dur"):
                kwargs["command"] = lambda c=col: self._sort_by(c)
            self.tree.heading(col, text=titles[col], **kwargs)
            self.tree.column(
                col, width=widths[col],
                anchor="w" if col in ("artist", "title") else "center",
                stretch=col in ("artist", "title"),
            )
        self.tree.tag_configure("odd", background="#f6f7f9")
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<space>", self._on_space)

        # 4. Параметры сохранения
        bottom = ttk.Frame(outer)
        bottom.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        bottom.columnconfigure(1, weight=1)

        ttk.Label(bottom, text="Папка:").grid(row=0, column=0, sticky="w")
        ttk.Entry(bottom, textvariable=self.folder_var).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(bottom, text="Обзор…", command=self._choose_folder).grid(row=0, column=2)
        ttk.Button(bottom, text="Открыть", command=self._open_folder).grid(row=0, column=3, padx=(6, 0))

        ttk.Label(bottom, text="Имя файла:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(bottom, textvariable=self.template_var).grid(
            row=1, column=1, sticky="ew", padx=6, pady=(6, 0)
        )
        ttk.Label(
            bottom, text="{artist} · {title} · {album} · {id} · {duration}",
            foreground="#666",
        ).grid(row=1, column=2, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Checkbutton(
            bottom, text="Пропускать уже скачанные", variable=self.skip_var
        ).grid(row=2, column=1, sticky="w", padx=6, pady=(6, 0))
        ttk.Label(bottom, text="Параллельных загрузок:").grid(
            row=2, column=2, sticky="e", pady=(6, 0)
        )
        self.workers_spin = ttk.Spinbox(
            bottom, from_=1, to=8, width=4, textvariable=self.workers_var
        )
        self.workers_spin.grid(row=2, column=3, sticky="w", padx=(6, 0), pady=(6, 0))

        prog = ttk.Frame(bottom)
        prog.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        prog.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(prog, mode="determinate", maximum=100)
        self.progress.grid(row=0, column=0, sticky="ew")
        ttk.Label(prog, textvariable=self.status_var).grid(row=1, column=0, sticky="w", pady=(4, 0))

        # 5. Журнал
        log_frame = ttk.LabelFrame(outer, text=" Журнал ", padding=6)
        log_frame.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        log_frame.columnconfigure(0, weight=1)
        self.log = tk.Text(
            log_frame, height=7, wrap="none", state="disabled",
            font=("Consolas", 9), background="#1e1e1e", foreground="#d4d4d4",
            insertbackground="#d4d4d4", relief="flat",
        )
        self.log.grid(row=0, column=0, sticky="ew")
        log_sb = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        log_sb.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=log_sb.set)

    # -- настройки ------------------------------------------------------------

    @staticmethod
    def _load_config():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_config(self):
        cfg = {
            "folder": self.folder_var.get().strip(),
            "template": self.template_var.get().strip() or "{artist} - {title}",
            "remember": self.remember_var.get(),
            "token": self.token_var.get().strip() if self.remember_var.get() else "",
            "workers": self._workers_count(),
        }
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _workers_count(self):
        try:
            value = int(self.workers_var.get())
        except (tk.TclError, ValueError):
            value = 4
        return max(1, min(8, value))

    # -- журнал ---------------------------------------------------------------

    def _log(self, text):
        stamp = time.strftime("%H:%M:%S")
        self.log.configure(state="normal")
        self.log.insert("end", f"[{stamp}] {text}\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    # -- события из рабочих потоков ------------------------------------------

    def _poll_events(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                self._handle_event(kind, payload)
        except queue.Empty:
            pass
        self.after(120, self._poll_events)

    def _handle_event(self, kind, payload):
        if kind == "load_status":
            self.status_var.set(payload["text"])
        elif kind == "tracks":
            self._set_loading(False)
            self._set_tracks(
                payload["tracks"], payload["token"],
                payload.get("source", "Моя музыка"),
            )
        elif kind == "error":
            self._set_loading(False)
            self.status_var.set("Ошибка")
            self._log("✖  " + payload["text"].splitlines()[0])
            messagebox.showerror(payload["title"], payload["text"])
        elif kind == "progress":
            self.progress["value"] = payload["value"] * 100
        elif kind == "status":
            self.status_var.set(payload["text"])
        elif kind == "row":
            track = self.by_iid.get(payload["iid"])
            if track is not None:
                track["status"] = payload["text"]
            if self.tree.exists(payload["iid"]):
                self.tree.set(payload["iid"], "status", payload["text"])
        elif kind == "log":
            self._log(payload["text"])
        elif kind == "access_denied":
            self.status_var.set("Нет доступа к аудио")
        elif kind == "finished":
            self._on_finished(
                payload["stats"],
                payload["cancelled"],
                payload.get("access_denied", False),
            )

    # -- загрузка списка ------------------------------------------------------

    def _set_loading(self, loading):
        self.loading = loading
        state = "disabled" if loading else "normal"
        self.btn_load.configure(
            state=state, text="Загружаю…" if loading else "Загрузить мою музыку"
        )
        self.btn_playlists.configure(state=state)
        self.btn_dl_checked.configure(state=state)
        self.btn_dl_all.configure(state=state)

    def _load_tracks(self):
        token = self.token_var.get().strip()
        if not token:
            messagebox.showwarning(
                "Нужен токен",
                "Вставьте токен VK в поле «Токен».\n"
                "Нажмите «Как получить токен», если его ещё нет.",
            )
            return
        if self.loading:
            return
        self._set_loading(True)
        self.status_var.set("Загружаю список аудиозаписей…")
        if self.remember_var.get():
            self._save_config()
        threading.Thread(target=self._load_worker, args=(token,), daemon=True).start()

    def _load_worker(self, token):
        def on_page(loaded, total):
            self.events.put((
                "load_status",
                {"text": f"Загружено {loaded} из {total if total else '?'}…"},
            ))

        try:
            tracks = fetch_tracks(token, on_page=on_page)
            self.events.put((
                "tracks",
                {"tracks": tracks, "token": token, "source": "Моя музыка"},
            ))
        except VkError as exc:
            self.events.put((
                "error",
                {"title": "Не удалось загрузить музыку", "text": describe_error(exc)},
            ))
        except Exception as exc:
            self.events.put((
                "error",
                {"title": "Не удалось загрузить музыку", "text": str(exc)},
            ))

    def _show_playlists(self):
        token = self.token_var.get().strip()
        if not token:
            messagebox.showwarning(
                "Нужен токен",
                "Вставьте токен VK, чтобы загрузить список плейлистов.",
            )
            return
        try:
            if self.playlist_dialog is not None and self.playlist_dialog.winfo_exists():
                self.playlist_dialog.lift()
                self.playlist_dialog.focus_force()
                return
        except tk.TclError:
            pass
        self.playlist_dialog = PlaylistDialog(self, token)

    def load_playlist(self, token, playlist):
        if self.loading:
            return
        self._set_loading(True)
        self.status_var.set(f"Загружаю плейлист «{playlist['title']}»…")
        threading.Thread(
            target=self._playlist_worker, args=(token, playlist), daemon=True
        ).start()

    def _playlist_worker(self, token, playlist):
        title = playlist["title"]

        def on_page(loaded, total):
            self.events.put((
                "load_status",
                {"text": f"«{title}»: загружено {loaded} из {total if total else '?'}…"},
            ))

        try:
            tracks = fetch_playlist_tracks(token, playlist, on_page=on_page)
            self.events.put((
                "tracks",
                {"tracks": tracks, "token": token, "source": f"Плейлист «{title}»"},
            ))
        except VkError as exc:
            self.events.put((
                "error",
                {"title": "Не удалось загрузить плейлист", "text": describe_error(exc)},
            ))
        except Exception as exc:
            self.events.put((
                "error",
                {"title": "Не удалось загрузить плейлист", "text": str(exc)},
            ))

    def _set_tracks(self, tracks, token, source="Моя музыка"):
        self.tracks = tracks
        self.by_iid = {track["iid"]: track for track in tracks}
        self.checked = set(self.by_iid)
        self.sort_col = None
        self.sort_reverse = False
        self._refresh_tree()

        if not tracks:
            self.status_var.set("Список пуст")
            self._log(f"{source}: список пуст.")
            messagebox.showinfo(
                "Пусто",
                f"{source}: VK вернул пустой список.\n\n"
                "Если треки там есть, скорее всего токен не имеет доступа к аудио "
                "— это ограничение VK для сторонних приложений.",
            )
            return

        self.status_var.set(f"{source}: {len(tracks)} трек(ов)")
        self._log(f"{source}: загружено треков — {len(tracks)}")

        stubs = sum(1 for track in tracks if "audio_api_unavailable" in track["url"])
        empty = sum(1 for track in tracks if not track["url"])
        if stubs:
            self._log(
                f"⚠  {stubs} из {len(tracks)} ссылок — заглушки audio_api_unavailable: "
                "VK не даёт доступ к этим трекам."
            )
        if empty:
            self._log(f"⚠  Для {empty} трек(ов) VK вообще не выдал ссылок.")
        if not any(track["url"] for track in tracks):
            self._log(
                "⚠  VK не выдал ни одной ссылки на файлы. Скорее всего токен без доступа "
                "к аудио — скачивание не сработает."
            )

    # -- таблица --------------------------------------------------------------

    def _refresh_tree(self):
        children = self.tree.get_children()
        if children:
            self.tree.delete(*children)

        rows = list(self.tracks)
        query = self.search_var.get().strip().lower()
        if query:
            rows = [
                track for track in rows
                if query in track["artist"].lower() or query in track["title"].lower()
            ]

        if self.sort_col == "artist":
            rows.sort(key=lambda t: (t["artist"].lower(), t["title"].lower()),
                      reverse=self.sort_reverse)
        elif self.sort_col == "title":
            rows.sort(key=lambda t: (t["title"].lower(), t["artist"].lower()),
                      reverse=self.sort_reverse)
        elif self.sort_col == "dur":
            rows.sort(key=lambda t: t["duration"], reverse=self.sort_reverse)

        for index, track in enumerate(rows, start=1):
            self.tree.insert(
                "", "end", iid=track["iid"],
                values=(
                    "☑" if track["iid"] in self.checked else "☐",
                    index,
                    track["artist"],
                    track["title"],
                    fmt_duration(track["duration"]),
                    track.get("status", ""),
                ),
                tags=("odd",) if index % 2 == 0 else (),
            )

        self._update_headings()
        self._update_counts()

    def _update_headings(self):
        for col, base in (("artist", "Исполнитель"), ("title", "Название"), ("dur", "Время")):
            arrow = ""
            if self.sort_col == col:
                arrow = " ▼" if self.sort_reverse else " ▲"
            self.tree.heading(col, text=base + arrow)

    def _sort_by(self, col):
        if self.sort_col == col:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_col = col
            self.sort_reverse = False
        self._refresh_tree()

    def _on_tree_click(self, event):
        if self.tree.identify("region", event.x, event.y) != "cell":
            return
        row = self.tree.identify_row(event.y)
        if not row:
            return
        if self.tree.identify_column(event.x) == "#1":
            self._toggle(row)
            return "break"

    def _on_space(self, _event):
        for iid in self.tree.selection():
            self._toggle(iid)
        return "break"

    def _toggle(self, iid):
        if iid in self.checked:
            self.checked.discard(iid)
        else:
            self.checked.add(iid)
        if self.tree.exists(iid):
            self.tree.set(iid, "chk", "☑" if iid in self.checked else "☐")
        self._update_counts()

    def _set_all(self, value):
        self.checked = set(self.by_iid) if value else set()
        for iid in self.tree.get_children():
            self.tree.set(iid, "chk", "☑" if value else "☐")
        self._update_counts()

    def _update_counts(self):
        self.btn_dl_checked.configure(text=f"Скачать отмеченные ({len(self.checked)})")

    # -- параметры ------------------------------------------------------------

    def _toggle_token_visibility(self):
        self.token_entry.configure(show="" if self.show_token_var.get() else "•")

    def _choose_folder(self):
        folder = filedialog.askdirectory(
            initialdir=self.folder_var.get() or os.path.expanduser("~")
        )
        if folder:
            self.folder_var.set(os.path.normpath(folder))

    def _open_folder(self):
        folder = self.folder_var.get().strip()
        if not folder:
            return
        try:
            os.makedirs(folder, exist_ok=True)
            os.startfile(folder)  # Windows
        except AttributeError:
            import subprocess
            import sys
            command = ["xdg-open", folder] if sys.platform.startswith("linux") else ["open", folder]
            subprocess.Popen(command)
        except OSError as exc:
            messagebox.showerror("Не удалось открыть папку", str(exc))

    # -- запуск скачивания ----------------------------------------------------

    def _download_checked(self):
        tracks = [track for track in self.tracks if track["iid"] in self.checked]
        if not tracks:
            messagebox.showinfo(
                "Ничего не отмечено",
                "Поставьте галочки у треков или нажмите «Отметить все».",
            )
            return
        self._start_download(tracks)

    def _download_all(self):
        if not self.tracks:
            messagebox.showinfo("Список пуст", "Сначала загрузите список треков.")
            return
        self._start_download(list(self.tracks))

    def _start_download(self, tracks):
        if self.worker is not None and self.worker.is_alive():
            messagebox.showinfo("Уже идёт загрузка", "Дождитесь окончания или нажмите «Стоп».")
            return

        folder = self.folder_var.get().strip()
        if not folder:
            messagebox.showwarning("Папка не выбрана", "Укажите папку для сохранения.")
            return
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("Не удалось создать папку", str(exc))
            return

        template = self.template_var.get().strip() or "{artist} - {title}"
        workers = self._workers_count()
        self._save_config()

        for track in tracks:
            track["status"] = ""
        self.total = len(tracks)
        self.progress["value"] = 0
        self.cancel_event = threading.Event()
        self.status_var.set(f"Скачиваю 0 из {self.total}…")
        self._log(
            f"Скачивание {self.total} трек(ов) в «{folder}» · потоков: {workers}"
        )
        self._set_busy(True)
        self.worker = threading.Thread(
            target=self._download_worker,
            args=(
                tracks, folder, template, self.skip_var.get(),
                self.token_var.get().strip(), workers,
            ),
            daemon=True,
        )
        self.worker.start()

    def _set_busy(self, busy):
        state = "disabled" if busy else "normal"
        self.btn_load.configure(state=state)
        self.btn_playlists.configure(state=state)
        self.btn_dl_checked.configure(state=state)
        self.btn_dl_all.configure(state=state)
        self.workers_spin.configure(state=state)
        self.btn_stop.configure(state="normal" if busy else "disabled")

    def _download_worker(self, tracks, folder, template, skip_existing, token, workers=4):
        total = len(tracks)
        lock = threading.Lock()
        progress = {track["iid"]: 0.0 for track in tracks}
        reserved = set()
        stats = {"ok": 0, "skip": 0, "fail": 0, "cancel": 0, "denied": 0}
        access_denied = threading.Event()
        last_emit = [0.0]

        def emit(kind, **payload):
            self.events.put((kind, payload))

        def overall():
            with lock:
                return sum(progress.values()) / total if total else 0.0

        def report(iid, fraction):
            with lock:
                progress[iid] = fraction
            now = time.time()
            if fraction < 1 and now - last_emit[0] < 0.1:
                return
            last_emit[0] = now
            value = overall()
            emit("progress", value=value)
            done = sum(1 for item in progress.values() if item >= 1)
            emit("status", text=f"Скачано {done} из {total} · {value * 100:.0f}%")

        def job(track):
            iid = track["iid"]

            if access_denied.is_set():
                with lock:
                    stats["denied"] += 1
                emit("row", iid=iid, text="нет доступа")
                report(iid, 1.0)
                return

            if self.cancel_event.is_set():
                with lock:
                    stats["cancel"] += 1
                report(iid, 1.0)
                return

            emit("row", iid=iid, text="…")
            filename = format_filename(track, template) + ".mp3"

            try:
                os.makedirs(folder, exist_ok=True)
            except OSError as exc:
                with lock:
                    stats["fail"] += 1
                emit("row", iid=iid, text="ошибка")
                emit("log", text=f"✖  {filename} — не удалось создать папку: {exc}")
                report(iid, 1.0)
                return

            with lock:
                dest = unique_path(folder, filename, reserved)

            if skip_existing and os.path.exists(dest) and existing_is_audio(dest):
                with lock:
                    stats["skip"] += 1
                emit("row", iid=iid, text="уже есть")
                emit("log", text=f"⏭  {filename} — уже есть, пропускаю")
                report(iid, 1.0)
                return

            if os.path.exists(dest):
                emit("log", text=f"↻  {filename} — старый файл повреждён, перекачиваю")

            def on_progress(fraction):
                report(iid, fraction if fraction is not None else 0.0)

            try:
                download_track(track, dest, self.cancel_event, on_progress, token)
            except DownloadCancelled:
                with lock:
                    stats["cancel"] += 1
                emit("row", iid=iid, text="отменено")
                emit("log", text=f"■  {filename} — отменено")
                report(iid, 1.0)
                return
            except NoAudioAccess as exc:
                with lock:
                    stats["fail"] += 1
                    stats["denied"] += 1
                    denied_now = stats["denied"]
                    should_abort = denied_now >= 3 and stats["ok"] == 0
                emit("row", iid=iid, text="нет доступа")
                emit("log", text=f"✖  {filename} — {exc}")
                report(iid, 1.0)
                if should_abort and not access_denied.is_set():
                    access_denied.set()
                    emit("log", text=(
                        "■  Похоже, токен не имеет доступа к аудио: вместо музыки VK присылает "
                        "заглушки или страницы ошибки. Останавливаю загрузку."
                    ))
                    emit("access_denied")
                return
            except Exception as exc:
                with lock:
                    stats["fail"] += 1
                emit("row", iid=iid, text="ошибка")
                emit("log", text=f"✖  {filename} — {exc}")
                report(iid, 1.0)
                return

            write_tags(dest, track)
            size = os.path.getsize(dest)
            with lock:
                stats["ok"] += 1
            emit("row", iid=iid, text="готово")
            emit("log", text=f"✔  {filename} — {fmt_size(size)}")
            report(iid, 1.0)

        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(job, track) for track in tracks]
                for future in as_completed(futures):
                    future.result()
        except Exception as exc:
            emit("log", text=f"✖  Ошибка загрузчика: {exc}")

        emit(
            "finished",
            stats=stats,
            cancelled=self.cancel_event.is_set(),
            access_denied=access_denied.is_set(),
        )

    def _stop(self):
        if self.worker is not None and self.worker.is_alive():
            self.cancel_event.set()
            self.status_var.set("Останавливаю…")
            self._log("■  Запрошена остановка, докачиваю текущие фрагменты…")

    def _on_finished(self, stats, cancelled, access_denied=False):
        self._set_busy(False)
        self.worker = None
        if not cancelled:
            self.progress["value"] = 100

        parts = [f"скачано: {stats['ok']}"]
        if stats["skip"]:
            parts.append(f"пропущено: {stats['skip']}")
        if stats["denied"]:
            parts.append(f"нет доступа: {stats['denied']}")
        other_fail = stats["fail"] - stats["denied"]
        if other_fail:
            parts.append(f"ошибок: {other_fail}")
        if stats["cancel"]:
            parts.append(f"отменено: {stats['cancel']}")
        summary = " · ".join(parts)

        self.status_var.set(("Остановлено. " if cancelled else "Готово! ") + summary)
        self._log(("■  Остановлено. " if cancelled else "✔  Готово. ") + summary)

        if access_denied:
            messagebox.showerror(
                "VK не отдаёт аудио",
                "Вместо музыкальных файлов VK присылает заглушки или страницы ошибки.\n\n"
                "Скорее всего, токен не имеет доступа к аудио: с 2017 года VK ограничивает "
                "audio API для сторонних приложений. Программа не обходит это ограничение.\n\n"
                f"Реально скачано треков: {stats['ok']}. Подробности — в журнале.",
            )
        elif not cancelled and stats["fail"] == 0:
            messagebox.showinfo("Загрузка завершена", f"Скачано файлов: {stats['ok']}")
        elif stats["fail"]:
            messagebox.showwarning(
                "Загрузка завершена с ошибками",
                summary + "\n\nПодробности — в журнале.",
            )

    # -- справка --------------------------------------------------------------

    def _show_token_help(self):
        text = (
            "1. Создайте Standalone-приложение: https://dev.vk.com → «Мои приложения» → «Создать».\n"
            "2. Скопируйте ID приложения и откройте в браузере ссылку (подставьте свой ID):\n\n"
            "https://oauth.vk.com/authorize?client_id=ВАШ_ID&display=page"
            "&redirect_uri=https://oauth.vk.com/blank.html&scope=audio,offline"
            "&response_type=token&v=5.131\n\n"
            "3. Разрешите доступ. Откроется пустая страница blank.html — скопируйте из адресной "
            "строки значение access_token=… (до символа &).\n"
            "4. Вставьте токен в поле «Токен» и нажмите «Загрузить мою музыку».\n\n"
            "Важно: с 2017 года VK ограничивает доступ к аудио для сторонних приложений. "
            "Если после загрузки списка в журнале появится предупреждение, что VK не выдал ссылки, "
            "значит токен не имеет доступа к аудио — программа не может это обойти.\n\n"
            "Скачивайте только свою музыку или контент, на который у вас есть права."
        )
        messagebox.showinfo("Как получить токен", text)


class PlaylistDialog(tk.Toplevel):
    """Окно со списком плейлистов пользователя."""

    def __init__(self, app, token):
        super().__init__(app)
        self.app = app
        self.token = token
        self.playlists = []
        self.by_iid = {}
        self.queue = queue.Queue()
        self.search_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Загружаю плейлисты…")

        self.title("Плейлисты VK")
        self.geometry("760x520")
        self.minsize(600, 400)
        self.transient(app)
        self.protocol("WM_DELETE_WINDOW", self._close)

        self._build_ui()
        self.search_var.trace_add("write", lambda *_: self._refresh())
        self.grab_set()
        self.after(100, self._poll)
        threading.Thread(target=self._load_worker, daemon=True).start()

    def _build_ui(self):
        outer = ttk.Frame(self, padding=10)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        top = ttk.Frame(outer)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text="Поиск:").grid(row=0, column=0)
        ttk.Entry(top, textvariable=self.search_var).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(top, text="Обновить", command=self._reload).grid(row=0, column=2)

        ttk.Label(outer, textvariable=self.status_var, foreground="#666").grid(
            row=1, column=0, sticky="w", pady=(6, 4)
        )

        tree_frame = ttk.Frame(outer)
        tree_frame.grid(row=2, column=0, sticky="nsew")
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)

        columns = ("title", "count", "desc")
        self.tree = ttk.Treeview(
            tree_frame, columns=columns, show="headings", selectmode="browse"
        )
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=vsb.set)

        self.tree.heading("title", text="Плейлист")
        self.tree.heading("count", text="Треков")
        self.tree.heading("desc", text="Описание")
        self.tree.column("title", width=340, anchor="w", stretch=True)
        self.tree.column("count", width=70, anchor="center")
        self.tree.column("desc", width=280, anchor="w", stretch=True)
        self.tree.bind("<Double-1>", lambda _event: self._open_selected())
        self.tree.bind("<Return>", lambda _event: self._open_selected())

        buttons = ttk.Frame(outer)
        buttons.grid(row=3, column=0, sticky="e", pady=(8, 0))
        ttk.Button(buttons, text="Закрыть", command=self._close).pack(side="right")
        ttk.Button(buttons, text="Загрузить треки", command=self._open_selected).pack(
            side="right", padx=(0, 8)
        )

    def _reload(self):
        self.playlists = []
        self.by_iid = {}
        self._refresh()
        self.status_var.set("Загружаю плейлисты…")
        threading.Thread(target=self._load_worker, daemon=True).start()

    def _load_worker(self):
        def on_page(loaded, total):
            self.queue.put(("progress", loaded, total))

        try:
            items = fetch_playlists(self.token, on_page=on_page)
            self.queue.put(("done", items))
        except VkError as exc:
            self.queue.put(("error", describe_error(exc)))
        except Exception as exc:
            self.queue.put(("error", str(exc)))

    def _poll(self):
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return

        try:
            while True:
                message = self.queue.get_nowait()
                kind = message[0]
                if kind == "progress":
                    self.status_var.set(
                        f"Загружено плейлистов: {message[1]} из {message[2] or '?'}…"
                    )
                elif kind == "done":
                    self._set_playlists(message[1])
                elif kind == "error":
                    self.status_var.set("Не удалось загрузить плейлисты")
                    messagebox.showerror("Плейлисты", message[1], parent=self)
        except queue.Empty:
            pass

        try:
            self.after(100, self._poll)
        except tk.TclError:
            pass

    def _set_playlists(self, items):
        self.playlists = items
        self.status_var.set(f"Плейлистов: {len(items)}")
        self._refresh()

    def _refresh(self):
        children = self.tree.get_children()
        if children:
            self.tree.delete(*children)

        query = self.search_var.get().strip().lower()
        self.by_iid = {}
        for index, playlist in enumerate(self.playlists):
            if query and query not in playlist["title"].lower():
                continue
            iid = f"pl_{index}"
            self.by_iid[iid] = playlist
            self.tree.insert(
                "", "end", iid=iid,
                values=(playlist["title"], playlist["count"], playlist["description"]),
            )

    def _open_selected(self):
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo(
                "Плейлист не выбран", "Выберите плейлист в списке.", parent=self
            )
            return
        playlist = self.by_iid.get(selection[0])
        if not playlist:
            return
        self.app.load_playlist(self.token, playlist)
        self._close()

    def _close(self):
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()

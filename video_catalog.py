from __future__ import annotations

import configparser
import hashlib
import json
import mimetypes
import os
import pickle
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

try:
    from passlib.context import CryptContext

    pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
    HAS_BCRYPT = True
except ImportError:
    pwd_context = None
    HAS_BCRYPT = False


def get_video_id(video_path: str) -> str:
    return hashlib.md5(video_path.encode("utf-8")).hexdigest()[:16]


def get_mime_type(file_path: str) -> str:
    mime_type, _ = mimetypes.guess_type(file_path)
    return mime_type or "application/octet-stream"


class VideoServer:
    def __init__(
        self,
        config_path: str,
        thumbnail_dir: Path,
        scan_cache_file: Path,
        is_temp_file: Callable[[str], bool],
    ):
        self.config = configparser.ConfigParser()
        self.config.read(config_path, encoding="utf-8")
        self.host = self.config.get("server", "host", fallback="0.0.0.0")
        self.port = self.config.getint("server", "port", fallback=8000)
        self.auth_enabled = self.config.getboolean("auth", "enabled", fallback=False)
        self.auth_username = self.config.get("auth", "username", fallback="admin")
        self.auth_password = self.config.get("auth", "password", fallback="hx123456")
        self.secret_key = self.config.get("auth", "secret_key", fallback="videoserver-secret-key-change-in-production")
        self.thumbnail_dir = thumbnail_dir
        self.scan_cache_file = scan_cache_file
        self.cache_dir = self.scan_cache_file.parent / ".media_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.video_info_cache_file = self.cache_dir / "video_info_cache.json"
        self.directory_listing_cache_file = self.cache_dir / "directory_listings.pkl"
        self.scan_cache_lock = threading.Lock()
        self.scan_cache_expire_hours = 1
        self.is_temp_file = is_temp_file
        self.video_index_lock = threading.Lock()
        self.video_index: dict[str, str] = {}
        self.video_index_built_at = 0.0
        self.video_index_ttl_seconds = 30
        self.video_meta_index: dict[str, dict] = {}
        self.video_meta_index_built_at = 0.0
        self.video_info_cache_lock = threading.Lock()
        self.video_info_cache: dict[str, dict] = {}
        self.video_info_cache_loaded = False
        self.video_info_cache_ttl_seconds = 3600 * 24
        self.directories_cache_lock = threading.Lock()
        self.directories_cache: list[dict] = []
        self.directories_cache_built_at = 0.0
        self.directories_cache_ttl_seconds = 15
        self.directory_listing_cache_lock = threading.Lock()
        self.directory_listing_cache: dict[str, dict] = {}
        self.directory_listing_cache_loaded = False
        self.scan_cache_memory: dict = {}
        self.scan_cache_memory_mtime: float = 0.0
        self.cache_validation_lock = threading.Lock()
        self.cache_validation_checked_at = 0.0
        self.cache_validation_cache_scanned_at = 0.0
        self.cache_validation_result = False
        self.cache_validate_interval_seconds = 5
        self.background_scan_lock = threading.Lock()
        self.background_scan_thread: threading.Thread | None = None

        dirs_str = self.config.get("video", "directories", fallback="~/Movies")
        self.video_dirs = []
        self.video_dir_names = {}
        for d in dirs_str.split(","):
            d = d.strip()
            if "=" in d:
                name, path = d.split("=", 1)
                name = name.strip()
                path = path.strip()
            else:
                name = os.path.basename(d)
                path = d
            if path.startswith("~"):
                path = os.path.expanduser(path)
            if os.path.isdir(path):
                self.video_dirs.append(path)
                self.video_dir_names[path] = name

        exts_str = self.config.get("video", "extensions", fallback="mp4,mkv,avi,mov,wmv,flv,webm,m4v")
        self.extensions = set(f'.{e.strip().lower().lstrip(".")}' for e in exts_str.split(","))
        self.videos_per_page = self.config.getint("ui", "videos_per_page", fallback=30)

    def _is_hashed_password(self, password: str) -> bool:
        if not HAS_BCRYPT:
            return False
        return password.startswith("$2a$") or password.startswith("$2b$") or password.startswith("$2y$")

    def verify_password(self, plain_password: str) -> bool:
        if HAS_BCRYPT and self._is_hashed_password(self.auth_password):
            try:
                return pwd_context.verify(plain_password, self.auth_password)
            except Exception:
                return False
        return plain_password == self.auth_password

    def get_directories(self, cached_only: bool = False) -> list[dict]:
        now = time.time()
        with self.directories_cache_lock:
            if (
                not cached_only
                and self.directories_cache
                and (now - self.directories_cache_built_at) < self.directories_cache_ttl_seconds
            ):
                return list(self.directories_cache)

        if cached_only:
            with self.scan_cache_lock:
                cache = self._load_scan_cache()
                all_videos = self._ensure_video_ids(cache.get("videos", []))
        else:
            all_videos = self.scan_videos(use_cache=True)
        counts: dict[str, int] = {}
        for video in all_videos:
            base_dir = video.get("base_dir")
            if not base_dir:
                continue
            counts[base_dir] = counts.get(base_dir, 0) + 1
        dirs = []
        for d in self.video_dirs:
            name = self.video_dir_names.get(d, os.path.basename(d))
            video_count = counts.get(d, 0)
            dirs.append({"name": name, "path": d, "video_count": video_count})
        if not cached_only:
            with self.directories_cache_lock:
                self.directories_cache = dirs
                self.directories_cache_built_at = now
        return dirs

    def _count_videos(self, directory: str) -> int:
        count = 0
        try:
            for root, _, files in os.walk(directory):
                for f in files:
                    if os.path.splitext(f)[1].lower() in self.extensions:
                        count += 1
        except Exception:
            pass
        return count

    def list_directory(self, directory: str, relative_path: str = "") -> dict:
        if directory not in self.video_dirs:
            valid = False
            for base_dir in self.video_dirs:
                if directory.startswith(base_dir + os.sep):
                    valid = True
                    break
            if not valid:
                return {"error": "无效的目录"}

        full_path = os.path.join(directory, relative_path) if relative_path else directory
        if not os.path.exists(full_path):
            return {"error": "目录不存在"}

        current_path = relative_path.strip(os.sep)
        current_prefix = f"{current_path}{os.sep}" if current_path else ""
        scan_signature = self._get_scan_signature()
        cache_key = f"{directory}|{current_path}"
        if scan_signature:
            self._ensure_directory_listing_cache_loaded()
            with self.directory_listing_cache_lock:
                cached_entry = self.directory_listing_cache.get(cache_key)
                if cached_entry and cached_entry.get("scan_signature") == scan_signature:
                    return dict(cached_entry["result"])
        base_videos = [
            dict(video)
            for video in self.scan_videos(use_cache=True)
            if video.get("base_dir") == directory
        ]
        folder_map: dict[str, dict] = {}
        videos = []

        for video in base_videos:
            rel_path = video.get("rel_path", "")
            if not rel_path:
                continue
            if current_prefix:
                if not rel_path.startswith(current_prefix):
                    continue
                remaining_path = rel_path[len(current_prefix):]
            else:
                remaining_path = rel_path
            if not remaining_path:
                continue

            path_parts = remaining_path.split(os.sep)
            if len(path_parts) == 1:
                videos.append(video)
                continue

            folder_name = path_parts[0]
            folder_rel_path = os.path.join(current_path, folder_name) if current_path else folder_name
            folder_entry = folder_map.setdefault(
                folder_rel_path,
                {
                    "name": folder_name,
                    "path": folder_rel_path,
                    "type": "folder",
                    "video_count": 0,
                    "subfolder_names": set(),
                },
            )
            if len(path_parts) == 2:
                folder_entry["video_count"] += 1
            else:
                folder_entry["subfolder_names"].add(path_parts[1])

        folders = []
        for folder_entry in sorted(folder_map.values(), key=lambda entry: entry["name"].lower()):
            folders.append(
                {
                    "name": folder_entry["name"],
                    "path": folder_entry["path"],
                    "type": folder_entry["type"],
                    "subfolder_count": len(folder_entry["subfolder_names"]),
                    "video_count": folder_entry["video_count"],
                }
            )
        videos.sort(key=lambda video: video["name"].lower())
        result = {
            "folders": folders,
            "videos": videos,
            "current_path": current_path,
            "parent_path": os.path.dirname(current_path) if current_path else None,
        }
        if scan_signature:
            self._ensure_directory_listing_cache_loaded()
            with self.directory_listing_cache_lock:
                self.directory_listing_cache[cache_key] = {
                    "scan_signature": scan_signature,
                    "result": result,
                }
            self._persist_directory_listing_cache()
        return result

    def _summarize_folder(self, directory: str) -> tuple[int, int]:
        subfolder_count = 0
        video_count = 0
        try:
            for item in os.listdir(directory):
                item_path = os.path.join(directory, item)
                if os.path.isdir(item_path):
                    if self._has_videos_recursive(item_path):
                        subfolder_count += 1
                elif os.path.isfile(item_path):
                    if os.path.splitext(item)[1].lower() in self.extensions and not self.is_temp_file(item_path):
                        video_count += 1
        except Exception:
            pass
        return subfolder_count, video_count

    def _has_videos_recursive(self, directory: str, max_depth: int = 3) -> bool:
        if max_depth <= 0:
            return False
        try:
            for item in os.listdir(directory):
                item_path = os.path.join(directory, item)
                if os.path.isfile(item_path):
                    if os.path.splitext(item)[1].lower() in self.extensions:
                        return True
                elif os.path.isdir(item_path):
                    if self._has_videos_recursive(item_path, max_depth - 1):
                        return True
        except Exception:
            pass
        return False

    def _get_dir_mtime(self, directory: str) -> float:
        max_mtime = 0
        try:
            for root, _, files in os.walk(directory):
                try:
                    mtime = os.stat(root).st_mtime
                    max_mtime = max(max_mtime, mtime)
                except Exception:
                    pass
                for f in files:
                    try:
                        mtime = os.stat(os.path.join(root, f)).st_mtime
                        max_mtime = max(max_mtime, mtime)
                    except Exception:
                        pass
        except Exception:
            pass
        return max_mtime

    def _get_top_dir_mtime(self, directory: str) -> float:
        try:
            return os.stat(directory).st_mtime
        except Exception:
            return 0.0

    def _load_scan_cache(self) -> dict:
        try:
            if not self.scan_cache_file.exists():
                return {}
            stat = self.scan_cache_file.stat()
            if self.scan_cache_memory and self.scan_cache_memory_mtime == stat.st_mtime:
                return self.scan_cache_memory
            with open(self.scan_cache_file, "rb") as f:
                cache = pickle.load(f)
            self.scan_cache_memory = cache
            self.scan_cache_memory_mtime = stat.st_mtime
            return cache
        except Exception:
            pass
        return {}

    def _save_scan_cache(self, cache: dict):
        try:
            with open(self.scan_cache_file, "wb") as f:
                pickle.dump(cache, f)
            stat = self.scan_cache_file.stat()
            self.scan_cache_memory = cache
            self.scan_cache_memory_mtime = stat.st_mtime
        except Exception:
            pass

    def _ensure_video_info_cache_loaded(self):
        with self.video_info_cache_lock:
            if self.video_info_cache_loaded:
                return
            self.video_info_cache_loaded = True
            try:
                if not self.video_info_cache_file.exists():
                    return
                data = json.loads(self.video_info_cache_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self.video_info_cache = data
            except Exception:
                self.video_info_cache = {}

    def _persist_video_info_cache(self):
        with self.video_info_cache_lock:
            payload = json.dumps(self.video_info_cache, ensure_ascii=False)
        try:
            self.video_info_cache_file.write_text(payload, encoding="utf-8")
        except Exception:
            pass

    def _ensure_directory_listing_cache_loaded(self):
        with self.directory_listing_cache_lock:
            if self.directory_listing_cache_loaded:
                return
            self.directory_listing_cache_loaded = True
            try:
                if not self.directory_listing_cache_file.exists():
                    return
                with open(self.directory_listing_cache_file, "rb") as f:
                    data = pickle.load(f)
                if isinstance(data, dict):
                    self.directory_listing_cache = data
            except Exception:
                self.directory_listing_cache = {}

    def _persist_directory_listing_cache(self):
        with self.directory_listing_cache_lock:
            payload = dict(self.directory_listing_cache)
        try:
            with open(self.directory_listing_cache_file, "wb") as f:
                pickle.dump(payload, f)
        except Exception:
            pass

    def _get_scan_signature(self) -> float:
        with self.scan_cache_lock:
            cache = self._load_scan_cache()
        return float(cache.get("scanned_at", 0) or 0)

    def _ensure_video_ids(self, videos: list[dict]) -> list[dict]:
        for video in videos:
            if "id" not in video:
                video["id"] = get_video_id(video["path"])
        return videos

    def _build_indexes_from_videos(self, videos: list[dict], built_at: float):
        path_index = {v["id"]: v["path"] for v in videos}
        meta_index = {v["id"]: v for v in videos}
        with self.video_index_lock:
            self.video_index = path_index
            self.video_meta_index = meta_index
            self.video_index_built_at = built_at
            self.video_meta_index_built_at = built_at

    def _is_cache_valid(self, cache: dict) -> bool:
        if not cache or "scanned_at" not in cache or "videos" not in cache:
            return False
        age_hours = (time.time() - cache.get("scanned_at", 0)) / 3600
        if age_hours > self.scan_cache_expire_hours:
            return False
        now = time.time()
        scanned_at = cache.get("scanned_at", 0)
        with self.cache_validation_lock:
            if (
                self.cache_validation_cache_scanned_at == scanned_at
                and (now - self.cache_validation_checked_at) < self.cache_validate_interval_seconds
            ):
                return self.cache_validation_result
        cached_mtimes = cache.get("dir_mtimes", {})
        is_valid = True
        for base_dir in self.video_dirs:
            current_mtime = self._get_top_dir_mtime(base_dir)
            cached_mtime = cached_mtimes.get(base_dir, 0)
            if current_mtime > cached_mtime:
                is_valid = False
                break
        with self.cache_validation_lock:
            self.cache_validation_cache_scanned_at = scanned_at
            self.cache_validation_checked_at = now
            self.cache_validation_result = is_valid
        return is_valid

    def has_usable_scan_cache(self) -> bool:
        with self.scan_cache_lock:
            cache = self._load_scan_cache()
        return self._is_cache_valid(cache)

    def is_background_scan_running(self) -> bool:
        with self.background_scan_lock:
            return bool(self.background_scan_thread and self.background_scan_thread.is_alive())

    def ensure_background_scan(self) -> bool:
        with self.background_scan_lock:
            if self.background_scan_thread and self.background_scan_thread.is_alive():
                return False

            def _scan():
                self.refresh_scan_cache()

            self.background_scan_thread = threading.Thread(target=_scan, daemon=True)
            self.background_scan_thread.start()
            return True

    def scan_videos(self, search: str = "", directory: str = None, use_cache: bool = True) -> list[dict]:
        dirs_to_scan = [directory] if directory else self.video_dirs
        if use_cache:
            with self.scan_cache_lock:
                cache = self._load_scan_cache()
                if self._is_cache_valid(cache):
                    cached_videos = self._ensure_video_ids(cache.get("videos", []))
                    if not search and not directory:
                        return cached_videos
                    normalized_search = search.lower()
                    return [
                        video
                        for video in cached_videos
                        if (not directory or video.get("base_dir") == directory)
                        and (not normalized_search or normalized_search in video.get("name", "").lower())
                    ]

        videos = []
        for base_dir in dirs_to_scan:
            if not os.path.exists(base_dir):
                continue
            for root, _, files in os.walk(base_dir):
                for file in files:
                    ext = os.path.splitext(file)[1].lower()
                    if ext not in self.extensions:
                        continue
                    full_path = os.path.join(root, file)
                    if self.is_temp_file(full_path):
                        continue
                    if search and search.lower() not in file.lower():
                        continue
                    try:
                        stat = os.stat(full_path)
                        rel_path = os.path.relpath(full_path, base_dir)
                        parent_dir = os.path.dirname(rel_path)
                        videos.append(
                            {
                                "id": get_video_id(full_path),
                                "name": file,
                                "path": full_path,
                                "rel_path": rel_path,
                                "parent_dir": parent_dir if parent_dir else "",
                                "size": stat.st_size,
                                "size_mb": round(stat.st_size / (1024 * 1024), 1),
                                "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                                "ext": ext[1:].upper(),
                                "base_dir": base_dir,
                            }
                        )
                    except (OSError, IOError):
                        continue
        videos.sort(key=lambda x: x["modified"], reverse=True)

        if not search and not directory:
            dir_mtimes = {d: self._get_top_dir_mtime(d) for d in self.video_dirs}
            with self.scan_cache_lock:
                self._save_scan_cache({"videos": videos, "scanned_at": time.time(), "dir_mtimes": dir_mtimes})
        return videos

    def refresh_scan_cache(self):
        with self.scan_cache_lock:
            if self.scan_cache_file.exists():
                try:
                    self.scan_cache_file.unlink()
                except Exception:
                    pass
            self.scan_cache_memory = {}
            self.scan_cache_memory_mtime = 0.0
        with self.directory_listing_cache_lock:
            self.directory_listing_cache = {}
            self.directory_listing_cache_loaded = True
            try:
                if self.directory_listing_cache_file.exists():
                    self.directory_listing_cache_file.unlink()
            except Exception:
                pass
        with self.video_index_lock:
            self.video_index = {}
            self.video_meta_index = {}
            self.video_index_built_at = 0.0
            self.video_meta_index_built_at = 0.0
        with self.directories_cache_lock:
            self.directories_cache = []
            self.directories_cache_built_at = 0.0
        self.scan_videos(use_cache=False)

    def _get_video_index(self) -> dict[str, str]:
        now = time.time()
        with self.video_index_lock:
            if self.video_index and (now - self.video_index_built_at) < self.video_index_ttl_seconds:
                return dict(self.video_index)
        videos = self.scan_videos()
        self._build_indexes_from_videos(videos, now)
        with self.video_index_lock:
            return dict(self.video_index)

    def get_video_path(self, video_id: str) -> Optional[str]:
        return self._get_video_index().get(video_id)

    def get_video_by_id(self, video_id: str) -> Optional[dict]:
        now = time.time()
        with self.video_index_lock:
            if self.video_meta_index and (now - self.video_meta_index_built_at) < self.video_index_ttl_seconds:
                video = self.video_meta_index.get(video_id)
                return dict(video) if video else None
        videos = self.scan_videos()
        self._build_indexes_from_videos(videos, now)
        with self.video_index_lock:
            video = self.video_meta_index.get(video_id)
            return dict(video) if video else None

    def get_video_paths(self, video_ids: list[str]) -> dict[str, str]:
        index = self._get_video_index()
        return {video_id: path for video_id in video_ids if (path := index.get(video_id))}

    def invalidate_video(self, video_id: str, video_path: str):
        with self.video_index_lock:
            self.video_index.pop(video_id, None)
            self.video_meta_index.pop(video_id, None)
        self._ensure_video_info_cache_loaded()
        with self.video_info_cache_lock:
            self.video_info_cache.pop(video_path, None)
        self._persist_video_info_cache()
        with self.directories_cache_lock:
            self.directories_cache = []
            self.directories_cache_built_at = 0.0
        with self.directory_listing_cache_lock:
            self.directory_listing_cache = {}
        self._persist_directory_listing_cache()

    def get_scan_cache_status(self) -> dict:
        with self.video_index_lock:
            index_size = len(self.video_index)
            index_age_seconds = round(time.time() - self.video_index_built_at, 1) if self.video_index_built_at else None
            meta_index_size = len(self.video_meta_index)
        with self.video_info_cache_lock:
            info_cache_size = len(self.video_info_cache)
        return {
            "exists": self.scan_cache_file.exists(),
            "index_size": index_size,
            "index_age_seconds": index_age_seconds,
            "meta_index_size": meta_index_size,
            "video_info_cache_size": info_cache_size,
        }

    def get_video_info(self, video_path: str) -> dict:
        info = {
            "duration": None,
            "duration_formatted": None,
            "width": None,
            "height": None,
            "resolution": None,
            "codec": None,
            "bitrate": None,
            "fps": None,
        }
        self._ensure_video_info_cache_loaded()
        try:
            stat = os.stat(video_path)
            now = time.time()
            with self.video_info_cache_lock:
                cached = self.video_info_cache.get(video_path)
                if cached:
                    if (
                        cached.get("mtime") == stat.st_mtime
                        and cached.get("size") == stat.st_size
                        and (now - cached.get("checked_at", 0)) < self.video_info_cache_ttl_seconds
                    ):
                        return self._normalize_video_info(dict(cached.get("info", info)))
        except OSError:
            return info
        try:
            cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", video_path]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                data = json.loads(result.stdout)
                for stream in data.get("streams", []):
                    if stream.get("codec_type") == "video":
                        info["width"] = stream.get("width")
                        info["height"] = stream.get("height")
                        info["codec"] = stream.get("codec_name")
                        fps_str = stream.get("r_frame_rate", "0/1")
                        if "/" in fps_str:
                            num, den = fps_str.split("/")
                            if int(den) != 0:
                                info["fps"] = round(int(num) / int(den), 2)
                        break
                fmt = data.get("format", {})
                duration = float(fmt.get("duration", 0))
                if duration > 0:
                    info["duration"] = duration
                    info["duration_formatted"] = self._format_duration(duration)
                    info["bitrate"] = fmt.get("bit_rate")
        except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
            pass
        try:
            stat = os.stat(video_path)
            with self.video_info_cache_lock:
                self.video_info_cache[video_path] = {
                    "mtime": stat.st_mtime,
                    "size": stat.st_size,
                    "checked_at": time.time(),
                    "info": dict(info),
                }
        except OSError:
            pass
        else:
            self._persist_video_info_cache()
        return self._normalize_video_info(info)

    def _normalize_video_info(self, info: dict) -> dict:
        normalized = dict(info)
        width = normalized.get("width")
        height = normalized.get("height")
        if width and height and not normalized.get("resolution"):
            normalized["resolution"] = f"{width}x{height}"
        return normalized

    def _format_duration(self, seconds: float) -> str:
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        if hours > 0:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        return f"{minutes}:{secs:02d}"

    def get_thumbnail(self, video_path: str, timestamp: float = 5.0) -> Optional[str]:
        video_hash = hashlib.md5(video_path.encode()).hexdigest()
        thumb_path = self.thumbnail_dir / f"{video_hash}.jpg"
        if thumb_path.exists():
            return str(thumb_path)
        try:
            cmd = [
                "ffmpeg",
                "-y",
                "-ss",
                str(timestamp),
                "-i",
                video_path,
                "-vframes",
                "1",
                "-vf",
                "scale=320:-1",
                "-q:v",
                "3",
                str(thumb_path),
            ]
            result = subprocess.run(cmd, capture_output=True, timeout=30)
            if result.returncode == 0 and thumb_path.exists():
                return str(thumb_path)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        return None

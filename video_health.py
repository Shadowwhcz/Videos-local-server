from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from typing import Dict, List, Optional

DOWNLOADING_EXTENSIONS = {
    ".part",
    ".downloading",
    ".temp",
    ".crdownload",
    ".partial",
    ".download",
    ".!ut",
    ".opdownload",
    ".xltd",
    ".td",
    ".tmp",
}

FILE_SIZE_CACHE: Dict[str, dict] = {}
FILE_SIZE_CACHE_LOCK = threading.Lock()
FILE_SIZE_CHECK_INTERVAL = 2.0

VIDEO_INTEGRITY_CACHE: Dict[str, dict] = {}
CACHE_LOCK = threading.Lock()
CACHE_EXPIRE_HOURS = 24


def is_temp_file(file_path: str) -> bool:
    _, ext = os.path.splitext(file_path)
    if ext.lower() in DOWNLOADING_EXTENSIONS:
        return True
    for temp_ext in DOWNLOADING_EXTENSIONS:
        if os.path.exists(file_path + temp_ext):
            return True
    return False


def is_file_locked(file_path: str) -> bool:
    try:
        result = subprocess.run(
            ["lsof", "-f", "--", file_path],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            lines = result.stdout.strip().split("\n")
            for line in lines[1:]:
                if line.strip():
                    return True
        return False
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    try:
        test_path = file_path + ".locktest"
        try:
            os.rename(file_path, test_path)
            os.rename(test_path, file_path)
            return False
        except OSError:
            return True
    except Exception:
        return False


def is_file_growing(file_path: str) -> bool:
    if not os.path.exists(file_path):
        return False
    try:
        current_size = os.path.getsize(file_path)
        current_time = time.time()
        with FILE_SIZE_CACHE_LOCK:
            cached = FILE_SIZE_CACHE.get(file_path)
            if cached:
                time_diff = current_time - cached.get("checked_at", 0)
                if time_diff >= FILE_SIZE_CHECK_INTERVAL:
                    if current_size != cached.get("size"):
                        FILE_SIZE_CACHE[file_path] = {"size": current_size, "checked_at": current_time}
                        return True
                    FILE_SIZE_CACHE[file_path] = {"size": current_size, "checked_at": current_time}
                    return False
                return False
            FILE_SIZE_CACHE[file_path] = {"size": current_size, "checked_at": current_time}
            return False
    except OSError:
        return False


def check_video_status(video_path: str) -> dict:
    if not os.path.exists(video_path):
        return {"status": "corrupted", "reason": "文件不存在"}
    if is_temp_file(video_path):
        return {"status": "downloading", "reason": "临时文件"}
    if is_file_locked(video_path):
        return {"status": "downloading", "reason": "文件被占用"}
    if is_file_growing(video_path):
        return {"status": "downloading", "reason": "正在写入"}
    try:
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "json",
            video_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            return {"status": "corrupted", "reason": "无法读取视频流"}
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        if not streams or not streams[0].get("codec_name"):
            return {"status": "corrupted", "reason": "无视频流"}
        return {"status": "normal", "reason": None}
    except subprocess.TimeoutExpired:
        return {"status": "corrupted", "reason": "检查超时"}
    except json.JSONDecodeError:
        return {"status": "corrupted", "reason": "解析失败"}
    except FileNotFoundError:
        return {"status": "normal", "reason": None}
    except Exception as e:
        return {"status": "corrupted", "reason": str(e)[:30]}


def check_single_video_integrity(video_path: str) -> dict:
    if not os.path.exists(video_path):
        return {"valid": False, "error": "文件不存在"}
    try:
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,width,height,duration",
            "-of",
            "json",
            video_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            error_msg = result.stderr.strip() if result.stderr else "无法读取视频信息"
            return {"valid": False, "error": error_msg[:100]}
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        if not streams:
            return {"valid": False, "error": "未找到视频流"}
        video_stream = streams[0]
        if not video_stream.get("codec_name"):
            return {"valid": False, "error": "缺少编解码器信息"}
        return {
            "valid": True,
            "info": {
                "codec": video_stream.get("codec_name"),
                "width": video_stream.get("width"),
                "height": video_stream.get("height"),
                "duration": video_stream.get("duration"),
            },
        }
    except subprocess.TimeoutExpired:
        return {"valid": False, "error": "检查超时"}
    except json.JSONDecodeError:
        return {"valid": False, "error": "解析视频信息失败"}
    except FileNotFoundError:
        return {"valid": False, "error": "ffprobe 未安装"}
    except Exception as e:
        return {"valid": False, "error": str(e)[:50]}


def get_cached_integrity(video_id: str) -> Optional[dict]:
    with CACHE_LOCK:
        cached = VIDEO_INTEGRITY_CACHE.get(video_id)
        if cached:
            age_hours = (time.time() - cached.get("checked_at", 0)) / 3600
            if age_hours < CACHE_EXPIRE_HOURS:
                return cached
    return None


def set_cached_integrity(video_id: str, result: dict):
    with CACHE_LOCK:
        VIDEO_INTEGRITY_CACHE[video_id] = {**result, "checked_at": time.time()}


def background_check_videos(video_ids: List[str], video_paths: Dict[str, str]):
    for video_id in video_ids:
        if get_cached_integrity(video_id):
            continue
        video_path = video_paths.get(video_id)
        if video_path:
            result = check_single_video_integrity(video_path)
            set_cached_integrity(video_id, result)
            time.sleep(0.1)


def clear_integrity_cache(video_id: str):
    with CACHE_LOCK:
        VIDEO_INTEGRITY_CACHE.pop(video_id, None)

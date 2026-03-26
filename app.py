"""
局域网视频网站服务 - 主应用
"""
import os
import configparser
from pathlib import Path
from typing import Optional, List, Dict
import mimetypes
import json
import subprocess
import hashlib
from datetime import datetime, timedelta
from urllib.parse import unquote
import threading
import time
import pickle
import secrets
from dataclasses import dataclass, field
import asyncio
from functools import lru_cache
from collections import OrderedDict
import io

from fastapi import FastAPI, Request, Query, HTTPException, Depends, Form, BackgroundTasks, Body
from fastapi.responses import StreamingResponse, FileResponse, HTMLResponse, Response, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
import uvicorn

# ==================== 性能优化配置 ====================
# 流媒体优化参数（针对外接硬盘优化）
STREAM_CHUNK_SIZE = 4 * 1024 * 1024  # 4MB 基础块大小（从1MB增加）
STREAM_PREFETCH_SIZE = 16 * 1024 * 1024  # 16MB 预读缓冲区
FILE_HANDLE_CACHE_SIZE = 32  # 缓存的文件句柄数量
FILE_HANDLE_TTL = 300  # 文件句柄缓存时间（秒）

# 初始化应用
app = FastAPI(title="局域网视频服务器")

# 读取配置以获取 secret_key
_config = configparser.ConfigParser()
_config.read("config.ini", encoding='utf-8')
_secret_key = _config.get('auth', 'secret_key', fallback='videoserver-secret-key-change-in-production')

# ==================== 服务端 Session 管理 ====================
# 支持同一账号多设备登录，每个设备有独立的 session

@dataclass
class UserSession:
    """用户会话信息"""
    session_id: str
    username: str
    created_at: float
    last_active: float
    device_info: str = ""

class SessionManager:
    """服务端 Session 管理器"""
    
    def __init__(self, session_expire_hours: int = 24 * 7):  # 默认7天过期
        self.sessions: Dict[str, UserSession] = {}  # session_id -> UserSession
        self.user_sessions: Dict[str, List[str]] = {}  # username -> [session_id1, session_id2, ...]
        self.lock = threading.RLock()
        self.expire_hours = session_expire_hours
    
    def create_session(self, username: str, device_info: str = "") -> str:
        """创建新会话，返回 session_id"""
        session_id = secrets.token_urlsafe(32)
        now = time.time()
        
        with self.lock:
            session = UserSession(
                session_id=session_id,
                username=username,
                created_at=now,
                last_active=now,
                device_info=device_info
            )
            self.sessions[session_id] = session
            
            # 添加到用户的会话列表
            if username not in self.user_sessions:
                self.user_sessions[username] = []
            self.user_sessions[username].append(session_id)
        
        return session_id
    
    def get_session(self, session_id: str) -> Optional[UserSession]:
        """获取会话信息"""
        with self.lock:
            session = self.sessions.get(session_id)
            if session:
                # 检查是否过期
                if time.time() - session.last_active > self.expire_hours * 3600:
                    self.delete_session(session_id)
                    return None
                # 更新最后活跃时间
                session.last_active = time.time()
            return session
    
    def delete_session(self, session_id: str):
        """删除会话"""
        with self.lock:
            session = self.sessions.pop(session_id, None)
            if session:
                # 从用户的会话列表中移除
                if session.username in self.user_sessions:
                    try:
                        self.user_sessions[session.username].remove(session_id)
                        if not self.user_sessions[session.username]:
                            del self.user_sessions[session.username]
                    except ValueError:
                        pass
    
    def delete_user_sessions(self, username: str):
        """删除用户的所有会话（登出所有设备）"""
        with self.lock:
            session_ids = self.user_sessions.pop(username, [])
            for sid in session_ids:
                self.sessions.pop(sid, None)
    
    def get_user_session_count(self, username: str) -> int:
        """获取用户的活跃会话数"""
        with self.lock:
            return len(self.user_sessions.get(username, []))
    
    def cleanup_expired(self):
        """清理过期会话"""
        now = time.time()
        expired = []
        
        with self.lock:
            for session_id, session in list(self.sessions.items()):
                if now - session.last_active > self.expire_hours * 3600:
                    expired.append(session_id)
        
        for sid in expired:
            self.delete_session(sid)

# 全局 Session 管理器
session_manager = SessionManager()

# 添加 Session 中间件（仅用于存储 session_id）
app.add_middleware(
    SessionMiddleware,
    secret_key=_secret_key,
    session_cookie="video_session_id",
    max_age=60 * 60 * 24 * 7,  # 7天
    same_site="lax",  # 允许跨站请求但有限制
)

# 获取项目根目录
BASE_DIR = Path(__file__).resolve().parent


def get_video_id(video_path: str) -> str:
    """生成确定性的视频ID（使用MD5，跨进程一致）"""
    return hashlib.md5(video_path.encode('utf-8')).hexdigest()[:16]


# ==================== 下载状态检测 ====================

def is_temp_file(file_path: str) -> bool:
    """检查文件是否有临时下载后缀"""
    # 检查文件本身的扩展名
    _, ext = os.path.splitext(file_path)
    if ext.lower() in DOWNLOADING_EXTENSIONS:
        return True
    
    # 检查同目录下是否有对应的临时文件
    # 例如 video.mp4.part 或 video.mp4.downloading
    for temp_ext in DOWNLOADING_EXTENSIONS:
        temp_path = file_path + temp_ext
        if os.path.exists(temp_path):
            return True
    
    return False


def is_file_locked(file_path: str) -> bool:
    """检查文件是否被其他进程占用/锁定（macOS/Linux）"""
    try:
        # 使用 lsof 检查文件是否被打开
        result = subprocess.run(
            ['lsof', '-f', '--', file_path],
            capture_output=True,
            text=True,
            timeout=5
        )
        # lsof 返回非零表示文件未被打开，返回零且有输出表示被打开
        if result.returncode == 0 and result.stdout.strip():
            # 过滤掉当前进程
            lines = result.stdout.strip().split('\n')
            for line in lines[1:]:  # 跳过标题行
                if line.strip():
                    return True
        return False
    except (subprocess.TimeoutExpired, FileNotFoundError):
        # lsof 不可用，尝试另一种方法
        pass
    
    # 备用方法：尝试以独占模式打开文件
    try:
        # 尝试重命名文件来测试是否被锁定
        test_path = file_path + '.locktest'
        try:
            os.rename(file_path, test_path)
            os.rename(test_path, file_path)
            return False
        except OSError:
            return True
    except:
        return False


def is_file_growing(file_path: str) -> bool:
    """检查文件大小是否在变化（表示正在下载）
    
    需要连续两次检查（间隔 FILE_SIZE_CHECK_INTERVAL 秒）且大小变化才返回 True
    首次检查只记录状态，不判断为正在下载
    """
    if not os.path.exists(file_path):
        return False
    
    try:
        # 获取当前大小
        current_size = os.path.getsize(file_path)
        current_time = time.time()
        
        with FILE_SIZE_CACHE_LOCK:
            cached = FILE_SIZE_CACHE.get(file_path)
            
            if cached:
                time_diff = current_time - cached.get("checked_at", 0)
                
                # 必须超过检测间隔才能判断
                if time_diff >= FILE_SIZE_CHECK_INTERVAL:
                    if current_size != cached.get("size"):
                        # 大小变化，正在下载，更新缓存
                        FILE_SIZE_CACHE[file_path] = {
                            "size": current_size,
                            "checked_at": current_time
                        }
                        return True
                    else:
                        # 大小没变，更新时间戳，不是下载中
                        FILE_SIZE_CACHE[file_path] = {
                            "size": current_size,
                            "checked_at": current_time
                        }
                        return False
                else:
                    # 还在等待间隔，返回上次结果（False，不确定）
                    return False
            
            # 首次检查，只记录大小，不判断为下载中
            FILE_SIZE_CACHE[file_path] = {
                "size": current_size,
                "checked_at": current_time
            }
            return False
    except OSError:
        return False


def check_video_status(video_path: str) -> dict:
    """
    检查视频文件状态
    返回: {
        "status": "normal" | "downloading" | "corrupted",
        "reason": str (可选，说明原因)
    }
    """
    if not os.path.exists(video_path):
        return {"status": "corrupted", "reason": "文件不存在"}
    
    # 1. 检查临时后缀
    if is_temp_file(video_path):
        return {"status": "downloading", "reason": "临时文件"}
    
    # 2. 检查文件是否被锁定
    if is_file_locked(video_path):
        return {"status": "downloading", "reason": "文件被占用"}
    
    # 3. 检查文件大小是否在变化
    if is_file_growing(video_path):
        return {"status": "downloading", "reason": "正在写入"}
    
    # 4. 检查视频完整性（快速检查）
    try:
        cmd = [
            'ffprobe', '-v', 'error',
            '-select_streams', 'v:0',
            '-show_entries', 'stream=codec_name',
            '-of', 'json',
            video_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        
        if result.returncode != 0:
            return {"status": "corrupted", "reason": "无法读取视频流"}
        
        data = json.loads(result.stdout)
        streams = data.get('streams', [])
        
        if not streams or not streams[0].get('codec_name'):
            return {"status": "corrupted", "reason": "无视频流"}
        
        return {"status": "normal", "reason": None}
        
    except subprocess.TimeoutExpired:
        return {"status": "corrupted", "reason": "检查超时"}
    except json.JSONDecodeError:
        return {"status": "corrupted", "reason": "解析失败"}
    except FileNotFoundError:
        # ffprobe 不可用，假设正常
        return {"status": "normal", "reason": None}
    except Exception as e:
        return {"status": "corrupted", "reason": str(e)[:30]}


# 缩略图缓存目录
THUMBNAIL_DIR = BASE_DIR / "thumbnails"
THUMBNAIL_DIR.mkdir(exist_ok=True)

# 视频完整性检查缓存（内存缓存）
# 结构: {video_id: {"valid": bool, "error": str, "checked_at": float, "info": dict}}
VIDEO_INTEGRITY_CACHE: Dict[str, dict] = {}
CACHE_LOCK = threading.Lock()
CACHE_EXPIRE_HOURS = 24  # 缓存过期时间

# 下载中文件检测临时后缀
DOWNLOADING_EXTENSIONS = {
    '.part', '.downloading', '.temp', '.crdownload', 
    '.partial', '.download', '.!ut', '.opdownload',
    '.xltd', '.td', '.tmp'
}

# 文件大小变化检测缓存
# 结构: {video_path: {"size": int, "checked_at": float}}
FILE_SIZE_CACHE: Dict[str, dict] = {}
FILE_SIZE_CACHE_LOCK = threading.Lock()
FILE_SIZE_CHECK_INTERVAL = 2.0  # 秒

# ==================== 文件句柄缓存 ====================
class FileHandleCache:
    """LRU缓存文件句柄，减少频繁打开文件的开销"""
    
    def __init__(self, max_size: int = FILE_HANDLE_CACHE_SIZE, ttl: int = FILE_HANDLE_TTL):
        self.max_size = max_size
        self.ttl = ttl
        self.cache: OrderedDict[str, dict] = OrderedDict()
        self.lock = threading.RLock()
    
    def get(self, file_path: str) -> Optional[io.BufferedReader]:
        """获取缓存的文件句柄"""
        with self.lock:
            if file_path in self.cache:
                entry = self.cache[file_path]
                # 检查是否过期
                if time.time() - entry['accessed_at'] < self.ttl:
                    # 移到末尾（最近使用）
                    self.cache.move_to_end(file_path)
                    entry['accessed_at'] = time.time()
                    return entry['handle']
                else:
                    # 过期，关闭并移除
                    try:
                        entry['handle'].close()
                    except:
                        pass
                    del self.cache[file_path]
        return None
    
    def put(self, file_path: str, handle: io.BufferedReader):
        """缓存文件句柄"""
        with self.lock:
            # 如果已存在，先关闭旧的
            if file_path in self.cache:
                try:
                    self.cache[file_path]['handle'].close()
                except:
                    pass
            
            # 添加新句柄
            self.cache[file_path] = {
                'handle': handle,
                'accessed_at': time.time()
            }
            self.cache.move_to_end(file_path)
            
            # 超出容量，移除最旧的
            while len(self.cache) > self.max_size:
                oldest_path, oldest_entry = self.cache.popitem(last=False)
                try:
                    oldest_entry['handle'].close()
                except:
                    pass
    
    def close_all(self):
        """关闭所有缓存的文件句柄"""
        with self.lock:
            for entry in self.cache.values():
                try:
                    entry['handle'].close()
                except:
                    pass
            self.cache.clear()

# 全局文件句柄缓存
file_handle_cache = FileHandleCache()

# ==================== 预读缓冲区 ====================
class PrefetchBuffer:
    """预读缓冲区，在后台线程中预读取数据"""
    
    def __init__(self, file_path: str, start_pos: int, prefetch_size: int = STREAM_PREFETCH_SIZE):
        self.file_path = file_path
        self.start_pos = start_pos
        self.prefetch_size = prefetch_size
        self.buffer = bytearray()
        self.error = None
        self.done = threading.Event()
        self.thread = None
    
    def start(self):
        """启动预读线程"""
        self.thread = threading.Thread(target=self._prefetch, daemon=True)
        self.thread.start()
    
    def _prefetch(self):
        """后台预读数据"""
        try:
            # 尝试使用缓存的文件句柄
            handle = file_handle_cache.get(self.file_path)
            own_handle = False
            
            if handle is None:
                handle = open(self.file_path, 'rb')
                own_handle = True
            
            try:
                handle.seek(self.start_pos)
                data = handle.read(self.prefetch_size)
                self.buffer.extend(data)
                
                # 如果是自己打开的，缓存起来
                if own_handle:
                    file_handle_cache.put(self.file_path, handle)
            finally:
                # 如果是自己打开的且没有缓存，则关闭
                if own_handle and not file_handle_cache.get(self.file_path):
                    handle.close()
        except Exception as e:
            self.error = e
        finally:
            self.done.set()
    
    def get_data(self, timeout: float = 2.0) -> Optional[bytes]:
        """获取预读的数据"""
        if self.done.wait(timeout):
            if self.error:
                return None
            return bytes(self.buffer)
        return None

def open_file_with_cache(file_path: str) -> io.BufferedReader:
    """打开文件（优先使用缓存）"""
    handle = file_handle_cache.get(file_path)
    if handle:
        # 检查文件是否仍然有效
        try:
            handle.seek(0, 2)  # 移到末尾测试
            handle.seek(0)  # 回到开头
            return handle
        except:
            # 文件句柄无效，重新打开
            pass
    
    handle = open(file_path, 'rb')
    file_handle_cache.put(file_path, handle)
    return file_handle_cache.get(file_path) or handle

# 视频扫描缓存（文件缓存）
VIDEO_SCAN_CACHE_FILE = BASE_DIR / ".video_scan_cache"
VIDEO_SCAN_CACHE: Dict[str, dict] = {}  # 结构: {"videos": [...], "scanned_at": float, "dir_mtimes": {...}}
VIDEO_SCAN_CACHE_LOCK = threading.Lock()
VIDEO_SCAN_CACHE_EXPIRE_HOURS = 1  # 缓存过期时间（小时）

# 静态文件和模板
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
app.mount("/thumbnails", StaticFiles(directory=THUMBNAIL_DIR), name="thumbnails")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

# 添加自定义 Jinja2 过滤器
def dirname_filter(path):
    """获取路径的父目录"""
    if not path:
        return ''
    return os.path.dirname(path)

templates.env.filters['dirname'] = dirname_filter


class VideoServer:
    """视频服务器配置和工具类"""
    
    def __init__(self, config_path: str = "config.ini"):
        self.config = configparser.ConfigParser()
        self.config.read(config_path, encoding='utf-8')
        
        # 服务器配置
        self.host = self.config.get('server', 'host', fallback='0.0.0.0')
        self.port = self.config.getint('server', 'port', fallback=8000)
        
        # 认证配置
        self.auth_enabled = self.config.getboolean('auth', 'enabled', fallback=False)
        self.auth_username = self.config.get('auth', 'username', fallback='admin')
        self.auth_password = self.config.get('auth', 'password', fallback='hx123456')
        self.secret_key = self.config.get('auth', 'secret_key', fallback='videoserver-secret-key-change-in-production')
        
        # 视频目录
        dirs_str = self.config.get('video', 'directories', fallback='~/Movies')
        self.video_dirs = []
        self.video_dir_names = {}  # 目录别名映射
        for d in dirs_str.split(','):
            d = d.strip()
            if '=' in d:
                # 支持别名: name=/path/to/dir
                name, path = d.split('=', 1)
                name = name.strip()
                path = path.strip()
            else:
                name = os.path.basename(d)
                path = d
            
            if path.startswith('~'):
                path = os.path.expanduser(path)
            
            if os.path.isdir(path):
                self.video_dirs.append(path)
                self.video_dir_names[path] = name
        
        # 支持的格式
        exts_str = self.config.get('video', 'extensions', fallback='mp4,mkv,avi,mov,wmv,flv,webm,m4v')
        self.extensions = set(f'.{e.strip().lower().lstrip(".")}' for e in exts_str.split(','))
        
        # UI配置
        self.videos_per_page = self.config.getint('ui', 'videos_per_page', fallback=30)
    
    def get_directories(self) -> List[dict]:
        """获取配置的视频目录列表"""
        dirs = []
        for d in self.video_dirs:
            name = self.video_dir_names.get(d, os.path.basename(d))
            video_count = self._count_videos(d)
            dirs.append({
                'name': name,
                'path': d,
                'video_count': video_count,
            })
        return dirs
    
    def _count_videos(self, directory: str) -> int:
        """计算目录中的视频数量"""
        count = 0
        try:
            for root, _, files in os.walk(directory):
                for f in files:
                    if os.path.splitext(f)[1].lower() in self.extensions:
                        count += 1
        except:
            pass
        return count
    
    def list_directory(self, directory: str, relative_path: str = "") -> dict:
        """列出指定目录下的文件夹和视频"""
        if directory not in self.video_dirs:
            # 检查是否是子目录
            valid = False
            for base_dir in self.video_dirs:
                if directory.startswith(base_dir + os.sep):
                    valid = True
                    break
            if not valid:
                return {'error': '无效的目录'}
        
        full_path = os.path.join(directory, relative_path) if relative_path else directory
        
        if not os.path.exists(full_path):
            return {'error': '目录不存在'}
        
        folders = []
        videos = []
        
        try:
            items = sorted(os.listdir(full_path), key=lambda x: x.lower())
        except PermissionError:
            return {'error': '无法访问该目录'}
        
        for item in items:
            item_path = os.path.join(full_path, item)
            item_rel_path = os.path.join(relative_path, item) if relative_path else item
            
            if os.path.isdir(item_path):
                # 检查文件夹是否包含视频
                has_videos = self._has_videos_recursive(item_path)
                if has_videos:
                    folders.append({
                        'name': item,
                        'path': item_rel_path,
                        'type': 'folder',
                    })
            else:
                ext = os.path.splitext(item)[1].lower()
                if ext in self.extensions:
                    # 跳过临时下载文件
                    if is_temp_file(item_path):
                        continue
                    
                    try:
                        stat = os.stat(item_path)
                        videos.append({
                            'name': item,
                            'path': item_path,
                            'rel_path': item_rel_path,
                            'size': stat.st_size,
                            'size_mb': round(stat.st_size / (1024 * 1024), 1),
                            'modified': datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M'),
                            'ext': ext[1:].upper(),
                            'base_dir': directory,
                        })
                    except (OSError, IOError):
                        continue
        
        return {
            'folders': folders,
            'videos': videos,
            'current_path': relative_path,
            'parent_path': os.path.dirname(relative_path) if relative_path else None,
        }
    
    def _has_videos_recursive(self, directory: str, max_depth: int = 3) -> bool:
        """检查目录是否包含视频文件（递归）"""
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
        except:
            pass
        return False
    
    def _get_dir_mtime(self, directory: str) -> float:
        """获取目录及其子目录的最新修改时间"""
        max_mtime = 0
        try:
            for root, dirs, files in os.walk(directory):
                # 检查目录修改时间
                try:
                    mtime = os.stat(root).st_mtime
                    max_mtime = max(max_mtime, mtime)
                except:
                    pass
                # 检查文件修改时间
                for f in files:
                    try:
                        mtime = os.stat(os.path.join(root, f)).st_mtime
                        max_mtime = max(max_mtime, mtime)
                    except:
                        pass
        except:
            pass
        return max_mtime
    
    def _load_scan_cache(self) -> dict:
        """从文件加载扫描缓存"""
        try:
            if VIDEO_SCAN_CACHE_FILE.exists():
                with open(VIDEO_SCAN_CACHE_FILE, 'rb') as f:
                    return pickle.load(f)
        except:
            pass
        return {}
    
    def _save_scan_cache(self, cache: dict):
        """保存扫描缓存到文件"""
        try:
            with open(VIDEO_SCAN_CACHE_FILE, 'wb') as f:
                pickle.dump(cache, f)
        except:
            pass
    
    def _is_cache_valid(self, cache: dict) -> bool:
        """检查缓存是否有效"""
        if not cache or 'scanned_at' not in cache or 'videos' not in cache:
            return False
        
        # 检查时间过期
        age_hours = (time.time() - cache.get('scanned_at', 0)) / 3600
        if age_hours > VIDEO_SCAN_CACHE_EXPIRE_HOURS:
            return False
        
        # 检查目录是否有变化
        cached_mtimes = cache.get('dir_mtimes', {})
        for base_dir in self.video_dirs:
            current_mtime = self._get_dir_mtime(base_dir)
            cached_mtime = cached_mtimes.get(base_dir, 0)
            if current_mtime > cached_mtime:
                return False
        
        return True
    
    def scan_videos(self, search: str = "", directory: str = None, use_cache: bool = True) -> list[dict]:
        """扫描视频文件（支持缓存）"""
        dirs_to_scan = [directory] if directory else self.video_dirs
        cache_key = ",".join(sorted(dirs_to_scan))
        
        # 尝试使用缓存（仅当不搜索且使用全部目录时）
        if use_cache and not search and not directory:
            with VIDEO_SCAN_CACHE_LOCK:
                cache = self._load_scan_cache()
                if self._is_cache_valid(cache):
                    return cache.get('videos', [])
        
        # 执行扫描
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
                    
                    # 跳过临时下载文件
                    if is_temp_file(full_path):
                        continue
                    
                    # 搜索过滤
                    if search and search.lower() not in file.lower():
                        continue
                    
                    # 获取文件信息
                    try:
                        stat = os.stat(full_path)
                        rel_path = os.path.relpath(full_path, base_dir)
                        
                        # 提取文件所在目录（相对路径的父目录）
                        parent_dir = os.path.dirname(rel_path)
                        
                        videos.append({
                            'name': file,
                            'path': full_path,
                            'rel_path': rel_path,
                            'parent_dir': parent_dir if parent_dir else '',  # 空表示在根目录
                            'size': stat.st_size,
                            'size_mb': round(stat.st_size / (1024 * 1024), 1),
                            'modified': datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M'),
                            'ext': ext[1:].upper(),
                            'base_dir': base_dir,
                        })
                    except (OSError, IOError):
                        continue
        
        # 按修改时间排序（最新在前）
        videos.sort(key=lambda x: x['modified'], reverse=True)
        
        # 更新缓存（仅当不搜索且使用全部目录时）
        if use_cache and not search and not directory:
            dir_mtimes = {d: self._get_dir_mtime(d) for d in self.video_dirs}
            with VIDEO_SCAN_CACHE_LOCK:
                cache = {
                    'videos': videos,
                    'scanned_at': time.time(),
                    'dir_mtimes': dir_mtimes,
                }
                self._save_scan_cache(cache)
        
        return videos
    
    def refresh_scan_cache(self):
        """强制刷新扫描缓存"""
        with VIDEO_SCAN_CACHE_LOCK:
            # 删除旧缓存
            if VIDEO_SCAN_CACHE_FILE.exists():
                try:
                    VIDEO_SCAN_CACHE_FILE.unlink()
                except:
                    pass
        # 重新扫描
        self.scan_videos(use_cache=False)
    
    def get_video_path(self, video_id: str) -> Optional[str]:
        """根据视频ID获取路径"""
        videos = self.scan_videos()
        for v in videos:
            # 使用确定性的ID匹配
            if get_video_id(v['path']) == video_id:
                return v['path']
        return None
    
    def get_video_info(self, video_path: str) -> dict:
        """获取视频详细信息（使用ffprobe）"""
        info = {
            'duration': None,
            'duration_formatted': None,
            'width': None,
            'height': None,
            'codec': None,
            'bitrate': None,
            'fps': None,
        }
        
        try:
            cmd = [
                'ffprobe', '-v', 'quiet', '-print_format', 'json',
                '-show_format', '-show_streams', video_path
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            
            if result.returncode == 0:
                data = json.loads(result.stdout)
                
                # 获取视频流信息
                for stream in data.get('streams', []):
                    if stream.get('codec_type') == 'video':
                        info['width'] = stream.get('width')
                        info['height'] = stream.get('height')
                        info['codec'] = stream.get('codec_name')
                        
                        # 获取帧率
                        fps_str = stream.get('r_frame_rate', '0/1')
                        if '/' in fps_str:
                            num, den = fps_str.split('/')
                            if int(den) != 0:
                                info['fps'] = round(int(num) / int(den), 2)
                        break
                
                # 获取格式信息
                fmt = data.get('format', {})
                duration = float(fmt.get('duration', 0))
                if duration > 0:
                    info['duration'] = duration
                    info['duration_formatted'] = self._format_duration(duration)
                    info['bitrate'] = fmt.get('bit_rate')
        
        except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
            pass
        
        return info
    
    def _format_duration(self, seconds: float) -> str:
        """格式化时长"""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        
        if hours > 0:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        else:
            return f"{minutes}:{secs:02d}"
    
    def get_thumbnail(self, video_path: str, timestamp: float = 5.0) -> Optional[str]:
        """生成视频缩略图"""
        # 使用视频路径的hash作为缓存文件名
        video_hash = hashlib.md5(video_path.encode()).hexdigest()
        thumb_path = THUMBNAIL_DIR / f"{video_hash}.jpg"
        
        # 如果缩略图已存在，直接返回
        if thumb_path.exists():
            return str(thumb_path)
        
        # 使用ffmpeg生成缩略图
        try:
            cmd = [
                'ffmpeg', '-y', '-ss', str(timestamp),
                '-i', video_path,
                '-vframes', '1',
                '-vf', 'scale=320:-1',
                '-q:v', '3',
                str(thumb_path)
            ]
            result = subprocess.run(cmd, capture_output=True, timeout=30)
            
            if result.returncode == 0 and thumb_path.exists():
                return str(thumb_path)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        
        return None


# 全局配置实例
video_server = VideoServer(str(BASE_DIR / "config.ini"))


def get_mime_type(file_path: str) -> str:
    """获取文件的MIME类型"""
    mime_type, _ = mimetypes.guess_type(file_path)
    return mime_type or 'application/octet-stream'


# ==================== 认证相关 ====================

def get_current_user(request: Request) -> Optional[str]:
    """获取当前登录用户"""
    session_id = request.session.get("session_id")
    if not session_id:
        return None
    
    session = session_manager.get_session(session_id)
    if not session:
        # Session 已过期或无效，清除 cookie
        request.session.clear()
        return None
    
    return session.username


def require_auth(request: Request):
    """验证用户是否已登录"""
    if video_server.auth_enabled:
        user = get_current_user(request)
        if not user:
            raise HTTPException(
                status_code=302,
                headers={"Location": "/login"}
            )
    return True


# ==================== 路由 ====================

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """登录页面"""
    # 如果已经登录，重定向到首页
    if get_current_user(request):
        return RedirectResponse(url="/", status_code=302)
    
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": None}
    )


@app.post("/login", response_class=HTMLResponse)
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...)
):
    """处理登录"""
    if username == video_server.auth_username and password == video_server.auth_password:
        # 获取设备信息（User-Agent）
        device_info = request.headers.get("user-agent", "Unknown")[:200]
        
        # 创建服务端会话
        session_id = session_manager.create_session(username, device_info)
        
        # 在 cookie 中只存储 session_id
        request.session["session_id"] = session_id
        
        print(f"✅ 用户 {username} 登录成功，当前活跃会话数: {session_manager.get_user_session_count(username)}")
        
        return RedirectResponse(url="/", status_code=302)
    
    print(f"❌ 登录失败: 用户名={username}")
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": "用户名或密码错误"}
    )


@app.get("/logout")
async def logout(request: Request):
    """登出当前设备"""
    session_id = request.session.get("session_id")
    if session_id:
        session = session_manager.get_session(session_id)
        if session:
            username = session.username
            session_manager.delete_session(session_id)
            print(f"👋 用户 {username} 登出，剩余活跃会话数: {session_manager.get_user_session_count(username)}")
    
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)

@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    search: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    dir_path: str = Query(default=""),
    browse: str = Query(default=""),
):
    """首页 - 视频列表/目录浏览"""
    # 认证检查
    if video_server.auth_enabled and not get_current_user(request):
        return RedirectResponse(url="/login", status_code=302)
    
    directories = video_server.get_directories()
    
    # 如果指定了浏览目录
    if browse:
        browse_result = video_server.list_directory(browse, dir_path)
        if 'error' in browse_result:
            raise HTTPException(status_code=400, detail=browse_result['error'])
        
        # 为视频生成ID
        for v in browse_result['videos']:
            v['id'] = get_video_id(v['path'])
        
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "directories": directories,
                "browse_result": browse_result,
                "search": search,
                "page": page,
                "current_browse": browse,
                "current_path": dir_path,
            }
        )
    
    # 全局搜索或全部视频列表
    all_videos = video_server.scan_videos(search)
    
    # 分页
    total = len(all_videos)
    per_page = video_server.videos_per_page
    total_pages = (total + per_page - 1) // per_page
    page = min(page, total_pages) if total_pages > 0 else 1
    
    start = (page - 1) * per_page
    videos = all_videos[start:start + per_page]
    
    # 为每个视频生成ID
    for v in videos:
        v['id'] = get_video_id(v['path'])
    
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "directories": directories,
            "videos": videos,
            "search": search,
            "page": page,
            "total_pages": total_pages,
            "total": total,
        }
    )


@app.get("/play/{video_id}", response_class=HTMLResponse)
async def play(request: Request, video_id: str):
    """播放页面"""
    # 认证检查
    if video_server.auth_enabled and not get_current_user(request):
        return RedirectResponse(url="/login", status_code=302)
    
    videos = video_server.scan_videos()
    video = None
    for v in videos:
        if get_video_id(v['path']) == video_id:
            video = v
            break
    
    if not video:
        raise HTTPException(status_code=404, detail="视频不存在")
    
    # 获取视频详细信息
    video_info = video_server.get_video_info(video['path'])
    video.update(video_info)
    video['id'] = video_id
    
    return templates.TemplateResponse(
        "play.html",
        {"request": request, "video": video}
    )


def generate_etag(file_path: str) -> str:
    """生成文件ETag（基于修改时间和大小）"""
    stat = os.stat(file_path)
    return f'"{stat.st_mtime}-{stat.st_size}"'


def parse_range_header(range_header: str, file_size: int) -> list[tuple[int, int]]:
    """
    解析Range请求头，返回(start, end)元组列表
    支持格式: bytes=0-499, bytes=500-, bytes=-500
    """
    ranges = []
    
    if not range_header or not range_header.startswith('bytes='):
        return ranges
    
    range_spec = range_header[6:]  # 去掉 'bytes='
    
    for part in range_spec.split(','):
        part = part.strip()
        if '-' not in part:
            continue
        
        start_str, end_str = part.split('-', 1)
        start_str = start_str.strip()
        end_str = end_str.strip()
        
        try:
            if start_str and end_str:
                # bytes=0-499
                start = int(start_str)
                end = int(end_str)
            elif start_str:
                # bytes=500- (从start到文件末尾)
                start = int(start_str)
                end = file_size - 1
            elif end_str:
                # bytes=-500 (最后500字节)
                start = max(0, file_size - int(end_str))
                end = file_size - 1
            else:
                continue
            
            # 验证范围
            if start < 0 or end < start or start >= file_size:
                continue
            
            # 限制end不超过文件大小
            end = min(end, file_size - 1)
            ranges.append((start, end))
        except ValueError:
            continue
    
    return ranges


@app.api_route("/stream/{video_id}", methods=["GET", "HEAD"])
async def stream_video(video_id: str, request: Request):
    """
    视频流传输（完整支持HTTP Range请求，优化版）
    
    优化特性:
    - 文件句柄缓存（减少频繁打开文件的开销）
    - 预读缓冲（后台线程预取数据，减少等待时间）
    - 增大的块大小（4MB，适合视频流）
    - 针对外接硬盘优化（减少IO次数）
    """
    # 认证检查
    if video_server.auth_enabled and not get_current_user(request):
        raise HTTPException(status_code=401, detail="未登录")
    
    videos = video_server.scan_videos()
    video_path = None
    
    for v in videos:
        if get_video_id(v['path']) == video_id:
            video_path = v['path']
            break
    
    if not video_path or not os.path.exists(video_path):
        raise HTTPException(status_code=404, detail="视频不存在")
    
    file_size = os.path.getsize(video_path)
    mime_type = get_mime_type(video_path)
    etag = generate_etag(video_path)
    
    # 基础响应头
    base_headers = {
        'Accept-Ranges': 'bytes',
        'Content-Type': mime_type,
        'ETag': etag,
        'Cache-Control': 'public, max-age=31536000',  # 缓存1年
        'X-Content-Type-Options': 'nosniff',
    }
    
    # HEAD请求只返回头信息
    if request.method == 'HEAD':
        return Response(
            status_code=200,
            headers={**base_headers, 'Content-Length': str(file_size)},
        )
    
    # 检查If-Range条件
    range_header = request.headers.get('range')
    if_range = request.headers.get('if-range')
    
    if range_header:
        # 如果有If-Range头，需要验证条件
        if if_range:
            # If-Range可以是ETag或日期
            # ETag格式需要完全匹配（包含引号）
            if if_range.strip('"') != etag.strip('"'):
                # 条件不满足，返回完整文件
                range_header = None
    
    if range_header:
        # 解析Range请求
        ranges = parse_range_header(range_header, file_size)
        
        if not ranges:
            # 无效的Range请求
            return Response(
                status_code=416,
                headers={
                    'Content-Range': f'bytes */{file_size}',
                    'Accept-Ranges': 'bytes',
                },
            )
        
        # 只处理第一个Range（浏览器通常只发一个）
        start, end = ranges[0]
        content_length = end - start + 1
        
        def iterfile_optimized():
            """优化的文件迭代器，支持预读缓冲"""
            try:
                # 尝试使用缓存的文件句柄
                f = file_handle_cache.get(video_path)
                own_handle = False
                
                if f is None:
                    f = open(video_path, 'rb')
                    own_handle = True
                
                f.seek(start)
                remaining = content_length
                chunk_size = STREAM_CHUNK_SIZE
                
                # 预读第一块数据（同步读取，确保立即可用）
                first_chunk_size = min(chunk_size * 2, remaining)  # 首次读取更大块
                data = f.read(first_chunk_size)
                if data:
                    remaining -= len(data)
                    yield data
                
                # 继续读取剩余数据
                while remaining > 0:
                    read_size = min(chunk_size, remaining)
                    data = f.read(read_size)
                    if not data:
                        break
                    remaining -= len(data)
                    yield data
                
                # 缓存文件句柄
                if own_handle:
                    file_handle_cache.put(video_path, f)
                    
            except Exception as e:
                print(f"Stream error: {e}")
                # 出错时确保关闭文件
                try:
                    if 'f' in locals() and f:
                        f.close()
                except:
                    pass
        
        headers = {
            **base_headers,
            'Content-Range': f'bytes {start}-{end}/{file_size}',
            'Content-Length': str(content_length),
        }
        
        return StreamingResponse(
            iterfile_optimized(),
            status_code=206,
            headers=headers,
        )
    else:
        # 无Range请求，返回完整文件
        def iterfile_full():
            """返回完整文件的迭代器"""
            try:
                f = file_handle_cache.get(video_path)
                own_handle = False
                
                if f is None:
                    f = open(video_path, 'rb')
                    own_handle = True
                
                chunk_size = STREAM_CHUNK_SIZE
                while chunk := f.read(chunk_size):
                    yield chunk
                
                # 缓存文件句柄
                if own_handle:
                    file_handle_cache.put(video_path, f)
                    
            except Exception as e:
                print(f"Stream error: {e}")
                try:
                    if 'f' in locals() and f:
                        f.close()
                except:
                    pass
        
        return StreamingResponse(
            iterfile_full(),
            media_type=mime_type,
            headers={
                **base_headers,
                'Content-Length': str(file_size),
            }
        )


@app.get("/api/videos")
async def api_videos(search: str = "", directory: str = None):
    """API: 获取视频列表"""
    videos = video_server.scan_videos(search, directory)
    for v in videos:
        v['id'] = get_video_id(v['path'])
    return {"videos": videos, "total": len(videos)}


@app.get("/api/directories")
async def api_directories():
    """API: 获取可用的视频目录列表"""
    return {"directories": video_server.get_directories()}


@app.get("/api/browse")
async def api_browse(
    directory: str = Query(default=""),
    path: str = Query(default=""),
):
    """API: 浏览目录内容"""
    if not directory:
        return {"error": "请指定目录"}
    
    result = video_server.list_directory(directory, path)
    return result


@app.get("/api/video/info/{video_id}")
async def api_video_info(video_id: str):
    """API: 获取视频详细信息"""
    video_path = video_server.get_video_path(video_id)
    if not video_path:
        raise HTTPException(status_code=404, detail="视频不存在")
    
    info = video_server.get_video_info(video_path)
    
    # 获取基本文件信息
    stat = os.stat(video_path)
    info['size'] = stat.st_size
    info['size_mb'] = round(stat.st_size / (1024 * 1024), 1)
    info['modified'] = datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M')
    
    return info


@app.get("/api/video/thumbnail/{video_id}")
async def api_video_thumbnail(
    video_id: str,
    timestamp: float = Query(default=5.0, description="截图时间点（秒）"),
):
    """API: 获取视频缩略图"""
    video_path = video_server.get_video_path(video_id)
    if not video_path:
        raise HTTPException(status_code=404, detail="视频不存在")
    
    thumb_path = video_server.get_thumbnail(video_path, timestamp)
    
    if thumb_path and os.path.exists(thumb_path):
        return FileResponse(thumb_path, media_type="image/jpeg")
    else:
        # 返回默认占位图
        raise HTTPException(status_code=404, detail="无法生成缩略图")


@app.get("/api/config")
async def api_config():
    """API: 获取配置信息"""
    return {
        "host": video_server.host,
        "port": video_server.port,
        "directories": video_server.video_dirs,
        "extensions": list(video_server.extensions),
        "auth_enabled": video_server.auth_enabled,
        "stream_optimization": {
            "chunk_size_mb": STREAM_CHUNK_SIZE / (1024 * 1024),
            "prefetch_size_mb": STREAM_PREFETCH_SIZE / (1024 * 1024),
            "file_handle_cache_size": FILE_HANDLE_CACHE_SIZE,
        }
    }


@app.get("/api/cache/status")
async def api_cache_status():
    """API: 获取缓存状态"""
    return {
        "file_handle_cache": {
            "size": len(file_handle_cache.cache),
            "max_size": file_handle_cache.max_size,
            "entries": [
                {
                    "path": os.path.basename(k),
                    "accessed_ago": round(time.time() - v['accessed_at'], 1)
                }
                for k, v in list(file_handle_cache.cache.items())[:10]
            ]
        },
        "video_scan_cache": {
            "exists": VIDEO_SCAN_CACHE_FILE.exists(),
            "cache_size": len(VIDEO_SCAN_CACHE)
        }
    }


@app.on_event("shutdown")
async def shutdown_event():
    """应用关闭时清理资源"""
    print("🔄 关闭文件句柄缓存...")
    file_handle_cache.close_all()
    print("✅ 清理完成")


@app.get("/api/session/status")
async def api_session_status(request: Request):
    """API: 获取当前会话状态"""
    if not video_server.auth_enabled:
        return {"auth_enabled": False, "logged_in": True, "username": "guest"}
    
    session_id = request.session.get("session_id")
    if not session_id:
        return {"auth_enabled": True, "logged_in": False}
    
    session = session_manager.get_session(session_id)
    if not session:
        return {"auth_enabled": True, "logged_in": False}
    
    return {
        "auth_enabled": True,
        "logged_in": True,
        "username": session.username,
        "session_id": session.session_id[:8] + "...",  # 只显示前8位
        "created_at": datetime.fromtimestamp(session.created_at).strftime('%Y-%m-%d %H:%M:%S'),
        "last_active": datetime.fromtimestamp(session.last_active).strftime('%Y-%m-%d %H:%M:%S'),
        "active_sessions": session_manager.get_user_session_count(session.username),
    }


@app.get("/api/session/sessions")
async def api_list_sessions(request: Request):
    """API: 列出当前用户的所有活跃会话（需要登录）"""
    if not video_server.auth_enabled:
        raise HTTPException(status_code=400, detail="认证未启用")
    
    session_id = request.session.get("session_id")
    if not session_id:
        raise HTTPException(status_code=401, detail="未登录")
    
    current_session = session_manager.get_session(session_id)
    if not current_session:
        raise HTTPException(status_code=401, detail="会话已过期")
    
    username = current_session.username
    sessions_info = []
    
    with session_manager.lock:
        user_session_ids = session_manager.user_sessions.get(username, [])
        for sid in user_session_ids:
            sess = session_manager.sessions.get(sid)
            if sess:
                sessions_info.append({
                    "session_id": sess.session_id[:8] + "...",
                    "created_at": datetime.fromtimestamp(sess.created_at).strftime('%Y-%m-%d %H:%M:%S'),
                    "last_active": datetime.fromtimestamp(sess.last_active).strftime('%Y-%m-%d %H:%M:%S'),
                    "device_info": sess.device_info[:50] + "..." if len(sess.device_info) > 50 else sess.device_info,
                    "is_current": sess.session_id == session_id,
                })
    
    return {
        "username": username,
        "total_sessions": len(sessions_info),
        "sessions": sessions_info,
    }


@app.post("/api/session/logout_all")
async def api_logout_all_sessions(request: Request):
    """API: 登出所有设备"""
    if not video_server.auth_enabled:
        raise HTTPException(status_code=400, detail="认证未启用")
    
    session_id = request.session.get("session_id")
    if not session_id:
        raise HTTPException(status_code=401, detail="未登录")
    
    current_session = session_manager.get_session(session_id)
    if not current_session:
        raise HTTPException(status_code=401, detail="会话已过期")
    
    username = current_session.username
    session_count = session_manager.get_user_session_count(username)
    
    # 删除所有会话
    session_manager.delete_user_sessions(username)
    
    request.session.clear()
    
    return {
        "success": True,
        "message": f"已登出所有设备（共 {session_count} 个会话）",
    }


# ==================== 视频完整性检查 ====================

def check_single_video_integrity(video_path: str) -> dict:
    """检查单个视频文件的完整性"""
    if not os.path.exists(video_path):
        return {"valid": False, "error": "文件不存在"}
    
    try:
        # 使用 ffprobe 快速检查
        cmd = [
            'ffprobe', '-v', 'error',
            '-select_streams', 'v:0',
            '-show_entries', 'stream=codec_name,width,height,duration',
            '-of', 'json',
            video_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        
        if result.returncode != 0:
            error_msg = result.stderr.strip() if result.stderr else "无法读取视频信息"
            return {"valid": False, "error": error_msg[:100]}
        
        data = json.loads(result.stdout)
        streams = data.get('streams', [])
        
        if not streams:
            return {"valid": False, "error": "未找到视频流"}
        
        video_stream = streams[0]
        
        if not video_stream.get('codec_name'):
            return {"valid": False, "error": "缺少编解码器信息"}
        
        return {
            "valid": True,
            "info": {
                "codec": video_stream.get('codec_name'),
                "width": video_stream.get('width'),
                "height": video_stream.get('height'),
                "duration": video_stream.get('duration'),
            }
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
    """获取缓存的完整性检查结果"""
    with CACHE_LOCK:
        cached = VIDEO_INTEGRITY_CACHE.get(video_id)
        if cached:
            # 检查是否过期
            age_hours = (time.time() - cached.get("checked_at", 0)) / 3600
            if age_hours < CACHE_EXPIRE_HOURS:
                return cached
    return None


def set_cached_integrity(video_id: str, result: dict):
    """缓存完整性检查结果"""
    with CACHE_LOCK:
        VIDEO_INTEGRITY_CACHE[video_id] = {
            **result,
            "checked_at": time.time()
        }


def background_check_videos(video_ids: List[str], video_paths: Dict[str, str]):
    """后台批量检查视频完整性"""
    for video_id in video_ids:
        # 跳过已有缓存的
        if get_cached_integrity(video_id):
            continue
        
        video_path = video_paths.get(video_id)
        if video_path:
            result = check_single_video_integrity(video_path)
            set_cached_integrity(video_id, result)
            time.sleep(0.1)  # 避免IO过载


@app.get("/api/video/integrity/{video_id}")
async def get_video_integrity(video_id: str):
    """API: 获取单个视频完整性状态"""
    # 先检查缓存
    cached = get_cached_integrity(video_id)
    if cached:
        return {"video_id": video_id, "cached": True, **cached}
    
    # 没有缓存则检查
    video_path = video_server.get_video_path(video_id)
    if not video_path:
        return {"video_id": video_id, "valid": False, "error": "视频不存在", "cached": False}
    
    result = check_single_video_integrity(video_path)
    set_cached_integrity(video_id, result)
    
    return {"video_id": video_id, "cached": False, **result}


@app.get("/api/video/status/{video_id}")
async def get_video_status_api(video_id: str):
    """API: 获取单个视频的完整状态（下载中/损坏/正常）"""
    video_path = video_server.get_video_path(video_id)
    if not video_path:
        return {"video_id": video_id, "status": "corrupted", "reason": "视频不存在"}
    
    status = check_video_status(video_path)
    return {"video_id": video_id, **status}


@app.post("/api/videos/status/batch")
async def batch_check_video_status(request: Request):
    """API: 批量检查视频状态（下载中/损坏/正常）"""
    try:
        body = await request.json()
        video_ids = body.get("video_ids", [])
    except:
        video_ids = []
    
    if not video_ids:
        return {"results": {}}
    
    results = {}
    
    for video_id in video_ids[:50]:  # 限制每次最多50个
        video_path = video_server.get_video_path(video_id)
        if video_path:
            results[video_id] = check_video_status(video_path)
        else:
            results[video_id] = {"status": "corrupted", "reason": "视频不存在"}
    
    return {"results": results}


@app.post("/api/videos/integrity/batch")
async def batch_check_integrity(
    background_tasks: BackgroundTasks,
    request: Request
):
    """API: 批量检查视频完整性，返回缓存状态并启动后台检查"""
    # 获取请求体中的 video_ids
    try:
        body = await request.json()
        video_ids = body.get("video_ids", [])
    except:
        video_ids = []
    
    if not video_ids:
        return {"results": {}, "pending": []}
    
    results = {}
    pending = []
    video_paths = {}
    
    # 收集需要检查的视频
    for video_id in video_ids:
        cached = get_cached_integrity(video_id)
        if cached:
            results[video_id] = cached
        else:
            video_path = video_server.get_video_path(video_id)
            if video_path:
                video_paths[video_id] = video_path
                pending.append(video_id)
    
    # 启动后台检查任务
    if pending:
        background_tasks.add_task(background_check_videos, pending, video_paths)
    
    return {
        "results": results,
        "pending": pending,
        "total": len(video_ids)
    }


@app.get("/api/videos/integrity/status")
async def get_integrity_status(video_ids: str = Query(default="")):
    """API: 获取多个视频的缓存完整性状态（用于轮询更新）"""
    if not video_ids:
        return {"results": {}}
    
    ids = video_ids.split(",")[:100]  # 最多100个
    results = {}
    
    for video_id in ids:
        cached = get_cached_integrity(video_id.strip())
        if cached:
            results[video_id] = cached
    
    return {"results": results}


@app.delete("/api/video/{video_id}")
async def delete_video(video_id: str, request: Request):
    """API: 删除视频文件"""
    # 需要登录才能删除
    if video_server.auth_enabled and not get_current_user(request):
        raise HTTPException(status_code=401, detail="未登录，无法删除")
    
    video_path = video_server.get_video_path(video_id)
    if not video_path:
        raise HTTPException(status_code=404, detail="视频不存在")
    
    if not os.path.exists(video_path):
        raise HTTPException(status_code=404, detail="视频文件不存在")
    
    # 验证文件在配置的目录内（安全检查）
    is_valid_path = False
    for base_dir in video_server.video_dirs:
        if video_path.startswith(base_dir):
            is_valid_path = True
            break
    
    if not is_valid_path:
        raise HTTPException(status_code=403, detail="无权删除此文件")
    
    try:
        # 删除文件
        os.remove(video_path)
        
        # 删除缩略图（如果存在）
        video_hash = hashlib.md5(video_path.encode()).hexdigest()
        thumb_path = THUMBNAIL_DIR / f"{video_hash}.jpg"
        if thumb_path.exists():
            thumb_path.unlink()
        
        # 清除完整性缓存
        with CACHE_LOCK:
            VIDEO_INTEGRITY_CACHE.pop(video_id, None)
        
        return {"success": True, "message": f"已删除: {os.path.basename(video_path)}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"删除失败: {str(e)}")





@app.get("/favicon.ico")
async def favicon():
    """网站图标"""
    favicon_path = BASE_DIR / "static" / "favicon.ico"
    if favicon_path.exists():
        return FileResponse(favicon_path, media_type="image/x-icon")
    return Response(status_code=404)


def main():
    """启动服务器"""
    print(f"""
╔═══════════════════════════════════════════════════════════╗
║          🎬 局域网视频服务器                              ║
╠═══════════════════════════════════════════════════════════╣
║  服务地址: http://{video_server.host}:{video_server.port}                ║
║  视频目录: {video_server.video_dirs[0] if video_server.video_dirs else '未配置'}...
║  按 Ctrl+C 停止服务                                      ║
╚═══════════════════════════════════════════════════════════╝
    """)
    
    uvicorn.run(
        app,
        host=video_server.host,
        port=video_server.port,
        log_level="info"
    )


if __name__ == "__main__":
    main()
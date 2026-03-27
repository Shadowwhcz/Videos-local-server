"""
局域网视频网站服务 - 主应用
"""
from __future__ import annotations

import os
import configparser
from pathlib import Path
from typing import Optional, List, Dict
import json
import hashlib
from datetime import datetime
from urllib.parse import unquote, urlencode
import threading
import time
import asyncio
from contextlib import asynccontextmanager
from functools import lru_cache
from collections import OrderedDict
import io

from fastapi import FastAPI, Request, Query, HTTPException, Depends, Form, BackgroundTasks, Body
from fastapi.responses import StreamingResponse, FileResponse, HTMLResponse, Response, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
import uvicorn
from session_store import SessionManager
from video_catalog import VideoServer, get_mime_type
from video_health import (
    background_check_videos,
    check_single_video_integrity,
    check_video_status,
    clear_integrity_cache,
    get_cached_integrity,
    is_temp_file,
    set_cached_integrity,
)

# ==================== 性能优化配置 ====================
# 流媒体优化参数（针对外接硬盘优化）
STREAM_CHUNK_SIZE = 4 * 1024 * 1024  # 4MB 基础块大小（从1MB增加）
STREAM_PREFETCH_SIZE = 16 * 1024 * 1024  # 16MB 预读缓冲区
FILE_HANDLE_CACHE_SIZE = 32  # 缓存的文件句柄数量
FILE_HANDLE_TTL = 300  # 文件句柄缓存时间（秒）

# 读取配置以获取 secret_key
_config = configparser.ConfigParser()
_config.read("config.ini", encoding='utf-8')
_secret_key = _config.get('auth', 'secret_key', fallback='videoserver-secret-key-change-in-production')

session_manager = None

# 获取项目根目录
BASE_DIR = Path(__file__).resolve().parent

# 初始化全局 Session 管理器
SESSION_STORAGE_DIR = BASE_DIR / "sessions"
session_manager = SessionManager(storage_dir=SESSION_STORAGE_DIR)

THUMBNAIL_DIR = BASE_DIR / "thumbnails"
THUMBNAIL_DIR.mkdir(exist_ok=True)

# ==================== 文件句柄缓存 ====================
class FileHandleCache:
    """LRU缓存文件句柄，减少频繁打开文件的开销"""
    
    def __init__(self, max_size: int = FILE_HANDLE_CACHE_SIZE, ttl: int = FILE_HANDLE_TTL):
        self.max_size = max_size
        self.ttl = ttl
        self.cache: OrderedDict[str, dict] = OrderedDict()
        self.lock = threading.RLock()
        self.stats = {
            "hits": 0,
            "misses": 0,
            "evictions": 0,
            "opens": 0,
            "dup_reads": 0,
        }
    
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
                    self.stats["hits"] += 1
                    return entry['handle']
                else:
                    # 过期，关闭并移除
                    try:
                        entry['handle'].close()
                    except:
                        pass
                    del self.cache[file_path]
            self.stats["misses"] += 1
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
                self.stats["evictions"] += 1

    def get_reader(self, file_path: str) -> io.BufferedReader:
        """获取用于当前请求的独立读取句柄（避免共享seek位置冲突）"""
        with self.lock:
            entry = self.cache.get(file_path)
            if entry and (time.time() - entry['accessed_at'] < self.ttl):
                self.cache.move_to_end(file_path)
                entry['accessed_at'] = time.time()
                self.stats["hits"] += 1
                try:
                    dup_fd = os.dup(entry["handle"].fileno())
                    self.stats["dup_reads"] += 1
                    return os.fdopen(dup_fd, "rb")
                except Exception:
                    try:
                        entry["handle"].close()
                    except:
                        pass
                    self.cache.pop(file_path, None)
            else:
                self.stats["misses"] += 1
                if entry:
                    try:
                        entry["handle"].close()
                    except:
                        pass
                    self.cache.pop(file_path, None)
            handle = open(file_path, "rb")
            self.stats["opens"] += 1
            self.cache[file_path] = {"handle": handle, "accessed_at": time.time()}
            self.cache.move_to_end(file_path)
            while len(self.cache) > self.max_size:
                _, oldest_entry = self.cache.popitem(last=False)
                try:
                    oldest_entry["handle"].close()
                except:
                    pass
                self.stats["evictions"] += 1
            dup_fd = os.dup(handle.fileno())
            self.stats["dup_reads"] += 1
            return os.fdopen(dup_fd, "rb")
    
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


@asynccontextmanager
async def app_lifespan(_: FastAPI):
    try:
        yield
    finally:
        print("🔄 关闭文件句柄缓存...")
        file_handle_cache.close_all()
        print("✅ 清理完成")


# 初始化应用
app = FastAPI(title="局域网视频服务器", lifespan=app_lifespan)

# 添加 Session 中间件（仅用于存储 session_id）
# max_age 设置为30天（记住登录的最长时间），实际过期由 SessionManager 控制
app.add_middleware(
    SessionMiddleware,
    secret_key=_secret_key,
    session_cookie="video_session_id",
    max_age=60 * 60 * 24 * 30,  # 30天（记住登录的最大时长）
    same_site="lax",  # 允许跨站请求但有限制
)

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
    return file_handle_cache.get_reader(file_path)

# 静态文件和模板
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
app.mount("/thumbnails", StaticFiles(directory=THUMBNAIL_DIR), name="thumbnails")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def dirname_filter(path):
    if not path:
        return ""
    return os.path.dirname(path)


templates.env.filters["dirname"] = dirname_filter

video_server = VideoServer(
    config_path=str(BASE_DIR / "config.ini"),
    thumbnail_dir=THUMBNAIL_DIR,
    scan_cache_file=BASE_DIR / ".video_scan_cache",
    is_temp_file=is_temp_file,
)


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


def get_actor_name(request: Request) -> str:
    current_user = get_current_user(request)
    return current_user or "guest"


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
    password: str = Form(...),
    remember: str = Form(default="")  # 记住我复选框
):
    """处理登录"""
    remember_me = remember == "on"  # 复选框选中时值为 "on"
    
    if username == video_server.auth_username and password == video_server.auth_password:
        # 获取设备信息（User-Agent）
        device_info = request.headers.get("user-agent", "Unknown")[:200]
        
        # 创建服务端会话（传入remember_me参数）
        session_id = session_manager.create_session(username, device_info, remember_me)
        
        # 在 cookie 中只存储 session_id
        request.session["session_id"] = session_id
        
        expire_desc = "30天" if remember_me else "7天"
        print(f"✅ 用户 {username} 登录成功（记住我: {remember_me}，有效期: {expire_desc}），当前活跃会话数: {session_manager.get_user_session_count(username)}")
        
        # 创建响应并设置cookie
        response = RedirectResponse(url="/", status_code=302)
        return response
    
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
    current_user = get_actor_name(request)

    def enrich_video(video: dict) -> dict:
        video_data = dict(video)
        if not video_data.get("poster_url"):
            video_data["poster_url"] = f"/api/video/thumbnail/{video_data['id']}"
        return video_data

    per_page = video_server.videos_per_page

    def build_page_url(target_page: int) -> str:
        params = {"page": target_page}
        if search:
            params["search"] = search
        if browse:
            params["browse"] = browse
        if dir_path:
            params["dir_path"] = dir_path
        return f"/?{urlencode(params)}"

    def build_pagination(total_items: int, total_page_count: int) -> Optional[dict]:
        if total_page_count <= 1:
            return None

        start_item = ((page - 1) * per_page) + 1 if total_items else 0
        end_item = min(page * per_page, total_items)
        return {
            "page_links": [
                {
                    "number": page_number,
                    "url": build_page_url(page_number),
                    "active": page_number == page,
                }
                for page_number in range(1, total_page_count + 1)
            ],
            "prev_url": build_page_url(page - 1) if page > 1 else None,
            "next_url": build_page_url(page + 1) if page < total_page_count else None,
            "summary": f"第 {start_item}-{end_item} 项 / 共 {total_items} 项",
        }

    def build_library_insights(view_videos: list[dict], total_items: int, browse_mode: bool) -> list[dict]:
        lane_label = "Folder View" if browse_mode else ("Search Results" if search else "Full Library")
        return [
            {"label": "Visible Now", "value": str(len(view_videos)), "meta": "当前画面"},
            {"label": "Directory Sets", "value": str(len(directories)), "meta": "已连接片库"},
            {"label": "Active Lane", "value": lane_label, "meta": "浏览模式"},
            {"label": "Library Crew", "value": current_user, "meta": f"总计 {total_items} 项"},
        ]

    def build_context_chips(total_items: int, browse_mode: bool) -> list[str]:
        chips = []
        if browse_mode:
            chips.append("Directory Browser")
        elif search:
            chips.append(f"Search: {search}")
        else:
            chips.append("All Media")

        if current_browse := browse:
            base_name = next((directory["name"] for directory in directories if directory["path"] == current_browse), "Directory")
            chips.append(base_name)

        if dir_path:
            chips.append(dir_path)

        chips.append(f"{total_items} Videos")
        return chips

    def build_spotlight_lane(view_videos: list[dict], browse_mode: bool) -> dict:
        if search:
            title = "Search Spotlight"
            detail = f"正在聚焦与 “{search}” 相关的片段。"
        elif browse_mode and dir_path:
            title = "Folder Stream"
            detail = f"当前停靠在 {dir_path}，继续向下浏览这一层目录。"
        elif browse_mode and browse:
            base_name = next((directory["name"] for directory in directories if directory["path"] == browse), "Directory")
            title = "Directory Current"
            detail = f"{base_name} 正在作为主浏览航道显示。"
        else:
            title = "Fresh Intake"
            detail = "新入库与最近更新内容会优先出现在这一屏。"

        return {
            "title": title,
            "detail": detail,
            "count": len(view_videos),
        }

    def build_view_feedback(total_items: int, browse_mode: bool) -> dict:
        if search:
            noun = "title" if total_items == 1 else "titles"
            return {
                "kicker": "Search Results",
                "title": f'Results for "{search}"',
                "detail": f"{total_items} matching {noun}",
            }
        if browse_mode and dir_path:
            return {
                "kicker": "Directory Focus",
                "title": f"Inside {dir_path}",
                "detail": f"{total_items} videos in this lane",
            }
        if browse_mode and browse:
            base_name = next((directory["name"] for directory in directories if directory["path"] == browse), "Directory")
            return {
                "kicker": "Directory Focus",
                "title": base_name,
                "detail": f"{total_items} videos ready to browse",
            }
        return {
            "kicker": "Media Library",
            "title": "Recently Added",
            "detail": f"{total_items} videos in the active library",
        }

    def build_empty_state(browse_mode: bool) -> dict:
        if search:
            return {
                "title": f'No matches for "{search}"',
                "detail": "试试更短的关键词，或者直接回到全库重新浏览。",
                "action_label": "Reset Search",
                "action_href": "/",
            }
        if browse_mode and dir_path:
            reset_params = {"browse": browse} if browse else {}
            return {
                "title": "This folder is quiet right now",
                "detail": "当前子目录没有可显示的视频，可以返回上一级或回到目录根。",
                "action_label": "Back to Folder Root",
                "action_href": f"/?{urlencode(reset_params)}" if reset_params else "/",
            }
        return {
            "title": "没有找到匹配的视频",
            "detail": "你可以切换目录、调整搜索词，或者检查当前视频目录配置。",
            "action_label": "Back to Library",
            "action_href": "/",
        }

    def build_curated_shelves(view_videos: list[dict], recent_candidates: list[dict]) -> list[dict]:
        shelves = [
            {
                "title": "Recent Drops",
                "subtitle": "刚更新或刚入库的内容优先在这里出现。",
                "items": recent_candidates[:6],
            }
        ]

        folder_picks = []
        featured_parent = (view_videos[0].get("parent_dir") if view_videos else "") or ""
        if featured_parent:
            folder_picks = [video for video in view_videos if video.get("parent_dir") == featured_parent][:6]
        elif len(view_videos) > 1:
            folder_picks = view_videos[1:7]

        if folder_picks:
            shelves.append(
                {
                    "title": "Folder Picks",
                    "subtitle": "沿着当前目录继续往下看，不用重新筛选。",
                    "items": folder_picks,
                }
            )

        return shelves
    
    # 如果指定了浏览目录
    if browse:
        browse_result = video_server.list_directory(browse, dir_path)
        if 'error' in browse_result:
            raise HTTPException(status_code=400, detail=browse_result['error'])
        
        browse_videos = [enrich_video(video) for video in browse_result.get("videos", [])]
        total_items = len(browse_videos)
        recent_videos = browse_videos[:12]
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "directories": directories,
                "browse_result": {
                    **browse_result,
                    "videos": browse_videos,
                },
                "videos": browse_videos,
                "featured_video": browse_videos[0] if browse_videos else None,
                "continue_video": browse_videos[0] if browse_videos else None,
                "recent_videos": recent_videos,
                "search": search,
                "page": page,
                "current_browse": browse,
                "current_path": dir_path,
                "directory_browse_mode": True,
                "current_user": current_user,
                "total": total_items,
                "total_pages": 1,
                "pagination": None,
                "library_insights": build_library_insights(browse_videos, total_items, True),
                "context_chips": build_context_chips(total_items, True),
                "spotlight_lane": build_spotlight_lane(browse_videos, True),
                "curated_shelves": build_curated_shelves(browse_videos, recent_videos),
                "view_feedback": build_view_feedback(total_items, True),
                "empty_state": build_empty_state(True),
            }
        )
    
    # 全局搜索或全部视频列表
    all_videos = video_server.scan_videos(search)
    
    # 分页
    total = len(all_videos)
    total_pages = (total + per_page - 1) // per_page
    page = min(page, total_pages) if total_pages > 0 else 1
    
    start = (page - 1) * per_page
    videos = [enrich_video(video) for video in all_videos[start:start + per_page]]
    featured_video = videos[0] if videos else (enrich_video(all_videos[0]) if all_videos else None)
    recent_videos = [enrich_video(video) for video in all_videos[:12]]
    
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "directories": directories,
            "videos": videos,
            "featured_video": featured_video,
            "continue_video": featured_video,
            "recent_videos": recent_videos,
            "search": search,
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "current_user": current_user,
            "current_browse": "",
            "current_path": "",
            "directory_browse_mode": False,
            "pagination": build_pagination(total, total_pages),
            "library_insights": build_library_insights(videos, total, False),
            "context_chips": build_context_chips(total, False),
            "spotlight_lane": build_spotlight_lane(videos, False),
            "curated_shelves": build_curated_shelves(videos, recent_videos),
            "view_feedback": build_view_feedback(total, False),
            "empty_state": build_empty_state(False),
        }
    )


@app.get("/play/{video_id}", response_class=HTMLResponse)
async def play(request: Request, video_id: str):
    """播放页面"""
    # 认证检查
    if video_server.auth_enabled and not get_current_user(request):
        return RedirectResponse(url="/login", status_code=302)
    
    video = video_server.get_video_by_id(video_id)
    if not video:
        raise HTTPException(status_code=404, detail="视频不存在")
    
    # 获取视频详细信息
    video_info = video_server.get_video_info(video['path'])
    video.update(video_info)
    video['id'] = video_id

    next_up_videos = []
    for candidate in video_server.scan_videos(directory=video.get("base_dir", "")):
        if candidate["id"] == video_id:
            continue
        next_up_videos.append(candidate)
        if len(next_up_videos) >= 6:
            break

    jump_to_next = next_up_videos[0] if next_up_videos else None
    
    return templates.TemplateResponse(
        request,
        "play.html",
        {
            "video": video,
            "next_up_videos": next_up_videos,
            "current_user": get_actor_name(request),
            "jump_to_next": jump_to_next,
        }
    )


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
    
    video_path = video_server.get_video_path(video_id)
    
    if not video_path:
        raise HTTPException(status_code=404, detail="视频不存在")

    try:
        stat = os.stat(video_path)
    except OSError:
        raise HTTPException(status_code=404, detail="视频不存在")

    file_size = stat.st_size
    mime_type = get_mime_type(video_path)
    etag = f'"{stat.st_mtime}-{stat.st_size}"'
    
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
            f = None
            try:
                f = open_file_with_cache(video_path)
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
                
            except Exception as e:
                print(f"Stream error: {e}")
            finally:
                try:
                    if f:
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
            f = None
            try:
                f = open_file_with_cache(video_path)
                chunk_size = STREAM_CHUNK_SIZE
                while chunk := f.read(chunk_size):
                    yield chunk
            except Exception as e:
                print(f"Stream error: {e}")
            finally:
                try:
                    if f:
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
    video = video_server.get_video_by_id(video_id)
    if not video:
        raise HTTPException(status_code=404, detail="视频不存在")
    video_path = video["path"]
    info = video_server.get_video_info(video_path)
    if "size" in video:
        info['size'] = video['size']
        info['size_mb'] = video.get('size_mb', round(video['size'] / (1024 * 1024), 1))
        info['modified'] = video.get('modified')
    else:
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
            "stats": file_handle_cache.stats,
            "entries": [
                {
                    "path": os.path.basename(k),
                    "accessed_ago": round(time.time() - v['accessed_at'], 1)
                }
                for k, v in list(file_handle_cache.cache.items())[:10]
            ]
        },
        "video_scan_cache": {
            **video_server.get_scan_cache_status()
        }
    }


@app.post("/api/library/refresh")
async def refresh_library(request: Request):
    """API: 手动刷新视频扫描缓存"""
    if video_server.auth_enabled and not get_current_user(request):
        raise HTTPException(status_code=401, detail="未登录")

    video_server.refresh_scan_cache()
    total_videos = len(video_server.scan_videos())
    return {
        "success": True,
        "message": "视频库已刷新",
        "total_videos": total_videos,
        "refreshed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

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
    target_ids = video_ids[:50]
    video_paths = video_server.get_video_paths(target_ids)
    
    for video_id in target_ids:
        video_path = video_paths.get(video_id)
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
    
    uncached_ids = []
    for video_id in video_ids:
        cached = get_cached_integrity(video_id)
        if cached:
            results[video_id] = cached
        else:
            uncached_ids.append(video_id)

    resolved_paths = video_server.get_video_paths(uncached_ids)
    for video_id in uncached_ids:
        video_path = resolved_paths.get(video_id)
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
        
        video_server.invalidate_video(video_id, video_path)
        clear_integrity_cache(video_id)
        
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

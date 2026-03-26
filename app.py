"""
局域网视频网站服务 - 主应用
"""
import os
import configparser
from pathlib import Path
from typing import Optional, List
import mimetypes
import json
import subprocess
import hashlib
from datetime import datetime, timedelta
from urllib.parse import unquote

from fastapi import FastAPI, Request, Query, HTTPException, Depends, Form
from fastapi.responses import StreamingResponse, FileResponse, HTMLResponse, Response, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
import uvicorn

# 初始化应用
app = FastAPI(title="局域网视频服务器")

# 读取配置以获取 secret_key
_config = configparser.ConfigParser()
_config.read("config.ini", encoding='utf-8')
_secret_key = _config.get('auth', 'secret_key', fallback='videoserver-secret-key-change-in-production')

# 添加 Session 中间件
app.add_middleware(
    SessionMiddleware,
    secret_key=_secret_key,
    session_cookie="video_session",
    max_age=60 * 60 * 24 * 7,  # 7天
)

# 获取项目根目录
BASE_DIR = Path(__file__).resolve().parent

# 缩略图缓存目录
THUMBNAIL_DIR = BASE_DIR / "thumbnails"
THUMBNAIL_DIR.mkdir(exist_ok=True)

# 静态文件和模板
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
app.mount("/thumbnails", StaticFiles(directory=THUMBNAIL_DIR), name="thumbnails")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


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
    
    def scan_videos(self, search: str = "", directory: str = None) -> list[dict]:
        """扫描视频文件"""
        videos = []
        
        dirs_to_scan = [directory] if directory else self.video_dirs
        
        for base_dir in dirs_to_scan:
            if not os.path.exists(base_dir):
                continue
            
            for root, _, files in os.walk(base_dir):
                for file in files:
                    ext = os.path.splitext(file)[1].lower()
                    if ext not in self.extensions:
                        continue
                    
                    full_path = os.path.join(root, file)
                    
                    # 搜索过滤
                    if search and search.lower() not in file.lower():
                        continue
                    
                    # 获取文件信息
                    try:
                        stat = os.stat(full_path)
                        rel_path = os.path.relpath(full_path, base_dir)
                        
                        videos.append({
                            'name': file,
                            'path': full_path,
                            'rel_path': rel_path,
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
        return videos
    
    def get_video_path(self, video_id: str) -> Optional[str]:
        """根据视频ID获取路径"""
        videos = self.scan_videos()
        for v in videos:
            # 使用文件路径的hash作为ID
            if str(hash(v['path'])) == video_id:
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
    return request.session.get("user")


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
        request.session["user"] = username
        return RedirectResponse(url="/", status_code=302)
    
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": "用户名或密码错误"}
    )


@app.get("/logout")
async def logout(request: Request):
    """登出"""
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
            v['id'] = str(hash(v['path']))
        
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
        v['id'] = str(hash(v['path']))
    
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
        if str(hash(v['path'])) == video_id:
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


@app.get("/stream/{video_id}")
async def stream_video(video_id: str, request: Request):
    """视频流传输（支持Range请求）"""
    # 认证检查
    if video_server.auth_enabled and not get_current_user(request):
        raise HTTPException(status_code=401, detail="未登录")
    
    videos = video_server.scan_videos()
    video_path = None
    
    for v in videos:
        if str(hash(v['path'])) == video_id:
            video_path = v['path']
            break
    
    if not video_path or not os.path.exists(video_path):
        raise HTTPException(status_code=404, detail="视频不存在")
    
    file_size = os.path.getsize(video_path)
    mime_type = get_mime_type(video_path)
    
    # 处理Range请求
    range_header = request.headers.get('range')
    
    if range_header:
        # 解析Range头
        range_match = range_header.replace('bytes=', '').split('-')
        start = int(range_match[0]) if range_match[0] else 0
        end = int(range_match[1]) if range_match[1] else file_size - 1
        
        # 确保范围有效
        if start >= file_size:
            raise HTTPException(status_code=416, detail="Range not satisfiable")
        
        end = min(end, file_size - 1)
        content_length = end - start + 1
        
        def iterfile():
            with open(video_path, 'rb') as f:
                f.seek(start)
                remaining = content_length
                chunk_size = 64 * 1024  # 64KB chunks
                while remaining > 0:
                    read_size = min(chunk_size, remaining)
                    data = f.read(read_size)
                    if not data:
                        break
                    remaining -= len(data)
                    yield data
        
        headers = {
            'Content-Range': f'bytes {start}-{end}/{file_size}',
            'Accept-Ranges': 'bytes',
            'Content-Length': str(content_length),
            'Content-Type': mime_type,
        }
        
        return StreamingResponse(
            iterfile(),
            status_code=206,
            headers=headers,
            media_type=mime_type,
        )
    else:
        # 无Range请求，返回完整文件
        def iterfile():
            with open(video_path, 'rb') as f:
                while chunk := f.read(64 * 1024):
                    yield chunk
        
        return StreamingResponse(
            iterfile(),
            media_type=mime_type,
            headers={
                'Accept-Ranges': 'bytes',
                'Content-Length': str(file_size),
            }
        )


@app.get("/api/videos")
async def api_videos(search: str = "", directory: str = None):
    """API: 获取视频列表"""
    videos = video_server.scan_videos(search, directory)
    for v in videos:
        v['id'] = str(hash(v['path']))
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
    }


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
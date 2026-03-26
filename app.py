"""
局域网视频网站服务 - 主应用
"""
import os
import configparser
from pathlib import Path
from typing import Optional
import mimetypes
from datetime import datetime

from fastapi import FastAPI, Request, Query, HTTPException
from fastapi.responses import StreamingResponse, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn

# 初始化应用
app = FastAPI(title="局域网视频服务器")

# 获取项目根目录
BASE_DIR = Path(__file__).resolve().parent

# 静态文件和模板
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


class VideoServer:
    """视频服务器配置和工具类"""
    
    def __init__(self, config_path: str = "config.ini"):
        self.config = configparser.ConfigParser()
        self.config.read(config_path, encoding='utf-8')
        
        # 服务器配置
        self.host = self.config.get('server', 'host', fallback='0.0.0.0')
        self.port = self.config.getint('server', 'port', fallback=8000)
        
        # 视频目录
        dirs_str = self.config.get('video', 'directories', fallback='~/Movies')
        self.video_dirs = []
        for d in dirs_str.split(','):
            d = d.strip()
            if d.startswith('~'):
                d = os.path.expanduser(d)
            if os.path.isdir(d):
                self.video_dirs.append(d)
        
        # 支持的格式
        exts_str = self.config.get('video', 'extensions', fallback='mp4,mkv,avi,mov,wmv,flv,webm,m4v')
        self.extensions = set(f'.{e.strip().lower().lstrip(".")}' for e in exts_str.split(','))
        
        # UI配置
        self.videos_per_page = self.config.getint('ui', 'videos_per_page', fallback=30)
    
    def scan_videos(self, search: str = "") -> list[dict]:
        """扫描视频文件"""
        videos = []
        
        for base_dir in self.video_dirs:
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


# 全局配置实例
video_server = VideoServer(str(BASE_DIR / "config.ini"))


def get_mime_type(file_path: str) -> str:
    """获取文件的MIME类型"""
    mime_type, _ = mimetypes.guess_type(file_path)
    return mime_type or 'application/octet-stream'


# ==================== 路由 ====================

@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    search: str = Query(default=""),
    page: int = Query(default=1, ge=1),
):
    """首页 - 视频列表"""
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
    videos = video_server.scan_videos()
    video = None
    for v in videos:
        if str(hash(v['path'])) == video_id:
            video = v
            break
    
    if not video:
        raise HTTPException(status_code=404, detail="视频不存在")
    
    video['id'] = video_id
    return templates.TemplateResponse(
        "play.html",
        {"request": request, "video": video}
    )


@app.get("/stream/{video_id}")
async def stream_video(video_id: str, request: Request):
    """视频流传输（支持Range请求）"""
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
async def api_videos(search: str = ""):
    """API: 获取视频列表"""
    videos = video_server.scan_videos(search)
    for v in videos:
        v['id'] = str(hash(v['path']))
    return {"videos": videos, "total": len(videos)}


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
    return FileResponse(
        BASE_DIR / "static" / "favicon.ico",
        media_type="image/x-icon"
    )


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
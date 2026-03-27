import asyncio
from pathlib import Path

import httpx
import pytest

import app as app_module


class AppClient:
    def __init__(self, app):
        self.app = app

    def get(self, path: str) -> httpx.Response:
        return asyncio.run(self._get(path))

    def post(self, path: str, json: dict | None = None) -> httpx.Response:
        return asyncio.run(self._post(path, json=json))

    async def _get(self, path: str) -> httpx.Response:
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get(path)

    async def _post(self, path: str, json: dict | None = None) -> httpx.Response:
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post(path, json=json)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    refresh_tracker = {"calls": 0}
    library_root = "/library"
    media_files = {
        "video-1": tmp_path / "sample.mp4",
        "video-2": tmp_path / "episode-2.mp4",
        "video-3": tmp_path / "episode-3.mp4",
    }
    for media_file in media_files.values():
        media_file.write_bytes(b"fake video bytes")

    fake_videos = [
        {
            "id": "video-1",
            "name": "Sample Feature.mp4",
            "path": str(media_files["video-1"]),
            "rel_path": "Sample Feature.mp4",
            "parent_dir": "",
            "size": media_files["video-1"].stat().st_size,
            "size_mb": 0.1,
            "modified": "2026-03-27 12:00",
            "ext": "MP4",
            "base_dir": library_root,
        },
        {
            "id": "video-2",
            "name": "Episode 2.mp4",
            "path": str(media_files["video-2"]),
            "rel_path": "Season 1/Episode 2.mp4",
            "parent_dir": "Season 1",
            "size": media_files["video-2"].stat().st_size,
            "size_mb": 0.1,
            "modified": "2026-03-26 22:15",
            "ext": "MP4",
            "base_dir": library_root,
        },
        {
            "id": "video-3",
            "name": "Episode 3.mp4",
            "path": str(media_files["video-3"]),
            "rel_path": "Season 1/Episode 3.mp4",
            "parent_dir": "Season 1",
            "size": media_files["video-3"].stat().st_size,
            "size_mb": 0.1,
            "modified": "2026-03-25 20:45",
            "ext": "MP4",
            "base_dir": library_root,
        },
    ]

    def fake_scan_videos(search="", directory=None, use_cache=True):
        videos = fake_videos
        if directory:
            videos = [video for video in videos if video["base_dir"] == directory]
        if search:
            term = search.lower()
            videos = [video for video in videos if term in video["name"].lower()]
        return videos

    def fake_list_directory(base_dir, dir_path=""):
        if str(base_dir) != library_root:
            return {"error": "目录不存在"}

        if dir_path == "Season 1":
            return {
                "current_path": dir_path,
                "folders": [],
                "videos": fake_videos[1:],
            }

        return {
            "current_path": dir_path,
            "folders": [{"name": "Season 1", "path": "Season 1"}],
            "videos": fake_videos,
        }

    monkeypatch.setattr(app_module.video_server, "auth_enabled", False)
    monkeypatch.setattr(app_module.video_server, "videos_per_page", 2)
    monkeypatch.setattr(
        app_module.video_server,
        "scan_videos",
        fake_scan_videos,
    )
    monkeypatch.setattr(
        app_module.video_server,
        "get_directories",
        lambda: [{"name": "Library", "path": library_root, "video_count": len(fake_videos)}],
    )
    monkeypatch.setattr(
        app_module.video_server,
        "list_directory",
        fake_list_directory,
    )
    monkeypatch.setattr(
        app_module.video_server,
        "get_video_by_id",
        lambda video_id: next((dict(video) for video in fake_videos if video["id"] == video_id), None),
    )
    monkeypatch.setattr(
        app_module.video_server,
        "get_video_info",
        lambda path: {"duration_formatted": "48:05", "resolution": "1920x1080"},
    )
    monkeypatch.setattr(
        app_module.video_server,
        "get_video_path",
        lambda video_id: str(media_files[video_id]) if video_id in media_files else None,
    )
    monkeypatch.setattr(
        app_module.video_server,
        "refresh_scan_cache",
        lambda: refresh_tracker.__setitem__("calls", refresh_tracker["calls"] + 1),
    )

    client = AppClient(app_module.app)
    client.refresh_tracker = refresh_tracker
    return client

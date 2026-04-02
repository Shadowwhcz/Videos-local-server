import time
from pathlib import Path

import pytest

from video_catalog import VideoServer


def _build_server(tmp_path: Path) -> tuple[VideoServer, Path]:
    media_root = tmp_path / "library"
    media_root.mkdir()
    thumbnails = tmp_path / "thumbnails"
    thumbnails.mkdir()
    config_path = tmp_path / "config.ini"
    config_path.write_text(
        "\n".join(
            [
                "[server]",
                "host=127.0.0.1",
                "port=8000",
                "[auth]",
                "enabled=false",
                "[video]",
                f"directories=Library={media_root}",
                "extensions=mp4",
                "[ui]",
                "videos_per_page=30",
            ]
        ),
        encoding="utf-8",
    )
    server = VideoServer(
        config_path=str(config_path),
        thumbnail_dir=thumbnails,
        scan_cache_file=tmp_path / ".video_scan_cache",
        is_temp_file=lambda _: False,
    )
    return server, media_root


def _explode(message: str):
    def _raiser(*args, **kwargs):
        raise AssertionError(message)

    return _raiser


def test_list_directory_builds_folder_summary_from_scanned_videos(tmp_path, monkeypatch):
    server, media_root = _build_server(tmp_path)
    season_dir = media_root / "Season 1"
    season_dir.mkdir()

    fake_videos = [
        {
            "id": "video-root",
            "name": "Feature.mp4",
            "path": str(media_root / "Feature.mp4"),
            "rel_path": "Feature.mp4",
            "parent_dir": "",
            "size": 128,
            "size_mb": 0.1,
            "modified": "2026-04-02 12:00",
            "ext": "MP4",
            "base_dir": str(media_root),
        },
        {
            "id": "video-s1-1",
            "name": "Episode 2.mp4",
            "path": str(season_dir / "Episode 2.mp4"),
            "rel_path": "Season 1/Episode 2.mp4",
            "parent_dir": "Season 1",
            "size": 128,
            "size_mb": 0.1,
            "modified": "2026-04-02 11:00",
            "ext": "MP4",
            "base_dir": str(media_root),
        },
        {
            "id": "video-s1-2",
            "name": "Episode 3.mp4",
            "path": str(season_dir / "Arc One" / "Episode 3.mp4"),
            "rel_path": "Season 1/Arc One/Episode 3.mp4",
            "parent_dir": "Season 1/Arc One",
            "size": 128,
            "size_mb": 0.1,
            "modified": "2026-04-02 10:00",
            "ext": "MP4",
            "base_dir": str(media_root),
        },
    ]

    monkeypatch.setattr(server, "scan_videos", lambda search="", directory=None, use_cache=True: list(fake_videos))
    monkeypatch.setattr(server, "_has_videos_recursive", _explode("directory browse should not recurse"))
    monkeypatch.setattr(server, "_summarize_folder", _explode("directory browse should not rescan folders"))

    result = server.list_directory(str(media_root))

    assert result["current_path"] == ""
    assert result["parent_path"] is None
    assert [folder["name"] for folder in result["folders"]] == ["Season 1"]
    assert result["folders"][0]["subfolder_count"] == 1
    assert result["folders"][0]["video_count"] == 1
    assert [video["name"] for video in result["videos"]] == ["Feature.mp4"]


def test_cache_validation_uses_top_level_directory_mtime(tmp_path, monkeypatch):
    server, media_root = _build_server(tmp_path)
    cache = {
        "scanned_at": time.time(),
        "videos": [],
        "dir_mtimes": {str(media_root): 100.0},
    }

    monkeypatch.setattr(server, "_get_dir_mtime", _explode("cache validation should not recurse through the whole drive"))
    monkeypatch.setattr(server, "_get_top_dir_mtime", lambda directory: 100.0)

    assert server._is_cache_valid(cache) is True

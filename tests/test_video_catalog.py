import json
import time
from types import SimpleNamespace
from pathlib import Path

import pytest

from video_catalog import VideoServer


def _build_server(tmp_path: Path) -> tuple[VideoServer, Path]:
    media_root = tmp_path / "library"
    media_root.mkdir(exist_ok=True)
    thumbnails = tmp_path / "thumbnails"
    thumbnails.mkdir(exist_ok=True)
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


def test_video_server_verify_password_supports_plain_text_config(tmp_path):
    server, _ = _build_server(tmp_path)

    assert server.verify_password("hx123456") is True
    assert server.verify_password("wrong-password") is False


def test_refresh_scan_cache_persists_full_library_scan(tmp_path):
    server, media_root = _build_server(tmp_path)
    (media_root / "Feature.mp4").write_bytes(b"video")

    server.refresh_scan_cache()

    cache = server._load_scan_cache()
    assert server.scan_cache_file.exists()
    assert cache["videos"]
    assert cache["videos"][0]["name"] == "Feature.mp4"


def test_scan_videos_ignores_macos_appledouble_and_ds_store(tmp_path):
    server, media_root = _build_server(tmp_path)
    (media_root / "Feature.mp4").write_bytes(b"video")
    (media_root / "._Feature.mp4").write_bytes(b"not-a-video")
    (media_root / ".DS_Store").write_bytes(b"metadata")

    server.refresh_scan_cache()
    videos = server.scan_videos(use_cache=True)

    assert [video["name"] for video in videos] == ["Feature.mp4"]
    assert server.get_directories()[0]["video_count"] == 1


def test_scan_videos_filters_directory_queries_from_valid_cache(tmp_path, monkeypatch):
    server, media_root = _build_server(tmp_path)
    other_root = tmp_path / "movies"
    other_root.mkdir()
    server.video_dirs.append(str(other_root))
    server.video_dir_names[str(other_root)] = "Movies"
    cached_videos = [
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
            "path": str(media_root / "Season 1" / "Episode 2.mp4"),
            "rel_path": "Season 1/Episode 2.mp4",
            "parent_dir": "Season 1",
            "size": 128,
            "size_mb": 0.1,
            "modified": "2026-04-02 11:00",
            "ext": "MP4",
            "base_dir": str(media_root),
        },
        {
            "id": "video-other",
            "name": "Movie Night.mp4",
            "path": str(other_root / "Movie Night.mp4"),
            "rel_path": "Movie Night.mp4",
            "parent_dir": "",
            "size": 128,
            "size_mb": 0.1,
            "modified": "2026-04-02 10:00",
            "ext": "MP4",
            "base_dir": str(other_root),
        },
    ]
    server._save_scan_cache(
        {
            "videos": cached_videos,
            "scanned_at": time.time(),
            "dir_mtimes": {
                str(media_root): 100.0,
                str(other_root): 100.0,
            },
        }
    )

    monkeypatch.setattr(server, "_get_top_dir_mtime", lambda directory: 100.0)
    monkeypatch.setattr("video_catalog.os.walk", _explode("directory queries should be served from the scan cache"))

    videos = server.scan_videos(directory=str(media_root))
    searched_videos = server.scan_videos(search="episode", directory=str(media_root))

    assert [video["name"] for video in videos] == ["Feature.mp4", "Episode 2.mp4"]
    assert [video["name"] for video in searched_videos] == ["Episode 2.mp4"]


def test_video_info_cache_persists_across_server_restarts(tmp_path, monkeypatch):
    server, media_root = _build_server(tmp_path)
    video_path = media_root / "Feature.mp4"
    video_path.write_bytes(b"video")
    ffprobe_payload = {
        "streams": [
            {
                "codec_type": "video",
                "width": 1920,
                "height": 1080,
                "codec_name": "h264",
                "r_frame_rate": "24/1",
            }
        ],
        "format": {
            "duration": "120.0",
            "bit_rate": "8000000",
        },
    }
    monkeypatch.setattr(
        "video_catalog.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(ffprobe_payload)),
    )

    info = server.get_video_info(str(video_path))
    restarted_server, _ = _build_server(tmp_path)
    monkeypatch.setattr(
        "video_catalog.subprocess.run",
        _explode("video info should be served from persisted cache"),
    )

    cached_info = restarted_server.get_video_info(str(video_path))

    assert info["width"] == 1920
    assert info["resolution"] == "1920x1080"
    assert cached_info["width"] == 1920
    assert cached_info["resolution"] == "1920x1080"
    assert cached_info["duration_formatted"] == "2:00"

from conftest import AppClient
import app as app_module


def test_index_renders_video_library_shell(client: AppClient):
    response = client.get("/")
    assert response.status_code == 200
    assert "media-library-shell" in response.text
    assert "Sample Feature.mp4" in response.text
    assert "Library" in response.text
    assert "片库根目录" in response.text
    assert "可立即播放" in response.text
    assert 'class="hero-secondary-btn" href="/?browse=%2Flibrary"' in response.text
    assert "ConnectHub" not in response.text


def test_index_renders_pagination_navigation(client: AppClient):
    response = client.get("/?page=2")
    assert response.status_code == 200
    assert "上一页" in response.text
    assert "下一页" not in response.text
    assert "?page=1" in response.text


def test_index_renders_directory_browser_rows(client: AppClient):
    response = client.get("/?browse=/library&dir_path=Season%201")
    assert response.status_code == 200
    assert "目录浏览" in response.text
    assert "Episode 2.mp4" in response.text
    assert "Season 1" in response.text
    assert "返回目录根" in response.text


def test_index_renders_back_to_parent_folder_for_nested_directory(client: AppClient):
    response = client.get("/?browse=/library&dir_path=Season%201/Arc%20One")
    assert response.status_code == 200
    assert "返回上一级目录" in response.text
    assert "dir_path=Season%201" in response.text
    assert '>Season 1</a>' in response.text


def test_index_renders_folder_row_counts(client: AppClient):
    response = client.get("/?browse=/library")
    assert response.status_code == 200
    assert "1 个子目录" in response.text
    assert "2 个视频" in response.text


def test_index_renders_directory_snapshot_for_browse_context(client: AppClient):
    response = client.get("/?browse=/library&dir_path=Season%201")
    assert response.status_code == 200
    assert "目录快照" in response.text
    assert "子目录数" in response.text
    assert "当前视频数" in response.text
    assert ">1<" in response.text
    assert ">2<" in response.text


def test_index_renders_library_insights_and_refresh_actions(client: AppClient):
    response = client.get("/")
    assert response.status_code == 200
    assert "片库脉冲" in response.text
    assert "刷新片库" in response.text
    assert "聚焦片段" in response.text
    assert "快速切换目录" in response.text
    assert 'name="browse"' in response.text


def test_index_renders_curated_shelves(client: AppClient):
    response = client.get("/")
    assert response.status_code == 200
    assert "继续观看" in response.text
    assert "最近入库" in response.text


def test_index_defers_initial_scan_with_loading_state(client: AppClient, monkeypatch):
    tracker = {"started": 0}
    monkeypatch.setattr(app_module.video_server, "has_usable_scan_cache", lambda: False)
    monkeypatch.setattr(
        app_module.video_server,
        "get_directories",
        lambda cached_only=False: [{"name": "Library", "path": "/library", "video_count": 0}],
    )
    monkeypatch.setattr(
        app_module.video_server,
        "ensure_background_scan",
        lambda: tracker.__setitem__("started", tracker["started"] + 1) or True,
    )
    monkeypatch.setattr(app_module.video_server, "is_background_scan_running", lambda: True)
    monkeypatch.setattr(
        app_module.video_server,
        "scan_videos",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not scan synchronously")),
    )

    response = client.get("/")

    assert response.status_code == 200
    assert "正在读取片库" in response.text
    assert "data-library-loading=\"true\"" in response.text
    assert tracker["started"] == 1


def test_index_renders_clear_filters_when_context_is_active(client: AppClient):
    response = client.get("/?search=sample&browse=/library&dir_path=Season%201")
    assert response.status_code == 200
    assert "清除筛选" in response.text


def test_index_renders_search_view_feedback(client: AppClient):
    response = client.get("/?search=sample")
    assert response.status_code == 200
    assert "搜索结果" in response.text
    assert "sample" in response.text
    assert "共匹配 1 个视频" in response.text


def test_index_renders_contextual_empty_search_state(client: AppClient):
    response = client.get("/?search=missing")
    assert response.status_code == 200
    assert "没有找到与" in response.text
    assert "missing" in response.text
    assert "重置搜索" in response.text


def test_refresh_library_endpoint_rebuilds_scan_cache(client: AppClient):
    response = client.post("/api/library/refresh")
    assert response.status_code == 200
    assert response.json()["success"] is True
    assert client.refresh_tracker["calls"] == 1


def test_play_renders_player_shell(client: AppClient):
    response = client.get("/play/video-1")
    assert response.status_code == 200
    assert "player-shell" in response.text
    assert "Sample Feature.mp4" in response.text
    assert "接下来播放" in response.text
    assert "Episode 2.mp4" in response.text
    assert "Episode 3.mp4" in response.text
    assert "正在播放" in response.text
    assert "跳到下一条" in response.text


def test_play_renders_current_folder_browse_link(client: AppClient):
    response = client.get("/play/video-3")
    assert response.status_code == 200
    assert "浏览当前目录" in response.text
    assert "dir_path=Season+1%2FArc+One" in response.text
    assert 'class="player-back-link"' in response.text
    assert 'href="/?browse=%2Flibrary&amp;dir_path=Season+1%2FArc+One"' in response.text


def test_play_prioritizes_same_folder_next_up(client: AppClient):
    response = client.get("/play/video-2")
    assert response.status_code == 200
    assert "来自当前目录" in response.text
    assert "Episode 4.mp4" in response.text


def test_play_does_not_block_on_video_info_lookup(client: AppClient, monkeypatch):
    monkeypatch.setattr(
        app_module.video_server,
        "get_video_info",
        lambda path: (_ for _ in ()).throw(AssertionError("play route should not call get_video_info synchronously")),
    )

    response = client.get("/play/video-1")

    assert response.status_code == 200


def test_play_renders_async_metadata_placeholders(client: AppClient):
    response = client.get("/play/video-1")
    assert response.status_code == 200
    assert "分辨率" in response.text
    assert 'class="js-player-resolution"' in response.text
    assert 'class="js-player-duration"' in response.text
    assert ">--<" in response.text

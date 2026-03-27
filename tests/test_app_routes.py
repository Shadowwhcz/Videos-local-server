from conftest import AppClient


def test_index_renders_video_library_shell(client: AppClient):
    response = client.get("/")
    assert response.status_code == 200
    assert "media-library-shell" in response.text
    assert "Sample Feature.mp4" in response.text
    assert "Library" in response.text
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
    assert "Directory Browser" in response.text
    assert "Episode 2.mp4" in response.text
    assert "Season 1" in response.text
    assert "Back to Folder Root" in response.text


def test_index_renders_back_to_parent_folder_for_nested_directory(client: AppClient):
    response = client.get("/?browse=/library&dir_path=Season%201/Arc%20One")
    assert response.status_code == 200
    assert "Back to Parent Folder" in response.text
    assert "dir_path=Season%201" in response.text
    assert '>Season 1</a>' in response.text


def test_index_renders_library_insights_and_refresh_actions(client: AppClient):
    response = client.get("/")
    assert response.status_code == 200
    assert "Library Pulse" in response.text
    assert "Refresh Library" in response.text
    assert "Spotlight Lane" in response.text


def test_index_renders_curated_shelves(client: AppClient):
    response = client.get("/")
    assert response.status_code == 200
    assert "Continue Watching" in response.text
    assert "Recent Drops" in response.text


def test_index_renders_clear_filters_when_context_is_active(client: AppClient):
    response = client.get("/?search=sample&browse=/library&dir_path=Season%201")
    assert response.status_code == 200
    assert "Clear Filters" in response.text


def test_index_renders_search_view_feedback(client: AppClient):
    response = client.get("/?search=sample")
    assert response.status_code == 200
    assert "Results for" in response.text
    assert "sample" in response.text
    assert "1 matching title" in response.text


def test_index_renders_contextual_empty_search_state(client: AppClient):
    response = client.get("/?search=missing")
    assert response.status_code == 200
    assert "No matches for" in response.text
    assert "missing" in response.text
    assert "Reset Search" in response.text


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
    assert "Next Up" in response.text
    assert "Episode 2.mp4" in response.text
    assert "Episode 3.mp4" in response.text
    assert "Now Streaming" in response.text
    assert "Jump to Next" in response.text

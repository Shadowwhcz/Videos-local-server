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


def test_play_renders_player_shell(client: AppClient):
    response = client.get("/play/video-1")
    assert response.status_code == 200
    assert "player-shell" in response.text
    assert "Sample Feature.mp4" in response.text
    assert "Next Up" in response.text
    assert "Episode 2.mp4" in response.text
    assert "Episode 3.mp4" in response.text

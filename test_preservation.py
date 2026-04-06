"""
Preservation 属性测试

验证修复不应改变的现有行为（基线行为保持）。
这些测试在未修复代码上应该 PASS，确认基线行为需要保持。

Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7
"""
import os
import sys
import pickle
import tempfile
import time

import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st
from pathlib import Path


# ==================== P9: Range 请求响应正确性 ====================

class TestP9RangeHeaderParsing:
    """
    P9: 对所有有效 Range 请求参数，parse_range_header() 返回正确的 (start, end) 元组。

    **Validates: Requirements 3.1, 3.2**
    """

    @given(
        start=st.integers(min_value=0, max_value=9999),
        end=st.integers(min_value=0, max_value=9999),
        file_size=st.integers(min_value=1, max_value=10000),
    )
    @settings(max_examples=100, deadline=5000)
    def test_explicit_range_returns_correct_tuple(self, start, end, file_size):
        """
        **Validates: Requirements 3.1**

        对 bytes=start-end 格式，当 start <= end 且 start < file_size 时，
        返回 [(start, min(end, file_size-1))]。
        """
        from app import parse_range_header

        # 只测试有效范围
        assume(start <= end)
        assume(start < file_size)

        header = f"bytes={start}-{end}"
        result = parse_range_header(header, file_size)

        assert len(result) == 1, f"有效范围应返回1个元组，实际: {len(result)}"
        r_start, r_end = result[0]
        assert r_start == start, f"start 应为 {start}，实际: {r_start}"
        expected_end = min(end, file_size - 1)
        assert r_end == expected_end, f"end 应为 {expected_end}，实际: {r_end}"

    @given(
        file_size=st.integers(min_value=1, max_value=10000),
        start=st.integers(min_value=0, max_value=9999),
    )
    @settings(max_examples=80, deadline=5000)
    def test_open_ended_range_extends_to_eof(self, file_size, start):
        """
        **Validates: Requirements 3.1**

        对 bytes=start- 格式（开放结尾），当 start < file_size 时，
        返回 [(start, file_size-1)]。
        """
        from app import parse_range_header

        assume(start < file_size)

        header = f"bytes={start}-"
        result = parse_range_header(header, file_size)

        assert len(result) == 1
        r_start, r_end = result[0]
        assert r_start == start
        assert r_end == file_size - 1, f"开放结尾应到 {file_size-1}，实际: {r_end}"

    @given(
        file_size=st.integers(min_value=1, max_value=10000),
        suffix_len=st.integers(min_value=1, max_value=10000),
    )
    @settings(max_examples=80, deadline=5000)
    def test_suffix_range_returns_last_n_bytes(self, file_size, suffix_len):
        """
        **Validates: Requirements 3.1**

        对 bytes=-N 格式（后缀范围），返回文件最后 N 字节的范围。
        """
        from app import parse_range_header

        header = f"bytes=-{suffix_len}"
        result = parse_range_header(header, file_size)

        assert len(result) == 1
        r_start, r_end = result[0]
        expected_start = max(0, file_size - suffix_len)
        assert r_start == expected_start, f"后缀起始应为 {expected_start}，实际: {r_start}"
        assert r_end == file_size - 1

    @given(file_size=st.integers(min_value=1, max_value=10000))
    @settings(max_examples=50, deadline=5000)
    def test_empty_or_invalid_header_returns_empty(self, file_size):
        """
        **Validates: Requirements 3.2**

        空字符串或无 bytes= 前缀的请求头返回空列表。
        """
        from app import parse_range_header

        assert parse_range_header("", file_size) == []
        assert parse_range_header("range=0-100", file_size) == []

    @given(
        start=st.integers(min_value=0, max_value=9999),
        end=st.integers(min_value=0, max_value=9999),
        file_size=st.integers(min_value=1, max_value=10000),
    )
    @settings(max_examples=50, deadline=5000)
    def test_invalid_range_returns_empty(self, start, end, file_size):
        """
        **Validates: Requirements 3.1**

        当 start > end 或 start >= file_size 时，返回空列表。
        """
        from app import parse_range_header

        # 只测试无效范围
        assume(start > end or start >= file_size)

        header = f"bytes={start}-{end}"
        result = parse_range_header(header, file_size)

        assert result == [], f"无效范围应返回空列表，实际: {result}"



# ==================== P10: 扫描缓存持久化和视频元数据 ====================

class TestP10ScanCachePersistenceAndMetadata:
    """
    P10: 扫描结果持久化到 .video_scan_cache，视频列表包含完整元数据。

    **Validates: Requirements 3.3, 3.4**
    """

    # 视频元数据必须包含的字段
    REQUIRED_METADATA_FIELDS = {"id", "name", "path", "size", "modified"}

    @given(
        num_videos=st.integers(min_value=1, max_value=5),
        file_sizes=st.lists(
            st.integers(min_value=100, max_value=10000),
            min_size=1,
            max_size=5,
        ),
    )
    @settings(max_examples=30, deadline=30000)
    def test_scan_persists_cache_with_complete_metadata(self, num_videos, file_sizes):
        """
        **Validates: Requirements 3.3, 3.4**

        扫描完成后，缓存文件应存在且可加载，
        每个视频应包含完整的元数据字段（id, name, path, size, modified）。
        """
        from video_catalog import VideoServer
        from video_health import is_temp_file

        # 确保 file_sizes 长度与 num_videos 匹配
        sizes = (file_sizes * num_videos)[:num_videos]

        with tempfile.TemporaryDirectory() as tmpdir:
            video_dir = os.path.join(tmpdir, "videos")
            os.makedirs(video_dir)

            # 创建测试视频文件
            for i, size in enumerate(sizes):
                video_path = os.path.join(video_dir, f"video_{i}.mp4")
                with open(video_path, "wb") as f:
                    f.write(b"x" * size)

            config_path = os.path.join(tmpdir, "config.ini")
            with open(config_path, "w") as f:
                f.write(f"[video]\ndirectories = {video_dir}\nextensions = mp4\n")
                f.write("[server]\nhost = 0.0.0.0\nport = 8000\n")
                f.write("[auth]\nenabled = false\n")
                f.write("[ui]\nvideos_per_page = 30\n")

            cache_file = Path(tmpdir) / ".video_scan_cache"
            server = VideoServer(
                config_path=config_path,
                thumbnail_dir=Path(tmpdir),
                scan_cache_file=cache_file,
                is_temp_file=is_temp_file,
            )

            # 执行扫描（不使用缓存，强制全量扫描）
            videos = server.scan_videos(use_cache=False)

            # 验证缓存文件已持久化
            assert cache_file.exists(), "扫描完成后缓存文件应存在"

            # 验证缓存可加载
            with open(cache_file, "rb") as f:
                cache = pickle.load(f)

            assert "videos" in cache, "缓存应包含 videos 键"
            assert "scanned_at" in cache, "缓存应包含 scanned_at 键"
            assert len(cache["videos"]) == num_videos, (
                f"缓存视频数应为 {num_videos}，实际: {len(cache['videos'])}"
            )

            # 验证每个视频的元数据完整性
            for video in videos:
                missing = self.REQUIRED_METADATA_FIELDS - set(video.keys())
                assert not missing, (
                    f"视频 {video.get('name', '?')} 缺少元数据字段: {missing}"
                )
                # 验证字段值非空
                assert video["id"], "id 不应为空"
                assert video["name"], "name 不应为空"
                assert video["path"], "path 不应为空"
                assert video["size"] > 0, "size 应大于 0"
                assert video["modified"], "modified 不应为空"

    def test_zero_byte_files_excluded_from_scan(self):
        """
        **Validates: Requirements 3.4**

        0 字节文件应被排除在扫描结果之外。
        """
        from video_catalog import VideoServer
        from video_health import is_temp_file

        with tempfile.TemporaryDirectory() as tmpdir:
            video_dir = os.path.join(tmpdir, "videos")
            os.makedirs(video_dir)

            # 创建一个正常文件和一个 0 字节文件
            normal = os.path.join(video_dir, "normal.mp4")
            with open(normal, "wb") as f:
                f.write(b"data" * 100)
            empty = os.path.join(video_dir, "empty.mp4")
            with open(empty, "wb") as f:
                pass  # 0 字节

            config_path = os.path.join(tmpdir, "config.ini")
            with open(config_path, "w") as f:
                f.write(f"[video]\ndirectories = {video_dir}\nextensions = mp4\n")
                f.write("[server]\nhost = 0.0.0.0\nport = 8000\n")
                f.write("[auth]\nenabled = false\n")
                f.write("[ui]\nvideos_per_page = 30\n")

            cache_file = Path(tmpdir) / ".video_scan_cache"
            server = VideoServer(
                config_path=config_path,
                thumbnail_dir=Path(tmpdir),
                scan_cache_file=cache_file,
                is_temp_file=is_temp_file,
            )

            videos = server.scan_videos(use_cache=False)
            names = [v["name"] for v in videos]
            assert "normal.mp4" in names, "正常文件应在扫描结果中"
            assert "empty.mp4" not in names, "0字节文件不应在扫描结果中"


# ==================== P11: 完整性缓存和资源清理 ====================

class TestP11IntegrityCacheAndResourceCleanup:
    """
    P11: 完整性缓存 24 小时有效期，close_all() 正确关闭所有句柄。

    **Validates: Requirements 3.5, 3.6, 3.7**
    """

    def test_integrity_cache_expire_hours_is_24(self):
        """
        **Validates: Requirements 3.5**

        VIDEO_INTEGRITY_CACHE 有效期应为 24 小时。
        """
        from video_health import CACHE_EXPIRE_HOURS

        assert CACHE_EXPIRE_HOURS == 24, (
            f"缓存有效期应为 24 小时，实际: {CACHE_EXPIRE_HOURS}"
        )

    @given(age_hours=st.floats(min_value=0.0, max_value=23.9))
    @settings(max_examples=50, deadline=5000)
    def test_integrity_cache_valid_within_24h(self, age_hours):
        """
        **Validates: Requirements 3.5**

        24 小时内的缓存条目应可正常获取。
        """
        from video_health import (
            VIDEO_INTEGRITY_CACHE,
            CACHE_LOCK,
            get_cached_integrity,
        )

        test_id = f"test_valid_{age_hours:.4f}"
        checked_at = time.time() - (age_hours * 3600)

        with CACHE_LOCK:
            VIDEO_INTEGRITY_CACHE[test_id] = {
                "valid": True,
                "info": {"codec": "h264"},
                "checked_at": checked_at,
            }

        try:
            result = get_cached_integrity(test_id)
            assert result is not None, (
                f"缓存 {age_hours:.1f} 小时前的条目应可获取"
            )
            assert result["valid"] is True
        finally:
            with CACHE_LOCK:
                VIDEO_INTEGRITY_CACHE.pop(test_id, None)

    @given(age_hours=st.floats(min_value=24.1, max_value=100.0))
    @settings(max_examples=30, deadline=5000)
    def test_integrity_cache_expired_after_24h(self, age_hours):
        """
        **Validates: Requirements 3.5**

        超过 24 小时的缓存条目应返回 None。
        """
        from video_health import (
            VIDEO_INTEGRITY_CACHE,
            CACHE_LOCK,
            get_cached_integrity,
        )

        test_id = f"test_expired_{age_hours:.4f}"
        checked_at = time.time() - (age_hours * 3600)

        with CACHE_LOCK:
            VIDEO_INTEGRITY_CACHE[test_id] = {
                "valid": True,
                "checked_at": checked_at,
            }

        try:
            result = get_cached_integrity(test_id)
            assert result is None, (
                f"缓存 {age_hours:.1f} 小时前的条目应已过期"
            )
        finally:
            with CACHE_LOCK:
                VIDEO_INTEGRITY_CACHE.pop(test_id, None)

    @given(num_handles=st.integers(min_value=1, max_value=8))
    @settings(max_examples=20, deadline=10000)
    def test_close_all_closes_all_handles(self, num_handles):
        """
        **Validates: Requirements 3.6**

        close_all() 应关闭所有缓存的文件句柄并清空缓存。
        """
        from app import FileHandleCache

        cache = FileHandleCache(max_size=16, ttl=60)
        temp_files = []
        handles = []

        try:
            for i in range(num_handles):
                f = tempfile.NamedTemporaryFile(delete=False, suffix=".bin")
                f.write(b"data")
                f.close()
                temp_files.append(f.name)

                h = open(f.name, "rb")
                handles.append(h)
                cache.put(f.name, h)

            # 验证缓存非空
            assert len(cache.cache) == num_handles

            # 执行 close_all
            cache.close_all()

            # 验证缓存已清空
            assert len(cache.cache) == 0, "close_all 后缓存应为空"

            # 验证所有句柄已关闭
            for i, h in enumerate(handles):
                assert h.closed, f"句柄 {i} 应已关闭"

        finally:
            for path in temp_files:
                try:
                    os.unlink(path)
                except OSError:
                    pass

"""
Bug Condition 探索测试

这些测试编码了期望行为，在未修复代码上应该 FAIL，
失败即确认 bug 存在。修复后通过即验证修复正确。

Validates: Requirements 2.1, 2.2, 2.3, 2.5, 2.8, 2.9
"""
import os
import sys
import threading
import tempfile
import time
import inspect

import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st


# ==================== C1: 并发文件句柄冲突 ====================

class TestC1ConcurrentFileHandleConflict:
    """
    C1: 并发文件句柄冲突
    Validates: Requirements 2.1, 2.2
    """

    def test_shared_handle_seek_conflict(self):
        """
        两个线程通过 get() 获取同一文件的共享句柄，
        分别 seek 到不同位置后读取，验证数据正确性。
        未修复代码上 get() 返回同一个对象，seek 位置会被覆盖。
        """
        from app import FileHandleCache

        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
            f.write(b'A' * 1024 + b'B' * 1024)
            temp_path = f.name

        try:
            cache = FileHandleCache(max_size=8, ttl=60)
            handle = open(temp_path, 'rb')
            cache.put(temp_path, handle)

            results = {}
            barrier = threading.Barrier(2, timeout=5)
            errors = []

            def read_at_position(tid, pos, expected):
                try:
                    h = cache.get(temp_path)
                    if h is None:
                        errors.append(f"线程{tid}: get()返回None")
                        return
                    barrier.wait()
                    h.seek(pos)
                    time.sleep(0.01)
                    data = h.read(64)
                    results[tid] = data
                except Exception as e:
                    errors.append(f"线程{tid}: {e}")

            t1 = threading.Thread(target=read_at_position, args=(1, 0, b'A'))
            t2 = threading.Thread(target=read_at_position, args=(2, 1024, b'B'))
            t1.start()
            t2.start()
            t1.join(timeout=10)
            t2.join(timeout=10)

            assert not errors, f"线程执行出错: {errors}"
            assert results.get(1) is not None, "线程1未返回数据"
            assert results.get(2) is not None, "线程2未返回数据"
            # 未修复代码上 get() 返回同一个 BufferedReader，
            # seek 位置冲突导致至少一个线程读到错误数据
            assert results[1] == b'A' * 64, (
                f"线程1应读到A*64，实际: {results[1][:16]}..."
            )
            assert results[2] == b'B' * 64, (
                f"线程2应读到B*64，实际: {results[2][:16]}..."
            )
        finally:
            cache.close_all()
            os.unlink(temp_path)


# ==================== C2: PrefetchBuffer 副作用 ====================

class TestC2PrefetchBufferSideEffects:
    """
    C2: PrefetchBuffer 副作用
    Validates: Requirements 2.2, 2.3
    """

    def test_prefetch_does_not_modify_cache(self):
        """
        PrefetchBuffer 运行后不应对全局 file_handle_cache 产生副作用。
        未修复代码上 _prefetch() 会调用 put() 和 get() 修改缓存。
        """
        from app import PrefetchBuffer, file_handle_cache

        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
            f.write(b'X' * 4096)
            temp_path = f.name

        try:
            file_handle_cache.close_all()
            initial_keys = set(file_handle_cache.cache.keys())

            pb = PrefetchBuffer(temp_path, start_pos=0, prefetch_size=2048)
            pb.start()
            data = pb.get_data(timeout=5)

            assert data is not None, "PrefetchBuffer 未能读取数据"
            assert len(data) == 2048

            current_keys = set(file_handle_cache.cache.keys())
            new_keys = current_keys - initial_keys

            # 期望：PrefetchBuffer 不应将句柄添加到全局缓存
            assert temp_path not in new_keys, (
                f"PrefetchBuffer 不应修改全局缓存，但新增了: {new_keys}"
            )
        finally:
            file_handle_cache.close_all()
            os.unlink(temp_path)

    def test_prefetch_source_no_cache_calls(self):
        """
        检查 _prefetch() 源代码中是否包含对 file_handle_cache 的调用。
        未修复代码包含 file_handle_cache.get() 和 put() 调用。
        """
        from app import PrefetchBuffer

        source = inspect.getsource(PrefetchBuffer._prefetch)
        has_get = "file_handle_cache.get" in source
        has_put = "file_handle_cache.put" in source

        assert not has_get and not has_put, (
            f"_prefetch() 不应调用 file_handle_cache 方法。"
            f" get={has_get}, put={has_put}"
        )


# ==================== C3: _is_cache_valid() mtime 容错 ====================

class TestC3MtimeTolerance:
    """
    C3: _is_cache_valid() mtime 容错
    Validates: Requirements 2.5
    """

    @given(delta=st.floats(min_value=0.1, max_value=1.9))
    @settings(max_examples=20, deadline=5000)
    def test_mtime_within_tolerance_should_be_valid(self, delta):
        """
        **Validates: Requirements 2.5**

        当 mtime 差值在 2 秒容错窗口内时，缓存应判定为有效。
        未修复代码使用 > 严格比较，任何正向差值都导致失效。
        """
        from pathlib import Path
        from video_catalog import VideoServer
        from video_health import is_temp_file

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "config.ini")
            with open(config_path, "w") as f:
                f.write(f"[video]\ndirectories = {tmpdir}\nextensions = mp4\n")
                f.write("[server]\nhost = 0.0.0.0\nport = 8000\n")
                f.write("[auth]\nenabled = false\n")
                f.write("[ui]\nvideos_per_page = 30\n")

            server = VideoServer(
                config_path=config_path,
                thumbnail_dir=Path(tmpdir),
                scan_cache_file=Path(tmpdir) / ".cache",
                is_temp_file=is_temp_file,
            )

            base_mtime = time.time() - 100
            cached_mtime = base_mtime
            current_mtime = base_mtime + delta

            cache = {
                "videos": [],
                "scanned_at": time.time(),
                "dir_mtimes": {tmpdir: cached_mtime},
            }

            # 重置验证缓存
            server.cache_validation_checked_at = 0
            server.cache_validation_cache_scanned_at = 0

            original_fn = server._get_top_dir_mtime
            server._get_top_dir_mtime = lambda d: current_mtime

            try:
                result = server._is_cache_valid(cache)
            finally:
                server._get_top_dir_mtime = original_fn

            # 差值在 2 秒内，缓存应有效
            # 未修复代码 current_mtime > cached_mtime 为 True → 返回 False
            assert result is True, (
                f"mtime差值{delta:.2f}s在2秒容错内，缓存应有效，但返回False"
            )


# ==================== C6: STREAM_CHUNK_SIZE 值 ====================

class TestC6StreamChunkSize:
    """
    C6: STREAM_CHUNK_SIZE 值
    验证 STREAM_CHUNK_SIZE 是否为 1MB。
    未修复代码上为 4MB。

    Validates: Requirements 2.8
    """

    def test_chunk_size_is_1mb(self):
        """
        STREAM_CHUNK_SIZE 应为 1MB (1 * 1024 * 1024)。
        未修复代码上为 4MB (4 * 1024 * 1024)。
        """
        from app import STREAM_CHUNK_SIZE

        expected = 1 * 1024 * 1024  # 1MB
        assert STREAM_CHUNK_SIZE == expected, (
            f"STREAM_CHUNK_SIZE 应为 1MB ({expected})，"
            f"实际为 {STREAM_CHUNK_SIZE / (1024*1024):.0f}MB ({STREAM_CHUNK_SIZE})"
        )


# ==================== C7: iterfile_optimized 是否使用 PrefetchBuffer ====================

class TestC7PrefetchBufferUsage:
    """
    C7: iterfile_optimized 是否使用 PrefetchBuffer
    验证流媒体迭代器是否使用了预读缓冲区。
    未修复代码上未使用。

    Validates: Requirements 2.9
    """

    def test_iterfile_uses_prefetch_or_removed(self):
        """
        iterfile_optimized 应使用 PrefetchBuffer，
        或者 PrefetchBuffer 类和 STREAM_PREFETCH_SIZE 应被移除。
        未修复代码上 PrefetchBuffer 存在但从未被 iterfile_optimized 使用。
        """
        from app import stream_video
        import app as app_module

        # 获取 stream_video 函数的源代码（包含 iterfile_optimized 定义）
        source = inspect.getsource(stream_video)

        has_prefetch_class = hasattr(app_module, 'PrefetchBuffer')
        has_prefetch_size = hasattr(app_module, 'STREAM_PREFETCH_SIZE')
        uses_prefetch_in_iter = "PrefetchBuffer" in source

        if has_prefetch_class or has_prefetch_size:
            # 如果 PrefetchBuffer 存在，iterfile_optimized 必须使用它
            assert uses_prefetch_in_iter, (
                "PrefetchBuffer 类存在但 iterfile_optimized 未使用它。"
                " 应在迭代器中使用预读缓冲区，或移除 PrefetchBuffer 死代码。"
            )
        else:
            # PrefetchBuffer 已被移除，这也是可接受的修复方案
            pass

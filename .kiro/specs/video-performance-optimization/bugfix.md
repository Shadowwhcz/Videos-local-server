# Bugfix 需求文档

## 简介

本项目是基于 FastAPI 的局域网视频服务器，视频源存储在外接移动硬盘（`/Volumes/One Touch/movies`）上。由于移动硬盘的 I/O 特性（高延迟、低随机读取速度），当前实现中存在多个性能缺陷，导致视频播放卡顿、页面加载缓慢、文件句柄泄漏和并发冲突等问题。

## Bug 分析

### 当前行为（缺陷）

1.1 WHEN `PrefetchBuffer._prefetch()` 通过 `file_handle_cache.get()` 获取共享文件句柄并在后台线程中执行 `seek` 和 `read` 操作 THEN 系统出现 seek 位置冲突，因为同一个文件句柄可能被主线程的流媒体传输同时使用，导致读取到错误的数据或抛出异常

1.2 WHEN `PrefetchBuffer._prefetch()` 自行打开文件句柄后调用 `file_handle_cache.put()` 缓存该句柄 THEN `put()` 方法会关闭同一路径下已缓存的旧句柄，而该旧句柄可能正在被其他请求的流媒体传输使用，导致 `ValueError: I/O operation on closed file` 错误

1.3 WHEN `PrefetchBuffer._prefetch()` 完成后在 `finally` 块中再次调用 `file_handle_cache.get()` 检查句柄是否已缓存 THEN 这次 `get()` 调用会将句柄标记为"最近使用"并重置 TTL，但不会返回给调用者使用，造成无意义的缓存副作用和潜在的句柄泄漏

1.4 WHEN 视频扫描缓存失效且 `scan_videos(use_cache=False)` 被调用 THEN 系统通过 `os.walk()` 遍历移动硬盘上的整个目录树，对于包含大量文件的移动硬盘（数千个视频文件），此操作耗时可达数十秒，期间页面完全无响应

1.5 WHEN `_is_cache_valid()` 方法验证缓存有效性时调用 `_get_top_dir_mtime()` 检查目录 mtime THEN 对于移动硬盘，`os.stat()` 调用本身就有较高延迟，且移动硬盘的 mtime 在某些文件系统（如 exFAT）上可能不可靠，导致缓存频繁误判为失效而触发全量重扫

1.6 WHEN `video_health.py` 中的 `check_video_status()` 对移动硬盘上的视频文件调用 `ffprobe` 子进程 THEN `ffprobe` 需要读取文件头部数据，对移动硬盘上的文件延迟显著增加，且 `is_file_locked()` 调用 `lsof` 命令进一步增加延迟，单个视频检查可能耗时超过 10 秒

1.7 WHEN `batch_check_video_status` API 被调用检查最多 50 个视频状态 THEN 系统在单线程中串行执行所有检查，每个检查都涉及 `ffprobe` 和 `lsof` 子进程调用，对移动硬盘上的文件总耗时可达数分钟

1.8 WHEN 流媒体传输使用 `STREAM_CHUNK_SIZE = 4MB` 的块大小从移动硬盘读取数据 THEN 单次 `read(4MB)` 调用在移动硬盘上可能耗时较长，导致视频播放出现明显的卡顿间隔，尤其是在硬盘处于休眠唤醒状态时

1.9 WHEN `PrefetchBuffer` 类被定义并配置了 `STREAM_PREFETCH_SIZE = 16MB` 的预读大小 THEN 该预读缓冲区在实际的 `iterfile_optimized()` 流媒体迭代器中从未被使用，预读优化完全无效，配置参数 `STREAM_PREFETCH_SIZE` 成为死代码

### 期望行为（正确）

2.1 WHEN 流媒体传输需要读取文件数据 THEN 系统 SHALL 通过 `get_reader()` 方法使用 `os.dup()` 获取独立的文件描述符副本，确保每个请求拥有独立的 seek 位置，避免并发冲突

2.2 WHEN 文件句柄被缓存或替换 THEN 系统 SHALL 确保不会关闭正在被其他请求使用的文件句柄，`PrefetchBuffer` 应使用独立的文件句柄而非共享缓存中的句柄

2.3 WHEN `PrefetchBuffer` 完成预读操作 THEN 系统 SHALL 正确关闭自行打开的文件句柄，不应对全局文件句柄缓存产生副作用

2.4 WHEN 视频扫描缓存失效需要重新扫描 THEN 系统 SHALL 在后台线程中执行扫描，前端页面应立即返回已缓存的数据（即使过期），并在扫描完成后自动更新，避免页面长时间无响应

2.5 WHEN 系统验证扫描缓存有效性 THEN 系统 SHALL 使用更可靠的验证策略（如仅检查顶层目录 mtime 并增加容错时间窗口），减少对移动硬盘的 `stat` 调用频率，避免在 exFAT 等文件系统上因 mtime 精度问题导致的误判

2.6 WHEN 系统检查移动硬盘上视频文件的健康状态 THEN 系统 SHALL 优先使用缓存结果，对 `ffprobe` 调用设置合理的超时时间，并跳过不必要的 `lsof` 检查以减少延迟

2.7 WHEN 批量检查视频状态 THEN 系统 SHALL 使用并发执行（如线程池）来并行处理多个视频检查，并设置合理的并发上限，将总耗时从串行的 N×T 降低到接近 T

2.8 WHEN 流媒体从移动硬盘传输数据 THEN 系统 SHALL 使用适合移动硬盘 I/O 特性的块大小（如 1MB），减少单次读取的等待时间，提高播放流畅度

2.9 WHEN 流媒体传输需要预读优化 THEN 系统 SHALL 在 `iterfile_optimized()` 中实际使用 `PrefetchBuffer` 进行后台预读，或者移除未使用的 `PrefetchBuffer` 类和 `STREAM_PREFETCH_SIZE` 配置以消除死代码

### 不变行为（回归预防）

3.1 WHEN 客户端发送带有有效 Range 请求头的 HTTP 请求 THEN 系统 SHALL 继续正确返回 206 Partial Content 响应，包含正确的 Content-Range 头和请求范围内的数据

3.2 WHEN 客户端发送不带 Range 请求头的 HTTP 请求 THEN 系统 SHALL 继续返回 200 OK 响应和完整的视频文件内容

3.3 WHEN 视频扫描完成并生成缓存 THEN 系统 SHALL 继续将扫描结果持久化到 `.video_scan_cache` 文件，确保服务重启后可以快速加载

3.4 WHEN 用户通过搜索或目录浏览请求视频列表 THEN 系统 SHALL 继续返回正确的视频列表，包含完整的视频元数据（id、name、path、size、modified 等）

3.5 WHEN `ffprobe` 检查视频完整性并返回结果 THEN 系统 SHALL 继续将结果缓存到 `VIDEO_INTEGRITY_CACHE` 中，缓存有效期保持 24 小时

3.6 WHEN 应用关闭时 THEN 系统 SHALL 继续通过 `file_handle_cache.close_all()` 正确关闭所有缓存的文件句柄，避免资源泄漏

3.7 WHEN 用户访问播放页面 THEN 系统 SHALL 继续正确显示视频信息、同目录推荐视频和导航链接

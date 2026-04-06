# 视频性能优化 Bugfix 设计

## Overview

本项目是基于 FastAPI 的局域网视频服务器，视频源存储在外接移动硬盘（`/Volumes/One Touch/movies`）上。当前实现存在 9 个性能缺陷，涵盖并发冲突、文件句柄泄漏、阻塞式扫描、不可靠的缓存验证、子进程延迟过高、串行批量检查、不合理的块大小以及未使用的预读缓冲区。

修复策略分为三个层面：
1. **并发安全层**（bug 1.1-1.3）：通过 `os.dup()` 独立文件描述符和 PrefetchBuffer 独立句柄，消除共享 seek 位置冲突和句柄泄漏
2. **I/O 优化层**（bug 1.4-1.6, 1.8-1.9）：后台扫描、容错缓存验证、减少子进程调用、调整块大小、启用预读
3. **并发执行层**（bug 1.7）：线程池并行批量检查

## Glossary

- **Bug_Condition (C)**: 触发性能缺陷的条件集合，包括并发文件句柄访问、缓存失效时的同步扫描、移动硬盘上的子进程调用等
- **Property (P)**: 修复后的期望行为——独立文件描述符、非阻塞扫描、可靠缓存验证、并发批量检查、合理块大小、有效预读
- **Preservation**: 修复不应改变的现有行为——Range 请求响应、扫描缓存持久化、视频列表元数据、完整性缓存、文件句柄清理
- **FileHandleCache**: `app.py` 中的 LRU 文件句柄缓存类，提供 `get()`、`put()`、`get_reader()`、`close_all()` 方法
- **PrefetchBuffer**: `app.py` 中的预读缓冲区类，在后台线程中预读取文件数据
- **VideoServer**: `video_catalog.py` 中的视频服务器类，负责扫描、缓存、目录浏览
- **iterfile_optimized()**: `app.py` 中流媒体传输的文件迭代器函数
- **check_video_status()**: `video_health.py` 中的视频健康检查函数，调用 ffprobe 和 lsof
- **background_check_videos()**: `video_health.py` 中的批量视频完整性检查函数

## Bug Details

### Bug Condition

系统存在 9 个相互关联的性能缺陷，可归纳为以下 bug 条件：

**C1 - 并发文件句柄冲突**（bug 1.1-1.3）：当多个请求或线程同时通过 `FileHandleCache` 访问同一文件时，共享的文件句柄导致 seek 位置冲突、句柄被意外关闭、以及无意义的缓存副作用。

**C2 - 同步阻塞扫描**（bug 1.4）：当扫描缓存失效时，`scan_videos(use_cache=False)` 在请求线程中同步执行 `os.walk()` 遍历整个移动硬盘目录树。

**C3 - 不可靠的缓存验证**（bug 1.5）：`_is_cache_valid()` 依赖 `_get_top_dir_mtime()` 检查目录 mtime，在 exFAT 文件系统上 mtime 精度不足导致频繁误判。

**C4 - 子进程延迟过高**（bug 1.6）：`check_video_status()` 对每个视频调用 `ffprobe` 和 `lsof` 子进程，在移动硬盘上延迟显著。

**C5 - 串行批量检查**（bug 1.7）：`batch_check_video_status` API 串行执行所有视频检查。

**C6 - 块大小不合理**（bug 1.8）：`STREAM_CHUNK_SIZE = 4MB` 在移动硬盘上单次读取耗时过长。

**C7 - 预读未使用**（bug 1.9）：`PrefetchBuffer` 类存在但 `iterfile_optimized()` 从未使用它。

**Formal Specification:**
```
FUNCTION isBugCondition(input)
  INPUT: input of type SystemOperation
  OUTPUT: boolean
  
  // C1: 并发文件句柄冲突
  IF input.type == "stream_request" AND
     input.concurrent_requests > 1 AND
     sharedFileHandle(input.file_path)
  THEN RETURN true
  
  // C2: 同步阻塞扫描
  IF input.type == "page_request" AND
     NOT scanCacheValid() AND
     scanExecutedSynchronously()
  THEN RETURN true
  
  // C3: 不可靠的缓存验证
  IF input.type == "cache_validation" AND
     fileSystem == "exFAT" AND
     mtimePrecisionInsufficient()
  THEN RETURN true
  
  // C4: 子进程延迟过高
  IF input.type == "health_check" AND
     videoOnExternalDrive(input.video_path) AND
     (callsFFprobe(input) OR callsLsof(input))
  THEN RETURN true
  
  // C5: 串行批量检查
  IF input.type == "batch_status_check" AND
     input.video_count > 1 AND
     executedSerially()
  THEN RETURN true
  
  // C6: 块大小不合理
  IF input.type == "stream_read" AND
     chunkSize > 1MB
  THEN RETURN true
  
  // C7: 预读未使用
  IF input.type == "stream_iteration" AND
     NOT prefetchBufferUsed()
  THEN RETURN true
  
  RETURN false
END FUNCTION
```

### Examples

- **并发冲突**：两个客户端同时请求同一视频的不同 Range 段，线程 A seek 到 100MB 位置后，线程 B seek 到 200MB，线程 A 的 read() 读取到 200MB 处的数据而非 100MB 处的数据
- **句柄泄漏**：PrefetchBuffer 打开文件后调用 `file_handle_cache.put()` 缓存句柄，`put()` 关闭了正在被流媒体传输使用的旧句柄，导致 `ValueError: I/O operation on closed file`
- **页面阻塞**：用户访问首页时缓存已过期，`scan_videos()` 同步遍历移动硬盘上 3000 个文件，页面 30 秒无响应
- **缓存误判**：exFAT 格式的移动硬盘 mtime 精度为 2 秒，两次 stat 调用间隔内 mtime 未变但被判定为失效
- **批量超时**：前端请求检查 50 个视频状态，串行执行每个 ffprobe + lsof 调用，总耗时超过 5 分钟
- **播放卡顿**：移动硬盘休眠唤醒后，单次 read(4MB) 耗时 2-3 秒，视频播放出现明显停顿

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- 带有效 Range 请求头的 HTTP 请求继续返回 206 Partial Content，包含正确的 Content-Range 头
- 不带 Range 请求头的 HTTP 请求继续返回 200 OK 和完整视频内容
- 扫描结果继续持久化到 `.video_scan_cache` 文件
- 视频列表继续返回完整元数据（id、name、path、size、modified 等）
- 完整性检查结果继续缓存到 `VIDEO_INTEGRITY_CACHE`，有效期 24 小时
- 应用关闭时继续通过 `close_all()` 正确关闭所有文件句柄
- 播放页面继续正确显示视频信息、推荐视频和导航链接

**Scope:**
所有不涉及上述 7 个 bug 条件的输入应完全不受修复影响。包括：
- 单客户端顺序访问视频流
- 缓存有效期内的页面请求
- 已缓存的视频健康检查结果查询
- 认证、登录、登出流程
- 视频删除、缩略图生成等管理操作

## Hypothesized Root Cause

基于代码分析，各 bug 的根因如下：

1. **PrefetchBuffer 共享文件句柄**（bug 1.1）：`_prefetch()` 方法调用 `file_handle_cache.get()` 获取共享句柄，该句柄的 seek 位置会被主线程的流媒体传输修改。`get()` 返回的是同一个 `io.BufferedReader` 对象，而非独立副本。

2. **put() 关闭正在使用的句柄**（bug 1.2）：`FileHandleCache.put()` 在缓存同一路径的新句柄时，会先 `close()` 旧句柄。如果旧句柄正在被另一个请求的 `iterfile_optimized()` 使用，该请求会收到 `ValueError`。

3. **finally 块中的无意义 get()**（bug 1.3）：`_prefetch()` 的 `finally` 块调用 `file_handle_cache.get()` 仅为检查句柄是否已缓存，但 `get()` 的副作用是更新 LRU 顺序和 TTL，且不使用返回值。

4. **scan_videos 同步执行**（bug 1.4）：当 `_is_cache_valid()` 返回 `False` 时，`scan_videos(use_cache=False)` 在当前请求线程中执行完整的 `os.walk()` 遍历。虽然已有 `ensure_background_scan()` 方法，但仅在首次加载时使用。

5. **mtime 比较无容错**（bug 1.5）：`_is_cache_valid()` 使用 `current_mtime > cached_mtime` 严格比较，exFAT 的 mtime 精度为 2 秒，可能导致同一时间窗口内的 stat 调用返回不同值。

6. **check_video_status 调用链过长**（bug 1.6）：每次检查依次调用 `is_temp_file()` → `is_file_locked(lsof)` → `is_file_growing()` → `ffprobe`，其中 `lsof` 和 `ffprobe` 都是子进程调用，在移动硬盘上延迟叠加。

7. **串行 for 循环**（bug 1.7）：`batch_check_video_status` API 使用 `for video_id in target_ids` 串行循环，每个迭代都等待 `check_video_status()` 完成。

8. **STREAM_CHUNK_SIZE = 4MB**（bug 1.8）：常量定义为 `4 * 1024 * 1024`，对于移动硬盘的随机读取特性，单次 4MB 读取耗时过长。

9. **PrefetchBuffer 未被调用**（bug 1.9）：`iterfile_optimized()` 直接使用 `open_file_with_cache()` 和 `f.read()`，从未实例化或调用 `PrefetchBuffer`。

## Correctness Properties

Property 1: Bug Condition - 独立文件描述符消除并发冲突

_For any_ 并发流媒体请求，其中多个线程同时访问同一视频文件的不同 Range 段，修复后的 `get_reader()` 方法 SHALL 通过 `os.dup()` 为每个请求返回独立的文件描述符，确保各请求的 seek 位置互不干扰，读取到正确的数据。

**Validates: Requirements 2.1, 2.2**

Property 2: Bug Condition - PrefetchBuffer 使用独立句柄

_For any_ PrefetchBuffer 预读操作，修复后的 `_prefetch()` 方法 SHALL 使用独立打开的文件句柄（而非从 `file_handle_cache` 获取共享句柄），完成后正确关闭该句柄，不对全局缓存产生副作用。

**Validates: Requirements 2.2, 2.3**

Property 3: Bug Condition - 后台扫描不阻塞页面

_For any_ 页面请求（当扫描缓存失效时），修复后的系统 SHALL 立即返回已缓存的数据（即使过期），并在后台线程中执行扫描，页面响应时间不受扫描耗时影响。

**Validates: Requirements 2.4**

Property 4: Bug Condition - 缓存验证容错

_For any_ 缓存验证操作，修复后的 `_is_cache_valid()` 方法 SHALL 在 mtime 比较中增加容错窗口（如 2 秒），避免因文件系统精度不足导致的误判。

**Validates: Requirements 2.5**

Property 5: Bug Condition - 健康检查优化

_For any_ 视频健康检查请求，修复后的 `check_video_status()` SHALL 优先使用缓存结果，跳过不必要的 `lsof` 检查，并对 `ffprobe` 设置合理超时，减少单次检查延迟。

**Validates: Requirements 2.6**

Property 6: Bug Condition - 批量检查并发执行

_For any_ 批量视频状态检查请求（最多 50 个视频），修复后的 `batch_check_video_status` API SHALL 使用线程池并发执行检查，总耗时接近单个检查耗时而非 N 倍。

**Validates: Requirements 2.7**

Property 7: Bug Condition - 流媒体块大小优化

_For any_ 流媒体传输操作，修复后的系统 SHALL 使用 1MB 的块大小（`STREAM_CHUNK_SIZE = 1 * 1024 * 1024`），减少单次读取等待时间。

**Validates: Requirements 2.8**

Property 8: Bug Condition - 预读缓冲区生效或移除

_For any_ 流媒体传输操作，修复后的 `iterfile_optimized()` SHALL 实际使用 `PrefetchBuffer` 进行后台预读，或者 `PrefetchBuffer` 类和 `STREAM_PREFETCH_SIZE` 配置被完全移除。

**Validates: Requirements 2.9**

Property 9: Preservation - Range 请求响应正确性

_For any_ 带有效 Range 请求头的 HTTP 请求，修复后的 `stream_video()` SHALL 返回与修复前相同的 206 Partial Content 响应，包含正确的 Content-Range 头和请求范围内的数据。

**Validates: Requirements 3.1, 3.2**

Property 10: Preservation - 扫描缓存持久化和视频元数据

_For any_ 视频扫描完成后，修复后的系统 SHALL 继续将结果持久化到 `.video_scan_cache` 文件，且视频列表包含完整的元数据字段（id、name、path、size、modified 等）。

**Validates: Requirements 3.3, 3.4**

Property 11: Preservation - 完整性缓存和资源清理

_For any_ 完整性检查结果，修复后的系统 SHALL 继续缓存到 `VIDEO_INTEGRITY_CACHE`（24 小时有效期），且应用关闭时正确关闭所有文件句柄。

**Validates: Requirements 3.5, 3.6, 3.7**

## Fix Implementation

### Changes Required

假设根因分析正确：

**File**: `app.py`

**1. 修复 PrefetchBuffer（bug 1.1-1.3）**：
- 重写 `_prefetch()` 方法，始终使用 `open(self.file_path, 'rb')` 打开独立句柄
- 移除对 `file_handle_cache.get()` 和 `file_handle_cache.put()` 的调用
- 在 `finally` 块中直接关闭自行打开的句柄，移除无意义的 `file_handle_cache.get()` 检查

**2. 调整 STREAM_CHUNK_SIZE（bug 1.8）**：
- 将 `STREAM_CHUNK_SIZE = 4 * 1024 * 1024` 改为 `STREAM_CHUNK_SIZE = 1 * 1024 * 1024`

**3. 在 iterfile_optimized() 中使用 PrefetchBuffer（bug 1.9）**：
- 在读取当前块的同时，启动 PrefetchBuffer 预读下一块数据
- 如果预读数据可用则直接使用，否则回退到同步读取
- 或者：如果 PrefetchBuffer 的复杂度不值得，则移除 PrefetchBuffer 类和 STREAM_PREFETCH_SIZE 配置

**File**: `video_catalog.py`

**4. 后台扫描替代同步扫描（bug 1.4）**：
- 修改 `scan_videos()` 方法，当缓存失效时返回过期缓存数据并触发后台扫描
- 利用已有的 `ensure_background_scan()` 方法

**5. 缓存验证增加容错窗口（bug 1.5）**：
- 在 `_is_cache_valid()` 的 mtime 比较中增加 2 秒容错：`current_mtime > cached_mtime + 2`
- 减少 `_get_top_dir_mtime()` 的调用频率

**File**: `video_health.py`

**6. 优化 check_video_status（bug 1.6）**：
- 跳过 `is_file_locked(lsof)` 检查（对移动硬盘不可靠且延迟高）
- 降低 `ffprobe` 超时时间（从 10 秒降到 5 秒）
- 增加结果缓存，避免重复检查

**7. 批量检查并发化（bug 1.7）**：
- 在 `batch_check_video_status` API 中使用 `concurrent.futures.ThreadPoolExecutor` 并发执行
- 设置合理的并发上限（如 `max_workers=8`）

## Testing Strategy

### Validation Approach

测试策略分两阶段：首先在未修复代码上发现反例以确认 bug，然后验证修复的正确性和行为保持。

### Exploratory Bug Condition Checking

**Goal**: 在实施修复前，发现能证明 bug 存在的反例，确认或否定根因分析。

**Test Plan**: 编写测试模拟并发文件访问、同步扫描阻塞、mtime 误判等场景，在未修复代码上运行以观察失败。

**Test Cases**:
1. **并发 seek 冲突测试**：两个线程同时通过 `file_handle_cache.get()` 获取同一文件句柄，分别 seek 到不同位置后读取，验证读取到的数据是否正确（在未修复代码上会失败）
2. **句柄关闭冲突测试**：线程 A 通过 `get()` 获取句柄并开始读取，线程 B 调用 `put()` 缓存新句柄导致旧句柄被关闭，验证线程 A 是否抛出异常（在未修复代码上会失败）
3. **PrefetchBuffer 副作用测试**：运行 PrefetchBuffer 后检查 `file_handle_cache` 的状态是否被意外修改（在未修复代码上会失败）
4. **同步扫描阻塞测试**：模拟缓存失效时的页面请求，测量响应时间（在未修复代码上会超时）

**Expected Counterexamples**:
- 并发读取返回错误数据（seek 位置被覆盖）
- `ValueError: I/O operation on closed file`（句柄被 put() 关闭）
- 页面响应时间 > 10 秒（同步扫描阻塞）

### Fix Checking

**Goal**: 验证对于所有触发 bug 条件的输入，修复后的函数产生期望行为。

**Pseudocode:**
```
FOR ALL input WHERE isBugCondition(input) DO
  result := fixedFunction(input)
  ASSERT expectedBehavior(result)
END FOR
```

具体验证：
- `get_reader()` 返回的句柄拥有独立 seek 位置
- PrefetchBuffer 使用独立句柄，不影响全局缓存
- 缓存失效时页面立即返回，后台扫描异步执行
- mtime 容错窗口内不触发重扫
- 批量检查并发执行，总耗时接近单次耗时
- 块大小为 1MB
- 预读缓冲区被实际使用或被移除

### Preservation Checking

**Goal**: 验证对于所有不触发 bug 条件的输入，修复后的函数产生与原函数相同的结果。

**Pseudocode:**
```
FOR ALL input WHERE NOT isBugCondition(input) DO
  ASSERT originalFunction(input) = fixedFunction(input)
END FOR
```

**Testing Approach**: 属性测试（Property-Based Testing）适合保持性检查，因为：
- 自动生成大量测试用例覆盖输入域
- 捕获手动测试可能遗漏的边界情况
- 对所有非 bug 输入提供强保证

**Test Plan**: 先在未修复代码上观察正常行为（鼠标点击、单客户端访问等），然后编写属性测试确保修复后行为不变。

**Test Cases**:
1. **Range 请求保持测试**：验证修复后的 `stream_video()` 对有效 Range 请求返回正确的 206 响应
2. **完整文件传输保持测试**：验证无 Range 请求时返回 200 和完整内容
3. **扫描缓存持久化保持测试**：验证扫描完成后缓存文件存在且可加载
4. **视频元数据保持测试**：验证视频列表包含所有必需字段

### Unit Tests

- 测试 `get_reader()` 返回独立文件描述符（fileno 不同）
- 测试 PrefetchBuffer 使用独立句柄且不调用缓存方法
- 测试 `_is_cache_valid()` 的 mtime 容错逻辑
- 测试 `check_video_status()` 跳过 lsof 调用
- 测试 `STREAM_CHUNK_SIZE` 值为 1MB
- 测试 PrefetchBuffer 在 iterfile_optimized 中被使用（或被移除）

### Property-Based Tests

- 生成随机 Range 请求参数，验证 `parse_range_header()` 和流媒体响应的正确性
- 生成随机并发场景（多线程 + 随机 seek 位置），验证 `get_reader()` 的独立性
- 生成随机 mtime 差值，验证 `_is_cache_valid()` 的容错行为
- 生成随机视频列表，验证批量检查的并发执行效果

### Integration Tests

- 测试完整的视频流传输流程（从请求到响应）
- 测试缓存失效 → 后台扫描 → 缓存更新的完整流程
- 测试批量健康检查 API 的端到端行为
- 测试应用启动和关闭时的资源管理

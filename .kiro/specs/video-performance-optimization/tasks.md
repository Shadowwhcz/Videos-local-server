# 实现计划

- [x] 1. 编写 Bug Condition 探索测试
  - **Property 1: Bug Condition** - 视频性能缺陷验证
  - **CRITICAL**: 此测试必须在未修复代码上 FAIL — 失败即确认 bug 存在
  - **DO NOT** 在测试失败时尝试修复测试或代码
  - **NOTE**: 此测试编码了期望行为 — 修复后通过即验证修复正确
  - **GOAL**: 发现反例以证明 bug 存在
  - **Scoped PBT Approach**: 针对确定性 bug，将属性限定到具体失败场景以确保可复现
  - 测试内容（来自 design.md Bug Condition）：
    - C1: 并发文件句柄冲突 — 两个线程通过 `file_handle_cache.get()` 获取同一文件的共享句柄，分别 seek 到不同位置后读取，验证数据正确性（未修复代码上会因 seek 位置冲突而失败）
    - C2: PrefetchBuffer 副作用 — 运行 PrefetchBuffer 后检查 `file_handle_cache` 状态是否被意外修改（未修复代码上 `_prefetch()` 会调用 `put()` 和 `get()` 产生副作用）
    - C3: `_is_cache_valid()` mtime 容错 — 当 `current_mtime` 与 `cached_mtime` 差值在 2 秒内时，验证缓存是否被判定为有效（未修复代码上严格比较 `>` 会误判为失效）
    - C6: STREAM_CHUNK_SIZE 值 — 验证 `STREAM_CHUNK_SIZE` 是否为 1MB（未修复代码上为 4MB）
    - C7: iterfile_optimized 是否使用 PrefetchBuffer — 验证流媒体迭代器是否使用了预读缓冲区（未修复代码上未使用）
  - 在未修复代码上运行测试
  - **EXPECTED OUTCOME**: 测试 FAIL（这是正确的 — 证明 bug 存在）
  - 记录发现的反例以理解根因
  - 当测试编写完成、运行完毕、失败已记录后标记任务完成
  - _Requirements: 2.1, 2.2, 2.3, 2.5, 2.8, 2.9_

- [x] 2. 编写 Preservation 属性测试（在实施修复前）
  - **Property 2: Preservation** - 现有行为保持验证
  - **IMPORTANT**: 遵循观察优先方法论
  - 在未修复代码上观察非 bug 输入的行为：
    - 观察: `parse_range_header("bytes=0-499", 1000)` 返回 `[(0, 499)]`
    - 观察: `parse_range_header("bytes=500-", 1000)` 返回 `[(500, 999)]`
    - 观察: `parse_range_header("bytes=-200", 1000)` 返回 `[(800, 999)]`
    - 观察: `scan_videos(use_cache=True)` 在缓存有效时返回缓存数据，包含完整元数据字段（id, name, path, size, modified 等）
    - 观察: `_is_cache_valid()` 在缓存未过期且 mtime 未变化时返回 `True`
    - 观察: `check_video_status()` 对正常视频返回 `{"status": "normal", "reason": None}`
    - 观察: `VIDEO_INTEGRITY_CACHE` 缓存有效期为 24 小时
    - 观察: `file_handle_cache.close_all()` 关闭所有缓存句柄
  - 编写属性测试（来自 design.md Preservation Requirements）：
    - P9: 对所有有效 Range 请求参数，`parse_range_header()` 返回正确的 (start, end) 元组
    - P10: 扫描结果持久化到 `.video_scan_cache`，视频列表包含完整元数据
    - P11: 完整性缓存 24 小时有效期，`close_all()` 正确关闭所有句柄
  - 在未修复代码上运行测试
  - **EXPECTED OUTCOME**: 测试 PASS（确认基线行为需要保持）
  - 当测试编写完成、运行完毕、在未修复代码上通过后标记任务完成
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7_

- [x] 3. 修复 PrefetchBuffer 并发冲突和副作用（bug 1.1-1.3）

  - [x] 3.1 重写 PrefetchBuffer._prefetch() 使用独立文件句柄
    - 移除 `file_handle_cache.get()` 调用，始终使用 `open(self.file_path, 'rb')` 打开独立句柄
    - 移除 `file_handle_cache.put()` 调用，不再将预读句柄缓存到全局缓存
    - 移除 `finally` 块中无意义的 `file_handle_cache.get()` 检查
    - 在 `finally` 块中直接关闭自行打开的句柄
    - _Bug_Condition: C1-C3 — PrefetchBuffer 通过共享句柄导致 seek 冲突、put() 关闭正在使用的句柄、finally 中无意义的 get() 副作用_
    - _Expected_Behavior: PrefetchBuffer 使用独立句柄，完成后正确关闭，不对全局缓存产生副作用_
    - _Preservation: 预读功能本身的数据读取行为不变_
    - _Requirements: 2.1, 2.2, 2.3_

  - [x] 3.2 验证 bug condition 探索测试现在通过（C1-C3 部分）
    - **Property 1: Expected Behavior** - PrefetchBuffer 独立句柄
    - **IMPORTANT**: 重新运行任务 1 中的相同测试 — 不要编写新测试
    - 运行 C1（并发句柄冲突）和 C2（PrefetchBuffer 副作用）相关测试
    - **EXPECTED OUTCOME**: 测试 PASS（确认 bug 已修复）
    - _Requirements: 2.1, 2.2, 2.3_

  - [x] 3.3 验证 preservation 测试仍然通过
    - **Property 2: Preservation** - 文件句柄行为保持
    - **IMPORTANT**: 重新运行任务 2 中的相同测试 — 不要编写新测试
    - **EXPECTED OUTCOME**: 测试 PASS（确认无回归）
    - 确认所有 preservation 测试在修复后仍然通过

- [x] 4. 调整 STREAM_CHUNK_SIZE 并启用 PrefetchBuffer（bug 1.8-1.9）

  - [x] 4.1 将 STREAM_CHUNK_SIZE 从 4MB 改为 1MB
    - 修改 `STREAM_CHUNK_SIZE = 4 * 1024 * 1024` 为 `STREAM_CHUNK_SIZE = 1 * 1024 * 1024`
    - _Bug_Condition: C6 — 4MB 块大小在移动硬盘上单次读取耗时过长_
    - _Expected_Behavior: 使用 1MB 块大小减少单次读取等待时间_
    - _Preservation: 流媒体传输的功能行为不变，仅块大小调整_
    - _Requirements: 2.8_

  - [x] 4.2 在 iterfile_optimized() 中使用 PrefetchBuffer 或移除死代码
    - 方案 A: 在 `iterfile_optimized()` 中实际使用 `PrefetchBuffer` 进行后台预读
    - 方案 B: 如果 PrefetchBuffer 复杂度不值得，移除 `PrefetchBuffer` 类和 `STREAM_PREFETCH_SIZE` 配置
    - _Bug_Condition: C7 — PrefetchBuffer 存在但从未被 iterfile_optimized() 使用_
    - _Expected_Behavior: 预读缓冲区生效或死代码被移除_
    - _Preservation: 流媒体传输的数据正确性不变_
    - _Requirements: 2.9_

  - [x] 4.3 验证 bug condition 探索测试现在通过（C6-C7 部分）
    - **Property 1: Expected Behavior** - 块大小和预读优化
    - **IMPORTANT**: 重新运行任务 1 中的相同测试 — 不要编写新测试
    - 运行 C6（STREAM_CHUNK_SIZE）和 C7（PrefetchBuffer 使用）相关测试
    - **EXPECTED OUTCOME**: 测试 PASS（确认 bug 已修复）
    - _Requirements: 2.8, 2.9_

  - [x] 4.4 验证 preservation 测试仍然通过
    - **Property 2: Preservation** - 流媒体传输行为保持
    - **IMPORTANT**: 重新运行任务 2 中的相同测试 — 不要编写新测试
    - **EXPECTED OUTCOME**: 测试 PASS（确认无回归）

- [x] 5. 后台扫描替代同步扫描（bug 1.4）

  - [x] 5.1 修改 scan_videos() 缓存失效时返回过期数据并触发后台扫描
    - 当 `_is_cache_valid()` 返回 `False` 时，检查是否存在过期缓存数据
    - 如果存在过期缓存，立即返回过期数据并调用 `ensure_background_scan()` 触发后台扫描
    - 如果不存在任何缓存数据，触发后台扫描并返回空列表
    - _Bug_Condition: C2 — 缓存失效时 scan_videos(use_cache=False) 在请求线程中同步执行 os.walk()_
    - _Expected_Behavior: 页面立即返回已缓存数据（即使过期），后台线程异步执行扫描_
    - _Preservation: 扫描结果继续持久化到 .video_scan_cache，视频元数据完整_
    - _Requirements: 2.4, 3.3, 3.4_

  - [x] 5.2 验证后台扫描不阻塞页面请求
    - 确认缓存失效时页面响应时间不受扫描耗时影响
    - _Requirements: 2.4_

- [x] 6. 缓存验证增加容错窗口（bug 1.5）

  - [x] 6.1 修改 _is_cache_valid() 的 mtime 比较逻辑
    - 将 `current_mtime > cached_mtime` 改为 `current_mtime > cached_mtime + 2`（2 秒容错）
    - 适配 exFAT 文件系统的 mtime 精度限制
    - _Bug_Condition: C3 — 严格 mtime 比较在 exFAT 上因精度不足导致频繁误判_
    - _Expected_Behavior: mtime 差值在 2 秒内不触发重扫_
    - _Preservation: 真正的目录变更仍能被检测到_
    - _Requirements: 2.5_

  - [x] 6.2 验证 bug condition 探索测试现在通过（C3 部分）
    - **Property 1: Expected Behavior** - 缓存验证容错
    - **IMPORTANT**: 重新运行任务 1 中的相同测试 — 不要编写新测试
    - 运行 C3（mtime 容错）相关测试
    - **EXPECTED OUTCOME**: 测试 PASS（确认 bug 已修复）
    - _Requirements: 2.5_

  - [x] 6.3 验证 preservation 测试仍然通过
    - **Property 2: Preservation** - 缓存验证行为保持
    - **IMPORTANT**: 重新运行任务 2 中的相同测试 — 不要编写新测试
    - **EXPECTED OUTCOME**: 测试 PASS（确认无回归）

- [x] 7. 优化 check_video_status（bug 1.6）

  - [x] 7.1 跳过 lsof 检查并降低 ffprobe 超时
    - 在 `check_video_status()` 中移除或跳过 `is_file_locked(lsof)` 调用
    - 将 `ffprobe` 超时从 10 秒降低到 5 秒
    - _Bug_Condition: C4 — 每次检查调用 lsof + ffprobe，在移动硬盘上延迟叠加_
    - _Expected_Behavior: 跳过不必要的 lsof 检查，ffprobe 超时合理_
    - _Preservation: 检查结果的语义不变（normal/downloading/corrupted）_
    - _Requirements: 2.6_

  - [x] 7.2 验证健康检查优化效果
    - 确认 `check_video_status()` 不再调用 `is_file_locked()`
    - 确认 `ffprobe` 超时为 5 秒
    - _Requirements: 2.6_

- [x] 8. 批量检查并发化（bug 1.7）

  - [x] 8.1 在 batch_check_video_status 中使用 ThreadPoolExecutor
    - 导入 `concurrent.futures.ThreadPoolExecutor`
    - 将串行 `for` 循环替换为 `executor.map()` 或 `executor.submit()` 并发执行
    - 设置 `max_workers=8` 作为并发上限
    - _Bug_Condition: C5 — 串行 for 循环逐个等待 check_video_status() 完成_
    - _Expected_Behavior: 线程池并发执行，总耗时接近单次检查耗时_
    - _Preservation: 返回结果格式和语义不变_
    - _Requirements: 2.7_

  - [x] 8.2 验证批量检查并发执行
    - 确认使用了 ThreadPoolExecutor
    - 确认 max_workers 设置合理
    - _Requirements: 2.7_

- [x] 9. Checkpoint - 确保所有测试通过
  - 运行所有 bug condition 探索测试（Property 1），确认全部 PASS
  - 运行所有 preservation 属性测试（Property 2），确认全部 PASS
  - 如有问题，询问用户

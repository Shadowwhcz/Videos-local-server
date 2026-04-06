# 实现计划：MPEG-TS 容器格式播放支持

## 概述

在现有局域网视频服务器中增加 MPEG-TS 容器格式的播放能力。后端通过文件头字节检测容器格式，前端根据检测结果选择原生播放或 mpegts.js 播放。实现按照后端检测 → API 扩展 → 模板修改 → 前端适配 → MIME 适配的顺序递进。

## Tasks

- [x] 1. 实现后端容器格式检测函数
  - [x] 1.1 在 `video_catalog.py` 中新增 `detect_container_format(file_path: str) -> str` 纯函数
    - 读取文件前 8 字节，根据魔数判断容器格式
    - 第一字节为 `0x47` 返回 `'mpegts'`，前 8 字节含 `b'ftyp'` 返回 `'mp4'`，其他返回 `'unknown'`
    - 文件不存在或读取失败时返回 `'unknown'` 并记录 `logger.warning`
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.6_

  - [ ]* 1.2 编写属性测试：容器格式检测正确性
    - **Property 1: 容器格式检测正确性**
    - 使用 hypothesis 生成随机 8~64 字节内容写入临时文件，验证 `detect_container_format` 返回值与文件头规则一致
    - **验证需求: 1.2, 1.3, 1.4**

  - [ ]* 1.3 编写单元测试：容器格式检测边界情况
    - 测试真实 TS 文件头（`0x47 0x40 0x11 ...`）
    - 测试真实 MP4 文件头（`\x00\x00\x00\x20ftyp`）
    - 测试空文件（0 字节）返回 `'unknown'`
    - 测试不存在的文件返回 `'unknown'`
    - _Requirements: 1.2, 1.3, 1.4, 1.6_

- [x] 2. 扩展后端 API 和路由
  - [x] 2.1 修改 `app.py` 中的 `api_video_info` 端点，在返回 JSON 中增加 `container_format` 字段
    - 导入 `detect_container_format`，调用后将结果写入 `info` 字典
    - _Requirements: 1.1, 1.5_

  - [x] 2.2 修改 `app.py` 中的 `play` 路由，获取容器格式并传入模板上下文
    - 调用 `detect_container_format(video["path"])` 获取格式
    - 将 `container_format` 加入模板渲染上下文
    - _Requirements: 2.1, 2.2, 2.3_

  - [x] 2.3 修改 `app.py` 中的 `stream_video` 端点，根据容器格式返回正确的 MIME 类型
    - 替换原有 `get_mime_type(video_path)` 调用，使用 `detect_container_format` 结果决定 MIME 类型
    - `mpegts` → `video/mp2t`，`mp4` → `video/mp4`，`unknown` → 回退到 `get_mime_type()`
    - _Requirements: 6.1, 6.2, 6.3_

  - [ ]* 2.4 编写属性测试：MIME 类型与容器格式一致性
    - **Property 2: MIME 类型与容器格式一致性**
    - 使用 FastAPI TestClient，创建不同容器格式的临时视频文件，验证 `/stream/{video_id}` 的 `Content-Type` 与检测结果一致
    - **验证需求: 6.1, 6.2, 6.3**

- [x] 3. 检查点 - 后端功能验证
  - 确保所有测试通过，如有问题请向用户确认。

- [x] 4. 修改播放页面模板和前端播放器
  - [x] 4.1 修改 `templates/play.html`，在 `<video>` 元素上添加 `data-container-format="{{ container_format }}"` 属性
    - 在 `</body>` 前、`app.js` 之前通过 CDN 引入 mpegts.js：`<script src="https://cdn.jsdelivr.net/npm/mpegts.js@1/dist/mpegts.min.js"></script>`
    - _Requirements: 2.2, 3.1_

  - [x] 4.2 修改 `static/app.js`，在 `initPlayer()` 中增加 TS 格式播放分支
    - 新增全局变量 `mpegtsPlayer`
    - 读取 `video.dataset.containerFormat`，当值为 `mpegts` 且 `mpegts.isSupported()` 时调用 `initTSPlayer(video)`
    - `initTSPlayer` 中移除原生 source 标签，创建 mpegts.js 播放器实例，绑定到 video 元素，加载并播放
    - 恢复 localStorage 中保存的播放位置
    - 监听 `mpegts.Events.ERROR` 并通过 `sendPlayerMonitorEvent` 上报
    - CDN 加载失败时（`typeof mpegts === 'undefined'`）回退到原生播放
    - _Requirements: 3.2, 3.3, 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7_

  - [x] 4.3 在 `static/app.js` 中添加 `beforeunload` 事件处理，清理 mpegts.js 资源
    - 检查 `mpegtsPlayer` 是否存在，存在则调用 `detachMediaElement()` 和 `destroy()`
    - 使用 try-catch 包裹，静默失败
    - _Requirements: 5.1, 5.2, 5.3_

- [x] 5. 集成连通与最终检查点
  - 确保所有测试通过，如有问题请向用户确认。

## 备注

- 标记 `*` 的任务为可选，可跳过以加速 MVP 交付
- 每个任务引用了对应的需求编号，确保可追溯性
- 属性测试验证设计文档中定义的正确性属性
- 现有 MP4 播放路径完全不变，仅在检测到 TS 格式时走新分支

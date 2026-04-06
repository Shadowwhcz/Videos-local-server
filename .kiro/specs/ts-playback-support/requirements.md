# 需求文档：MPEG-TS 容器格式播放支持

## 简介

当前项目使用原生 HTML5 `<video>` 标签播放视频。部分视频文件虽然扩展名为 `.mp4`，但实际容器格式为 MPEG-TS（文件头以 `0x47` TS sync byte 开头，而非 `ftyp` MP4 magic）。浏览器原生 video 标签无法播放 TS 容器格式的视频。需要在前端引入 mpegts.js 库来检测并处理这类非标准容器格式，实现透明的播放体验。

## 术语表

- **Player**：前端视频播放器模块，位于 `static/app.js` 中的 `initPlayer()` 函数及相关逻辑
- **Container_Detector**：后端容器格式检测模块，通过读取文件头部字节判断实际容器格式
- **Video_Info_API**：后端 `/api/video/info/{video_id}` 端点，返回视频元数据
- **Stream_Endpoint**：后端 `/stream/{video_id}` 端点，提供视频流传输
- **TS_Player**：基于 mpegts.js 的前端 MPEG-TS 播放适配层
- **Play_Page**：播放页面模板 `templates/play.html`
- **MPEG-TS**：MPEG Transport Stream，一种流媒体容器格式，文件头以 `0x47` 字节开头
- **MP4**：ISO Base Media File Format 容器，文件头包含 `ftyp` 标识

## 需求

### 需求 1：后端容器格式检测

**用户故事：** 作为播放器前端，我需要知道视频文件的实际容器格式，以便选择正确的播放策略。

#### 验收标准

1. WHEN Video_Info_API 接收到视频信息请求, THE Container_Detector SHALL 读取视频文件的前 8 个字节来判断实际容器格式
2. WHEN 文件头部第一个字节为 `0x47`, THE Container_Detector SHALL 将容器格式标识为 `mpegts`
3. WHEN 文件头部前 8 字节中包含 `ftyp` 标识, THE Container_Detector SHALL 将容器格式标识为 `mp4`
4. WHEN 容器格式无法通过文件头识别, THE Container_Detector SHALL 将容器格式标识为 `unknown`
5. THE Video_Info_API SHALL 在响应 JSON 中包含 `container_format` 字段，值为 `mpegts`、`mp4` 或 `unknown`
6. IF 文件读取失败, THEN THE Container_Detector SHALL 将容器格式标识为 `unknown` 并记录警告日志

### 需求 2：播放页面传递容器格式信息

**用户故事：** 作为前端播放器，我需要在页面加载时就知道视频的容器格式，以便在视频开始加载前选择正确的播放方式。

#### 验收标准

1. WHEN Play_Page 渲染播放页面, THE Play_Page SHALL 通过 Video_Info_API 获取视频的容器格式信息
2. THE Play_Page SHALL 将容器格式信息作为 `data-container-format` 属性写入 `<video>` 元素
3. WHEN 容器格式信息获取失败, THE Play_Page SHALL 将 `data-container-format` 设置为 `unknown`

### 需求 3：前端引入 mpegts.js 库

**用户故事：** 作为开发者，我需要在前端引入 mpegts.js 库，以便支持 MPEG-TS 容器格式的视频播放。

#### 验收标准

1. THE Play_Page SHALL 通过 CDN 引入 mpegts.js 库脚本
2. THE Player SHALL 在 mpegts.js 加载完成后才初始化 TS 播放逻辑
3. IF mpegts.js 加载失败, THEN THE Player SHALL 回退到原生 video 标签播放并在控制台记录警告

### 需求 4：MPEG-TS 视频播放适配

**用户故事：** 作为用户，我希望 MPEG-TS 格式的视频能像普通 MP4 一样正常播放，无需任何额外操作。

#### 验收标准

1. WHEN `data-container-format` 为 `mpegts`, THE TS_Player SHALL 使用 mpegts.js 创建播放器实例并绑定到 video 元素
2. WHEN `data-container-format` 不为 `mpegts`, THE Player SHALL 使用原生 video 标签播放（保持现有行为不变）
3. THE TS_Player SHALL 配置 mpegts.js 使用 `mpegts.MediaDataSource` 类型为 `mpegts`，URL 指向 Stream_Endpoint
4. WHEN TS_Player 初始化成功, THE TS_Player SHALL 触发视频自动播放（与当前 MP4 播放行为一致）
5. WHEN TS_Player 播放过程中发生错误, THE TS_Player SHALL 通过 monitor 事件上报错误详情
6. THE TS_Player SHALL 支持现有的播放位置保存和恢复功能（localStorage 中的 `video_pos_` 记录）
7. THE TS_Player SHALL 支持现有的键盘快捷键控制（空格暂停、方向键快进快退、F 全屏等）

### 需求 5：播放器资源清理

**用户故事：** 作为系统，我需要在页面卸载或播放器销毁时正确释放 mpegts.js 占用的资源，避免内存泄漏。

#### 验收标准

1. WHEN 用户离开播放页面, THE TS_Player SHALL 调用 mpegts.js 实例的 `detachMediaElement()` 和 `destroy()` 方法释放资源
2. WHEN 播放器实例不存在（非 TS 格式视频）, THE Player SHALL 跳过 mpegts.js 资源清理步骤
3. THE TS_Player SHALL 在 `beforeunload` 事件中执行资源清理

### 需求 6：Stream_Endpoint MIME 类型适配

**用户故事：** 作为 mpegts.js 库，我需要接收正确的 MIME 类型响应头，以便正确解析视频流数据。

#### 验收标准

1. WHEN Stream_Endpoint 传输 MPEG-TS 格式的视频文件, THE Stream_Endpoint SHALL 返回 `Content-Type: video/mp2t` 响应头
2. WHEN Stream_Endpoint 传输标准 MP4 格式的视频文件, THE Stream_Endpoint SHALL 返回 `Content-Type: video/mp4` 响应头
3. THE Stream_Endpoint SHALL 使用 Container_Detector 的检测结果来确定正确的 MIME 类型，而非仅依赖文件扩展名

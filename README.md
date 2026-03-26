# 🎬 局域网视频服务器

一个简洁美观的局域网视频网站服务，支持多设备访问、响应式设计和流式播放。

## ✨ 功能特点

- 🎯 **简洁美观的Web界面** - 现代化深色主题设计
- 📱 **响应式布局** - 完美适配电脑、平板、手机
- 🎬 **流式播放** - 支持拖动进度条，Range请求优化
- 🔍 **视频搜索** - 快速查找本地视频
- 💾 **播放记忆** - 自动保存播放进度
- ⌨️ **快捷键** - 键盘控制播放、音量、全屏
- 📁 **多目录支持** - 可配置多个视频目录
- 🔒 **纯局域网** - 无需认证，私人使用

## 📋 系统要求

- Python 3.8+
- 现代浏览器（Chrome、Firefox、Safari、Edge）

## 🚀 快速开始

### 1. 配置视频目录

编辑 `config.ini` 文件，设置你的视频目录：

```ini
[video]
# 支持多个目录，用逗号分隔
directories = ~/Movies, ~/Videos, /path/to/your/videos
```

### 2. 启动服务器

```bash
# 方式一：使用启动脚本（推荐）
./start.sh

# 方式二：手动启动
pip install -r requirements.txt
python app.py
```

### 3. 访问网站

服务器启动后，在浏览器中访问：

- **本机访问**: http://localhost:8000
- **局域网访问**: http://你的IP地址:8000

手机访问：确保手机和电脑在同一局域网，然后在手机浏览器输入电脑的IP地址加端口。

## ⚙️ 配置说明

编辑 `config.ini` 文件：

```ini
[server]
# 服务绑定地址
# 0.0.0.0 允许所有网络访问
# 127.0.0.1 仅本机访问
host = 0.0.0.0

# 服务端口
port = 8000

[video]
# 视频目录（支持 ~ 表示用户目录）
# 多个目录用逗号分隔
directories = ~/Movies, ~/Videos

# 支持的视频格式
extensions = mp4,mkv,avi,mov,wmv,flv,webm,m4v

[ui]
# 每页显示视频数量
videos_per_page = 30
```

## ⌨️ 快捷键

在播放页面，支持以下键盘快捷键：

| 按键 | 功能 |
|------|------|
| `空格` | 暂停/播放 |
| `←` `→` | 快退/快进 5秒 |
| `↑` `↓` | 音量增/减 |
| `F` | 全屏/退出全屏 |
| `M` | 静音 |
| `Esc` | 退出全屏 |

**移动端**：双击视频区域切换全屏

## 📱 移动端支持

- 响应式设计，自动适配屏幕尺寸
- 触控友好的播放器控制
- 支持手机浏览器原生播放器功能

## 🎥 支持的格式

- MP4
- MKV
- AVI
- MOV
- WebM
- M4V
- WMV
- FLV

> ⚠️ 注意：实际播放支持取决于浏览器能力。Chrome 支持最多格式。

## 🔧 常见问题

### Q: 手机无法访问？

确保：
1. 手机和电脑在同一局域网
2. 电脑防火墙允许端口访问
3. `config.ini` 中 `host` 设置为 `0.0.0.0`

### Q: 视频无法播放？

可能原因：
1. 视频格式浏览器不支持 - 尝试用 Chrome
2. 视频文件损坏
3. 文件权限问题

### Q: 如何查看本机IP？

```bash
# macOS/Linux
ifconfig | grep "inet " | grep -v 127.0.0.1

# 或
ipconfig getifaddr en0
```

### Q: 如何开机自启动？

创建 systemd 服务（Linux）或 LaunchAgent（macOS）：

**macOS LaunchAgent 示例**:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.local.videoserver</string>
    <key>ProgramArguments</key>
    <array>
        <string>/path/to/Videos-local-server/start.sh</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
</dict>
</plist>
```

## 📝 开发说明

### 项目结构

```
Videos-local-server/
├── app.py              # 主应用
├── config.ini          # 配置文件
├── requirements.txt    # Python依赖
├── start.sh           # 启动脚本
├── static/
│   ├── style.css      # 样式文件
│   └── app.js         # 前端脚本
└── templates/
    ├── index.html     # 视频列表页
    └── play.html      # 播放页面
```

### API 接口

- `GET /` - 视频列表页面
- `GET /play/{video_id}` - 播放页面
- `GET /stream/{video_id}` - 视频流（支持Range请求）
- `GET /api/videos` - 视频列表API
- `GET /api/config` - 配置信息API

## 📜 许可证

MIT License

## 🙏 致谢

- [FastAPI](https://fastapi.tiangolo.com/) - 现代高效的 Python Web 框架
- [Bootstrap 5](https://getbootstrap.com/) - 响应式前端框架
- [Bootstrap Icons](https://icons.getbootstrap.com/) - 精美图标
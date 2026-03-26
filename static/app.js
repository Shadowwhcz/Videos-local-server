/**
 * 视频服务器前端脚本
 */

// 全局配置
let appConfig = {
    authEnabled: false
};

// 待检查的视频ID列表
let pendingVideos = [];
let pollInterval = null;

// ==================== 性能优化配置 ====================
const BATCH_DELAY = 100;        // 批量请求延迟(ms)
const MAX_CONCURRENT = 3;       // 最大并发请求数
const STATUS_BATCH_SIZE = 20;   // 状态检查批量大小
const DURATION_BATCH_SIZE = 10; // 时长获取批量大小

// 请求队列管理
class RequestQueue {
    constructor(maxConcurrent = MAX_CONCURRENT) {
        this.maxConcurrent = maxConcurrent;
        this.running = 0;
        this.queue = [];
    }
    
    async add(fn) {
        return new Promise((resolve, reject) => {
            this.queue.push({ fn, resolve, reject });
            this.process();
        });
    }
    
    async process() {
        while (this.queue.length > 0 && this.running < this.maxConcurrent) {
            this.running++;
            const { fn, resolve, reject } = this.queue.shift();
            try {
                const result = await fn();
                resolve(result);
            } catch (err) {
                reject(err);
            } finally {
                this.running--;
                this.process();
            }
        }
    }
}

const requestQueue = new RequestQueue();

document.addEventListener('DOMContentLoaded', function() {
    // 初始化所有功能（分批加载，减少并发）
    initConfig().then(() => {
        initSearch();
        initVideoCards();
        initThumbnails();
        initPlayer();
        initDeleteButtons();
        
        // 延迟加载非关键功能
        setTimeout(() => {
            initVideoPreview();
        }, 500);
        
        // 进一步延迟加载状态检查
        setTimeout(() => {
            initAutoIntegrityCheck();
        }, 1000);
        
        // 最后加载时长信息
        setTimeout(() => {
            initVideoDurations();
        }, 1500);
    });
});

/**
 * 加载配置
 */
async function initConfig() {
    try {
        const response = await fetch('/api/config');
        if (response.ok) {
            appConfig = await response.json();
        }
    } catch (e) {
        console.log('无法加载配置');
    }
}

/**
 * 搜索框交互
 */
function initSearch() {
    const searchInput = document.querySelector('.search-box input');
    if (searchInput) {
        searchInput.addEventListener('keypress', function(e) {
            if (e.key === 'Enter') {
                this.form.submit();
            }
        });
    }
}

/**
 * 视频卡片点击
 */
function initVideoCards() {
    document.querySelectorAll('.video-card').forEach(card => {
        card.addEventListener('click', function(e) {
            const wrapper = this.closest('.video-card-wrapper');
            
            // 如果是下载中视频，阻止跳转
            if (wrapper && wrapper.classList.contains('downloading')) {
                e.preventDefault();
                showDownloadingAlert(wrapper.dataset.videoName, wrapper.dataset.downloadReason);
                return false;
            }
            
            // 如果是损坏视频，阻止跳转
            if (wrapper && wrapper.classList.contains('corrupted')) {
                e.preventDefault();
                showCorruptedAlert(wrapper.dataset.videoName);
                return false;
            }
        });
    });
}

/**
 * 显示下载中提示
 */
function showDownloadingAlert(videoName, reason) {
    const modal = document.getElementById('downloadingAlertModal');
    if (modal) {
        const nameEl = modal.querySelector('.downloading-video-name');
        if (nameEl) nameEl.textContent = videoName;
        modal.classList.add('show');
    } else {
        // 备用：toast提示
        let toast = document.getElementById('downloadingToast');
        if (!toast) {
            toast = document.createElement('div');
            toast.id = 'downloadingToast';
            toast.className = 'downloading-toast';
            toast.innerHTML = `
                <div class="toast-icon"><i class="bi bi-download"></i></div>
                <div class="toast-content">
                    <div class="toast-title">正在下载</div>
                    <div class="toast-message"></div>
                </div>
            `;
            document.body.appendChild(toast);
        }
        
        toast.querySelector('.toast-message').textContent = videoName;
        toast.classList.add('show');
        
        setTimeout(() => {
            toast.classList.remove('show');
        }, 3000);
    }
}

/**
 * 显示损坏视频提示
 */
function showCorruptedAlert(videoName) {
    const modal = document.getElementById('corruptedAlertModal');
    if (modal) {
        const nameEl = modal.querySelector('.corrupted-video-name');
        if (nameEl) nameEl.textContent = videoName;
        modal.classList.add('show');
    } else {
        alert(`视频 "${videoName}" 已损坏，无法播放`);
    }
}

/**
 * 缩略图懒加载
 */
function initThumbnails() {
    const thumbnails = document.querySelectorAll('.video-thumbnail');
    
    if ('IntersectionObserver' in window) {
        const imageObserver = new IntersectionObserver((entries, observer) => {
            entries.forEach(entry => {
                if (entry.isIntersecting) {
                    const img = entry.target;
                    const src = img.dataset.src;
                    
                    if (src) {
                        img.onload = () => {
                            img.classList.add('loaded');
                        };
                        img.onerror = () => {
                            // 加载失败，保持占位符
                            img.style.display = 'none';
                        };
                        img.src = src;
                    }
                    
                    observer.unobserve(img);
                }
            });
        }, {
            rootMargin: '100px 0px',
            threshold: 0.1
        });
        
        thumbnails.forEach(img => imageObserver.observe(img));
    } else {
        // 降级处理：直接加载所有图片
        thumbnails.forEach(img => {
            if (img.dataset.src) {
                img.src = img.dataset.src;
                img.onload = () => img.classList.add('loaded');
            }
        });
    }
}

/**
 * 视频预览（悬停播放）
 */
function initVideoPreview() {
    const videoCards = document.querySelectorAll('.video-card-wrapper');
    
    videoCards.forEach(wrapper => {
        const card = wrapper.querySelector('.video-card');
        const previewContainer = card?.querySelector('.video-preview');
        const video = previewContainer?.querySelector('video');
        const thumbnail = card?.querySelector('.video-thumbnail');
        
        if (!previewContainer || !video) return;
        
        // 损坏视频不启用预览
        if (wrapper.classList.contains('corrupted')) return;
        
        let loaded = false;
        let loadPromise = null;
        
        // 鼠标进入时开始加载
        card.addEventListener('mouseenter', () => {
            if (!loaded && !loadPromise) {
                const src = previewContainer.dataset.src;
                if (src) {
                    video.src = src + '#t=3'; // 从第3秒开始
                    loadPromise = video.load();
                    loaded = true;
                }
            }
        });
        
        // 悬停一段时间后开始播放预览
        let hoverTimeout;
        card.addEventListener('mouseenter', () => {
            hoverTimeout = setTimeout(() => {
                if (video.readyState >= 2) {
                    video.play().catch(() => {});
                    thumbnail?.classList.add('preview-active');
                }
            }, 800); // 悬停800ms后开始预览
        });
        
        card.addEventListener('mouseleave', () => {
            clearTimeout(hoverTimeout);
            video.pause();
            video.currentTime = 3;
            thumbnail?.classList.remove('preview-active');
        });
        
        // 点击时停止预览
        card.addEventListener('click', () => {
            video.pause();
        });
    });
}

/**
 * 获取视频时长（批量优化版）
 */
function initVideoDurations() {
    const durationElements = document.querySelectorAll('.video-duration');
    if (durationElements.length === 0) return;
    
    // 收集需要获取时长的视频
    const toFetch = [];
    durationElements.forEach(el => {
        const videoId = el.dataset.videoId;
        if (!videoId) return;
        
        // 尝试从缓存获取
        const cacheKey = `video_duration_${videoId}`;
        const cached = localStorage.getItem(cacheKey);
        
        if (cached) {
            updateDurationDisplay(el, cached);
            return;
        }
        
        toFetch.push({ el, videoId });
    });
    
    if (toFetch.length === 0) return;
    
    // 分批获取时长
    (async () => {
        const batches = [];
        for (let i = 0; i < toFetch.length; i += DURATION_BATCH_SIZE) {
            batches.push(toFetch.slice(i, i + DURATION_BATCH_SIZE));
        }
        
        for (const batch of batches) {
            await Promise.all(batch.map(async ({ el, videoId }) => {
                try {
                    const info = await requestQueue.add(() => fetchVideoInfo(videoId));
                    if (info.duration_formatted) {
                        updateDurationDisplay(el, info.duration_formatted);
                        // 缓存1小时
                        localStorage.setItem(`video_duration_${videoId}`, info.duration_formatted);
                        localStorage.setItem(`video_duration_${videoId}_time`, Date.now().toString());
                    }
                } catch (err) {
                    // 静默失败
                }
            }));
            
            // 批次间延迟
            if (batches.indexOf(batch) < batches.length - 1) {
                await new Promise(r => setTimeout(r, BATCH_DELAY));
            }
        }
    })();
}

/**
 * 更新时长显示
 */
function updateDurationDisplay(element, duration) {
    const textEl = element.querySelector('.duration-text');
    if (textEl) {
        textEl.textContent = duration;
    }
}

/**
 * 获取视频信息API
 */
async function fetchVideoInfo(videoId) {
    const response = await fetch(`/api/video/info/${videoId}`);
    if (!response.ok) {
        throw new Error('Failed to fetch video info');
    }
    return response.json();
}

/**
 * 播放器功能
 */
function initPlayer() {
    const video = document.getElementById('videoPlayer');
    const mobileFullscreenBtn = document.getElementById('mobileFullscreenBtn');
    const playerWrapper = document.getElementById('playerWrapper');
    
    if (!video) return;
    
    // 从本地存储恢复播放位置
    const savedPosition = localStorage.getItem(`video_pos_${video.dataset.videoId}`);
    if (savedPosition) {
        video.currentTime = parseFloat(savedPosition);
    }
    
    // 定期保存播放位置
    let saveTimeout;
    video.addEventListener('timeupdate', function() {
        clearTimeout(saveTimeout);
        saveTimeout = setTimeout(() => {
            localStorage.setItem(`video_pos_${video.dataset.videoId}`, video.currentTime);
        }, 1000);
    });
    
    // 视频结束清除保存的位置
    video.addEventListener('ended', function() {
        localStorage.removeItem(`video_pos_${video.dataset.videoId}`);
    });
    
    // 移动端全屏按钮点击事件
    if (mobileFullscreenBtn) {
        mobileFullscreenBtn.addEventListener('click', function(e) {
            e.stopPropagation();
            toggleMobileFullscreen(video, playerWrapper, this);
        });
    }
    
    // 键盘快捷键
    document.addEventListener('keydown', function(e) {
        // 如果正在输入文字则不处理
        if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
        
        switch(e.key.toLowerCase()) {
            case ' ':  // 空格 - 暂停/播放
                e.preventDefault();
                video.paused ? video.play() : video.pause();
                break;
            case 'arrowleft':  // 左箭头 - 后退5秒
                e.preventDefault();
                video.currentTime = Math.max(0, video.currentTime - 5);
                break;
            case 'arrowright':  // 右箭头 - 前进5秒
                e.preventDefault();
                video.currentTime = Math.min(video.duration, video.currentTime + 5);
                break;
            case 'arrowup':  // 上箭头 - 音量+
                e.preventDefault();
                video.volume = Math.min(1, video.volume + 0.1);
                break;
            case 'arrowdown':  // 下箭头 - 音量-
                e.preventDefault();
                video.volume = Math.max(0, video.volume - 0.1);
                break;
            case 'f':  // F - 全屏
                e.preventDefault();
                toggleFullscreen();
                break;
            case 'm':  // M - 静音
                e.preventDefault();
                video.muted = !video.muted;
                break;
            case 'escape':  // ESC - 退出全屏
                if (isFullscreen()) {
                    exitFullscreen();
                }
                break;
        }
    });
    
    // 双击全屏
    video.addEventListener('dblclick', function() {
        toggleFullscreen();
    });
    
    // 监听全屏状态变化
    document.addEventListener('fullscreenchange', handleFullscreenChange);
    document.addEventListener('webkitfullscreenchange', handleFullscreenChange);
    document.addEventListener('mozfullscreenchange', handleFullscreenChange);
    document.addEventListener('MSFullscreenChange', handleFullscreenChange);
}

/**
 * 移动端全屏切换
 */
function toggleMobileFullscreen(video, playerWrapper, button) {
    const isCurrentlyFullscreen = playerWrapper.classList.contains('fullscreen-active');
    
    if (isCurrentlyFullscreen) {
        // 退出全屏
        exitMobileFullscreen(playerWrapper, button);
    } else {
        // 进入全屏
        enterMobileFullscreen(video, playerWrapper, button);
    }
}

/**
 * 进入移动端全屏
 */
function enterMobileFullscreen(video, playerWrapper, button) {
    // iOS Safari: 使用 webkitEnterFullscreen
    if (video.webkitEnterFullscreen) {
        try {
            // iOS 原生全屏
            video.webkitEnterFullscreen();
            
            // 尝试锁定横屏（如果支持）
            lockLandscapeOrientation();
            
            // 更新按钮图标
            if (button) {
                button.innerHTML = '<i class="bi bi-fullscreen-exit"></i>';
            }
            
            return;
        } catch (err) {
            console.log('iOS webkitEnterFullscreen 失败:', err);
        }
    }
    
    // Android Chrome / 其他浏览器: 使用标准 requestFullscreen
    if (playerWrapper.requestFullscreen) {
        playerWrapper.requestFullscreen().then(() => {
            playerWrapper.classList.add('fullscreen-active');
            
            // 尝试锁定横屏
            lockLandscapeOrientation();
            
            // 更新按钮图标
            if (button) {
                button.innerHTML = '<i class="bi bi-fullscreen-exit"></i>';
            }
        }).catch(err => {
            console.log('标准 requestFullscreen 失败:', err);
            // 降级到 CSS 全屏
            fallbackFullscreen(playerWrapper, button);
        });
    } else if (playerWrapper.webkitRequestFullscreen) {
        // 旧版 Chrome/Safari
        playerWrapper.webkitRequestFullscreen();
        playerWrapper.classList.add('fullscreen-active');
        lockLandscapeOrientation();
        if (button) {
            button.innerHTML = '<i class="bi bi-fullscreen-exit"></i>';
        }
    } else if (playerWrapper.mozRequestFullScreen) {
        // Firefox
        playerWrapper.mozRequestFullScreen();
        playerWrapper.classList.add('fullscreen-active');
        lockLandscapeOrientation();
        if (button) {
            button.innerHTML = '<i class="bi bi-fullscreen-exit"></i>';
        }
    } else if (playerWrapper.msRequestFullscreen) {
        // IE/Edge
        playerWrapper.msRequestFullscreen();
        playerWrapper.classList.add('fullscreen-active');
        lockLandscapeOrientation();
        if (button) {
            button.innerHTML = '<i class="bi bi-fullscreen-exit"></i>';
        }
    } else {
        // 都不支持，使用 CSS 降级方案
        fallbackFullscreen(playerWrapper, button);
    }
}

/**
 * 退出移动端全屏
 */
function exitMobileFullscreen(playerWrapper, button) {
    // 先尝试退出原生全屏
    if (isFullscreen()) {
        exitFullscreen();
    }
    
    // 移除 CSS 全屏类
    playerWrapper.classList.remove('fullscreen-active');
    
    // 尝试解锁屏幕方向
    unlockOrientation();
    
    // 更新按钮图标
    if (button) {
        button.innerHTML = '<i class="bi bi-arrows-fullscreen"></i>';
    }
}

/**
 * CSS 降级全屏方案
 */
function fallbackFullscreen(playerWrapper, button) {
    playerWrapper.classList.add('fullscreen-active');
    
    // 尝试锁定横屏
    lockLandscapeOrientation();
    
    // 更新按钮图标
    if (button) {
        button.innerHTML = '<i class="bi bi-fullscreen-exit"></i>';
    }
    
    console.log('使用 CSS 降级全屏方案');
}

/**
 * 锁定横屏方向
 */
function lockLandscapeOrientation() {
    if (screen.orientation && screen.orientation.lock) {
        screen.orientation.lock('landscape').then(() => {
            console.log('屏幕已锁定为横屏');
        }).catch(err => {
            console.log('无法锁定屏幕方向:', err);
        });
    }
}

/**
 * 解锁屏幕方向
 */
function unlockOrientation() {
    if (screen.orientation && screen.orientation.unlock) {
        screen.orientation.unlock();
        console.log('屏幕方向已解锁');
    }
}

/**
 * 检查是否处于全屏状态
 */
function isFullscreen() {
    return !!(document.fullscreenElement || 
              document.webkitFullscreenElement || 
              document.mozFullScreenElement || 
              document.msFullscreenElement);
}

/**
 * 退出全屏
 */
function exitFullscreen() {
    if (document.exitFullscreen) {
        document.exitFullscreen();
    } else if (document.webkitExitFullscreen) {
        document.webkitExitFullscreen();
    } else if (document.mozCancelFullScreen) {
        document.mozCancelFullScreen();
    } else if (document.msExitFullscreen) {
        document.msExitFullscreen();
    }
}

/**
 * 处理全屏状态变化
 */
function handleFullscreenChange() {
    const playerWrapper = document.getElementById('playerWrapper');
    const mobileFullscreenBtn = document.getElementById('mobileFullscreenBtn');
    
    if (!isFullscreen()) {
        // 退出全屏时
        if (playerWrapper) {
            playerWrapper.classList.remove('fullscreen-active');
        }
        
        // 解锁屏幕方向
        unlockOrientation();
        
        // 更新按钮图标
        if (mobileFullscreenBtn) {
            mobileFullscreenBtn.innerHTML = '<i class="bi bi-arrows-fullscreen"></i>';
        }
    }
}

/**
 * 全屏切换（桌面端使用）
 */
function toggleFullscreen() {
    const playerWrapper = document.getElementById('playerWrapper');
    const video = document.getElementById('videoPlayer');
    const mobileFullscreenBtn = document.getElementById('mobileFullscreenBtn');
    
    if (!playerWrapper) return;
    
    if (isFullscreen()) {
        exitFullscreen();
    } else {
        // 优先使用标准 API
        if (playerWrapper.requestFullscreen) {
            playerWrapper.requestFullscreen().catch(err => {
                console.log('全屏请求失败:', err);
            });
        } else if (playerWrapper.webkitRequestFullscreen) {
            playerWrapper.webkitRequestFullscreen();
        } else if (playerWrapper.mozRequestFullScreen) {
            playerWrapper.mozRequestFullScreen();
        } else if (playerWrapper.msRequestFullscreen) {
            playerWrapper.msRequestFullscreen();
        }
    }
}

/**
 * 清理过期的缓存
 */
function cleanExpiredCache() {
    const now = Date.now();
    const expireTime = 60 * 60 * 1000; // 1小时
    
    for (let i = localStorage.length - 1; i >= 0; i--) {
        const key = localStorage.key(i);
        if (key && key.startsWith('video_duration_') && key.endsWith('_time')) {
            const time = parseInt(localStorage.getItem(key));
            if (now - time > expireTime) {
                // 删除过期的缓存
                const cacheKey = key.replace('_time', '');
                localStorage.removeItem(cacheKey);
                localStorage.removeItem(key);
            }
        }
    }
}

// 页面加载时清理过期缓存
cleanExpiredCache();

// ==================== 删除功能 ====================

/**
 * 初始化删除按钮
 */
function initDeleteButtons() {
    document.querySelectorAll('.video-delete-btn').forEach(btn => {
        btn.addEventListener('click', async function(e) {
            e.preventDefault();
            e.stopPropagation();
            
            const videoId = this.dataset.videoId;
            const videoName = this.dataset.videoName;
            
            if (!videoId) return;
            
            // 显示确认弹窗
            showDeleteConfirm(videoId, videoName, this);
        });
    });
}

/**
 * 显示删除确认弹窗
 */
function showDeleteConfirm(videoId, videoName, buttonEl) {
    // 检查是否已有弹窗
    let modal = document.getElementById('deleteModal');
    if (!modal) {
        modal = createDeleteModal();
        document.body.appendChild(modal);
    }
    
    const nameEl = modal.querySelector('.delete-modal-filename');
    const confirmBtn = modal.querySelector('.btn-confirm-delete');
    const cancelBtn = modal.querySelector('.btn-cancel-delete');
    
    if (nameEl) nameEl.textContent = videoName;
    
    // 确认删除
    const handleConfirm = async () => {
        confirmBtn.disabled = true;
        confirmBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>删除中...';
        
        try {
            const response = await fetch(`/api/video/${videoId}`, {
                method: 'DELETE'
            });
            
            const result = await response.json();
            
            if (response.ok && result.success) {
                // 移除视频卡片
                const wrapper = buttonEl.closest('.video-card-wrapper');
                if (wrapper) {
                    wrapper.style.transform = 'scale(0.8)';
                    wrapper.style.opacity = '0';
                    setTimeout(() => wrapper.remove(), 300);
                }
                closeModal(modal);
            } else {
                alert(result.detail || '删除失败');
                confirmBtn.disabled = false;
                confirmBtn.textContent = '确认删除';
            }
        } catch (err) {
            alert('删除失败: ' + err.message);
            confirmBtn.disabled = false;
            confirmBtn.textContent = '确认删除';
        }
    };
    
    // 取消
    const handleCancel = () => {
        closeModal(modal);
    };
    
    // 绑定事件（先移除旧的）
    confirmBtn.replaceWith(confirmBtn.cloneNode(true));
    cancelBtn.replaceWith(cancelBtn.cloneNode(true));
    
    modal.querySelector('.btn-confirm-delete').addEventListener('click', handleConfirm);
    modal.querySelector('.btn-cancel-delete').addEventListener('click', handleCancel);
    
    // 显示弹窗
    modal.classList.add('show');
}

/**
 * 创建删除确认弹窗
 */
function createDeleteModal() {
    const modal = document.createElement('div');
    modal.id = 'deleteModal';
    modal.className = 'delete-modal';
    modal.innerHTML = `
        <div class="delete-modal-content">
            <div class="delete-modal-icon">
                <i class="bi bi-trash3"></i>
            </div>
            <h3>确认删除视频</h3>
            <p>确定要删除以下视频吗？<br><span class="delete-modal-filename"></span></p>
            <p style="color: #ef4444; font-size: 0.8rem; margin-top: -0.5rem;">此操作无法撤销</p>
            <div class="delete-modal-actions">
                <button class="btn btn-secondary btn-cancel-delete">取消</button>
                <button class="btn btn-danger btn-confirm-delete">确认删除</button>
            </div>
        </div>
    `;
    
    // 点击背景关闭
    modal.addEventListener('click', (e) => {
        if (e.target === modal) {
            closeModal(modal);
        }
    });
    
    return modal;
}

/**
 * 关闭弹窗
 */
function closeModal(modal) {
    modal.classList.remove('show');
}

// ==================== 自动完整性检查 ====================

/**
 * 初始化自动完整性检查
 * 优化：减少批量大小，延迟执行
 */
function initAutoIntegrityCheck() {
    const videoWrappers = document.querySelectorAll('.video-card-wrapper');
    if (videoWrappers.length === 0) return;
    
    // 收集所有视频ID（限制最大数量，避免页面加载时过多请求）
    const videoIds = [];
    const MAX_INITIAL_CHECK = 50; // 初始最多检查50个视频
    
    videoWrappers.forEach((wrapper, index) => {
        const videoId = wrapper.dataset.videoId;
        if (videoId && index < MAX_INITIAL_CHECK) {
            videoIds.push(videoId);
        }
    });
    
    if (videoIds.length === 0) return;
    
    // 先批量检查下载状态
    batchCheckVideoStatus(videoIds).then(() => {
        // 然后批量检查完整性（仅对状态为normal的视频）
        batchCheckIntegrity(videoIds);
    });
}

/**
 * 批量检查视频状态（下载中/损坏/正常）
 * 优化：分批请求，避免一次性请求过多
 */
async function batchCheckVideoStatus(videoIds) {
    // 分批处理，每批 STATUS_BATCH_SIZE 个
    const batches = [];
    for (let i = 0; i < videoIds.length; i += STATUS_BATCH_SIZE) {
        batches.push(videoIds.slice(i, i + STATUS_BATCH_SIZE));
    }
    
    // 串行处理每批请求
    for (const batch of batches) {
        try {
            const response = await requestQueue.add(() => 
                fetch('/api/videos/status/batch', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ video_ids: batch })
                })
            );
            
            if (!response.ok) continue;
            
            const data = await response.json();
            
            if (data.results) {
                Object.entries(data.results).forEach(([videoId, result]) => {
                    updateVideoCardStatus(videoId, result);
                });
            }
        } catch (err) {
            console.log('状态检查失败:', err);
        }
        
        // 批次间短暂延迟，避免IO过载
        if (batches.indexOf(batch) < batches.length - 1) {
            await new Promise(r => setTimeout(r, BATCH_DELAY));
        }
    }
}

/**
 * 更新视频卡片状态显示
 */
function updateVideoCardStatus(videoId, result) {
    const wrapper = document.querySelector(`.video-card-wrapper[data-video-id="${videoId}"]`);
    if (!wrapper) return;
    
    // 移除所有状态类
    wrapper.classList.remove('corrupted', 'downloading');
    
    if (result.status === 'downloading') {
        // 标记为下载中
        wrapper.classList.add('downloading');
        wrapper.dataset.downloadReason = result.reason || '';
        
        // 添加下载中占位图
        const thumb = wrapper.querySelector('.video-thumb');
        if (thumb && !thumb.querySelector('.downloading-placeholder')) {
            const thumbnail = thumb.querySelector('.video-thumbnail');
            const preview = thumb.querySelector('.video-preview');
            const playOverlay = thumb.querySelector('.play-overlay');
            
            if (thumbnail) thumbnail.style.display = 'none';
            if (preview) preview.style.display = 'none';
            if (playOverlay) playOverlay.style.display = 'none';
            
            const placeholder = document.createElement('div');
            placeholder.className = 'downloading-placeholder';
            placeholder.innerHTML = `
                <div class="downloading-icon">
                    <i class="bi bi-download"></i>
                </div>
                <div class="downloading-text">
                    正在下载
                    <small>${result.reason || '文件正在写入'}</small>
                </div>
            `;
            thumb.appendChild(placeholder);
        }
    } else if (result.status === 'corrupted') {
        // 标记为损坏（但如果是下载中的临时文件，不显示为损坏）
        const reason = result.reason || '';
        if (!reason.includes('临时文件')) {
            wrapper.classList.add('corrupted');
            showCorruptedPlaceholder(wrapper, result.reason);
        }
    }
    // status === 'normal' 不需要特殊处理
}

/**
 * 显示损坏占位图
 */
function showCorruptedPlaceholder(wrapper, reason) {
    const thumb = wrapper.querySelector('.video-thumb');
    if (thumb && !thumb.querySelector('.corrupted-placeholder')) {
        const thumbnail = thumb.querySelector('.video-thumbnail');
        const preview = thumb.querySelector('.video-preview');
        const playOverlay = thumb.querySelector('.play-overlay');
        
        if (thumbnail) thumbnail.style.display = 'none';
        if (preview) preview.style.display = 'none';
        if (playOverlay) playOverlay.style.display = 'none';
        
        const placeholder = document.createElement('div');
        placeholder.className = 'corrupted-placeholder';
        placeholder.innerHTML = `
            <div class="corrupted-icon">
                <i class="bi bi-file-earmark-x"></i>
            </div>
            <div class="corrupted-text">
                视频已损坏
                <small>${reason || '无法播放'}</small>
            </div>
        `;
        thumb.appendChild(placeholder);
    }
}

/**
 * 批量检查视频完整性
 * 优化：分批请求，使用请求队列
 */
async function batchCheckIntegrity(videoIds) {
    // 分批处理
    const batches = [];
    for (let i = 0; i < videoIds.length; i += STATUS_BATCH_SIZE) {
        batches.push(videoIds.slice(i, i + STATUS_BATCH_SIZE));
    }
    
    for (const batch of batches) {
        try {
            const response = await requestQueue.add(() =>
                fetch('/api/videos/integrity/batch', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ video_ids: batch })
                })
            );
            
            if (!response.ok) continue;
            
            const data = await response.json();
            
            // 更新已缓存的结果
            if (data.results) {
                Object.entries(data.results).forEach(([videoId, result]) => {
                    updateVideoCard(videoId, result);
                });
            }
            
            // 如果有待检查的视频，启动轮询
            if (data.pending && data.pending.length > 0) {
                pendingVideos = [...pendingVideos, ...data.pending];
            }
        } catch (err) {
            console.log('完整性检查失败:', err);
        }
        
        // 批次间延迟
        if (batches.indexOf(batch) < batches.length - 1) {
            await new Promise(r => setTimeout(r, BATCH_DELAY));
        }
    }
    
    // 启动轮询（如果有待检查的视频）
    if (pendingVideos.length > 0) {
        startPolling();
    }
}

/**
 * 开始轮询检查结果
 */
function startPolling() {
    if (pollInterval) return;
    
    pollInterval = setInterval(async () => {
        if (pendingVideos.length === 0) {
            stopPolling();
            return;
        }
        
        try {
            // 只轮询前20个待检查的视频
            const toCheck = pendingVideos.slice(0, 20);
            const response = await fetch(`/api/videos/integrity/status?video_ids=${toCheck.join(',')}`);
            if (!response.ok) return;
            
            const data = await response.json();
            
            if (data.results) {
                Object.entries(data.results).forEach(([videoId, result]) => {
                    updateVideoCard(videoId, result);
                    // 从待检查列表移除
                    pendingVideos = pendingVideos.filter(id => id !== videoId);
                });
            }
        } catch (err) {
            console.log('轮询失败:', err);
        }
    }, 3000); // 每3秒轮询一次（从2秒增加）
}

/**
 * 停止轮询
 */
function stopPolling() {
    if (pollInterval) {
        clearInterval(pollInterval);
        pollInterval = null;
    }
}

/**
 * 更新视频卡片显示（用于完整性检查结果）
 */
function updateVideoCard(videoId, result) {
    const wrapper = document.querySelector(`.video-card-wrapper[data-video-id="${videoId}"]`);
    if (!wrapper) return;
    
    // 如果已经标记为下载中，不覆盖为损坏
    if (wrapper.classList.contains('downloading')) return;
    
    if (result.valid === false) {
        // 标记为损坏
        wrapper.classList.add('corrupted');
        showCorruptedPlaceholder(wrapper, result.error);
    }
}

/**
 * 检查所有视频按钮（可选功能）
 */
function initIntegrityCheck() {
    const checkAllBtn = document.getElementById('checkAllVideos');
    if (checkAllBtn) {
        checkAllBtn.addEventListener('click', async function() {
            const videoWrappers = document.querySelectorAll('.video-card-wrapper');
            const videoIds = [];
            videoWrappers.forEach(wrapper => {
                const videoId = wrapper.dataset.videoId;
                if (videoId) videoIds.push(videoId);
            });
            
            if (videoIds.length > 0) {
                this.disabled = true;
                this.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>检查中...';
                
                await batchCheckIntegrity(videoIds);
                
                setTimeout(() => {
                    this.disabled = false;
                    this.innerHTML = '<i class="bi bi-shield-check"></i> 检查视频完整性';
                }, 3000);
            }
        });
    }
}

// 页面卸载时停止轮询
window.addEventListener('beforeunload', stopPolling);
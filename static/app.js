/**
 * 视频服务器前端脚本
 */

document.addEventListener('DOMContentLoaded', function() {
    // 初始化所有功能
    initSearch();
    initVideoCards();
    initThumbnails();
    initVideoPreview();
    initVideoDurations();
    initPlayer();
});

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
            // 不阻止默认行为，让链接正常跳转
        });
    });
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
    const videoCards = document.querySelectorAll('.video-card');
    
    videoCards.forEach(card => {
        const previewContainer = card.querySelector('.video-preview');
        const video = previewContainer?.querySelector('video');
        const thumbnail = card.querySelector('.video-thumbnail');
        
        if (!previewContainer || !video) return;
        
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
 * 获取视频时长
 */
function initVideoDurations() {
    const durationElements = document.querySelectorAll('.video-duration');
    
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
        
        // 异步获取时长
        fetchVideoInfo(videoId).then(info => {
            if (info.duration_formatted) {
                updateDurationDisplay(el, info.duration_formatted);
                // 缓存1小时
                localStorage.setItem(cacheKey, info.duration_formatted);
                localStorage.setItem(`${cacheKey}_time`, Date.now().toString());
            }
        }).catch(() => {
            // 静默失败
        });
    });
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
                if (document.fullscreenElement) {
                    document.exitFullscreen();
                }
                break;
        }
    });
    
    // 双击全屏
    video.addEventListener('dblclick', toggleFullscreen);
    
    // 显示播放进度提示
    let progressTimeout;
    function showProgress() {
        const percent = (video.currentTime / video.duration * 100).toFixed(1);
        console.log(`播放进度: ${percent}%`);
    }
    
    video.addEventListener('seeked', showProgress);
}

/**
 * 全屏切换
 */
function toggleFullscreen() {
    const container = document.querySelector('.video-wrapper');
    if (!container) return;
    
    if (document.fullscreenElement) {
        document.exitFullscreen();
    } else {
        container.requestFullscreen().catch(err => {
            console.log('全屏请求失败:', err);
        });
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
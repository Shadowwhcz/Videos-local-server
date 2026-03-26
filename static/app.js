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

document.addEventListener('DOMContentLoaded', function() {
    // 初始化所有功能
    initConfig().then(() => {
        initSearch();
        initVideoCards();
        initThumbnails();
        initVideoPreview();
        initVideoDurations();
        initPlayer();
        initDeleteButtons();
        initAutoIntegrityCheck();
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
            // 如果是损坏视频，阻止跳转
            const wrapper = this.closest('.video-card-wrapper');
            if (wrapper && wrapper.classList.contains('corrupted')) {
                e.preventDefault();
                showCorruptedAlert(wrapper.dataset.videoName);
                return false;
            }
        });
    });
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
 */
function initAutoIntegrityCheck() {
    const videoWrappers = document.querySelectorAll('.video-card-wrapper');
    if (videoWrappers.length === 0) return;
    
    // 收集所有视频ID
    const videoIds = [];
    videoWrappers.forEach(wrapper => {
        const videoId = wrapper.dataset.videoId;
        if (videoId) videoIds.push(videoId);
    });
    
    if (videoIds.length === 0) return;
    
    // 批量检查完整性
    batchCheckIntegrity(videoIds);
}

/**
 * 批量检查视频完整性
 */
async function batchCheckIntegrity(videoIds) {
    try {
        const response = await fetch('/api/videos/integrity/batch', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ video_ids: videoIds })
        });
        
        if (!response.ok) return;
        
        const data = await response.json();
        
        // 更新已缓存的结果
        if (data.results) {
            Object.entries(data.results).forEach(([videoId, result]) => {
                updateVideoCard(videoId, result);
            });
        }
        
        // 如果有待检查的视频，启动轮询
        if (data.pending && data.pending.length > 0) {
            pendingVideos = data.pending;
            startPolling();
        }
        
    } catch (err) {
        console.log('完整性检查失败:', err);
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
            const response = await fetch(`/api/videos/integrity/status?video_ids=${pendingVideos.join(',')}`);
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
    }, 2000); // 每2秒轮询一次
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
 * 更新视频卡片显示
 */
function updateVideoCard(videoId, result) {
    const wrapper = document.querySelector(`.video-card-wrapper[data-video-id="${videoId}"]`);
    if (!wrapper) return;
    
    if (result.valid === false) {
        // 标记为损坏
        wrapper.classList.add('corrupted');
        
        // 替换缩略图为损坏占位图
        const thumb = wrapper.querySelector('.video-thumb');
        if (thumb && !thumb.querySelector('.corrupted-placeholder')) {
            // 移除原有内容
            const thumbnail = thumb.querySelector('.video-thumbnail');
            const preview = thumb.querySelector('.video-preview');
            const playOverlay = thumb.querySelector('.play-overlay');
            
            if (thumbnail) thumbnail.style.display = 'none';
            if (preview) preview.style.display = 'none';
            if (playOverlay) playOverlay.style.display = 'none';
            
            // 添加损坏占位图
            const placeholder = document.createElement('div');
            placeholder.className = 'corrupted-placeholder';
            placeholder.innerHTML = `
                <div class="corrupted-icon">
                    <i class="bi bi-file-earmark-x"></i>
                </div>
                <div class="corrupted-text">
                    视频已损坏
                    <small>${result.error || '无法播放'}</small>
                </div>
            `;
            thumb.appendChild(placeholder);
        }
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
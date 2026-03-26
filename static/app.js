// 视频服务器前端脚本

document.addEventListener('DOMContentLoaded', function() {
    // 搜索框交互
    const searchInput = document.querySelector('.search-box input');
    if (searchInput) {
        searchInput.addEventListener('keypress', function(e) {
            if (e.key === 'Enter') {
                this.form.submit();
            }
        });
    }
    
    // 视频卡片点击
    document.querySelectorAll('.video-card').forEach(card => {
        card.addEventListener('click', function(e) {
            e.preventDefault();
            const href = this.getAttribute('href');
            if (href) {
                window.location.href = href;
            }
        });
    });
});

// 播放器功能
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

// 全屏切换
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

// 页面加载完成后初始化播放器
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initPlayer);
} else {
    initPlayer();
}
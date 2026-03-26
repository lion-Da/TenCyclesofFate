// --- Constants ---
const API_BASE_URL = "/api";

// --- State Management ---
const appState = {
    gameState: null,
    lastRollEventId: null,  // 用于检测骰子事件变化
    legacyData: null,       // 继承系统数据
};

// --- Processing / Error Tracking ---
const processingState = {
    safetyTimer: null,          // 安全超时计时器（防止永远卡在 is_processing）
    processingStartTime: 0,     // 开始处理的时间戳
    consecutiveErrors: 0,       // 连续错误次数
    SAFETY_TIMEOUT_MS: 180000,  // 3分钟安全超时
    PLACEHOLDER_PROCESSING: '世界演化中...',
    PLACEHOLDER_DEFAULT: '汝欲何为...',
    PLACEHOLDER_ERROR: '天机已恢复，请重新输入...',
    // Queue status (persisted across re-renders)
    lastQueueStatus: null,      // { position, total, etaText }
};

// --- Streaming State ---
const streamState = {
    activeStreamId: null,       // 当前正在接收的流ID
    streamBuffer: "",           // 已确认显示的文本
    streamElement: null,        // 当前流式输出的DOM元素
    streamRenderTimer: null,    // 流式渲染定时器
    // --- Typewriter queue ---
    typewriterQueue: "",        // 尚未显示的字符队列
    typewriterTimer: null,      // 打字机定时器
};

// --- Stream Speed Settings ---
// Speed levels: 0=instant, 1=fast, 2=medium, 3=slow, 4=very slow
const SPEED_LABELS = ['瞬', '快', '适中', '慢', '极慢'];
// [chars per tick, ms per tick]
const SPEED_CONFIGS = [
    [9999, 0],    // 0: instant — dump everything at once
    [6, 20],      // 1: fast — 6 chars every 20ms ≈ 300 char/s
    [3, 30],      // 2: medium — 3 chars every 30ms ≈ 100 char/s
    [2, 50],      // 3: slow — 2 chars every 50ms ≈ 40 char/s
    [1, 80],      // 4: very slow — 1 char every 80ms ≈ 12 char/s
];

function getStreamSpeed() {
    const slider = document.getElementById('stream-speed');
    if (slider) return parseInt(slider.value, 10);
    const saved = localStorage.getItem('stream_speed');
    return saved !== null ? parseInt(saved, 10) : 2;
}

function getSpeedConfig() {
    return SPEED_CONFIGS[getStreamSpeed()] || SPEED_CONFIGS[2];
}

// --- Smooth Scroll State ---
const scrollState = {
    animationId: null,
    isUserScrolling: false,
    lastScrollTop: 0,
    scrollTimeout: null,
    isFirstRender: true,  // 标记是否为首次渲染（重连后）
};

// --- DOM Elements ---
const DOMElements = {
    loginView: document.getElementById('login-view'),
    gameView: document.getElementById('game-view'),
    loginError: document.getElementById('login-error'),
    logoutButton: document.getElementById('logout-button'),
    fullscreenButton: document.getElementById('fullscreen-button'),
    narrativeWindow: document.getElementById('narrative-window'),
    characterStatus: document.getElementById('character-status'),
    cultivationPanel: document.getElementById('cultivation-panel'),
    cultivationTechniques: document.getElementById('cultivation-techniques'),
    cultivationPower: document.getElementById('cultivation-power'),
    opportunitiesSpan: document.getElementById('opportunities'),
    actionInput: document.getElementById('action-input'),
    actionButton: document.getElementById('action-button'),
    startTrialButton: document.getElementById('start-trial-button'),
    loadingSpinner: document.getElementById('loading-spinner'),
    rollOverlay: document.getElementById('roll-overlay'),
    rollPanel: document.getElementById('roll-panel'),
    rollType: document.getElementById('roll-type'),
    rollBreakdown: document.getElementById('roll-breakdown'),
    breakdownItems: document.getElementById('breakdown-items'),
    breakdownFinalRate: document.getElementById('breakdown-final-rate'),
    rollDiceArea: document.getElementById('roll-dice-area'),
    rollDiceNumber: document.getElementById('roll-dice-number'),
    rollResultDisplay: document.getElementById('roll-result-display'),
    rollOutcome: document.getElementById('roll-outcome'),
    rollValue: document.getElementById('roll-value'),
    rollTarget: document.getElementById('roll-target'),
    rollSides: document.getElementById('roll-sides'),
    rollSummary: document.getElementById('roll-summary'),
    legacyPanel: document.getElementById('legacy-panel'),
    legacyPointsSpan: document.getElementById('legacy-points'),
    legacyToggle: document.getElementById('legacy-toggle'),
    blessingsList: document.getElementById('blessings-list'),
    socialPanel: document.getElementById('social-panel'),
    socialRelations: document.getElementById('social-relations'),
    legacyCloseBtn: document.getElementById('legacy-close-btn'),
    endGameButton: document.getElementById('end-game-button'),
};

// --- API Client ---
const api = {
    async initGame() {
        const response = await fetch(`${API_BASE_URL}/game/init`, {
            method: 'POST',
        });
        if (response.status === 401) {
            throw new Error('Unauthorized');
        }
        if (!response.ok) throw new Error('Failed to initialize game session');
        return response.json();
    },
    async logout() {
        await fetch(`${API_BASE_URL}/logout`, { method: 'POST' });
        window.location.href = '/';
    },
    async getLegacy() {
        const response = await fetch(`${API_BASE_URL}/legacy`);
        if (!response.ok) return null;
        return response.json();
    },
    async purchaseBlessing(blessingId) {
        const response = await fetch(`${API_BASE_URL}/legacy/purchase`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ blessing_id: blessingId }),
        });
        if (!response.ok) return { success: false, message: '请求失败' };
        return response.json();
    },
};

// --- WebSocket Manager ---
const socketManager = {
    socket: null,
    connect() {
        return new Promise((resolve, reject) => {
            if (this.socket && this.socket.readyState === WebSocket.OPEN) {
                resolve();
                return;
            }
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            const host = window.location.host;
            const wsUrl = `${protocol}//${host}${API_BASE_URL}/ws`;
            this.socket = new WebSocket(wsUrl);
            this.socket.binaryType = 'arraybuffer';

            this.socket.onopen = () => { console.log("WebSocket established."); resolve(); };
            this.socket.onmessage = (event) => {
                let message;
                if (event.data instanceof ArrayBuffer) {
                    try {
                        const decompressed = pako.ungzip(new Uint8Array(event.data), { to: 'string' });
                        message = JSON.parse(decompressed);
                    } catch (err) {
                        console.error('Failed to decompress or parse message:', err);
                        return;
                    }
                } else {
                    message = JSON.parse(event.data);
                }
                
                switch (message.type) {
                    case 'full_state':
                        appState.gameState = message.data;
                        render();
                        break;
                    case 'patch':
                        if (appState.gameState && message.patch) {
                            try {
                                const result = jsonpatch.applyPatch(appState.gameState, message.patch, true, false);
                                appState.gameState = result.newDocument;
                                render();
                            } catch (err) {
                                console.error('Failed to apply patch:', err);
                            }
                        }
                        break;
                    case 'roll_event':
                        // Dedicated immediate roll event (bypasses debounce)
                        if (message.data) {
                            renderRollEvent(message.data);
                        }
                        break;
                    case 'stream_chunk':
                        handleStreamChunk(message);
                        break;
                    case 'stream_end':
                        handleStreamEnd(message);
                        break;
                    case 'queue_status':
                        handleQueueStatus(message);
                        break;
                    case 'error':
                        showToast(`⚠ ${message.detail || '连接异常'}`, 'error', 6000);
                        // 错误发生后确保输入不被锁定
                        if (appState.gameState && appState.gameState.is_processing) {
                            appState.gameState.is_processing = false;
                            showLoading(false);
                        }
                        break;
                }
            };
            this.socket.onclose = () => {
                console.log("Reconnecting...");
                _clearSafetyTimeout();
                showLoading(true);
                setTimeout(() => this.connect(), 5000);
            };
            this.socket.onerror = (error) => { console.error("WebSocket error:", error); DOMElements.loginError.textContent = '无法连接。'; reject(error); };
        });
    },
    sendAction(action) {
        if (this.socket && this.socket.readyState === WebSocket.OPEN) {
            this.socket.send(JSON.stringify({ action }));
        } else {
            alert("连接已断开，请刷新。");
        }
    }
};

// --- Streaming Handlers ---
function handleStreamChunk(message) {
    const { stream_id, content } = message;
    
    // 如果是新的流，创建新的DOM元素并启动打字机
    if (streamState.activeStreamId !== stream_id) {
        // 清理上一个流的残留
        _stopTypewriter();
        
        // 移除"天道演化中"提示
        const hint = DOMElements.narrativeWindow.querySelector('.narrative-processing-hint');
        if (hint) hint.remove();
        
        streamState.activeStreamId = stream_id;
        streamState.streamBuffer = "";
        streamState.typewriterQueue = "";
        
        // 创建流式输出容器
        const streamDiv = document.createElement('div');
        streamDiv.classList.add('stream-narrative');
        streamDiv.id = `stream-${stream_id}`;
        DOMElements.narrativeWindow.appendChild(streamDiv);
        streamState.streamElement = streamDiv;
        
        _startTypewriter();
    }
    
    // 追加到待显示队列
    streamState.typewriterQueue += content;
}

function _startTypewriter() {
    if (streamState.typewriterTimer) return;
    
    const tick = () => {
        if (!streamState.typewriterQueue && !streamState.activeStreamId) {
            // 队列空且流已结束，停止
            _stopTypewriter();
            return;
        }
        
        if (streamState.typewriterQueue) {
            const [charsPerTick] = getSpeedConfig();
            const take = Math.min(charsPerTick, streamState.typewriterQueue.length);
            const chars = streamState.typewriterQueue.slice(0, take);
            streamState.typewriterQueue = streamState.typewriterQueue.slice(take);
            streamState.streamBuffer += chars;
            
            _renderStreamBuffer();
        }
        
        // 重新调度（速度可能已变化）
        const [, msPerTick] = getSpeedConfig();
        if (msPerTick === 0) {
            // Instant mode: drain everything
            streamState.streamBuffer += streamState.typewriterQueue;
            streamState.typewriterQueue = "";
            _renderStreamBuffer();
            // 继续轮询以等待后续chunks
            streamState.typewriterTimer = setTimeout(tick, 50);
        } else {
            streamState.typewriterTimer = setTimeout(tick, msPerTick);
        }
    };
    
    tick();
}

function _stopTypewriter() {
    if (streamState.typewriterTimer) {
        clearTimeout(streamState.typewriterTimer);
        streamState.typewriterTimer = null;
    }
}

function _renderStreamBuffer() {
    if (!streamState.streamElement) return;
    
    // 使用节流渲染，避免频繁DOM更新
    if (!streamState.streamRenderTimer) {
        streamState.streamRenderTimer = requestAnimationFrame(() => {
            if (streamState.streamElement) {
                streamState.streamElement.innerHTML = renderMarkdownSafe(streamState.streamBuffer);
                // 自动滚动
                if (!scrollState.isUserScrolling) {
                    DOMElements.narrativeWindow.scrollTop = DOMElements.narrativeWindow.scrollHeight;
                }
            }
            streamState.streamRenderTimer = null;
        });
    }
}

function handleStreamEnd(message) {
    const { stream_id } = message;
    
    if (streamState.activeStreamId === stream_id) {
        // 将剩余队列全部flush到buffer
        if (streamState.typewriterQueue) {
            streamState.streamBuffer += streamState.typewriterQueue;
            streamState.typewriterQueue = "";
        }
        
        // 最终渲染
        if (streamState.streamElement) {
            streamState.streamElement.innerHTML = renderMarkdownSafe(streamState.streamBuffer);
        }
        
        // 清除流式状态
        _stopTypewriter();
        streamState.activeStreamId = null;
        streamState.streamBuffer = "";
        streamState.streamElement = null;
        if (streamState.streamRenderTimer) {
            cancelAnimationFrame(streamState.streamRenderTimer);
            streamState.streamRenderTimer = null;
        }
    }
}

// --- Queue Status Handler ---
function handleQueueStatus(message) {
    const { position, total, eta_seconds } = message;
    
    if (position <= 0) {
        // We've been granted our slot — hide queue indicator
        processingState.lastQueueStatus = null;
        _hideQueueIndicator();
        return;
    }
    
    // Update the processing placeholder with queue info
    const etaText = eta_seconds <= 5
        ? '即将开始'
        : eta_seconds <= 60
            ? `约${Math.ceil(eta_seconds)}秒`
            : `约${Math.ceil(eta_seconds / 60)}分钟`;
    
    const queueText = total > 1
        ? `排队中（第${position}/${total}位，${etaText}）...`
        : `等待天道响应中（${etaText}）...`;
    
    DOMElements.actionInput.placeholder = queueText;
    
    // Save for re-render persistence
    processingState.lastQueueStatus = { position, total, etaText };
    
    // Also show/update a queue indicator banner in the narrative window
    _showQueueIndicator(position, total, etaText);
}

function _showQueueIndicator(position, total, etaText) {
    let indicator = document.getElementById('queue-indicator');
    if (!indicator) {
        indicator = document.createElement('div');
        indicator.id = 'queue-indicator';
        indicator.className = 'queue-indicator';
        // Insert before the narrative window's last child or append
        DOMElements.narrativeWindow.appendChild(indicator);
    }
    
    if (total > 1) {
        indicator.innerHTML = `
            <div class="queue-indicator-inner">
                <span class="queue-spinner">⏳</span>
                <span>天道繁忙 · 排队等待中</span>
                <span class="queue-position">第 <strong>${position}</strong> / ${total} 位</span>
                <span class="queue-eta">预计${etaText}</span>
            </div>
        `;
    } else {
        indicator.innerHTML = `
            <div class="queue-indicator-inner">
                <span class="queue-spinner">⏳</span>
                <span>世界演化中 · ${etaText}</span>
            </div>
        `;
    }
    
    // Auto-scroll to show the indicator
    if (!scrollState.isUserScrolling) {
        DOMElements.narrativeWindow.scrollTop = DOMElements.narrativeWindow.scrollHeight;
    }
}

function _hideQueueIndicator() {
    processingState.lastQueueStatus = null;
    const indicator = document.getElementById('queue-indicator');
    if (indicator) {
        indicator.remove();
    }
}

// --- UI & Rendering ---
function showView(viewId) {
    document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
    document.getElementById(viewId).classList.add('active');
}

function renderMarkdownSafe(markdownText) {
    const rawHtml = marked.parse(markdownText || "", { mangle: false, headerIds: false });
    return DOMPurify.sanitize(rawHtml, {
        USE_PROFILES: { html: true },
        FORBID_TAGS: ["script", "style", "iframe", "object", "embed", "link", "meta"],
        FORBID_ATTR: ["onerror", "onload", "onclick", "onmouseover", "onfocus", "onmouseenter", "onmouseleave", "style"],
    });
}

/**
 * 从消息文本中提取 <!--error-details:BASE64--> 标记，
 * 返回 { cleanText, detailsHtml }。
 * - cleanText: 去除标记后的文本，用于正常 markdown 渲染
 * - detailsHtml: 若存在错误详情，返回可折叠面板的 HTML 字符串；否则为空
 */
function extractErrorDetails(text) {
    const pattern = /<!--error-details:([A-Za-z0-9+/=]+)-->/;
    const match = text.match(pattern);
    if (!match) {
        return { cleanText: text, detailsHtml: '' };
    }

    const cleanText = text.replace(pattern, '').trim();
    let detailsHtml = '';

    try {
        const decoded = atob(match[1]);
        // atob 返回 latin1，需要用 TextDecoder 处理 UTF-8
        const bytes = Uint8Array.from(decoded, c => c.charCodeAt(0));
        const jsonStr = new TextDecoder('utf-8').decode(bytes);
        const info = JSON.parse(jsonStr);

        const rawText = (info.raw || '').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        const errorMsg = (info.error || '未知错误').replace(/</g, '&lt;').replace(/>/g, '&gt;');

        detailsHtml = `
            <details class="error-details-panel">
                <summary class="error-details-summary">📜 查看天机原文（调试详情）</summary>
                <div class="error-details-content">
                    <div class="error-details-section">
                        <strong>⚠ 错误原因：</strong>
                        <pre class="error-details-reason">${errorMsg}</pre>
                    </div>
                    <div class="error-details-section">
                        <strong>📖 大模型原始返回：</strong>
                        <pre class="error-details-raw">${rawText}</pre>
                    </div>
                </div>
            </details>`;
    } catch (e) {
        console.warn('Failed to decode error details:', e);
    }

    return { cleanText, detailsHtml };
}

// --- Smooth Scroll Functions ---
function stopSmoothScroll() {
    if (scrollState.animationId) {
        cancelAnimationFrame(scrollState.animationId);
        scrollState.animationId = null;
    }
}

function smoothScrollToBottom(element, pixelsPerSecond = 150) {
    stopSmoothScroll();
    
    if (scrollState.isUserScrolling) {
        return;
    }
    
    const startScrollTop = element.scrollTop;
    const minScrollDistance = 50;
    
    function tryStartScroll(retryCount = 0) {
        const targetScrollTop = element.scrollHeight - element.clientHeight;
        const distance = targetScrollTop - startScrollTop;
        
        if (distance < minScrollDistance && retryCount < 10) {
            setTimeout(() => tryStartScroll(retryCount + 1), 100);
            return;
        }
        
        if (distance <= 0) {
            return;
        }
        
        if (scrollState.isUserScrolling) {
            return;
        }
        
        const startTime = performance.now();
        const duration = (distance / pixelsPerSecond) * 1000;
        
        function animateScroll(currentTime) {
            if (scrollState.isUserScrolling) {
                scrollState.animationId = null;
                return;
            }
            
            const elapsed = currentTime - startTime;
            const progress = Math.min(elapsed / duration, 1);
            const easeProgress = 1 - (1 - progress) * (1 - progress);
            
            element.scrollTop = startScrollTop + (distance * easeProgress);
            
            if (progress < 1) {
                scrollState.animationId = requestAnimationFrame(animateScroll);
            } else {
                scrollState.animationId = null;
            }
        }
        
        scrollState.animationId = requestAnimationFrame(animateScroll);
    }
    
    tryStartScroll();
}

function setupScrollInterruptListener(element) {
    element.addEventListener('wheel', () => {
        scrollState.isUserScrolling = true;
        stopSmoothScroll();
        
        if (scrollState.scrollTimeout) {
            clearTimeout(scrollState.scrollTimeout);
        }
        
        scrollState.scrollTimeout = setTimeout(() => {
            scrollState.isUserScrolling = false;
        }, 2000);
    }, { passive: true });
    
    element.addEventListener('touchstart', () => {
        scrollState.isUserScrolling = true;
        stopSmoothScroll();
    }, { passive: true });
    
    element.addEventListener('touchend', () => {
        if (scrollState.scrollTimeout) {
            clearTimeout(scrollState.scrollTimeout);
        }
        scrollState.scrollTimeout = setTimeout(() => {
            scrollState.isUserScrolling = false;
        }, 2000);
    }, { passive: true });
}

function showLoading(isLoading) {
    const showFullscreenSpinner = isLoading && !appState.gameState;
    DOMElements.loadingSpinner.style.display = showFullscreenSpinner ? 'flex' : 'none';
    
    const isProcessing = appState.gameState ? appState.gameState.is_processing : false;
    const buttonsDisabled = isLoading || isProcessing;
    DOMElements.actionInput.disabled = buttonsDisabled;
    DOMElements.actionButton.disabled = buttonsDisabled;
    DOMElements.startTrialButton.disabled = buttonsDisabled;
    
    if (buttonsDisabled && appState.gameState) {
        DOMElements.actionButton.textContent = '⏳';
        // 设置处理中占位文本和样式
        DOMElements.actionInput.placeholder = processingState.PLACEHOLDER_PROCESSING;
        DOMElements.actionInput.classList.add('processing');
        // 启动安全超时（防止永远卡住）
        _startSafetyTimeout();
    } else {
        DOMElements.actionButton.textContent = '定';
        DOMElements.actionInput.classList.remove('processing');
        // 取消安全超时
        _clearSafetyTimeout();
        // 根据是否刚经历过错误来设置占位文本
        if (processingState.consecutiveErrors > 0) {
            DOMElements.actionInput.placeholder = processingState.PLACEHOLDER_ERROR;
            // 2秒后恢复默认提示
            setTimeout(() => {
                DOMElements.actionInput.placeholder = processingState.PLACEHOLDER_DEFAULT;
            }, 3000);
        } else {
            DOMElements.actionInput.placeholder = processingState.PLACEHOLDER_DEFAULT;
        }
    }
}

/**
 * 立即在叙事窗口底部注入"天道演化中"提示。
 * 在 handleAction 发送后立刻调用，无需等后端推送 is_processing。
 * 若已有提示则不重复添加；后续 render() 或流式开始时会自然替换。
 */
function _injectProcessingHint() {
    if (!DOMElements.narrativeWindow) return;
    // 已经存在则跳过
    if (DOMElements.narrativeWindow.querySelector('.narrative-processing-hint')) return;
    
    const hint = document.createElement('div');
    hint.className = 'narrative-processing-hint';
    hint.innerHTML = '<span class="processing-dot-1">·</span><span class="processing-dot-2">·</span><span class="processing-dot-3">·</span> 天道演化中 <span class="processing-dot-1">·</span><span class="processing-dot-2">·</span><span class="processing-dot-3">·</span>';
    DOMElements.narrativeWindow.appendChild(hint);
    
    // 滚动到底部让提示可见
    DOMElements.narrativeWindow.scrollTop = DOMElements.narrativeWindow.scrollHeight;
}

// --- Safety Timeout ---
// 防止后端异常导致 is_processing 永远为 true，玩家被永久锁定
function _startSafetyTimeout() {
    if (processingState.safetyTimer) return; // 已有计时器
    processingState.processingStartTime = Date.now();
    processingState.safetyTimer = setTimeout(() => {
        // 超时强制解锁输入
        console.warn('Safety timeout: force-unlocking input after', processingState.SAFETY_TIMEOUT_MS, 'ms');
        if (appState.gameState) {
            appState.gameState.is_processing = false;
        }
        showLoading(false);
        showToast('天道运转似有阻滞，输入已恢复。若无响应请刷新页面。', 'warning', 8000);
    }, processingState.SAFETY_TIMEOUT_MS);
}

function _clearSafetyTimeout() {
    if (processingState.safetyTimer) {
        clearTimeout(processingState.safetyTimer);
        processingState.safetyTimer = null;
    }
    processingState.processingStartTime = 0;
}

// --- Toast Notification System ---
function showToast(message, type = 'info', duration = 5000) {
    const container = document.getElementById('toast-container');
    if (!container) return;
    
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.textContent = message;
    container.appendChild(toast);
    
    // 自动消失
    const dismissTimer = setTimeout(() => {
        toast.classList.add('toast-out');
        toast.addEventListener('animationend', () => toast.remove());
    }, duration);
    
    // 点击提前关闭
    toast.addEventListener('click', () => {
        clearTimeout(dismissTimer);
        toast.classList.add('toast-out');
        toast.addEventListener('animationend', () => toast.remove());
    });
}

// --- AI Error Detection ---
// 检测 display_history 中的连续错误并通知玩家
let _lastCheckedHistoryLen = 0;

function _detectAiErrors() {
    const history = appState.gameState?.display_history || [];
    const currentLen = history.length;
    
    // 仅在有新消息时检查
    if (currentLen <= _lastCheckedHistoryLen) return;
    
    const latestMsg = history[currentLen - 1] || '';
    _lastCheckedHistoryLen = currentLen;
    
    if (latestMsg.includes('天机紊乱')) {
        processingState.consecutiveErrors++;
        
        if (processingState.consecutiveErrors === 1) {
            showToast('⚠ 天道运转出现波动，请稍后再试', 'warning', 5000);
        } else if (processingState.consecutiveErrors === 2) {
            showToast('⚠ 天机再次紊乱，大模型服务可能暂时不稳定', 'error', 6000);
        } else if (processingState.consecutiveErrors >= 3) {
            showToast('❌ 大模型服务持续异常（连续 ' + processingState.consecutiveErrors + ' 次），建议稍后再试或刷新页面', 'error', 10000);
        }
    } else if (latestMsg && !latestMsg.startsWith('> ')) {
        // 收到正常AI回复，重置错误计数
        processingState.consecutiveErrors = 0;
    }
}

function render() {
    if (!appState.gameState) { showLoading(true); return; }
    
    // --- 错误检测：检查最新消息是否为"天机紊乱" ---
    _detectAiErrors();
    
    showLoading(appState.gameState.is_processing);
    
    // 当处理结束时，清除排队指示器
    if (!appState.gameState.is_processing) {
        _hideQueueIndicator();
    }
    
    DOMElements.opportunitiesSpan.textContent = appState.gameState.opportunities_remaining;
    renderCharacterStatus();

    const historyContainer = document.createDocumentFragment();
    (appState.gameState.display_history || []).forEach(text => {
        const p = document.createElement('div');
        // 提取错误详情标记（如有），分离干净文本和折叠面板
        const { cleanText, detailsHtml } = extractErrorDetails(text);
        p.innerHTML = renderMarkdownSafe(cleanText) + detailsHtml;
        if (text.startsWith('> ')) p.classList.add('user-input-message');
        else if (text.startsWith('【')) p.classList.add('system-message');
        historyContainer.appendChild(p);
    });
    
    // 保留流式元素（如果正在流式输出）
    const activeStreamEl = streamState.streamElement;
    DOMElements.narrativeWindow.innerHTML = '';
    DOMElements.narrativeWindow.appendChild(historyContainer);
    if (activeStreamEl && streamState.activeStreamId) {
        DOMElements.narrativeWindow.appendChild(activeStreamEl);
    }
    
    // --- 处理中提示：当正在等待AI响应且无活跃流式输出时，显示"天道演化中" ---
    if (appState.gameState.is_processing && !streamState.activeStreamId) {
        const processingHint = document.createElement('div');
        processingHint.className = 'narrative-processing-hint';
        processingHint.innerHTML = '<span class="processing-dot-1">·</span><span class="processing-dot-2">·</span><span class="processing-dot-3">·</span> 天道演化中 <span class="processing-dot-1">·</span><span class="processing-dot-2">·</span><span class="processing-dot-3">·</span>';
        DOMElements.narrativeWindow.appendChild(processingHint);
    }
    
    // 恢复排队指示器（如果仍在排队中）
    if (processingState.lastQueueStatus && appState.gameState.is_processing) {
        const { position, total, etaText } = processingState.lastQueueStatus;
        _showQueueIndicator(position, total, etaText);
    }
    
    // 首次渲染直接跳到底部，之后使用平滑滚动
    if (scrollState.isFirstRender) {
        DOMElements.narrativeWindow.scrollTop = DOMElements.narrativeWindow.scrollHeight;
        scrollState.isFirstRender = false;
    } else {
        smoothScrollToBottom(DOMElements.narrativeWindow, 150);
    }
    
    const { is_in_trial, daily_success_achieved, opportunities_remaining } = appState.gameState;
    DOMElements.actionInput.parentElement.classList.toggle('hidden', !(is_in_trial || daily_success_achieved || opportunities_remaining < 0));
    const startButton = DOMElements.startTrialButton;
    startButton.classList.toggle('hidden', is_in_trial || daily_success_achieved || opportunities_remaining < 0);

    // 结束试炼按钮: 仅在试炼中且非处理中时可见
    if (DOMElements.endGameButton) {
        DOMElements.endGameButton.classList.toggle('hidden', !is_in_trial);
        DOMElements.endGameButton.disabled = appState.gameState.is_processing;
    }

    // 显示/隐藏继承系统按钮: 不在试炼中、有机缘且未完成时可见
    if (DOMElements.legacyToggle) {
        const showLegacy = !is_in_trial && !daily_success_achieved && opportunities_remaining > 0;
        DOMElements.legacyToggle.classList.toggle('hidden', !showLegacy);
    }

    if (daily_success_achieved) {
         startButton.textContent = "今日功德圆满";
         startButton.disabled = true;
    } else if (opportunities_remaining <= 0) {
        startButton.textContent = "机缘已尽";
        startButton.disabled = true;
    } else {
        if (opportunities_remaining === 10) {
            startButton.textContent = "开始第一次试炼";
        } else {
            startButton.textContent = "开启下一次试炼";
        }
        startButton.disabled = appState.gameState.is_processing;
    }
}

function renderValue(container, value, level = 0) {
    if (Array.isArray(value)) {
        value.forEach(item => renderValue(container, item, level + 1));
    } else if (typeof value === 'object' && value !== null) {
        const subContainer = document.createElement('div');
        subContainer.style.paddingLeft = `${level * 10}px`;
        Object.entries(value).forEach(([key, val]) => {
            const propDiv = document.createElement('div');
            propDiv.classList.add('property-item');
            
            const keySpan = document.createElement('span');
            keySpan.classList.add('property-key');
            keySpan.textContent = `${key}: `;
            propDiv.appendChild(keySpan);

            renderValue(propDiv, val, level + 1);
            subContainer.appendChild(propDiv);
        });
        container.appendChild(subContainer);
    } else {
        const valueSpan = document.createElement('span');
        valueSpan.classList.add('property-value');
        valueSpan.textContent = value;
        container.appendChild(valueSpan);
    }
}

function renderCharacterStatus() {
    const { current_life } = appState.gameState;
    const container = DOMElements.characterStatus;
    container.innerHTML = '';

    if (!current_life) {
        container.textContent = '静待天命...';
        renderCultivationPanel(null);
        renderSocialRelations(null);
        return;
    }

    // --- 定义渲染顺序和特殊处理 ---
    const SKIP_KEYS = ['人物关系', '功法'];
    // 判断是否应该跳过：精确匹配或前缀匹配（如 "人物关系.柳如烟"）
    const shouldSkip = (k) => SKIP_KEYS.some(sk => k === sk || k.startsWith(sk + '.'));
    // 临时事件字段前缀：以 "~" 开头的字段视为临时事件，其余全部为持久字段
    const TEMP_FIELD_PREFIX = '~';
    const PRIORITY_KEYS = ['人物背景', '生命值', '灵石', '属性', '物品', '状态效果', '位置', '故事事件'];

    // --- 英文key→中文映射兜底（AI偶尔返回英文key时自动修正显示） ---
    const EN_TO_CN_KEY_MAP = {
        'story_events': '故事事件', 'current_cultivation': '当前修炼',
        'cultivation': '功法', 'hp': '生命值', 'max_hp': '最大生命值',
        'items': '物品', 'inventory': '物品', 'location': '位置',
        'position': '位置', 'status': '状态效果', 'status_effects': '状态效果',
        'attributes': '属性', 'stats': '属性', 'spirit_stones': '灵石',
        'background': '人物背景', 'relationships': '人物关系',
        'combat_power': '战斗力', 'realm': '境界', 'cultivation_progress': '修炼进度',
        'sect': '门派', 'reputation': '声望', 'faction': '势力',
        'name': '姓名', 'gender': '性别', 'appearance': '外貌',
    };

    // 收集所有需要渲染的 key，优先级排列
    const allKeys = Object.keys(current_life);
    const orderedKeys = [];
    for (const pk of PRIORITY_KEYS) {
        if (allKeys.includes(pk)) orderedKeys.push(pk);
    }
    for (const k of allKeys) {
        if (!orderedKeys.includes(k) && !shouldSkip(k)) orderedKeys.push(k);
    }

    orderedKeys.forEach((key) => {
        if (shouldSkip(key)) return;
        const value = current_life[key];

        // 英文key自动翻译为中文显示名
        const cnKey = EN_TO_CN_KEY_MAP[key.toLowerCase()] || EN_TO_CN_KEY_MAP[key] || null;
        const displayKey = cnKey || key;
        // 用于特殊渲染匹配的规范key（优先中文）
        const matchKey = cnKey || key;

        // ── 人物背景: 特殊渲染为固定展开的概要区 ──
        if (matchKey === '人物背景') {
            const bgSection = document.createElement('div');
            bgSection.classList.add('character-background-section');

            const bgTitle = document.createElement('div');
            bgTitle.classList.add('character-background-title');
            // 提取姓名（第一行通常是 【姓名】·性别）
            const firstLine = (typeof value === 'string' ? value.split('\n')[0] : '角色信息');
            bgTitle.textContent = firstLine;
            bgSection.appendChild(bgTitle);

            const bgDetails = document.createElement('details');
            bgDetails.classList.add('character-background-details');
            const bgSummary = document.createElement('summary');
            bgSummary.textContent = '详细背景';
            bgDetails.appendChild(bgSummary);

            const bgContent = document.createElement('div');
            bgContent.classList.add('character-background-content');
            // 除第一行外的其余内容
            if (typeof value === 'string') {
                const lines = value.split('\n').slice(1);
                lines.forEach(line => {
                    if (!line.trim()) return;
                    const p = document.createElement('p');
                    p.classList.add('background-line');
                    p.textContent = line;
                    bgContent.appendChild(p);
                });
            }
            bgDetails.appendChild(bgContent);
            bgSection.appendChild(bgDetails);
            container.appendChild(bgSection);
            return;
        }

        // ── 故事事件: 特殊渲染 ──
        if (matchKey === '故事事件' && Array.isArray(value)) {
            const details = document.createElement('details');
            const summary = document.createElement('summary');
            summary.textContent = `故事事件 (${value.length})`;
            details.appendChild(summary);

            const content = document.createElement('div');
            content.classList.add('details-content', 'story-events-list');

            value.forEach((ev, idx) => {
                const evDiv = document.createElement('div');
                const evText = typeof ev === 'string' ? ev : JSON.stringify(ev);
                const isSummary = evText.startsWith('【前事摘要】');
                evDiv.classList.add('story-event-item');
                if (isSummary) evDiv.classList.add('story-event-summary');
                // 最近3条高亮
                if (!isSummary && idx >= value.length - 3) {
                    evDiv.classList.add('story-event-recent');
                }
                evDiv.textContent = evText;
                content.appendChild(evDiv);
            });

            details.appendChild(content);
            // 故事事件默认展开
            details.open = true;
            container.appendChild(details);
            return;
        }

        // ── 物品: 增强渲染，显示数量 ──
        if (matchKey === '物品' && Array.isArray(value)) {
            const details = document.createElement('details');
            const summary = document.createElement('summary');
            summary.textContent = `物品 (${value.length})`;
            details.appendChild(summary);

            const content = document.createElement('div');
            content.classList.add('details-content', 'items-list');

            if (value.length === 0) {
                const empty = document.createElement('div');
                empty.classList.add('property-value');
                empty.textContent = '身无长物';
                content.appendChild(empty);
            } else {
                value.forEach(item => {
                    const itemDiv = document.createElement('div');
                    itemDiv.classList.add('item-entry');
                    if (typeof item === 'object' && item !== null) {
                        const name = item['名称'] || '未知物品';
                        const qty = item['数量'] || 1;
                        const effect = item['效果'] || item['描述'] || '';
                        itemDiv.innerHTML = `<span class="item-name">${name}</span>` +
                            (qty > 1 ? `<span class="item-qty">×${qty}</span>` : '') +
                            (effect ? `<span class="item-effect">${effect}</span>` : '');
                    } else {
                        itemDiv.textContent = String(item);
                    }
                    content.appendChild(itemDiv);
                });
            }

            details.appendChild(content);
            details.open = true;
            container.appendChild(details);
            return;
        }

        // ── 默认渲染逻辑 ──
        // 以 ~ 开头的字段为临时事件字段
        const isEventField = key.startsWith(TEMP_FIELD_PREFIX);
        // 显示名：去掉~前缀 + 英文翻译
        const baseName = isEventField ? key.slice(TEMP_FIELD_PREFIX.length) : displayKey;
        const details = document.createElement('details');
        if (isEventField) details.classList.add('event-field');
        const summary = document.createElement('summary');
        summary.textContent = isEventField ? `▸ ${baseName}` : baseName;
        details.appendChild(summary);

        const content = document.createElement('div');
        content.classList.add('details-content');
        
        renderValue(content, value);
        
        details.appendChild(content);
        container.appendChild(details);
    });

    renderCultivationPanel(current_life);
    renderSocialRelations(current_life);
}

// --- 功法品阶配置 ---
const GRADE_CONFIG = {
    '天': { color: '#ff4500', icon: '☳', rank: 4 },
    '地': { color: '#cd853f', icon: '☲', rank: 3 },
    '玄': { color: '#6a5acd', icon: '☱', rank: 2 },
    '黄': { color: '#b8860b', icon: '☰', rank: 1 },
};

const TIER_RANK = { '极品': 4, '上品': 3, '中品': 2, '下品': 1 };

function renderCultivationPanel(currentLife) {
    const panel = DOMElements.cultivationPanel;
    const container = DOMElements.cultivationTechniques;
    const powerDisplay = DOMElements.cultivationPower;
    if (!panel || !container) return;

    const techniques = currentLife ? currentLife['功法'] : null;
    if (!techniques || !Array.isArray(techniques) || techniques.length === 0) {
        panel.classList.add('hidden');
        return;
    }

    panel.classList.remove('hidden');
    container.innerHTML = '';

    // Helper to escape HTML
    const esc = (str) => {
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    };

    // Sort: higher grade first, then higher tier
    const sorted = [...techniques].sort((a, b) => {
        const ga = GRADE_CONFIG[a['品阶']]?.rank || 0;
        const gb = GRADE_CONFIG[b['品阶']]?.rank || 0;
        if (gb !== ga) return gb - ga;
        return (TIER_RANK[b['等阶']] || 0) - (TIER_RANK[a['等阶']] || 0);
    });

    sorted.forEach(tech => {
        if (!tech || typeof tech !== 'object') return;

        const name = tech['名称'] || '未知功法';
        const grade = tech['品阶'] || '黄';
        const tier = tech['等阶'] || '下品';
        const type = tech['类型'] || '';
        const desc = tech['描述'] || '';

        const config = GRADE_CONFIG[grade] || GRADE_CONFIG['黄'];
        const gradeLabel = `${grade}阶${tier}`;

        const card = document.createElement('div');
        card.classList.add('cultivation-card', `cultivation-grade-${grade}`);

        card.innerHTML = `
            <div class="cultivation-card-header">
                <span class="cultivation-icon" style="color:${config.color}">${config.icon}</span>
                <span class="cultivation-name">${esc(name)}</span>
                <span class="cultivation-grade-badge" style="border-color:${config.color};color:${config.color}">${esc(gradeLabel)}</span>
            </div>
            ${type ? `<div class="cultivation-type">${esc(type)}</div>` : ''}
            ${desc ? `<div class="cultivation-desc">${esc(desc)}</div>` : ''}
        `;

        container.appendChild(card);
    });

    // Display total combat power
    if (powerDisplay) {
        const combatPower = currentLife['属性']?.['战力'] || 0;
        if (combatPower > 0) {
            powerDisplay.innerHTML = `<span class="cultivation-power-label">总战力</span><span class="cultivation-power-value">${combatPower}</span>`;
            powerDisplay.classList.remove('hidden');
        } else {
            powerDisplay.classList.add('hidden');
        }
    }
}

function renderSocialRelations(currentLife) {
    const panel = DOMElements.socialPanel;
    const container = DOMElements.socialRelations;
    if (!panel || !container) return;

    const npcs = currentLife ? currentLife['人物关系'] : null;
    if (!npcs || typeof npcs !== 'object' || Object.keys(npcs).length === 0) {
        panel.classList.add('hidden');
        return;
    }

    panel.classList.remove('hidden');
    container.innerHTML = '';

    // 按好感度排序
    const sorted = Object.entries(npcs).sort(
        ([, a], [, b]) => (b['好感度'] || 0) - (a['好感度'] || 0)
    );

    // Helper to escape HTML entities to prevent XSS from AI-generated NPC data
    const esc = (str) => {
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    };

    sorted.forEach(([name, npc]) => {
        if (!npc || typeof npc !== 'object') return;

        const score = npc['好感度'] || 0;
        const stage = npc['关系阶段'] || '陌生';
        const personality = npc['性格'] || '';
        const identity = npc['身份'] || '';
        const marks = npc['特殊标记'] || [];

        const card = document.createElement('div');
        card.classList.add('social-npc-card');
        card.classList.add(`social-tier-${getAffinityTier(score)}`);

        // 好感度条颜色
        const barPercent = Math.min(100, Math.abs(score));
        const barColor = score >= 60 ? '#e8b84b' :
                         score >= 20 ? '#6a8b6a' :
                         score >= 0  ? '#888' :
                         score >= -40 ? '#a08060' : '#a8453c';

        const marksHtml = marks.length > 0
            ? `<span class="social-marks">${marks.map(m => `【${esc(String(m))}】`).join('')}</span>`
            : '';

        card.innerHTML = `
            <div class="social-npc-header">
                <span class="social-npc-name">${esc(String(name))}</span>
                <span class="social-npc-stage">${esc(String(stage))}</span>
            </div>
            <div class="social-npc-info">
                ${identity ? `<span class="social-npc-identity">${esc(String(identity))}</span>` : ''}
                ${personality ? `<span class="social-npc-personality">${esc(String(personality))}</span>` : ''}
                ${marksHtml}
            </div>
            <div class="social-affinity-bar-container">
                <div class="social-affinity-bar" style="width:${barPercent}%;background:${barColor}"></div>
                <span class="social-affinity-score">${score > 0 ? '+' : ''}${score}</span>
            </div>
        `;

        container.appendChild(card);
    });
}

function getAffinityTier(score) {
    if (score >= 80) return 'sworn';
    if (score >= 60) return 'close';
    if (score >= 40) return 'friend';
    if (score >= 20) return 'acquaintance';
    if (score >= -20) return 'stranger';
    if (score >= -60) return 'hostile';
    return 'nemesis';
}

function renderRollEvent(rollEvent) {
    // ── Phase 1: Show overlay + type + breakdown (immediate) ──
    DOMElements.rollType.textContent = `⚔ ${rollEvent.type}`;
    DOMElements.rollResultDisplay.classList.add('hidden');
    DOMElements.rollDiceNumber.classList.add('hidden');
    DOMElements.rollBreakdown.classList.add('hidden');

    // Reset dice animation
    const diceCup = DOMElements.rollDiceArea.querySelector('.dice-cup');
    if (diceCup) {
        diceCup.style.display = '';
        diceCup.style.animation = 'none';
        void diceCup.offsetHeight; // trigger reflow
        diceCup.style.animation = '';
    }

    DOMElements.rollOverlay.classList.remove('hidden');

    // ── Phase 2: Animate breakdown items (staggered) ──
    const breakdown = rollEvent.breakdown || {};
    const items = [];

    // Base rate
    items.push({ label: '基础成功率', value: `${breakdown.base_rate ?? rollEvent.original_target ?? '?'}%`, cls: 'neutral', isBase: true });

    // Attribute bonus
    if (breakdown.attribute_bonus && breakdown.attribute_bonus !== 0) {
        const attrName = breakdown.attribute_name || '属性';
        const attrVal = breakdown.attribute_value != null ? `(${attrName}:${breakdown.attribute_value})` : `(${attrName})`;
        items.push({
            label: `属性加成 ${attrVal}`,
            value: `${breakdown.attribute_bonus > 0 ? '+' : ''}${breakdown.attribute_bonus}%`,
            cls: breakdown.attribute_bonus > 0 ? 'positive' : 'negative'
        });
    }

    // Item bonus
    if (breakdown.item_bonus && breakdown.item_bonus !== 0) {
        items.push({
            label: '道具加成',
            value: `${breakdown.item_bonus > 0 ? '+' : ''}${breakdown.item_bonus}%`,
            cls: breakdown.item_bonus > 0 ? 'positive' : 'negative'
        });
    }

    // Status bonus
    if (breakdown.status_bonus && breakdown.status_bonus !== 0) {
        items.push({
            label: '状态修正',
            value: `${breakdown.status_bonus > 0 ? '+' : ''}${breakdown.status_bonus}%`,
            cls: breakdown.status_bonus > 0 ? 'positive' : 'negative'
        });
    }

    // Combat power (功法战力) bonus
    if (breakdown.combat_bonus && breakdown.combat_bonus !== 0) {
        const powerLabel = breakdown.combat_power ? `(战力:${breakdown.combat_power})` : '';
        items.push({
            label: `功法加成 ${powerLabel}`,
            value: `+${breakdown.combat_bonus}%`,
            cls: 'positive'
        });
    }

    // Legacy bonus
    if (breakdown.legacy_bonus && breakdown.legacy_bonus !== 0) {
        items.push({
            label: '功德加成',
            value: `+${breakdown.legacy_bonus}%`,
            cls: 'positive'
        });
    }

    // Render breakdown
    DOMElements.breakdownItems.innerHTML = '';
    items.forEach((item, index) => {
        const el = document.createElement('div');
        el.className = `breakdown-item${item.isBase ? ' base-item' : ''}`;
        el.style.animationDelay = `${index * 150}ms`;
        el.innerHTML = `<span class="label">${item.label}</span><span class="value ${item.cls}">${item.value}</span>`;
        DOMElements.breakdownItems.appendChild(el);
    });
    DOMElements.breakdownFinalRate.textContent = breakdown.final_rate ?? '?';
    
    // Show breakdown with slight delay
    setTimeout(() => {
        DOMElements.rollBreakdown.classList.remove('hidden');
    }, 200);

    // ── Phase 3: Show dice number (after breakdown animation) ──
    const breakdownDuration = 200 + items.length * 150 + 500; // wait for all items + pause

    setTimeout(() => {
        // Hide dice emoji, show number
        if (diceCup) diceCup.style.display = 'none';
        DOMElements.rollDiceNumber.textContent = rollEvent.result;
        DOMElements.rollDiceNumber.classList.remove('hidden');
        // Re-trigger animation
        DOMElements.rollDiceNumber.style.animation = 'none';
        void DOMElements.rollDiceNumber.offsetHeight;
        DOMElements.rollDiceNumber.style.animation = '';
    }, breakdownDuration);

    // ── Phase 4: Show final result (after dice number) ──
    setTimeout(() => {
        DOMElements.rollOutcome.textContent = rollEvent.outcome;
        DOMElements.rollOutcome.className = `outcome-${rollEvent.outcome}`;
        DOMElements.rollSides.textContent = rollEvent.sides || 100;
        DOMElements.rollValue.textContent = rollEvent.result;
        DOMElements.rollTarget.textContent = rollEvent.target;
        DOMElements.rollResultDisplay.classList.remove('hidden');
    }, breakdownDuration + 600);

    // ── Phase 5: Auto-hide overlay ──
    setTimeout(() => {
        DOMElements.rollOverlay.classList.add('hidden');
    }, breakdownDuration + 600 + 3000);
}

// --- Legacy System UI ---
async function loadLegacyData() {
    const data = await api.getLegacy();
    if (data) {
        appState.legacyData = data;
        renderLegacyPanel();
    }
}

function renderLegacyPanel() {
    const data = appState.legacyData;
    if (!data || !DOMElements.legacyPanel) return;

    if (DOMElements.legacyPointsSpan) {
        DOMElements.legacyPointsSpan.textContent = data.legacy_points || 0;
    }

    const list = DOMElements.blessingsList;
    if (!list) return;
    list.innerHTML = '';

    const activeSet = new Set(data.active_blessings || []);

    (data.available_blessings || []).forEach(blessing => {
        const item = document.createElement('div');
        item.classList.add('blessing-item');

        const isActive = activeSet.has(blessing.id);
        const canAfford = data.legacy_points >= blessing.cost;

        item.innerHTML = `
            <div class="blessing-header">
                <span class="blessing-name">${blessing.name}</span>
                <span class="blessing-cost">${blessing.cost} 功德</span>
            </div>
            <div class="blessing-desc">${blessing.description}</div>
            <div class="blessing-category">${blessing.category}</div>
        `;

        const btn = document.createElement('button');
        btn.classList.add('blessing-btn');
        if (isActive) {
            btn.textContent = '已激活';
            btn.disabled = true;
            btn.classList.add('active');
        } else if (!canAfford) {
            btn.textContent = '功德不足';
            btn.disabled = true;
            btn.classList.add('disabled');
        } else {
            btn.textContent = '兑换';
            btn.addEventListener('click', async () => {
                btn.disabled = true;
                btn.textContent = '兑换中...';
                const result = await api.purchaseBlessing(blessing.id);
                if (result.success) {
                    btn.textContent = '已激活';
                    btn.classList.add('active');
                    // 刷新数据
                    await loadLegacyData();
                } else {
                    btn.textContent = result.message || '兑换失败';
                    setTimeout(() => {
                        btn.textContent = '兑换';
                        btn.disabled = false;
                    }, 2000);
                }
            });
        }

        item.appendChild(btn);
        list.appendChild(item);
    });
}

function toggleLegacyPanel() {
    if (!DOMElements.legacyPanel) return;
    const isHidden = DOMElements.legacyPanel.classList.contains('hidden');
    DOMElements.legacyPanel.classList.toggle('hidden');
    if (isHidden) {
        loadLegacyData();
    }
}

// --- Fullscreen Management ---
function toggleFullscreen() {
    document.body.classList.toggle('app-fullscreen');
    updateFullscreenButton();
}

function updateFullscreenButton() {
    const isFullscreen = document.body.classList.contains('app-fullscreen');
    if (DOMElements.fullscreenButton) {
        DOMElements.fullscreenButton.textContent = isFullscreen ? '⛶' : '⛶';
        DOMElements.fullscreenButton.title = isFullscreen ? '退出全屏' : '全屏模式';
    }
}

// --- Event Handlers ---
function handleLogout() {
    api.logout();
}

function handleAction(actionOverride = null) {
    const action = actionOverride || DOMElements.actionInput.value.trim();
    if (!action) return;

    const actionBase = action.split(":")[0].trim();
    if (actionBase === "开始试炼" || actionBase === "主动结束试炼") {
        // Allow starting/ending a trial even if the previous async task is in its finally block
    } else {
        if (appState.gameState && appState.gameState.is_processing) return;
    }

    DOMElements.actionInput.value = '';
    socketManager.sendAction(action);
    
    // --- 立即显示处理中提示（不等后端 state 推送） ---
    _injectProcessingHint();
}

function showCompanionChoice() {
    // Show the companion choice dialog before starting a trial
    const dialog = document.getElementById('companion-dialog');
    if (dialog) {
        dialog.classList.remove('hidden');
    }
}

// --- Initialization ---
async function initializeGame() {
    showLoading(true);
    try {
        const initialState = await api.initGame();
        appState.gameState = initialState;
        // 跳过历史记录中已有的消息，仅对后续新增消息做错误检测
        _lastCheckedHistoryLen = (initialState.display_history || []).length;
        processingState.consecutiveErrors = 0;
        render();
        showView('game-view');
        await socketManager.connect();
        
        // 加载继承系统数据
        loadLegacyData();
        
        console.log("Initialization complete and WebSocket is ready.");
    } catch (error) {
        showView('login-view');
        if (error.message !== 'Unauthorized') {
             console.error(`Session initialization failed: ${error.message}`);
        }
    } finally {
        showLoading(false);
    }
}

function init() {
    initializeGame();

    setupScrollInterruptListener(DOMElements.narrativeWindow);

    DOMElements.logoutButton.addEventListener('click', handleLogout);
    DOMElements.fullscreenButton.addEventListener('click', toggleFullscreen);

    // 结束试炼按钮
    if (DOMElements.endGameButton) {
        DOMElements.endGameButton.addEventListener('click', () => {
            if (confirm('确定要结束当前试炼吗？\n\n主动结束将放弃本次试炼中的所有灵石，但仍可获得功德点（取决于当前境界）。\n此操作不可撤销。')) {
                handleAction('主动结束试炼');
            }
        });
    }
    
    // 文字速度滑块
    const speedSlider = document.getElementById('stream-speed');
    const speedText = document.getElementById('speed-text');
    if (speedSlider && speedText) {
        // 从 localStorage 恢复用户偏好
        const savedSpeed = localStorage.getItem('stream_speed');
        if (savedSpeed !== null) {
            speedSlider.value = savedSpeed;
        }
        speedText.textContent = SPEED_LABELS[parseInt(speedSlider.value, 10)] || '适中';
        
        speedSlider.addEventListener('input', () => {
            const val = parseInt(speedSlider.value, 10);
            speedText.textContent = SPEED_LABELS[val] || '适中';
            localStorage.setItem('stream_speed', val);
        });
    }
    DOMElements.actionButton.addEventListener('click', () => handleAction());
    DOMElements.actionInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') handleAction(); });
    DOMElements.startTrialButton.addEventListener('click', () => showCompanionChoice());
    
    // Difficulty selection buttons
    document.querySelectorAll('.difficulty-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.difficulty-btn').forEach(b => b.classList.remove('selected'));
            btn.classList.add('selected');
        });
    });

    // Helper: get currently selected difficulty
    function getSelectedDifficulty() {
        const sel = document.querySelector('.difficulty-btn.selected');
        return sel ? sel.dataset.difficulty : '凡人修仙';
    }

    // Companion choice buttons (now include difficulty)
    document.getElementById('btn-companion-solo')?.addEventListener('click', () => {
        document.getElementById('companion-dialog').classList.add('hidden');
        handleAction(`开始试炼:独行:${getSelectedDifficulty()}`);
    });
    document.getElementById('btn-companion-party')?.addEventListener('click', () => {
        document.getElementById('companion-dialog').classList.add('hidden');
        handleAction(`开始试炼:同行:${getSelectedDifficulty()}`);
    });
    
    // 继承系统按钮
    if (DOMElements.legacyToggle) {
        DOMElements.legacyToggle.addEventListener('click', toggleLegacyPanel);
    }
    // 关闭按钮
    if (DOMElements.legacyCloseBtn) {
        DOMElements.legacyCloseBtn.addEventListener('click', toggleLegacyPanel);
    }
    // 点击遮罩层背景也关闭
    if (DOMElements.legacyPanel) {
        DOMElements.legacyPanel.addEventListener('click', (e) => {
            if (e.target === DOMElements.legacyPanel) toggleLegacyPanel();
        });
    }
}

// --- Email Auth Functions ---
function switchAuthTab(tab) {
    document.querySelectorAll('.auth-tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
    document.querySelectorAll('.auth-form').forEach(f => f.classList.remove('active'));
    document.getElementById(`auth-${tab}`).classList.add('active');
}

async function handleSendCode() {
    const email = document.getElementById('register-email').value.trim();
    const btn = document.getElementById('send-code-btn');
    const errorEl = document.getElementById('login-error');
    errorEl.textContent = '';

    if (!email || !email.includes('@')) {
        errorEl.textContent = '请输入有效的邮箱地址';
        return;
    }

    btn.disabled = true;
    btn.textContent = '发送中...';

    try {
        const resp = await fetch(`${API_BASE_URL}/auth/send-code`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email, purpose: 'register' }),
        });
        const data = await resp.json();

        if (data.success) {
            // Start countdown
            let countdown = 60;
            btn.textContent = `${countdown}s`;
            const timer = setInterval(() => {
                countdown--;
                btn.textContent = `${countdown}s`;
                if (countdown <= 0) {
                    clearInterval(timer);
                    btn.disabled = false;
                    btn.textContent = '发送验证码';
                }
            }, 1000);
        } else {
            errorEl.textContent = data.message || '发送失败';
            btn.disabled = false;
            btn.textContent = '发送验证码';
        }
    } catch (e) {
        errorEl.textContent = '网络错误，请重试';
        btn.disabled = false;
        btn.textContent = '发送验证码';
    }
}

async function handleEmailRegister() {
    const email = document.getElementById('register-email').value.trim();
    const code = document.getElementById('register-code').value.trim();
    const password = document.getElementById('register-password').value;
    const errorEl = document.getElementById('login-error');
    errorEl.textContent = '';

    if (!email || !code || !password) {
        errorEl.textContent = '请填写所有字段';
        return;
    }

    try {
        const resp = await fetch(`${API_BASE_URL}/auth/register`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email, password, code }),
        });
        const data = await resp.json();

        if (data.success) {
            errorEl.style.color = '#6a8b6a';
            errorEl.textContent = '注册成功！正在跳转登录...';
            switchAuthTab('login');
            document.getElementById('login-email').value = email;
            setTimeout(() => { errorEl.textContent = ''; errorEl.style.color = ''; }, 2000);
        } else {
            errorEl.textContent = data.message || '注册失败';
        }
    } catch (e) {
        errorEl.textContent = '网络错误，请重试';
    }
}

async function handleEmailLogin() {
    const email = document.getElementById('login-email').value.trim();
    const password = document.getElementById('login-password').value;
    const errorEl = document.getElementById('login-error');
    errorEl.textContent = '';

    if (!email || !password) {
        errorEl.textContent = '请填写邮箱和密码';
        return;
    }

    try {
        const resp = await fetch(`${API_BASE_URL}/auth/login`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email, password }),
        });
        const data = await resp.json();

        if (data.success) {
            // Cookie is set by server, just reload
            window.location.reload();
        } else {
            errorEl.textContent = data.message || '登录失败';
        }
    } catch (e) {
        errorEl.textContent = '网络错误，请重试';
    }
}

// Make switchAuthTab available globally for onclick handlers in HTML
window.switchAuthTab = switchAuthTab;
window.handleSendCode = handleSendCode;
window.handleEmailRegister = handleEmailRegister;
window.handleEmailLogin = handleEmailLogin;

// --- Start the App ---
init();
const appShell = document.getElementById("appShell");
const sidebar = document.getElementById("sidebar");
const rightPanel = document.getElementById("rightPanel");
const leftEdgeZone = document.getElementById("leftEdgeZone");
const rightEdgeZone = document.getElementById("rightEdgeZone");

const fileInput = document.getElementById("fileInput");
const filePickBtn = document.getElementById("filePickBtn");
const fileNameDisplay = document.getElementById("fileNameDisplay");
const fileInfoRow = document.getElementById("fileInfoRow");
const clearFileBtn = document.getElementById("clearFileBtn");
const videoSourceFile = document.getElementById("videoSourceFile");
const videoSourceUrl = document.getElementById("videoSourceUrl");
const fileSourceWrap = document.getElementById("fileSourceWrap");
const urlSourceWrap = document.getElementById("urlSourceWrap");
const csvInput = document.getElementById("csvInput");
const csvPickBtn = document.getElementById("csvPickBtn");
const csvNameDisplay = document.getElementById("csvNameDisplay");
const csvInfoRow = document.getElementById("csvInfoRow");
const clearCsvBtn = document.getElementById("clearCsvBtn");
const uploadStatus = document.getElementById("uploadStatus");
const uploadProgress = document.getElementById("uploadProgress");
const processStatusWrap = document.getElementById("processStatusWrap");
const downloadSpinner = document.getElementById("downloadSpinner");
const processProgressWrap = document.getElementById("processProgressWrap");
const urlInput = document.getElementById("urlInput");
const downloadBtn = document.getElementById("downloadBtn");
const trimToggle = document.getElementById("trimToggle");
const trimFields = document.getElementById("trimFields");
const trimStartInput = document.getElementById("trimStartInput");
const trimEndInput = document.getElementById("trimEndInput");
const frameIntervalInput = document.getElementById("frameIntervalInput");
const confLimitInput = document.getElementById("confLimitInput");
const sessionTimeoutInput = document.getElementById("sessionTimeoutInput");
const phantomTimeoutInput = document.getElementById("phantomTimeoutInput");

const statusMeta = document.getElementById("statusMeta");
const spinnerWrap = document.getElementById("spinnerWrap");

const startBtn = document.getElementById("startBtn");
const cancelBtn = document.getElementById("cancelBtn");
const resetStateBtn = document.getElementById("resetStateBtn");

const pinSidebarBtn = document.getElementById("pinSidebarBtn");
const pinRightPanelBtn = document.getElementById("pinRightPanelBtn");
const topHamburgerBtn = document.getElementById("topHamburgerBtn");
const videoFileIcon = document.getElementById("videoFileIcon");
const csvFileIcon = document.getElementById("csvFileIcon");

const videoPlayer = document.getElementById("videoPlayer");
const overlayCanvas = document.getElementById("overlayCanvas");
const videoError = document.getElementById("videoError");

const segmentsList = document.getElementById("segmentsList");
const segmentSearch = document.getElementById("segmentSearch");
const clearFilterBtn = document.getElementById("clearFilterBtn");

const resultsText = document.getElementById("resultsText");
const downloadTxtBtn = document.getElementById("downloadTxtBtn");

const eventLogBox = document.getElementById("eventLogBox");
const stateLogBox = document.getElementById("stateLogBox");
const eventsDetails = document.getElementById("eventsDetails");
const stateDetails = document.getElementById("stateDetails");
const copyEventsBtn = document.getElementById("copyEventsBtn");

let latestState = null;
let saveTextTimer = null;
let saveUiTimer = null;
let savePlaybackTimer = null;
let hasVideoLoadError = false;
let isApplyingUi = false;
let userExplicitlyUnmuted = false;
let currentMediaKey = null;
let pendingSeekSeconds = null;
let logsSelectionLocked = false;
let isStateLoading = false;
let stateLoadFailures = 0;
let stateEventSource = null;
let sseRetryTimer = null;

const ACTIVE_PHASES = new Set(["uploading", "downloading", "converting", "processing"]);
const UI_CACHE_KEY = "video_app_v5_ui";
const CSV_SIZE_CACHE_KEY = "video_app_v5_csv_sizes";

function normalizePhase(phase) {
    return String(phase || "idle").trim().toLowerCase();
}

function describePhase(phase) {
    const map = {
        idle: "",
        uploading: "Загрузка файла в локальное хранилище.",
        downloading: "Скачивание видео по URL.",
        uploaded: "Файл загружен, можно запускать анализ.",
        downloaded: "Видео скачано, можно запускать анализ.",
        converting: "Видео конвертируется в web-формат.",
        converted: "Конвертация завершена.",
        processing: "Идёт анализ видео.",
        done: "Обработка завершена.",
        error: "Ошибка в процессе. Проверьте лог событий."
    };
    return map[phase] || "Состояние обновлено.";
}

function phaseStatusLabel(phase) {
    const map = {
        uploading: "Загрузка файла",
        downloading: "Скачивание видео",
        converting: "Конвертация видео",
        processing: "Анализ видео"
    };
    return map[phase] || "";
}

function setProgress(percent) {
    const safe = Number.isFinite(percent) ? Math.max(0, Math.min(100, percent)) : 0;
    uploadProgress.style.width = `${safe}%`;
    uploadProgress.innerText = `${safe}%`;
}

function formatBytes(bytes) {
    if (!Number.isFinite(bytes) || bytes <= 0) {
        return "";
    }
    const units = ["B", "KB", "MB", "GB"];
    let size = bytes;
    let unit = 0;
    while (size >= 1024 && unit < units.length - 1) {
        size /= 1024;
        unit += 1;
    }
    return `${size.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

function composeVideoLabel(name, bytes) {
    const size = formatBytes(bytes);
    return size ? `${name} · ${size}` : name;
}

function readCsvSizeCache() {
    try {
        const raw = localStorage.getItem(CSV_SIZE_CACHE_KEY);
        if (!raw) {
            return {};
        }
        const parsed = JSON.parse(raw);
        return parsed && typeof parsed === "object" ? parsed : {};
    } catch {
        return {};
    }
}

function writeCsvSizeCache(cache) {
    try {
        localStorage.setItem(CSV_SIZE_CACHE_KEY, JSON.stringify(cache));
    } catch {
        // ignore storage errors
    }
}

function cacheCsvSize(name, size) {
    if (!name || !Number.isFinite(size) || size <= 0) {
        return;
    }
    const cache = readCsvSizeCache();
    cache[name] = size;
    writeCsvSizeCache(cache);
}

function formatDuration(seconds) {
    const total = Math.max(0, Math.floor(seconds));
    const m = Math.floor(total / 60);
    const s = total % 60;
    if (m > 0) {
        return `${m} мин ${s.toString().padStart(2, "0")} сек`;
    }
    return `${s} сек`;
}

function clampInt(value, fallback, min, max) {
    const num = Number.parseInt(value, 10);
    if (!Number.isFinite(num)) {
        return fallback;
    }
    return Math.max(min, Math.min(max, num));
}

function setVideoSource(mode) {
    const isFile = mode === "file";
    if (videoSourceFile) {
        videoSourceFile.checked = isFile;
    }
    if (videoSourceUrl) {
        videoSourceUrl.checked = !isFile;
    }
    if (fileSourceWrap && urlSourceWrap) {
        fileSourceWrap.hidden = !isFile;
        urlSourceWrap.hidden = isFile;
    }
    if (processStatusWrap) {
        processStatusWrap.hidden = true;
    }
    uploadStatus.innerText = "";
    setProgress(0);
    syncTrimVisibility();
}

function resetFileDisplay() {
    fileNameDisplay.innerText = "";
    clearFileBtn.hidden = true;
    fileInfoRow.hidden = true;
    if (videoFileIcon) {
        videoFileIcon.hidden = true;
    }
}

function resetCsvDisplay() {
    csvNameDisplay.innerText = "";
    clearCsvBtn.hidden = true;
    csvInfoRow.hidden = true;
    if (csvFileIcon) {
        csvFileIcon.hidden = true;
    }
}

function setVideoInfoVisible(visible) {
    fileInfoRow.hidden = !visible;
    if (videoFileIcon) {
        videoFileIcon.hidden = !visible;
    }
}

function setCsvInfoVisible(visible) {
    csvInfoRow.hidden = !visible;
    if (csvFileIcon) {
        csvFileIcon.hidden = !visible;
    }
}

function readSettingsFromUI() {
    return {
        frame_interval_sec: clampInt(frameIntervalInput.value, 3, 1, 30),
        conf_limit: clampInt(confLimitInput.value, 3, 1, 10),
        session_timeout_sec: clampInt(sessionTimeoutInput.value, 360, 10, 3600),
        phantom_timeout_sec: clampInt(phantomTimeoutInput.value, 60, 5, 3600)
    };
}

function applySettingsFromState(state) {
    const settings = state?.settings || {};
    const setValue = (input, value) => {
        if (document.activeElement === input) {
            return;
        }
        input.value = value;
    };

    setValue(frameIntervalInput, settings.frame_interval_sec ?? 3);
    setValue(confLimitInput, settings.conf_limit ?? 3);
    setValue(sessionTimeoutInput, settings.session_timeout_sec ?? 360);
    setValue(phantomTimeoutInput, settings.phantom_timeout_sec ?? 60);
}

function getUIPrefs(state) {
    const ui = state?.ui || {};
    return {
        sidebar_hidden: ui.sidebar_hidden !== false,
        right_panel_collapsed: ui.right_panel_collapsed !== false,
        sidebar_pinned: Boolean(ui.sidebar_pinned),
        right_panel_pinned: Boolean(ui.right_panel_pinned),
        events_open: ui.events_open !== false,
        state_open: Boolean(ui.state_open)
    };
}

function readCachedUIPrefs() {
    try {
        const raw = localStorage.getItem(UI_CACHE_KEY);
        if (!raw) {
            return null;
        }
        const parsed = JSON.parse(raw);
        return getUIPrefs({ ui: parsed });
    } catch {
        return null;
    }
}

function writeCachedUIPrefs(ui) {
    try {
        localStorage.setItem(UI_CACHE_KEY, JSON.stringify(ui));
    } catch {
        // ignore storage errors
    }
}

function setSidebarHoverOpen(open) {
    appShell.classList.toggle("sidebar-hover-open", open);
}

function setRightHoverOpen(open) {
    appShell.classList.toggle("right-hover-open", open);
}

function applyUIPrefs(ui) {
    isApplyingUi = true;
    appShell.classList.toggle("sidebar-hidden", ui.sidebar_hidden);
    appShell.classList.toggle("right-hidden", ui.right_panel_collapsed);
    appShell.classList.toggle("sidebar-pinned", ui.sidebar_pinned);
    appShell.classList.toggle("right-pinned", ui.right_panel_pinned);
    pinSidebarBtn.classList.toggle("btn-secondary", ui.sidebar_pinned);
    pinSidebarBtn.classList.toggle("btn-outline-secondary", !ui.sidebar_pinned);
    pinRightPanelBtn.classList.toggle("btn-secondary", ui.right_panel_pinned);
    pinRightPanelBtn.classList.toggle("btn-outline-secondary", !ui.right_panel_pinned);
    pinSidebarBtn.title = ui.sidebar_pinned ? "Открепить панель" : "Закрепить панель";
    pinRightPanelBtn.title = ui.right_panel_pinned ? "Открепить панель" : "Закрепить панель";

    if (eventsDetails.open !== ui.events_open) {
        eventsDetails.open = ui.events_open;
    }
    if (stateDetails.open !== ui.state_open) {
        stateDetails.open = ui.state_open;
    }
    writeCachedUIPrefs(ui);
    appShell.classList.remove("ui-booting");
    isApplyingUi = false;
}

async function persistUiPatch(partial) {
    if (!latestState) {
        return;
    }

    const merged = { ...getUIPrefs(latestState), ...partial };
    latestState.ui = merged;
    applyUIPrefs(merged);

    if (saveUiTimer) {
        clearTimeout(saveUiTimer);
    }

    saveUiTimer = setTimeout(async () => {
        try {
            await callJson("/state", { ui: merged });
        } catch {
            // Backend event log will capture request failures.
        }
    }, 250);
}

function updateControls(state) {
    const phase = normalizePhase(state.phase);
    const processing = Boolean(state.processing);
    const hasVideo = Boolean(state.video);
    const hasProtocol = Boolean(state.protocol_csv);
    const operationActive = ACTIVE_PHASES.has(phase) && processing;
    const canStart = hasVideo && hasProtocol && !operationActive;
    const canCancel = ACTIVE_PHASES.has(phase) && processing;
    const isUrlMode = videoSourceUrl?.checked;
    const isFileMode = videoSourceFile?.checked;

    startBtn.disabled = !canStart;
    cancelBtn.disabled = !canCancel;
    cancelBtn.title = canCancel ? "Прервать анализ с сохранением текущих результатов" : "";
    downloadBtn.disabled = operationActive || !isUrlMode;
    fileInput.disabled = operationActive || !isFileMode;
    filePickBtn.disabled = operationActive || !isFileMode;
    urlInput.disabled = operationActive || !isUrlMode;
    csvInput.disabled = operationActive;
    csvPickBtn.disabled = operationActive;
    resetStateBtn.disabled = operationActive;
    trimToggle.disabled = operationActive || !urlInput.value.trim() || !isUrlMode;
    clearFileBtn.disabled = operationActive;
    clearCsvBtn.disabled = operationActive;
    videoSourceFile.disabled = operationActive;
    videoSourceUrl.disabled = operationActive;
    resultsText.disabled = operationActive;
    downloadTxtBtn.disabled = operationActive && !resultsText.value.trim();

    spinnerWrap.classList.toggle("d-none", !operationActive);
    processStatusWrap.hidden = !operationActive;
    if (operationActive) {
        const label = phaseStatusLabel(phase);
        uploadStatus.innerText = label || describePhase(phase);
        if (phase === "downloading") {
            downloadSpinner.hidden = false;
            processProgressWrap.hidden = true;
        } else {
            downloadSpinner.hidden = true;
            processProgressWrap.hidden = false;
        }
    } else if (phase === "error") {
        processStatusWrap.hidden = false;
        uploadStatus.innerText = "Ошибка";
        downloadSpinner.hidden = true;
        processProgressWrap.hidden = true;
    } else {
        uploadStatus.innerText = "";
        downloadSpinner.hidden = true;
        processProgressWrap.hidden = true;
    }

    if (state.video) {
        fileNameDisplay.innerText = composeVideoLabel(state.video, state.video_bytes);
        clearFileBtn.hidden = false;
        setVideoInfoVisible(true);
    } else if (fileInput.files && fileInput.files[0]) {
        fileNameDisplay.innerText = composeVideoLabel(fileInput.files[0].name, fileInput.files[0].size);
        clearFileBtn.hidden = false;
        setVideoInfoVisible(true);
    } else {
        resetFileDisplay();
    }
    if (state.protocol_csv) {
        const stateCsvSize = Number(state.protocol_csv_bytes);
        let csvSize = Number.isFinite(stateCsvSize) && stateCsvSize > 0 ? stateCsvSize : null;
        if (!csvSize) {
            const cached = readCsvSizeCache()[state.protocol_csv];
            csvSize = Number.isFinite(cached) && cached > 0 ? cached : null;
        }
        csvNameDisplay.innerText = composeVideoLabel(state.protocol_csv, csvSize);
        clearCsvBtn.hidden = false;
        setCsvInfoVisible(true);
    } else if (csvInput.files && csvInput.files[0]) {
        csvNameDisplay.innerText = composeVideoLabel(csvInput.files[0].name, csvInput.files[0].size);
        clearCsvBtn.hidden = false;
        setCsvInfoVisible(true);
    } else {
        resetCsvDisplay();
    }
}

function updateStatusMeta(state) {
    const phase = normalizePhase(state.phase);
    const progress = Number(state.progress);
    const parts = [];

    if (ACTIVE_PHASES.has(phase)) {
        parts.push(`Операция: ${phase}`);
    }

    const startedAt = Number(state.phase_started_at);
    if (ACTIVE_PHASES.has(phase) && Number.isFinite(startedAt) && startedAt > 0) {
        const elapsed = Math.max(0, (Date.now() / 1000) - startedAt);
        if (elapsed >= 1) {
            parts.push(`Прошло: ${formatDuration(elapsed)}`);
        }
        if (Number.isFinite(progress) && progress > 0 && progress < 100) {
            const etaSec = (elapsed * (100 - progress)) / progress;
            if (Number.isFinite(etaSec) && etaSec >= 1) {
                const etaMin = Math.max(1, Math.round(etaSec / 60));
                parts.push(`Осталось ~${etaMin} мин`);
            }
        }
    }

    statusMeta.innerText = parts.join(" · ");
}

function getCurrentUI() {
    return getUIPrefs(latestState || {});
}

function getMediaKey(state) {
    if (state.converted) {
        return `converted:${state.converted}`;
    }
    if (state.video) {
        return `video:${state.video}`;
    }
    return null;
}

function syncVideoSource(state) {
    const converted = state.converted;
    const primary = converted ? `/converted/${encodeURIComponent(converted)}` : null;
    const fallback = state.video ? `/video/${encodeURIComponent(state.video)}` : null;
    const nextSource = primary || fallback;
    const nextKey = getMediaKey(state);

    if (!nextSource) {
        videoPlayer.removeAttribute("src");
        videoPlayer.load();
        currentMediaKey = null;
        pendingSeekSeconds = null;
        return;
    }

    const currentSrc = videoPlayer.getAttribute("src") || "";
    if (!currentSrc.endsWith(nextSource)) {
        hasVideoLoadError = false;
        videoError.hidden = true;
        videoError.innerText = "";
        videoPlayer.setAttribute("src", nextSource);
        if (!userExplicitlyUnmuted) {
            videoPlayer.muted = true;
        }
        videoPlayer.load();

        currentMediaKey = nextKey;
        const playback = state.playback || {};
        if (playback.source === nextKey && Number.isFinite(playback.position) && playback.position > 0) {
            pendingSeekSeconds = playback.position;
        } else {
            pendingSeekSeconds = null;
        }
    }

    if (hasVideoLoadError && primary && fallback && !currentSrc.endsWith(fallback)) {
        hasVideoLoadError = false;
        videoError.hidden = true;
        videoPlayer.setAttribute("src", fallback);
        if (!userExplicitlyUnmuted) {
            videoPlayer.muted = true;
        }
        videoPlayer.load();
        currentMediaKey = state.video ? `video:${state.video}` : currentMediaKey;
    }
}

function toTimeLabel(seconds) {
    const s = Number(seconds);
    if (!Number.isFinite(s) || s < 0) {
        return "00:00";
    }
    const mm = Math.floor(s / 60).toString().padStart(2, "0");
    const ss = Math.floor(s % 60).toString().padStart(2, "0");
    return `${mm}:${ss}`;
}

function renderSegments(state) {
    const term = (segmentSearch.value || "").trim().toLowerCase();
    const timestamps = Array.isArray(state.timestamps) ? state.timestamps : [];

    const items = timestamps
        .slice()
        .sort((a, b) => (a?.time || 0) - (b?.time || 0))
        .filter((item) => {
            if (!term) {
                return true;
            }
            const text = `${item?.label || ""} ${toTimeLabel(item?.time)}`.toLowerCase();
            return text.includes(term);
        });

    segmentsList.innerHTML = "";

    if (!items.length) {
        const empty = document.createElement("li");
        empty.className = "list-group-item text-body-secondary";
        empty.innerText = "Сегменты пока отсутствуют";
        segmentsList.appendChild(empty);
        return;
    }

    for (const item of items) {
        const li = document.createElement("li");
        li.className = "list-group-item d-flex justify-content-between align-items-center segment-item";
        li.innerHTML = `<span>${item.label || "Событие"}</span><span class="badge text-bg-light">${toTimeLabel(item.time)}</span>`;
        li.addEventListener("click", () => {
            if (Number.isFinite(item.time)) {
                videoPlayer.currentTime = item.time;
                void videoPlayer.play().catch(() => {});
            }
        });
        segmentsList.appendChild(li);
    }
}

function drawOverlay(state) {
    const bboxes = Array.isArray(state.bboxes) ? state.bboxes : [];
    const rect = videoPlayer.getBoundingClientRect();

    overlayCanvas.width = Math.max(1, Math.floor(rect.width));
    overlayCanvas.height = Math.max(1, Math.floor(rect.height));

    const ctx = overlayCanvas.getContext("2d");
    if (!ctx) {
        return;
    }

    ctx.clearRect(0, 0, overlayCanvas.width, overlayCanvas.height);
    ctx.strokeStyle = "#13d878";
    ctx.fillStyle = "rgba(19, 216, 120, 0.15)";
    ctx.lineWidth = 2;
    ctx.font = "12px sans-serif";

    for (const box of bboxes) {
        if (![box?.x, box?.y, box?.w, box?.h].every(Number.isFinite)) {
            continue;
        }
        const x = box.x * overlayCanvas.width;
        const y = box.y * overlayCanvas.height;
        const w = box.w * overlayCanvas.width;
        const h = box.h * overlayCanvas.height;

        ctx.fillRect(x, y, w, h);
        ctx.strokeRect(x, y, w, h);

        if (box.label) {
            ctx.fillStyle = "#13d878";
            ctx.fillText(String(box.label), x + 4, y + 14);
            ctx.fillStyle = "rgba(19, 216, 120, 0.15)";
        }
    }
}

function renderEventLog(state) {
    if (logsSelectionLocked) {
        return;
    }

    const events = Array.isArray(state.events) ? state.events : [];
    if (!events.length) {
        eventLogBox.innerText = "Журнал событий пуст";
    } else {
        const lines = events.slice(-120).map((item) => {
            const ts = String(item.ts || "").replace("T", " ").replace("+00:00", "Z");
            const type = (item.type || "event").toUpperCase();
            const level = (item.level || "info").toUpperCase();
            const msg = item.message || "";
            return `[${ts}] [${type}] [${level}] ${msg}`;
        });

        eventLogBox.innerText = lines.join("\n");
        eventLogBox.scrollTop = eventLogBox.scrollHeight;
    }

    const { events: _events, ...stateWithoutEvents } = state;
    stateLogBox.innerText = JSON.stringify(stateWithoutEvents, null, 2);
}

function renderState(state) {
    latestState = state;
    setProgress(state.progress);
    updateControls(state);
    applyUIPrefs(getUIPrefs(state));
    applySettingsFromState(state);
    updateStatusMeta(state);
    syncVideoSource(state);
    renderSegments(state);
    drawOverlay(state);
    renderEventLog(state);

    if (typeof state.results_text === "string" && document.activeElement !== resultsText) {
        resultsText.value = state.results_text;
    }
}

async function fetchState() {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 4000);
    const res = await fetch("/state", {
        cache: "no-store",
        signal: controller.signal
    }).finally(() => clearTimeout(timeoutId));
    if (!res.ok) {
        throw new Error(`State request failed: ${res.status}`);
    }
    return res.json();
}

async function loadState() {
    if (isStateLoading) {
        return;
    }
    isStateLoading = true;
    try {
        const state = await fetchState();
        stateLoadFailures = 0;
        renderState(state);
    } catch {
        stateLoadFailures += 1;
        if (stateLoadFailures >= 2) {
            uploadStatus.innerText = "Не удалось загрузить состояние. Проверьте, не завис ли процесс.";
        }
    } finally {
        isStateLoading = false;
    }
}

function connectStateStream() {
    if (!window.EventSource) {
        return;
    }
    if (stateEventSource) {
        stateEventSource.close();
    }

    stateEventSource = new EventSource("/state/stream");

    stateEventSource.addEventListener("state", (event) => {
        try {
            const state = JSON.parse(event.data || "{}");
            stateLoadFailures = 0;
            renderState(state);
        } catch {
            // ignore malformed event
        }
    });

    stateEventSource.onerror = () => {
        if (stateEventSource) {
            stateEventSource.close();
            stateEventSource = null;
        }
        if (sseRetryTimer) {
            return;
        }
        sseRetryTimer = setTimeout(() => {
            sseRetryTimer = null;
            connectStateStream();
        }, 3000);
    };
}

async function callJson(url, payload = null) {
    const init = payload
        ? {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        }
        : { method: "POST" };

    const res = await fetch(url, init);
    if (!res.ok) {
        let error = "request failed";
        try {
            const data = await res.json();
            error = data.error || error;
        } catch {}
        throw new Error(error);
    }
    return res.json();
}

function queueSavePlayback(force = false) {
    if (!currentMediaKey || !Number.isFinite(videoPlayer.currentTime)) {
        return;
    }

    const payload = {
        playback: {
            source: currentMediaKey,
            position: Number(videoPlayer.currentTime.toFixed(2))
        }
    };

    if (force) {
        void callJson("/state", payload).catch(() => {});
        return;
    }

    if (savePlaybackTimer) {
        return;
    }

    savePlaybackTimer = setTimeout(async () => {
        savePlaybackTimer = null;
        try {
            await callJson("/state", payload);
        } catch {
            // ignore transient failures
        }
    }, 1000);
}

fileInput.addEventListener("change", function () {
    const file = this.files[0];
    if (!file) {
        resetFileDisplay();
        return;
    }

    fileNameDisplay.innerText = file.name;
    setVideoInfoVisible(true);
    clearFileBtn.hidden = false;
    uploadStatus.innerText = "Загрузка...";
    processStatusWrap.hidden = false;
    setProgress(0);

    const formData = new FormData();
    formData.append("file", file);

    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/upload", true);

    xhr.upload.onprogress = function (event) {
        if (event.lengthComputable) {
            const percent = Math.round((event.loaded / event.total) * 100);
            setProgress(percent);
        }
    };

    xhr.onload = function () {
        if (xhr.status === 200) {
            uploadStatus.innerText = "Загрузка завершена";
            void loadState();
        } else if (xhr.status === 413) {
            uploadStatus.innerText = "Файл превышает 2GB";
        } else {
            uploadStatus.innerText = "Ошибка загрузки";
        }
    };

    xhr.onerror = function () {
        uploadStatus.innerText = "Ошибка загрузки";
    };

    xhr.send(formData);
});

csvInput.addEventListener("change", function () {
    const file = this.files[0];
    if (!file) {
        resetCsvDisplay();
        return;
    }

    csvNameDisplay.innerText = composeVideoLabel(file.name, file.size);
    cacheCsvSize(file.name, file.size);
    setCsvInfoVisible(true);
    clearCsvBtn.hidden = false;
    const formData = new FormData();
    formData.append("file", file);

    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/protocol/upload", true);
    xhr.onload = function () {
        if (xhr.status === 200) {
            void loadState();
        } else {
            csvNameDisplay.innerText = "Ошибка загрузки протокола";
            clearCsvBtn.hidden = true;
        }
    };
    xhr.onerror = function () {
        csvNameDisplay.innerText = "Ошибка загрузки протокола";
        clearCsvBtn.hidden = true;
    };
    xhr.send(formData);
});

csvPickBtn.addEventListener("click", () => {
    csvInput.click();
});

downloadBtn.addEventListener("click", async function () {
    const url = urlInput.value.trim();
    if (!url) {
        uploadStatus.innerText = "Введите URL";
        return;
    }

    uploadStatus.innerText = "Скачивание запущено";
    processStatusWrap.hidden = false;

    try {
        const payload = { url };
        if (trimToggle.checked) {
            const startVal = clampInt(trimStartInput.value, 0, 0, 999999);
            const endVal = clampInt(trimEndInput.value, 0, 0, 999999);
            if (endVal > startVal) {
                payload.start_time = startVal;
                payload.end_time = endVal;
            } else {
                uploadStatus.innerText = "Конец должен быть больше начала";
                return;
            }
        }
        await callJson("/download", payload);
    } catch (err) {
        uploadStatus.innerText = `Ошибка скачивания: ${err.message}`;
    }

    void loadState();
});

filePickBtn.addEventListener("click", () => {
    fileInput.click();
});

clearFileBtn.addEventListener("click", async () => {
    try {
        await callJson("/video/clear");
        fileInput.value = "";
        resetFileDisplay();
        void loadState();
    } catch (err) {
        uploadStatus.innerText = `Ошибка удаления: ${err.message}`;
        processStatusWrap.hidden = false;
    }
});

clearCsvBtn.addEventListener("click", async () => {
    try {
        await callJson("/protocol/clear");
        csvInput.value = "";
        resetCsvDisplay();
        void loadState();
    } catch (err) {
        csvNameDisplay.innerText = `Ошибка удаления: ${err.message}`;
        clearCsvBtn.hidden = true;
    }
});

videoSourceFile.addEventListener("change", () => {
    if (videoSourceFile.checked) {
        setVideoSource("file");
        updateControls(latestState || {});
    }
});

videoSourceUrl.addEventListener("change", () => {
    if (videoSourceUrl.checked) {
        setVideoSource("url");
        updateControls(latestState || {});
    }
});

startBtn.addEventListener("click", async () => {
    uploadStatus.innerText = "Анализ запущен";
    try {
        await callJson("/process/start", { settings: readSettingsFromUI() });
    } catch (err) {
        uploadStatus.innerText = `Ошибка запуска: ${err.message}`;
    }
    void loadState();
});

cancelBtn.addEventListener("click", async () => {
    try {
        await callJson("/process/cancel");
        uploadStatus.innerText = "Отмена запрошена";
    } catch (err) {
        uploadStatus.innerText = `Ошибка отмены: ${err.message}`;
    }
    void loadState();
});

resetStateBtn.addEventListener("click", async () => {
    try {
        await callJson("/state/reset", { clear_events: true });
        fileInput.value = "";
        csvInput.value = "";
        urlInput.value = "";
        resultsText.value = "";
        segmentSearch.value = "";
        setVideoSource("file");
        resetFileDisplay();
        resetCsvDisplay();
        syncTrimVisibility();
        uploadStatus.innerText = "Состояние сброшено";
        videoError.hidden = true;
        videoError.innerText = "";
        hasVideoLoadError = false;
    } catch (err) {
        uploadStatus.innerText = `Ошибка сброса: ${err.message}`;
    }
    void loadState();
});

if (topHamburgerBtn) {
    topHamburgerBtn.addEventListener("click", () => {
        const current = getUIPrefs(latestState || {});
        void persistUiPatch({ sidebar_hidden: !current.sidebar_hidden });
        setSidebarHoverOpen(false);
    });
}

pinSidebarBtn.addEventListener("click", () => {
    const current = getUIPrefs(latestState || {});
    void persistUiPatch({
        sidebar_pinned: !current.sidebar_pinned,
        sidebar_hidden: false,
    });
});

pinRightPanelBtn.addEventListener("click", () => {
    const current = getUIPrefs(latestState || {});
    void persistUiPatch({
        right_panel_pinned: !current.right_panel_pinned,
        right_panel_collapsed: false,
    });
});

eventsDetails.addEventListener("toggle", () => {
    if (isApplyingUi) {
        return;
    }
    const current = getUIPrefs(latestState || {});
    if (current.events_open !== eventsDetails.open) {
        void persistUiPatch({ events_open: eventsDetails.open });
    }
});

stateDetails.addEventListener("toggle", () => {
    if (isApplyingUi) {
        return;
    }
    const current = getUIPrefs(latestState || {});
    if (current.state_open !== stateDetails.open) {
        void persistUiPatch({ state_open: stateDetails.open });
    }
});

segmentSearch.addEventListener("input", () => {
    if (latestState) {
        renderSegments(latestState);
    }
});

clearFilterBtn.addEventListener("click", () => {
    segmentSearch.value = "";
    if (latestState) {
        renderSegments(latestState);
    }
});

copyEventsBtn.addEventListener("click", async (event) => {
    event.preventDefault();
    try {
        await navigator.clipboard.writeText(eventLogBox.innerText || "");
    } catch {
        // clipboard can be blocked by browser policy
    }
});

downloadTxtBtn.addEventListener("click", () => {
    const blob = new Blob([resultsText.value || ""], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "results.txt";
    a.click();
    URL.revokeObjectURL(url);
});

resultsText.addEventListener("input", () => {
    const phase = normalizePhase(latestState?.phase);
    if (ACTIVE_PHASES.has(phase) && latestState?.processing) {
        return;
    }
    if (saveTextTimer) {
        clearTimeout(saveTextTimer);
    }
    saveTextTimer = setTimeout(async () => {
        try {
            await callJson("/state", { results_text: resultsText.value });
        } catch {
            // Backend event log will show failure.
        }
    }, 500);
});

videoPlayer.addEventListener("error", () => {
    hasVideoLoadError = true;
    videoError.hidden = false;
    videoError.innerText = "Видео не удалось загрузить. Проверьте URL/формат в логе событий.";
});

videoPlayer.addEventListener("loadeddata", () => {
    hasVideoLoadError = false;
    videoError.hidden = true;
    videoError.innerText = "";
    if (!userExplicitlyUnmuted) {
        videoPlayer.muted = true;
    }
});

videoPlayer.addEventListener("loadedmetadata", () => {
    if (pendingSeekSeconds == null) {
        return;
    }
    const max = Number.isFinite(videoPlayer.duration) ? Math.max(0, videoPlayer.duration - 0.2) : pendingSeekSeconds;
    videoPlayer.currentTime = Math.min(pendingSeekSeconds, max);
    pendingSeekSeconds = null;
});

videoPlayer.addEventListener("volumechange", () => {
    if (!videoPlayer.muted) {
        userExplicitlyUnmuted = true;
    }
});

for (const el of [eventLogBox, stateLogBox]) {
    el.addEventListener("mousedown", () => {
        logsSelectionLocked = true;
    });
    el.addEventListener("mouseup", () => {
        setTimeout(() => {
            const selection = window.getSelection();
            logsSelectionLocked = Boolean(selection && !selection.isCollapsed);
        }, 0);
    });
    el.addEventListener("mouseleave", () => {
        const selection = window.getSelection();
        logsSelectionLocked = Boolean(selection && !selection.isCollapsed);
    });
}

setInterval(() => {
    if (stateEventSource && stateEventSource.readyState === EventSource.OPEN) {
        return;
    }
    void loadState();
}, 5000);

window.addEventListener("resize", () => {
    if (latestState) {
        drawOverlay(latestState);
    }
});

leftEdgeZone.addEventListener("mouseenter", () => {
    const ui = getCurrentUI();
    if (ui.sidebar_hidden) {
        setSidebarHoverOpen(true);
    }
});

rightEdgeZone.addEventListener("mouseenter", () => {
    const ui = getCurrentUI();
    if (ui.right_panel_collapsed) {
        setRightHoverOpen(true);
    }
});

sidebar.addEventListener("mouseleave", () => {
    const ui = getCurrentUI();
    if (!ui.sidebar_pinned && ui.sidebar_hidden) {
        setSidebarHoverOpen(false);
    }
});

rightPanel.addEventListener("mouseleave", () => {
    const ui = getCurrentUI();
    if (!ui.right_panel_pinned && ui.right_panel_collapsed) {
        setRightHoverOpen(false);
    }
});

function syncTrimVisibility() {
    const hasUrl = Boolean(urlInput.value.trim());
    const isUrlMode = videoSourceUrl?.checked;
    trimToggle.disabled = !hasUrl || !isUrlMode;
    if (!hasUrl || !isUrlMode) {
        trimToggle.checked = false;
    }
    trimFields.hidden = !(trimToggle.checked && hasUrl && isUrlMode);
}

urlInput.addEventListener("input", () => {
    syncTrimVisibility();
});

trimToggle.addEventListener("change", () => {
    syncTrimVisibility();
});

document.addEventListener("mousedown", (event) => {
    const current = getUIPrefs(latestState || {});
    const target = event.target;
    const sidebarVisible = !current.sidebar_hidden || appShell.classList.contains("sidebar-hover-open");
    const rightVisible = !current.right_panel_collapsed || appShell.classList.contains("right-hover-open");

    if (
        sidebarVisible &&
        !current.sidebar_pinned &&
        sidebar &&
        target instanceof Node &&
        !sidebar.contains(target) &&
        !leftEdgeZone.contains(target)
    ) {
        if (current.sidebar_hidden) {
            setSidebarHoverOpen(false);
        } else {
            void persistUiPatch({ sidebar_hidden: true });
            setSidebarHoverOpen(false);
        }
    }

    if (
        rightVisible &&
        !current.right_panel_pinned &&
        rightPanel &&
        target instanceof Node &&
        !rightPanel.contains(target) &&
        !rightEdgeZone.contains(target)
    ) {
        if (current.right_panel_collapsed) {
            setRightHoverOpen(false);
        } else {
            void persistUiPatch({ right_panel_collapsed: true });
            setRightHoverOpen(false);
        }
    }
});

videoPlayer.addEventListener("timeupdate", () => {
    if (latestState) {
        drawOverlay(latestState);
    }
    queueSavePlayback(false);
});

videoPlayer.addEventListener("pause", () => {
    queueSavePlayback(true);
});

window.addEventListener("beforeunload", () => {
    queueSavePlayback(true);
});

const cachedUI = readCachedUIPrefs();
if (cachedUI) {
    applyUIPrefs(cachedUI);
} else {
    appShell.classList.remove("ui-booting");
}

setVideoSource("file");
syncTrimVisibility();
connectStateStream();
void loadState();

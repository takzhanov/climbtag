from __future__ import annotations

import asyncio
import json
import logging
import time
import multiprocessing as mp
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Lock, Thread
from urllib.parse import unquote, urlparse

import requests
import subprocess
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.processing import (
    CancelledError,
    ProcessingError,
    ensure_playable_input,
    run_protocol_analysis,
    validate_video_file,
)
from app.state import (
    append_event,
    get_state_version,
    load_state,
    save_state,
    update_state,
    wait_for_state_change,
)

try:
    import yt_dlp
except ImportError:  # pragma: no cover - optional at runtime
    yt_dlp = None

app = FastAPI()

BASE_DIR = Path(__file__).resolve().parent.parent
UPLOAD_DIR = BASE_DIR / "input" / "videos"
CONVERTED_DIR = BASE_DIR / "outputs" / "converted"
PROTOCOL_DIR = BASE_DIR / "input" / "protocols"
MODEL_PATH = BASE_DIR / "models" / "yolov8n.pt"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
CONVERTED_DIR.mkdir(parents=True, exist_ok=True)
PROTOCOL_DIR.mkdir(parents=True, exist_ok=True)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
ACTIVE_PHASES = {"uploading", "downloading", "converting", "processing"}
WORKER_REQUIRED_PHASES = {"downloading", "converting", "processing"}
RECONCILE_START_GRACE_SEC = 5.0
DEFAULT_SETTINGS = {
    "frame_interval_sec": 3,
    "conf_limit": 3,
    "session_timeout_sec": 240,
    "phantom_timeout_sec": 60,
}
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

_server_logger = logging.getLogger("climbtag.server")
if not _server_logger.handlers:
    _server_logger.setLevel(logging.INFO)
    _server_logger.propagate = False
    _server_handler = RotatingFileHandler(
        LOG_DIR / "backend.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    _server_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    _server_logger.addHandler(_server_handler)

_worker_lock = Lock()
_worker_thread: Thread | None = None

_process_lock = Lock()
_process_worker: mp.Process | None = None
_process_queue: mp.Queue | None = None
_process_cancel: mp.Event | None = None
_process_listener: Thread | None = None


def _set_worker(thread: Thread):
    global _worker_thread
    with _worker_lock:
        _worker_thread = thread


def _clear_worker():
    global _worker_thread
    with _worker_lock:
        _worker_thread = None


def _worker_active() -> bool:
    with _worker_lock:
        thread_active = _worker_thread is not None and _worker_thread.is_alive()
    with _process_lock:
        process_active = _process_worker is not None and _process_worker.is_alive()
    return thread_active or process_active


def _set_process_worker(
    process: mp.Process,
    queue: mp.Queue,
    cancel_event: mp.Event,
    listener: Thread,
):
    global _process_worker, _process_queue, _process_cancel, _process_listener
    with _process_lock:
        _process_worker = process
        _process_queue = queue
        _process_cancel = cancel_event
        _process_listener = listener


def _clear_process_worker():
    global _process_worker, _process_queue, _process_cancel, _process_listener
    with _process_lock:
        _process_worker = None
        _process_queue = None
        _process_cancel = None
        _process_listener = None


def _process_active() -> bool:
    with _process_lock:
        return _process_worker is not None and _process_worker.is_alive()


def _cancel_requested() -> bool:
    return bool(load_state().get("cancel_requested"))


def _safe_name(raw_name: str | None, fallback: str) -> str:
    cleaned = Path(unquote(raw_name or "")).name.strip()
    return cleaned or fallback


def _safe_unlink(path: Path, *, retries: int = 3, delay_sec: float = 0.1):
    for attempt in range(retries):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            if attempt == retries - 1:
                return
            time.sleep(delay_sec)
        except OSError:
            return


def _normalize_phase(raw: str | None) -> str:
    phase = (raw or "idle").strip().lower()
    if phase in {
        "idle", "uploading", "downloading", "uploaded", "downloaded",
        "converting", "converted", "processing", "done", "error"
    }:
        return phase
    return "idle"


def _reset_state(*, clear_events: bool = False):
    state = load_state()
    state.update({
        "video": None,
        "converted": None,
        "protocol_csv": None,
        "processing": False,
        "phase": "idle",
        "progress": 0,
        "phase_started_at": None,
        "cancel_requested": False,
        "results_text": "",
        "playback": {"source": None, "position": 0},
        "video_bytes": None,
        "converted_bytes": None,
        "bboxes": [],
        "timestamps": [],
        "settings": dict(DEFAULT_SETTINGS),
    })
    if clear_events:
        state["events"] = []
    save_state(state)


def _reconcile_runtime_state() -> dict:
    state = load_state()
    changed = False

    phase = _normalize_phase(state.get("phase"))
    if phase != state.get("phase"):
        state["phase"] = phase
        changed = True

    processing = bool(state.get("processing"))
    worker_active = _worker_active()
    active_phase = phase in ACTIVE_PHASES
    worker_required_phase = phase in WORKER_REQUIRED_PHASES
    phase_started_at = state.get("phase_started_at")
    if isinstance(phase_started_at, (int, float)):
        elapsed = time.time() - phase_started_at
    else:
        elapsed = None
    in_start_grace = elapsed is not None and 0 <= elapsed < RECONCILE_START_GRACE_SEC

    # Clear restored file pointers if files no longer exist after restart.
    video_name = state.get("video")
    if isinstance(video_name, str) and video_name:
        video_path = (UPLOAD_DIR / video_name).resolve()
        if video_path.parent != UPLOAD_DIR.resolve() or not video_path.exists():
            state["video"] = None
            state["video_bytes"] = None
            state["converted"] = None
            state["converted_bytes"] = None
            changed = True

    converted_name = state.get("converted")
    if isinstance(converted_name, str) and converted_name:
        converted_path = (CONVERTED_DIR / converted_name).resolve()
        if converted_path.parent != CONVERTED_DIR.resolve() or not converted_path.exists():
            state["converted"] = None
            state["converted_bytes"] = None
            changed = True

    protocol_name = state.get("protocol_csv")
    if isinstance(protocol_name, str) and protocol_name:
        protocol_path = (PROTOCOL_DIR / protocol_name).resolve()
        if protocol_path.parent != PROTOCOL_DIR.resolve() or not protocol_path.exists():
            state["protocol_csv"] = None
            changed = True

    if (processing or worker_required_phase) and not worker_active and not in_start_grace:
        state["processing"] = False
        state["cancel_requested"] = False
        state["phase"] = "idle"
        state["progress"] = 0
        state["phase_started_at"] = None
        changed = True
        append_event(
            "Runtime reconcile: worker missing, phase reset to idle",
            event_type="process",
            level="warning",
        )

    if changed:
        save_state(state)

    return state


def _processing_worker_process(
    source_path_str: str,
    protocol_path_str: str,
    model_path_str: str,
    settings: dict,
    queue: mp.Queue,
    cancel_event: mp.Event,
):
    def send_patch(patch: dict):
        queue.put({"type": "patch", "data": patch})

    def send_event(message: str, *, event_type: str = "process", level: str = "info", details: dict | None = None):
        payload = {"type": "event", "message": message, "event_type": event_type, "level": level}
        if details:
            payload["details"] = details
        queue.put(payload)

    try:
        source_path = Path(source_path_str)
        protocol_path = Path(protocol_path_str)
        model_path = Path(model_path_str)

        analysis_path, was_converted = ensure_playable_input(
            source_path,
            CONVERTED_DIR,
            check_cancel=cancel_event.is_set,
            progress_cb=lambda p: send_patch({"phase": "converting", "progress": p}),
            event_cb=lambda msg: send_event(msg),
        )

        if cancel_event.is_set():
            raise CancelledError("cancelled after conversion")

        converted_bytes = analysis_path.stat().st_size if was_converted else None
        send_patch({
            "phase": "converted",
            "progress": 100,
            "phase_started_at": time.time(),
            "converted": analysis_path.name if was_converted else None,
            "converted_bytes": converted_bytes,
        })

        send_patch({"phase": "processing", "progress": 0, "phase_started_at": time.time()})

        analysis = run_protocol_analysis(
            analysis_path,
            protocol_path,
            model_path,
            settings=settings,
            partial_cb=lambda patch: send_patch(patch),
            check_cancel=cancel_event.is_set,
            progress_cb=lambda p: send_patch({"progress": p}),
            event_cb=lambda msg: send_event(msg),
        )

        if cancel_event.is_set():
            raise CancelledError("cancelled before finishing")

        send_patch({
            "phase": "done",
            "progress": 100,
            "phase_started_at": time.time(),
            "processing": False,
            "cancel_requested": False,
            "converted_bytes": converted_bytes,
            **analysis,
        })
        send_event("Pipeline done", event_type="process")
    except CancelledError:
        send_patch({
            "phase": "idle",
            "progress": 0,
            "phase_started_at": None,
            "processing": False,
            "cancel_requested": False,
        })
        send_event("Pipeline cancelled", event_type="process", level="warning")
    except FileNotFoundError:
        send_patch({
            "phase": "error",
            "processing": False,
            "phase_started_at": time.time(),
            "cancel_requested": False,
        })
        send_event("Pipeline failed: source video not found", event_type="process", level="error")
    except ProcessingError as exc:
        send_patch({
            "phase": "error",
            "processing": False,
            "phase_started_at": time.time(),
            "cancel_requested": False,
        })
        send_event(f"Pipeline failed: {exc}", event_type="process", level="error")
    except Exception as exc:
        send_patch({
            "phase": "error",
            "processing": False,
            "phase_started_at": time.time(),
            "cancel_requested": False,
        })
        send_event(f"Pipeline failed: {exc}", event_type="process", level="error")
    finally:
        queue.put({"type": "final"})


def _start_process_listener(queue: mp.Queue, process: mp.Process) -> Thread:
    def _listener():
        while True:
            try:
                message = queue.get(timeout=0.5)
            except Exception:
                if not process.is_alive():
                    break
                continue

            if not isinstance(message, dict):
                continue

            msg_type = message.get("type")
            if msg_type == "patch":
                update_state(message.get("data", {}))
            elif msg_type == "event":
                append_event(
                    message.get("message", ""),
                    event_type=message.get("event_type", "process"),
                    level=message.get("level", "info"),
                    details=message.get("details"),
                )
            elif msg_type == "final":
                break

        _clear_process_worker()

    listener = Thread(target=_listener, daemon=True)
    listener.start()
    return listener


def _download_with_ytdlp(url: str, *, start_time: int | None = None, end_time: int | None = None) -> Path:
    if yt_dlp is None:
        raise RuntimeError("yt-dlp is not installed")

    append_event("Downloader selected: yt-dlp", event_type="process", details={"url": url})

    if start_time is not None and end_time is not None and end_time > start_time:
        clip_tag = f"_clip_{start_time:06d}_{end_time:06d}"
        output_template = str(UPLOAD_DIR / f"%(id)s{clip_tag}.%(ext)s")
    else:
        output_template = str(UPLOAD_DIR / "%(id)s.%(ext)s")

    def hook(data: dict):
        if _cancel_requested():
            raise CancelledError("download cancelled")

        if data.get("status") != "downloading":
            return

        total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
        downloaded = data.get("downloaded_bytes") or 0
        if total:
            update_state({"progress": int((downloaded * 100) / total)})

    opts = {
        "format": "best[ext=mp4][height<=720]/best[ext=mp4]/best",
        "outtmpl": output_template,
        "noplaylist": True,
        "progress_hooks": [hook],
        "quiet": True,
        "no_warnings": True,
        "overwrites": True,
        "restrictfilenames": True,
    }

    if start_time is not None and end_time is not None and end_time > start_time:
        opts["external_downloader"] = "ffmpeg"
        opts["external_downloader_args"] = {
            "ffmpeg_i": [
                "-ss", str(start_time),
                "-to", str(end_time),
            ]
        }
        opts["download_sections"] = [{
            "start_time": start_time,
            "end_time": end_time,
        }]
        opts["force_keyframes_at_cuts"] = True

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        file_path = Path(ydl.prepare_filename(info)).resolve()

    if file_path.parent != UPLOAD_DIR.resolve() or not file_path.exists():
        raise RuntimeError("yt-dlp did not produce a local file")

    append_event("yt-dlp download complete", event_type="process", details={"file": file_path.name})
    return file_path


def _download_direct(url: str) -> Path:
    parsed = urlparse(url)
    fallback_name = f"download-{int(time.time())}.mp4"
    local_name = _safe_name(parsed.path, fallback_name)
    if "." not in local_name:
        local_name = f"{local_name}.mp4"

    file_path = (UPLOAD_DIR / local_name).resolve()
    if file_path.parent != UPLOAD_DIR.resolve():
        raise RuntimeError("invalid download path")

    append_event("Downloader selected: direct HTTP", event_type="process", details={"file": local_name})

    with requests.get(url, stream=True, timeout=30) as response:
        response.raise_for_status()
        content_type = (response.headers.get("content-type") or "").lower()
        if content_type.startswith("text/") or "html" in content_type:
            raise RuntimeError(f"downloaded content is not video (content-type: {content_type})")
        total = int(response.headers.get("content-length", 0))
        downloaded = 0

        with file_path.open("wb") as out:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                if _cancel_requested():
                    _safe_unlink(file_path)
                    raise CancelledError("download cancelled")

                out.write(chunk)
                downloaded += len(chunk)
                if total > 0:
                    update_state({"progress": int((downloaded * 100) / total)})

    if not file_path.exists() or file_path.stat().st_size == 0:
        raise RuntimeError("downloaded file is empty")
    try:
        with file_path.open("rb") as fh:
            head = fh.read(512).lstrip().lower()
            if head.startswith(b"<!doctype") or head.startswith(b"<html"):
                raise RuntimeError("downloaded content is HTML, not video")
    except OSError:
        pass

    return file_path


def _trim_video(
    source_path: Path,
    output_dir: Path,
    *,
    start_time: int,
    end_time: int,
    check_cancel,
) -> Path:
    if end_time <= start_time:
        raise RuntimeError("invalid trim range")

    output_dir.mkdir(parents=True, exist_ok=True)
    target_path = (output_dir / f"{source_path.stem}_trim_{start_time}_{end_time}.mp4").resolve()

    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        str(start_time),
        "-to",
        str(end_time),
        "-i",
        str(source_path),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-movflags",
        "+faststart",
        "-c:a",
        "aac",
        str(target_path),
    ]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    try:
        if proc.stderr is not None:
            for _ in proc.stderr:
                if check_cancel():
                    proc.kill()
                    raise CancelledError("trim cancelled")
    finally:
        proc.wait()

    if proc.returncode != 0 or not target_path.exists():
        raise RuntimeError("trim failed")

    return target_path


def _remux_to_mp4(source_path: Path, output_dir: Path, *, check_cancel) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    target_path = (output_dir / f"{source_path.stem}_remux.mp4").resolve()

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(source_path),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(target_path),
    ]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    try:
        if proc.stderr is not None:
            for _ in proc.stderr:
                if check_cancel():
                    proc.kill()
                    raise CancelledError("remux cancelled")
    finally:
        proc.wait()

    if proc.returncode != 0 or not target_path.exists():
        raise RuntimeError("remux failed")

    return target_path


@app.middleware("http")
async def log_requests(request: Request, call_next):
    path = request.url.path
    skip_prefixes = ("/static",)
    should_log = path not in {"/state", "/health"} and not path.startswith(skip_prefixes)

    started_at = time.perf_counter()
    if should_log:
        append_event(
            f"{request.method} {path} started",
            event_type="request",
            details={"query": str(request.url.query)}
        )

    try:
        response = await call_next(request)
    except Exception as exc:
        _server_logger.exception("%s %s failed", request.method, path)
        if should_log:
            append_event(
                f"{request.method} {path} failed: {exc}",
                event_type="request",
                level="error",
            )
        raise

    if should_log:
        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        _server_logger.info("%s %s -> %s in %dms", request.method, path, response.status_code, elapsed_ms)
        append_event(
            f"{request.method} {path} -> {response.status_code}",
            event_type="request",
            details={"elapsed_ms": elapsed_ms}
        )

    return response


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/video/{filename}")
async def get_video(filename: str):
    file_path = (UPLOAD_DIR / filename).resolve()
    if file_path.parent != UPLOAD_DIR.resolve() or not file_path.exists():
        raise HTTPException(status_code=404, detail="video not found")
    return FileResponse(path=file_path)


@app.get("/converted/{filename}")
async def get_converted_video(filename: str):
    file_path = (CONVERTED_DIR / filename).resolve()
    if file_path.parent != CONVERTED_DIR.resolve() or not file_path.exists():
        raise HTTPException(status_code=404, detail="converted video not found")
    return FileResponse(path=file_path)


@app.get("/state")
async def get_state():
    return _reconcile_runtime_state()


@app.get("/state/probe")
async def get_state_probe():
    state = _reconcile_runtime_state()
    return {
        "probe_runtime_id": state.get("probe_runtime_id"),
        "probe_pid": state.get("probe_pid"),
        "probe_persist_id": state.get("probe_persist_id"),
        "probe_startups": state.get("probe_startups"),
    }


@app.get("/state/stream")
async def state_stream(request: Request):
    async def _events():
        state = _reconcile_runtime_state()
        last_version = get_state_version()
        yield f"event: state\ndata: {json.dumps(state, ensure_ascii=False)}\n\n"

        while True:
            if await request.is_disconnected():
                break
            next_version = await asyncio.to_thread(wait_for_state_change, last_version, 20.0)
            if next_version <= last_version:
                yield "event: ping\ndata: {}\n\n"
                continue
            state = _reconcile_runtime_state()
            last_version = get_state_version()
            yield f"event: state\ndata: {json.dumps(state, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        _events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/state")
async def patch_state(payload: dict):
    allowed = {"results_text", "ui", "playback"}
    patch = {k: payload[k] for k in allowed if k in payload}
    if not patch:
        return JSONResponse({"error": "no allowed fields"}, status_code=400)

    if "ui" in patch and not isinstance(patch["ui"], dict):
        return JSONResponse({"error": "ui must be object"}, status_code=400)
    if "playback" in patch:
        playback = patch["playback"]
        if not isinstance(playback, dict):
            return JSONResponse({"error": "playback must be object"}, status_code=400)
        source = playback.get("source")
        position = playback.get("position")
        if source is not None and not isinstance(source, str):
            return JSONResponse({"error": "playback.source must be string or null"}, status_code=400)
        if not isinstance(position, (int, float)):
            return JSONResponse({"error": "playback.position must be number"}, status_code=400)
        patch["playback"] = {"source": source, "position": max(0, float(position))}

    update_state(patch)
    return {"status": "ok"}


@app.post("/state/reset")
async def reset_state(payload: dict | None = None):
    if _worker_active():
        return JSONResponse({"error": "cannot reset during active process"}, status_code=409)

    clear_events = bool((payload or {}).get("clear_events"))
    _reset_state(clear_events=clear_events)
    append_event("State reset requested from UI", event_type="event", level="warning")
    return {"status": "ok"}


@app.post("/upload")
async def upload_video(file: UploadFile = File(...)):
    file_name = _safe_name(file.filename, "upload.bin")
    file_path = UPLOAD_DIR / file_name

    update_state({
        "phase": "uploading",
        "progress": 0,
        "phase_started_at": time.time(),
        "processing": True,
        "cancel_requested": False,
    })
    append_event("Upload started", event_type="process", details={"file": file_name})

    total_read = 0
    try:
        with file_path.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                total_read += len(chunk)
                if total_read > MAX_UPLOAD_BYTES:
                    out.close()
                    _safe_unlink(file_path)
                    update_state({
                        "phase": "error",
                        "processing": False,
                        "progress": 0,
                        "phase_started_at": time.time(),
                    })
                    append_event(
                        "Upload rejected: file exceeds 2GB",
                        event_type="process",
                        level="error",
                    )
                    return JSONResponse({"error": "file too large"}, status_code=413)

                out.write(chunk)
                if total_read % (20 * 1024 * 1024) == 0:
                    append_event(
                        f"Upload progress: {total_read // (1024 * 1024)} MB",
                        event_type="process"
                    )
    except Exception as exc:
        update_state({"phase": "error", "processing": False, "progress": 0, "phase_started_at": time.time()})
        append_event(f"Upload failed: {exc}", event_type="process", level="error")
        return JSONResponse({"error": "upload failed"}, status_code=500)

    try:
        validate_video_file(file_path)
    except ProcessingError as exc:
        _safe_unlink(file_path)
        patch = {"phase": "error", "processing": False, "progress": 0, "phase_started_at": time.time()}
        if load_state().get("video") == file_name:
            patch["video"] = None
            patch["converted"] = None
            patch["video_bytes"] = None
            patch["converted_bytes"] = None
        update_state(patch)
        append_event(f"Upload failed: {exc}", event_type="process", level="error")
        return JSONResponse({"error": "uploaded file is not a valid video"}, status_code=400)

    update_state({
        "video": file_name,
        "converted": None,
        "protocol_csv": load_state().get("protocol_csv"),
        "phase": "uploaded",
        "progress": 100,
        "phase_started_at": time.time(),
        "processing": False,
        "cancel_requested": False,
        "video_bytes": file_path.stat().st_size,
        "converted_bytes": None,
        "bboxes": [],
        "timestamps": [],
        "results_text": "",
    })
    append_event("Upload complete", event_type="process", details={"file": file_name})

    return {"status": "ok", "filename": file_name}


@app.post("/protocol/upload")
async def upload_protocol(file: UploadFile = File(...)):
    file_name = _safe_name(file.filename, "protocol.csv")
    if not file_name.lower().endswith((".csv", ".txt")):
        return JSONResponse({"error": "protocol file must be .csv or .txt"}, status_code=400)

    path = (PROTOCOL_DIR / file_name).resolve()
    if path.parent != PROTOCOL_DIR.resolve():
        return JSONResponse({"error": "invalid protocol filename"}, status_code=400)

    data = await file.read()
    if not data:
        return JSONResponse({"error": "empty protocol file"}, status_code=400)

    path.write_bytes(data)
    update_state({"protocol_csv": file_name})
    append_event("Protocol file uploaded", event_type="process", details={"file": file_name})
    return {"status": "ok", "filename": file_name}


@app.post("/video/clear")
async def clear_video():
    if _worker_active():
        return JSONResponse({"error": "cannot clear during active process"}, status_code=409)

    update_state({
        "video": None,
        "converted": None,
        "video_bytes": None,
        "converted_bytes": None,
        "bboxes": [],
        "timestamps": [],
        "results_text": "",
        "progress": 0,
        "phase": "idle",
        "processing": False,
        "cancel_requested": False,
        "phase_started_at": None,
        "playback": {"source": None, "position": 0},
    })
    append_event("Video cleared from UI", event_type="event", level="warning")
    return {"status": "ok"}


@app.post("/protocol/clear")
async def clear_protocol():
    if _worker_active():
        return JSONResponse({"error": "cannot clear during active process"}, status_code=409)

    update_state({"protocol_csv": None})
    append_event("Protocol file cleared from UI", event_type="event", level="warning")
    return {"status": "ok"}


def _download_worker(url: str, *, start_time: int | None = None, end_time: int | None = None):
    try:
        update_state({
            "phase": "downloading",
            "progress": 0,
            "phase_started_at": time.time(),
            "processing": True,
            "cancel_requested": False,
        })
        append_event("Download started", event_type="process", details={"url": url})

        host = (urlparse(url).hostname or "").lower()
        use_ytdlp = yt_dlp is not None

        try:
            if use_ytdlp:
                file_path = _download_with_ytdlp(url, start_time=start_time, end_time=end_time)
            else:
                file_path = _download_direct(url)
        except Exception as primary_error:
            if use_ytdlp:
                append_event(
                    f"yt-dlp failed, fallback to direct download: {primary_error}",
                    event_type="process",
                    level="warning",
                )
                file_path = _download_direct(url)
            else:
                raise

        if start_time is not None and end_time is not None and not use_ytdlp:
            append_event(
                "Trimming downloaded file with ffmpeg",
                event_type="process",
                details={"start": start_time, "end": end_time},
            )
            trimmed = _trim_video(
                file_path,
                CONVERTED_DIR,
                start_time=start_time,
                end_time=end_time,
                check_cancel=_cancel_requested,
            )
            file_path = trimmed
        try:
            validate_video_file(file_path)
        except ProcessingError as exc:
            if not use_ytdlp:
                append_event(
                    "Direct download invalid, attempting ffmpeg remux",
                    event_type="process",
                    level="warning",
                )
                remuxed = _remux_to_mp4(file_path, CONVERTED_DIR, check_cancel=_cancel_requested)
                validate_video_file(remuxed)
                file_path = remuxed
            else:
                raise exc

        update_state({
            "video": file_path.name,
            "converted": None,
            "protocol_csv": load_state().get("protocol_csv"),
            "phase": "downloaded",
            "progress": 100,
            "phase_started_at": time.time(),
            "processing": False,
            "cancel_requested": False,
            "video_bytes": file_path.stat().st_size,
            "converted_bytes": None,
            "bboxes": [],
            "timestamps": [],
            "results_text": "",
        })
        append_event("Download complete", event_type="process", details={"file": file_path.name})

    except CancelledError:
        update_state({"phase": "idle", "progress": 0, "phase_started_at": None, "processing": False, "cancel_requested": False})
        append_event("Download cancelled", event_type="process", level="warning")
    except Exception as exc:
        update_state({"phase": "error", "processing": False, "phase_started_at": time.time()})
        append_event(f"Download failed: {exc}", event_type="process", level="error")
    finally:
        _clear_worker()


@app.post("/download")
async def download_video(payload: dict):
    if _worker_active():
        return JSONResponse({"error": "another process is running"}, status_code=409)

    url = str(payload.get("url", "")).strip()
    if not url:
        return JSONResponse({"error": "no url"}, status_code=400)

    def _parse_int(name: str) -> int | None:
        raw = payload.get(name)
        if raw is None or raw == "":
            return None
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None
        return max(0, value)

    start_time = _parse_int("start_time")
    end_time = _parse_int("end_time")
    if start_time is not None and end_time is not None and end_time <= start_time:
        return JSONResponse({"error": "end_time must be greater than start_time"}, status_code=400)

    worker = Thread(target=_download_worker, args=(url,), kwargs={"start_time": start_time, "end_time": end_time}, daemon=True)
    _set_worker(worker)
    worker.start()
    return {"status": "accepted"}


def _processing_worker(source_name: str, settings: dict):
    try:
        source_path = (UPLOAD_DIR / source_name).resolve()
        if source_path.parent != UPLOAD_DIR.resolve() or not source_path.exists():
            raise FileNotFoundError("source video is missing")
        state = load_state()
        protocol_name = state.get("protocol_csv")
        if not protocol_name:
            raise ProcessingError("upload protocol file (.csv or .txt) before processing")

        protocol_path = (PROTOCOL_DIR / protocol_name).resolve()
        if protocol_path.parent != PROTOCOL_DIR.resolve() or not protocol_path.exists():
            raise ProcessingError("protocol file not found on disk")

        update_state({
            "phase": "converting",
            "progress": 0,
            "phase_started_at": time.time(),
            "processing": True,
            "cancel_requested": False,
            "bboxes": [],
            "timestamps": [],
            "results_text": "",
            "settings": settings,
        })
        append_event("Pipeline started", event_type="process", details={"video": source_name})

        analysis_path, was_converted = ensure_playable_input(
            source_path,
            CONVERTED_DIR,
            check_cancel=_cancel_requested,
            progress_cb=lambda p: update_state({"progress": p}),
            event_cb=lambda msg: append_event(msg, event_type="process"),
        )

        if _cancel_requested():
            raise CancelledError("cancelled after conversion")

        update_state({
            "phase": "converted",
            "progress": 100,
            "phase_started_at": time.time(),
            "converted": analysis_path.name if was_converted else None,
            "converted_bytes": analysis_path.stat().st_size if was_converted else None,
        })

        update_state({"phase": "processing", "progress": 0, "phase_started_at": time.time()})
        def _progress_cb(p: int):
            update_state({"progress": p})

        def _partial_cb(patch: dict):
            update_state(patch)

        analysis = run_protocol_analysis(
            analysis_path,
            protocol_path,
            MODEL_PATH,
            settings=settings,
            partial_cb=_partial_cb,
            check_cancel=_cancel_requested,
            progress_cb=_progress_cb,
            event_cb=lambda msg: append_event(msg, event_type="process"),
        )

        if _cancel_requested():
            raise CancelledError("cancelled before finishing")

        update_state({
            "phase": "done",
            "progress": 100,
            "phase_started_at": time.time(),
            "processing": False,
            "cancel_requested": False,
            "converted_bytes": analysis_path.stat().st_size if was_converted else None,
            **analysis,
        })
        append_event("Pipeline done", event_type="process")

    except CancelledError:
        update_state({"phase": "idle", "progress": 0, "phase_started_at": None, "processing": False, "cancel_requested": False})
        append_event("Pipeline cancelled", event_type="process", level="warning")
    except FileNotFoundError:
        update_state({"phase": "error", "processing": False, "phase_started_at": time.time()})
        append_event("Pipeline failed: source video not found", event_type="process", level="error")
    except ProcessingError as exc:
        update_state({"phase": "error", "processing": False, "phase_started_at": time.time()})
        append_event(f"Pipeline failed: {exc}", event_type="process", level="error")
    except Exception as exc:
        update_state({"phase": "error", "processing": False, "phase_started_at": time.time()})
        append_event(f"Pipeline failed: {exc}", event_type="process", level="error")
    finally:
        _clear_worker()


def _parse_settings(payload: dict | None) -> dict:
    raw = (payload or {}).get("settings") or {}

    def _int(name: str, default: int, min_value: int, max_value: int) -> int:
        value = raw.get(name, default)
        try:
            value = int(value)
        except (TypeError, ValueError):
            return default
        return max(min_value, min(max_value, value))

    return {
        "frame_interval_sec": _int("frame_interval_sec", DEFAULT_SETTINGS["frame_interval_sec"], 1, 30),
        "conf_limit": _int("conf_limit", DEFAULT_SETTINGS["conf_limit"], 1, 10),
        "session_timeout_sec": _int("session_timeout_sec", DEFAULT_SETTINGS["session_timeout_sec"], 10, 3600),
        "phantom_timeout_sec": _int("phantom_timeout_sec", DEFAULT_SETTINGS["phantom_timeout_sec"], 5, 3600),
    }


@app.post("/process/start")
async def start_processing(payload: dict | None = None):
    if _worker_active():
        return JSONResponse({"error": "another process is running"}, status_code=409)

    state = load_state()
    settings = _parse_settings(payload)
    source_name = state.get("video")
    if not source_name:
        return JSONResponse({"error": "no video selected"}, status_code=400)
    source_path = (UPLOAD_DIR / source_name).resolve()
    if source_path.parent != UPLOAD_DIR.resolve() or not source_path.exists():
        update_state({
            "phase": "error",
            "processing": False,
            "progress": 0,
            "phase_started_at": time.time(),
            "video": None,
        })
        append_event("Pipeline failed: selected video not found", event_type="process", level="error")
        return JSONResponse({"error": "selected video not found"}, status_code=400)
    try:
        validate_video_file(source_path)
    except ProcessingError as exc:
        update_state({"phase": "error", "processing": False, "progress": 0, "phase_started_at": time.time(), "video": None, "converted": None})
        append_event(f"Pipeline failed: {exc}", event_type="process", level="error")
        return JSONResponse({"error": "selected file is not a valid video"}, status_code=400)
    protocol_name = state.get("protocol_csv")
    if not protocol_name:
        return JSONResponse({"error": "no protocol file uploaded"}, status_code=400)
    protocol_path = (PROTOCOL_DIR / protocol_name).resolve()
    if protocol_path.parent != PROTOCOL_DIR.resolve() or not protocol_path.exists():
        return JSONResponse({"error": "protocol file not found"}, status_code=400)

    update_state({
        "phase": "converting",
        "progress": 0,
        "phase_started_at": time.time(),
        "processing": True,
        "cancel_requested": False,
        "bboxes": [],
        "timestamps": [],
        "results_text": "",
        "settings": settings,
    })
    append_event("Pipeline started", event_type="process", details={"video": source_name})

    ctx = mp.get_context("spawn")
    queue = ctx.Queue()
    cancel_event = ctx.Event()
    process = ctx.Process(
        target=_processing_worker_process,
        args=(str(source_path), str(protocol_path), str(MODEL_PATH), settings, queue, cancel_event),
        daemon=True,
    )
    process.start()
    listener = _start_process_listener(queue, process)
    _set_process_worker(process, queue, cancel_event, listener)
    return {"status": "accepted"}


@app.post("/process/cancel")
async def cancel_processing():
    state = load_state()
    if not _worker_active():
        if bool(state.get("cancel_requested")):
            return {"status": "accepted", "detail": "cancellation already requested"}
        return {"status": "accepted", "detail": "no active process"}

    already_requested = bool(state.get("cancel_requested"))
    update_state({"cancel_requested": True, "phase": "cancelling", "phase_started_at": time.time()})
    append_event("Cancellation requested", event_type="process", level="warning")

    with _process_lock:
        process = _process_worker
        cancel_event = _process_cancel

    if cancel_event is not None:
        cancel_event.set()

    if already_requested and process is not None and process.is_alive():
        process.terminate()
        process.join(timeout=2)
        _clear_process_worker()
        update_state({
            "phase": "idle",
            "progress": 0,
            "phase_started_at": None,
            "processing": False,
            "cancel_requested": False,
        })
        append_event("Process force-terminated after repeated cancel", event_type="process", level="warning")
    return {"status": "accepted"}

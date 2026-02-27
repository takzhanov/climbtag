import json
import logging
import os
import uuid
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Condition, RLock

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_FILE = BASE_DIR / "state.json"
LOG_DIR = BASE_DIR / "logs"
EVENTS_LOG_FILE = LOG_DIR / "state-events.log"
LOG_DIR.mkdir(parents=True, exist_ok=True)

_lock = RLock()
_state_changed = Condition(_lock)
_runtime_state: dict | None = None
_state_version = 0
_process_boot_id = uuid.uuid4().hex
_ALLOWED_SETTINGS = {
    "frame_interval_sec",
    "conf_limit",
    "session_timeout_sec",
    "phantom_timeout_sec",
}

_event_logger = logging.getLogger("climbtag.events")
if not _event_logger.handlers:
    _event_logger.setLevel(logging.INFO)
    _event_logger.propagate = False
    _handler = RotatingFileHandler(EVENTS_LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    _event_logger.addHandler(_handler)


def _default_state():
    return {
        "video": None,
        "converted": None,
        "protocol_csv": None,
        "processing": False,
        "phase": "idle",
        "progress": 0,
        "phase_started_at": None,
        "cancel_requested": False,
        "results_text": "",
        "playback": {
            "source": None,
            "position": 0
        },
        "video_bytes": None,
        "converted_bytes": None,
        "bboxes": [],
        "timestamps": [],
        "events": [],
        "settings": {
            "frame_interval_sec": 3,
            "conf_limit": 3,
            "session_timeout_sec": 360,
            "phantom_timeout_sec": 60,
        },
        "ui": {
            "sidebar_hidden": False,
            "right_panel_collapsed": True,
            "sidebar_pinned": True,
            "right_panel_pinned": False,
            "events_open": True,
            "state_open": False,
        },
        "probe_runtime_id": _process_boot_id,
        "probe_pid": os.getpid(),
        "probe_persist_id": None,
        "probe_startups": 0,
    }


def _persisted_state(state: dict) -> dict:
    """Persist lightweight UI/runtime hints across restarts."""
    raw_settings = state.get("settings") if isinstance(state.get("settings"), dict) else {}
    settings = {k: raw_settings[k] for k in _ALLOWED_SETTINGS if k in raw_settings}
    ui = state.get("ui") if isinstance(state.get("ui"), dict) else {}
    playback = state.get("playback") if isinstance(state.get("playback"), dict) else {"source": None, "position": 0}
    timestamps = state.get("timestamps") if isinstance(state.get("timestamps"), list) else []
    return {
        "settings": settings,
        "ui": ui,
        "playback": {
            "source": playback.get("source"),
            "position": float(playback.get("position", 0) or 0),
        },
        "video": state.get("video"),
        "converted": state.get("converted"),
        "protocol_csv": state.get("protocol_csv"),
        "video_bytes": state.get("video_bytes"),
        "converted_bytes": state.get("converted_bytes"),
        "results_text": state.get("results_text", ""),
        "timestamps": timestamps,
        "probe_persist_id": state.get("probe_persist_id"),
        "probe_startups": int(state.get("probe_startups", 0) or 0),
    }


def load_state():
    with _lock:
        global _runtime_state
        if _runtime_state is not None:
            return dict(_runtime_state)

        default = _default_state()

        if not STATE_FILE.exists():
            _runtime_state = dict(default)
            save_state(_runtime_state)
            return dict(_runtime_state)

        try:
            loaded = json.loads(STATE_FILE.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            loaded = {}

        if not isinstance(loaded, dict):
            loaded = {}

        # Only settings are persisted between runs.
        settings = loaded.get("settings", {})
        if not isinstance(settings, dict):
            settings = {}
        settings = {k: settings[k] for k in _ALLOWED_SETTINGS if k in settings}
        state = dict(default)
        state["settings"] = {**default["settings"], **settings}
        state["probe_runtime_id"] = _process_boot_id
        state["probe_pid"] = os.getpid()
        persisted_probe = loaded.get("probe_persist_id")
        if not isinstance(persisted_probe, str) or not persisted_probe:
            persisted_probe = uuid.uuid4().hex
        loaded_startups = loaded.get("probe_startups", 0)
        if not isinstance(loaded_startups, int):
            loaded_startups = 0
        state["probe_persist_id"] = persisted_probe
        state["probe_startups"] = loaded_startups + 1
        if isinstance(loaded.get("ui"), dict):
            state["ui"] = {**default["ui"], **loaded["ui"]}
        if isinstance(loaded.get("playback"), dict):
            source = loaded["playback"].get("source")
            position = loaded["playback"].get("position", 0)
            state["playback"] = {
                "source": source if isinstance(source, str) else None,
                "position": float(position) if isinstance(position, (int, float)) else 0,
            }
        for field in ("video", "converted", "protocol_csv", "video_bytes", "converted_bytes", "results_text"):
            if field in loaded:
                state[field] = loaded.get(field)
        if isinstance(loaded.get("timestamps"), list):
            state["timestamps"] = loaded["timestamps"]
        _runtime_state = state

        if _persisted_state(state) != loaded:
            save_state(state)

        return dict(_runtime_state)


def save_state(state: dict):
    with _lock:
        global _state_version
        STATE_FILE.write_text(json.dumps(_persisted_state(state), indent=2, ensure_ascii=False), encoding="utf-8")
        _state_version += 1
        _state_changed.notify_all()


def update_state(patch: dict):
    with _lock:
        state = load_state()
        state.update(patch)
        global _runtime_state
        _runtime_state = dict(state)
        save_state(state)
        return dict(_runtime_state)


def append_event(
    message: str,
    *,
    event_type: str = "event",
    level: str = "info",
    details: dict | None = None
):
    with _lock:
        state = load_state()
        events = state.get("events", [])

        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": event_type,
            "level": level,
            "message": message
        }

        if details:
            entry["details"] = details

        events.append(entry)
        state["events"] = events[-300:]
        global _runtime_state
        _runtime_state = dict(state)
        save_state(state)
        log_level = entry["level"].upper()
        if log_level == "ERROR":
            _event_logger.error("[%s] %s", entry["type"], entry["message"])
        elif log_level == "WARNING":
            _event_logger.warning("[%s] %s", entry["type"], entry["message"])
        else:
            _event_logger.info("[%s] %s", entry["type"], entry["message"])
        return entry


def get_state_version() -> int:
    with _lock:
        return _state_version


def wait_for_state_change(since_version: int, timeout_sec: float = 20.0) -> int:
    with _lock:
        if _state_version > since_version:
            return _state_version
        _state_changed.wait(timeout=timeout_sec)
        return _state_version

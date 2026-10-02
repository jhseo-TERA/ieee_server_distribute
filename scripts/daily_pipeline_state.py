# -*- coding: utf-8 -*-
"""Persistent checkpoints for the daily PDF and Zotero pipeline."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "logs" / "pdf_download_state"
STATE_PATH = STATE_DIR / "daily_pipeline.json"
POST_LOCK_PATH = STATE_DIR / "daily_pipeline.lock"
POST_STAGES = ("zotero_metadata", "zotero_pdf_attachments", "obsidian")


def utcnow_text() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_pipeline(path: Path = STATE_PATH) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_pipeline(state: dict[str, Any], path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary, path)


def begin_pipeline(run_id: str, run_date: str, *, path: Path = STATE_PATH) -> dict[str, Any]:
    now = utcnow_text()
    state: dict[str, Any] = {
        "version": 1,
        "run_id": run_id,
        "date": run_date,
        "status": "running",
        "started_at": now,
        "updated_at": now,
        "stages": {
            "download": {"status": "running", "started_at": now},
            **{stage: {"status": "pending"} for stage in POST_STAGES},
        },
    }
    save_pipeline(state, path)
    return state


def update_stage(
    stage: str,
    status: str,
    *,
    path: Path = STATE_PATH,
    run_id: str | None = None,
    return_code: int | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state = load_pipeline(path)
    if not state:
        raise RuntimeError("daily pipeline checkpoint does not exist")
    if run_id is not None and state.get("run_id") != run_id:
        raise RuntimeError("daily pipeline checkpoint belongs to another run")
    if stage not in ("download", *POST_STAGES):
        raise ValueError(f"unknown daily pipeline stage: {stage}")

    now = utcnow_text()
    stage_state = state.setdefault("stages", {}).setdefault(stage, {})
    stage_state["status"] = status
    if status == "running":
        stage_state["started_at"] = now
        stage_state.pop("completed_at", None)
        stage_state.pop("failed_at", None)
    elif status in {"completed", "skipped"}:
        stage_state["completed_at"] = now
        stage_state.pop("failed_at", None)
    elif status in {"failed", "deferred"}:
        stage_state[f"{status}_at"] = now
    if return_code is not None:
        stage_state["return_code"] = int(return_code)
    if details:
        stage_state.update(details)

    if status == "failed":
        state["status"] = "failed"
        state["failed_stage"] = stage
    elif status == "deferred":
        state["status"] = "deferred"
        state["deferred_stage"] = stage
    elif stage == "download" and status == "completed":
        state["status"] = "download_completed"
        state.pop("failed_stage", None)
        state.pop("deferred_stage", None)
    elif stage in POST_STAGES and status == "running":
        state["status"] = "postprocess_running"
        if state.get("failed_stage") == stage:
            state.pop("failed_stage", None)

    if all(
        state.get("stages", {}).get(name, {}).get("status") in {"completed", "skipped"}
        for name in ("download", *POST_STAGES)
    ):
        state["status"] = "completed"
        state["completed_at"] = now
        state.pop("failed_stage", None)
        state.pop("deferred_stage", None)

    state["updated_at"] = now
    save_pipeline(state, path)
    return state

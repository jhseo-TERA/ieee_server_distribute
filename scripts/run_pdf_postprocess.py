# -*- coding: utf-8 -*-
"""Run or resume the checkpointed Zotero/Obsidian post-processing chain."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

try:
    from .daily_pipeline_state import (
        POST_LOCK_PATH,
        POST_STAGES,
        load_pipeline,
        update_stage,
    )
except ImportError:  # Direct execution from the scripts directory.
    from daily_pipeline_state import (
        POST_LOCK_PATH,
        POST_STAGES,
        load_pipeline,
        update_stage,
    )


ROOT = Path(__file__).resolve().parent.parent
SCRIPT_DIR = ROOT / "scripts"
EXIT_SKIPPED_LOCKED = 75
STALE_LOCK_SECONDS = 6 * 3600
STAGE_COMMANDS = {
    "zotero_metadata": SCRIPT_DIR / "sync_zotero_favorites.bat",
    "zotero_pdf_attachments": SCRIPT_DIR / "sync_zotero_pdf_attachments.bat",
    "obsidian": SCRIPT_DIR / "zotero_tag_ingest.bat",
}
STAGE_LABELS = {
    "zotero_metadata": "Zotero metadata/collection sync",
    "zotero_pdf_attachments": "Zotero linked PDF attachment sync",
    "obsidian": "Zotero tag ingest to Obsidian",
}


def zotero_enabled() -> bool:
    config_path = ROOT / "config" / "pdf_postprocess.json"
    if not config_path.exists():
        return True
    config = json.loads(config_path.read_text(encoding="utf-8"))
    return config.get("zotero_enabled", True) is not False


def acquire_post_lock(path: Path = POST_LOCK_PATH) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            age = 0
        if age > STALE_LOCK_SECONDS:
            try:
                path.unlink()
            except OSError:
                return False
            return acquire_post_lock(path)
        return False
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(f"pid={os.getpid()}\nstarted={datetime.now().astimezone().isoformat()}\n")
    return True


def run_stage(command: Path) -> int:
    comspec = os.environ.get("COMSPEC", "cmd.exe")
    completed = subprocess.run(
        [comspec, "/d", "/c", str(command)], cwd=ROOT, check=False
    )
    return int(completed.returncode)


def run_postprocess(*, expected_date: str | None = None, resume: bool = False) -> int:
    state = load_pipeline()
    if not state:
        print("[FAIL] Daily pipeline checkpoint does not exist.")
        return 2
    if expected_date and state.get("date") != expected_date:
        print(
            f"[FAIL] Checkpoint date is {state.get('date')}; requested {expected_date}."
        )
        return 2
    if state.get("stages", {}).get("download", {}).get("status") != "completed":
        print("[FAIL] Download stage is not completed; post-processing will not run.")
        return 3
    # Apply the same rule to direct execution and --resume, not only the BAT.
    acquired = state["stages"]["download"].get("successful_downloads", 0)
    if type(acquired) is not int or acquired <= 0:
        print("[OUTCOME] IDLE: no verified PDF was downloaded; post-processing skipped.")
        return 0
    if not acquire_post_lock():
        print("[OUTCOME] SKIPPED_LOCKED: post-processing is already running.")
        return EXIT_SKIPPED_LOCKED

    expected_run_id = state.get("run_id")
    try:
        state = load_pipeline()
        if state.get("run_id") != expected_run_id:
            print("[FAIL] Download checkpoint changed before post-processing.")
            return 3
        run_id = str(state.get("run_id") or "")
        mode = "resume" if resume else "normal"
        print(f"[CHECKPOINT] Post-processing mode={mode}, run_id={run_id}")
        for stage in POST_STAGES:
            state = load_pipeline()
            if state.get("run_id") != run_id:
                print("[FAIL] Download checkpoint changed during post-processing.")
                return 3
            current = state.get("stages", {}).get(stage, {}).get("status")
            if current == "completed":
                print(f"[CHECKPOINT] {STAGE_LABELS[stage]} already completed; skipped.")
                continue

            if not zotero_enabled():
                update_stage(stage, "skipped", run_id=run_id, return_code=0,
                             details={"reason": "zotero_disabled_by_user"})
                print(f"[SKIP] {STAGE_LABELS[stage]}: Zotero integration disabled")
                continue
            print(f"[POST] {STAGE_LABELS[stage]} started")
            update_stage(stage, "running", run_id=run_id)
            result = run_stage(STAGE_COMMANDS[stage])
            if result:
                update_stage(stage, "failed", run_id=run_id, return_code=result)
                print(f"[FAIL] {STAGE_LABELS[stage]} returned {result}")
                return result
            update_stage(stage, "completed", run_id=run_id, return_code=0)
            print(f"[OK] {STAGE_LABELS[stage]} completed")
        return 0
    finally:
        try:
            POST_LOCK_PATH.unlink()
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--date", help="checkpoint date to resume (YYYY-MM-DD)")
    args = parser.parse_args()
    return run_postprocess(expected_date=args.date, resume=args.resume)


if __name__ == "__main__":
    raise SystemExit(main())

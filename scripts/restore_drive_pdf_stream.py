"""Receive Google Drive PDF payloads over stdin without exposing their contents.

Protocol: one JSON metadata line followed by one base64 payload line.  The
receiver never overwrites an existing file.  Identical files are skipped;
different collisions are preserved beside the original with a Drive-ID suffix.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import json
import msvcrt
import os
import sys
from pathlib import Path


def _set_console_echo(enabled: bool, previous_mode: int | None = None) -> int | None:
    handle = msvcrt.get_osfhandle(sys.stdin.fileno())
    kernel32 = ctypes.windll.kernel32
    mode = ctypes.c_uint()
    if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        return None
    if previous_mode is None:
        previous_mode = mode.value
    new_mode = previous_mode if enabled else mode.value & ~0x0004
    kernel32.SetConsoleMode(handle, new_mode)
    return previous_mode


def _safe_destination(root: Path, relative_path: str) -> Path:
    candidate = (root / Path(relative_path.replace("/", os.sep))).resolve()
    if os.path.commonpath((str(root), str(candidate))) != str(root):
        raise ValueError("destination escapes the restore root")
    return candidate


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _existing_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _collision_path(path: Path, drive_id: str) -> Path:
    suffix = "".join(ch for ch in drive_id if ch.isalnum() or ch in "-_")[:24]
    return path.with_name(f"{path.stem}.drive-{suffix}{path.suffix}")


def _write_manifest(root: Path, manifest_name: str, record: dict) -> None:
    manifest = root / manifest_name
    with manifest.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def receive_one(root: Path, manifest_name: str, metadata: dict, encoded: bytes) -> dict:
    relative_path = str(metadata["relative_path"])
    drive_id = str(metadata["drive_id"])
    expected_size = int(metadata.get("size") or 0)
    data = base64.b64decode(encoded.strip(), validate=True)

    if expected_size and len(data) != expected_size:
        raise ValueError(f"size mismatch: expected {expected_size}, got {len(data)}")
    if not data.startswith(b"%PDF-"):
        raise ValueError("payload does not have a PDF header")

    destination = _safe_destination(root, relative_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = _sha256(data)
    status = "written"

    if destination.exists():
        if _existing_sha256(destination) == digest:
            status = "identical"
        else:
            destination = _collision_path(destination, drive_id)
            if destination.exists():
                if _existing_sha256(destination) == digest:
                    status = "identical_collision"
                else:
                    raise FileExistsError(f"unresolved collision: {destination}")
            else:
                status = "collision_preserved"

    if status in {"written", "collision_preserved"}:
        with destination.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

    record = {
        "drive_id": drive_id,
        "modified_time": metadata.get("modified_time"),
        "path": str(destination),
        "relative_path": relative_path,
        "sha256": digest,
        "size": len(data),
        "status": status,
    }
    _write_manifest(root, manifest_name, record)
    return record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--manifest-name", default="restore_manifest.jsonl")
    args = parser.parse_args()

    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    previous_mode = _set_console_echo(False)
    print(json.dumps({"ready": True, "root": str(root)}), flush=True)

    try:
        while True:
            header = sys.stdin.buffer.readline()
            if not header:
                break
            if header.strip() == b"__QUIT__":
                print(json.dumps({"done": True}), flush=True)
                break

            metadata = {}
            try:
                metadata = json.loads(header.decode("utf-8"))
                encoded = sys.stdin.buffer.readline()
                if not encoded:
                    raise EOFError("missing base64 payload")
                record = receive_one(root, args.manifest_name, metadata, encoded)
                print(json.dumps({"ok": True, **record}, ensure_ascii=False), flush=True)
            except Exception as exc:  # Keep the stream alive for later files.
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "drive_id": metadata.get("drive_id"),
                            "error": f"{type(exc).__name__}: {exc}",
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
    finally:
        if previous_mode is not None:
            _set_console_echo(True, previous_mode)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

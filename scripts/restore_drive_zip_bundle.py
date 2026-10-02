#!/usr/bin/env python3
"""Validate and restore the Google Drive PDF ZIP bundle without overwrites.

The script has no delete path.  It rejects unsafe ZIP names, verifies every
restored file as a PDF, supports long Windows paths, and refuses to replace a
different file already present in either staging or the project tree.

Run without --apply for a read-only archive/CRC validation.  Run with --apply
to extract into ``drive-zip-extracted`` and copy the four approved top-level
PDF trees into the project.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import time
import zipfile
import zlib
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ZIP_DIR = ROOT / "drive-zip-incoming"
DEFAULT_STAGING_DIR = ROOT / "drive-zip-extracted"
APPROVED_ROOTS = {"ieee-pdf", "nature-pdf", "optica-pdf", "DesignCon"}
WINDOWS_INVALID_CHARS = set('<>:"|?*')
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def fs_path(path: Path) -> str:
    """Return a Windows extended-length path for every filesystem operation."""
    value = os.path.abspath(os.fspath(path))
    if os.name != "nt" or value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


def path_exists(path: Path) -> bool:
    return os.path.exists(fs_path(path))


def ensure_directory(path: Path) -> None:
    os.makedirs(fs_path(path), exist_ok=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(fs_path(path), "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def crc32_file(path: Path) -> int:
    checksum = 0
    with open(fs_path(path), "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            checksum = zlib.crc32(chunk, checksum)
    return checksum & 0xFFFFFFFF


def identical_files(left: Path, right: Path) -> bool:
    if os.path.getsize(fs_path(left)) != os.path.getsize(fs_path(right)):
        return False
    return sha256_file(left) == sha256_file(right)


def safe_relative_path(raw_name: str) -> PurePosixPath:
    normalized = raw_name.replace("\\", "/")
    if not normalized or normalized.startswith("/") or "\x00" in normalized:
        raise ValueError(f"unsafe absolute or empty ZIP path: {raw_name!r}")

    relative = PurePosixPath(normalized)
    if relative.is_absolute() or not relative.parts:
        raise ValueError(f"unsafe absolute ZIP path: {raw_name!r}")
    if relative.parts[0] not in APPROVED_ROOTS:
        raise ValueError(f"unexpected top-level directory: {raw_name!r}")

    for component in relative.parts:
        if component in {"", ".", ".."}:
            raise ValueError(f"unsafe path component: {raw_name!r}")
        if component.endswith((" ", ".")):
            raise ValueError(f"Windows-ambiguous path component: {raw_name!r}")
        if any(char in WINDOWS_INVALID_CHARS for char in component):
            raise ValueError(f"Windows-invalid path component: {raw_name!r}")
        device_name = component.split(".", 1)[0].upper()
        if device_name in WINDOWS_RESERVED_NAMES:
            raise ValueError(f"Windows reserved device name: {raw_name!r}")

    if relative.suffix.lower() != ".pdf":
        raise ValueError(f"non-PDF file in recovery bundle: {raw_name!r}")
    return relative


def is_symlink(info: zipfile.ZipInfo) -> bool:
    unix_mode = (info.external_attr >> 16) & 0o170000
    return unix_mode == stat.S_IFLNK


@dataclass(frozen=True)
class ArchiveEntry:
    zip_path: Path
    info: zipfile.ZipInfo
    relative: PurePosixPath


def inventory(zip_dir: Path) -> tuple[list[Path], list[ArchiveEntry]]:
    zip_paths = sorted(zip_dir.glob("*.zip"), key=lambda path: path.name.lower())
    if not zip_paths:
        raise ValueError(f"no ZIP files found in {zip_dir}")

    entries: list[ArchiveEntry] = []
    seen: dict[str, str] = {}
    for zip_path in zip_paths:
        with zipfile.ZipFile(fs_path(zip_path), "r") as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                if is_symlink(info):
                    raise ValueError(f"symbolic link entry rejected: {info.filename!r}")
                relative = safe_relative_path(info.filename)
                collision_key = relative.as_posix().casefold()
                previous = seen.get(collision_key)
                if previous is not None:
                    raise ValueError(
                        "case-insensitive duplicate ZIP path: "
                        f"{previous!r} and {relative.as_posix()!r}"
                    )
                seen[collision_key] = relative.as_posix()
                entries.append(ArchiveEntry(zip_path, info, relative))
    return zip_paths, entries


def validate_crc(zip_paths: list[Path]) -> None:
    for zip_path in zip_paths:
        with zipfile.ZipFile(fs_path(zip_path), "r") as archive:
            bad_name = archive.testzip()
            if bad_name is not None:
                raise ValueError(f"CRC validation failed in {zip_path.name}: {bad_name}")
        print(f"CRC OK: {zip_path.name}", flush=True)


def inspect_pdf_signatures(entries: list[ArchiveEntry]) -> dict[str, str]:
    invalid: dict[str, str] = {}
    for entry in entries:
        with zipfile.ZipFile(fs_path(entry.zip_path), "r") as archive:
            with archive.open(entry.info, "r") as source:
                prefix = source.read(5)
        if prefix != b"%PDF-":
            invalid[entry.relative.as_posix().casefold()] = prefix.hex()
    return invalid


def write_stream_to_new_file(source, destination: Path) -> tuple[int, str, bytes]:
    digest = hashlib.sha256()
    total = 0
    prefix = bytearray()
    with open(fs_path(destination), "xb") as target:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            if len(prefix) < 5:
                prefix.extend(chunk[: 5 - len(prefix)])
            target.write(chunk)
            digest.update(chunk)
            total += len(chunk)
    return total, digest.hexdigest(), bytes(prefix)


def extract_entry(entry: ArchiveEntry, staging_dir: Path) -> tuple[Path, str]:
    relative_path = Path(*entry.relative.parts)
    destination = staging_dir / relative_path
    if path_exists(destination):
        if (
            os.path.getsize(fs_path(destination)) != entry.info.file_size
            or crc32_file(destination) != entry.info.CRC
        ):
            raise FileExistsError(f"different staging file already exists: {destination}")
        with open(fs_path(destination), "rb") as handle:
            if handle.read(5) != b"%PDF-":
                raise ValueError(f"invalid existing staged PDF: {destination}")
        return destination, "staging-existing"

    ensure_directory(destination.parent)
    partial_dir = staging_dir / "_partial"
    ensure_directory(partial_dir)
    partial_name = hashlib.sha256(
        f"{entry.zip_path.name}\0{entry.relative.as_posix()}\0{time.time_ns()}".encode()
    ).hexdigest() + ".partial"
    partial = partial_dir / partial_name

    with zipfile.ZipFile(fs_path(entry.zip_path), "r") as archive:
        with archive.open(entry.info, "r") as source:
            size, _digest, prefix = write_stream_to_new_file(source, partial)
    if size != entry.info.file_size:
        raise ValueError(f"size mismatch after extraction: {entry.relative.as_posix()}")
    if prefix != b"%PDF-":
        raise ValueError(f"invalid PDF signature: {entry.relative.as_posix()}")
    if path_exists(destination):
        raise FileExistsError(f"destination appeared during extraction: {destination}")
    os.rename(fs_path(partial), fs_path(destination))
    return destination, "extracted"


def preserve_invalid_entry(entry: ArchiveEntry, staging_dir: Path) -> tuple[Path, str]:
    relative_path = Path(*entry.relative.parts)
    destination = staging_dir / "_invalid" / relative_path.with_suffix(
        relative_path.suffix + ".not-a-pdf"
    )
    if path_exists(destination):
        if (
            os.path.getsize(fs_path(destination)) != entry.info.file_size
            or crc32_file(destination) != entry.info.CRC
        ):
            raise FileExistsError(
                f"different preserved invalid file already exists: {destination}"
            )
        return destination, "invalid-existing"

    ensure_directory(destination.parent)
    partial_dir = staging_dir / "_partial"
    ensure_directory(partial_dir)
    with os.scandir(fs_path(partial_dir)) as candidates:
        for candidate in candidates:
            candidate_path = partial_dir / candidate.name
            if (
                candidate.is_file()
                and candidate.name.endswith(".partial")
                and candidate.stat().st_size == entry.info.file_size
                and crc32_file(candidate_path) == entry.info.CRC
            ):
                os.rename(fs_path(candidate_path), fs_path(destination))
                return destination, "invalid-preserved-from-partial"

    partial_name = hashlib.sha256(
        f"invalid\0{entry.zip_path.name}\0{entry.relative.as_posix()}\0{time.time_ns()}".encode()
    ).hexdigest() + ".partial"
    partial = partial_dir / partial_name
    with zipfile.ZipFile(fs_path(entry.zip_path), "r") as archive:
        with archive.open(entry.info, "r") as source:
            size, _digest, _prefix = write_stream_to_new_file(source, partial)
    if size != entry.info.file_size or crc32_file(partial) != entry.info.CRC:
        raise ValueError(
            f"invalid-file preservation check failed: {entry.relative.as_posix()}"
        )
    if path_exists(destination):
        raise FileExistsError(
            f"invalid-file destination appeared during extraction: {destination}"
        )
    os.rename(fs_path(partial), fs_path(destination))
    return destination, "invalid-preserved"


def copy_without_overwrite(source: Path, destination: Path) -> str:
    if path_exists(destination):
        if identical_files(source, destination):
            return "project-identical"
        raise FileExistsError(f"different project file already exists: {destination}")

    ensure_directory(destination.parent)
    partial = destination.with_name(
        destination.name + ".drive-importing-" + hashlib.sha256(
            destination.as_posix().encode("utf-8")
        ).hexdigest()[:12]
    )
    if path_exists(partial):
        raise FileExistsError(f"previous partial project copy is preserved: {partial}")
    with open(fs_path(source), "rb") as input_handle:
        size, _digest, prefix = write_stream_to_new_file(input_handle, partial)
    if size != os.path.getsize(fs_path(source)) or prefix != b"%PDF-":
        raise ValueError(f"project copy validation failed: {destination}")
    if path_exists(destination):
        raise FileExistsError(f"destination appeared during project copy: {destination}")
    os.rename(fs_path(partial), fs_path(destination))
    return "project-copied"


def summarize(entries: list[ArchiveEntry]) -> dict:
    roots: dict[str, int] = {name: 0 for name in sorted(APPROVED_ROOTS)}
    total_bytes = 0
    max_relative_chars = 0
    for entry in entries:
        roots[entry.relative.parts[0]] += 1
        total_bytes += entry.info.file_size
        max_relative_chars = max(max_relative_chars, len(entry.relative.as_posix()))
    return {
        "entries": len(entries),
        "uncompressed_bytes": total_bytes,
        "roots": roots,
        "max_relative_path_chars": max_relative_chars,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zip-dir", type=Path, default=DEFAULT_ZIP_DIR)
    parser.add_argument("--staging-dir", type=Path, default=DEFAULT_STAGING_DIR)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Extract to staging and copy into the project without overwrites.",
    )
    parser.add_argument(
        "--skip-crc",
        action="store_true",
        help="Skip the separate full CRC pass when it was already completed.",
    )
    args = parser.parse_args()

    zip_dir = args.zip_dir.resolve()
    staging_dir = args.staging_dir.resolve()
    zip_paths, entries = inventory(zip_dir)
    summary = summarize(entries)
    invalid_signatures = inspect_pdf_signatures(entries)
    summary["invalid_pdf_signatures"] = len(invalid_signatures)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    for entry in entries:
        signature = invalid_signatures.get(entry.relative.as_posix().casefold())
        if signature is not None:
            print(
                "NON-PDF PRESERVED OUTSIDE PROJECT: "
                f"{entry.relative.as_posix()} (prefix={signature})",
                flush=True,
            )

    if not args.skip_crc:
        validate_crc(zip_paths)
    if not args.apply:
        print("Read-only validation completed; no files were extracted.")
        return 0

    ensure_directory(staging_dir)
    counters = {
        "extracted": 0,
        "staging-existing": 0,
        "project-copied": 0,
        "project-identical": 0,
        "invalid-preserved": 0,
        "invalid-preserved-from-partial": 0,
        "invalid-existing": 0,
    }
    restored: list[dict[str, object]] = []
    for index, entry in enumerate(entries, start=1):
        invalid_signature = invalid_signatures.get(
            entry.relative.as_posix().casefold()
        )
        if invalid_signature is not None:
            invalid_path, invalid_status = preserve_invalid_entry(entry, staging_dir)
            counters[invalid_status] += 1
            restored.append(
                {
                    "path": entry.relative.as_posix(),
                    "bytes": entry.info.file_size,
                    "crc32": f"{entry.info.CRC:08x}",
                    "extract_status": invalid_status,
                    "preserved_path": str(invalid_path),
                    "project_status": None,
                    "invalid_prefix": invalid_signature,
                }
            )
            if index % 100 == 0 or index == len(entries):
                print(f"Restored {index}/{len(entries)}", flush=True)
            continue
        staged_path, extract_status = extract_entry(entry, staging_dir)
        project_path = ROOT / Path(*entry.relative.parts)
        copy_status = copy_without_overwrite(staged_path, project_path)
        counters[extract_status] += 1
        counters[copy_status] += 1
        restored.append(
            {
                "path": entry.relative.as_posix(),
                "bytes": entry.info.file_size,
                "crc32": f"{entry.info.CRC:08x}",
                "extract_status": extract_status,
                "project_status": copy_status,
            }
        )
        if index % 100 == 0 or index == len(entries):
            print(f"Restored {index}/{len(entries)}", flush=True)

    manifest = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "zip_files": [path.name for path in zip_paths],
        "summary": summary,
        "counters": counters,
        "files": restored,
    }
    manifest_path = staging_dir / (
        "RESTORE_MANIFEST_" + time.strftime("%Y%m%d_%H%M%S") + ".json"
    )
    with open(fs_path(manifest_path), "x", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps(counters, ensure_ascii=False, indent=2))
    print(f"Manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"RESTORE FAILED: {exc}", file=sys.stderr)
        raise

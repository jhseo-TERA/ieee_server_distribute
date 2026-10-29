"""Local MinerU extraction with immutable, review-only SerDes evidence.

No model packages are imported into the web process.  Parsing runs in the
isolated document-parser Python environment; HTTP handlers only read caches.
Numbers from different paragraphs or table columns are never pooled into one
operating point, and comparison tables require an explicit This Work label.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile

from paper_server.serdes.enrichment import classify_energy_component_scope
from paper_server.serdes.metrics import extract_performance, plain_text

from paper_server.paths import ROOT
DEFAULT_CACHE_ROOT = ROOT / "outputs" / "serdes_mineru"
ADAPTER_VERSION = "serdes-mineru-1.0"
MINERU_VERSION = "4.0.10"
NUMERIC_FIELDS = (
    "reported_rate_gbps", "reported_rate_min_gbps", "reported_rate_max_gbps",
    "lane_rate_gbps", "lane_count", "aggregate_rate_gbps", "symbol_rate_gbaud",
    "throughput_density_gbps_per_mm", "power_mw", "energy_pj_bit",
    "energy_loss_normalized_pj_bit_db", "process_nm", "channel_loss_db",
    "loss_frequency_ghz", "ber",
)
THIS_WORK = re.compile(r"\b(?:this\s*(?:work|paper|design)|proposed(?:\s+work)?)\b", re.I)
PRIOR_WORK = re.compile(r"\b(?:previous|prior|earlier|reported\s+in|compared\s+with)\b|\[\s*\d+(?:\s*[-,]\s*\d+)*\s*\]", re.I)


def source_sha256(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def mineru_environment(*, download_models=False) -> dict:
    """Pin caches and disable remote inference even if the host has a profile."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("MINERU_")}
    root = ROOT / ".runtime" / "document-parsers" / "models"
    env.update({
        "PYTHONUTF8": "1", "MINERU_HOME": str(root / "mineru-home"),
        "MINERU_MODEL_BASE_DIR": str(root / "mineru"),
        "MINERU_MODEL_SOURCE": "huggingface" if download_models else "local",
        "MINERU_MODEL_SMALL_BACKEND": "onnx", "MINERU_TABLE_DEVICE": "cpu",
        "MINERU_MODEL_VLM_SERVER_URL": "", "MINERU_MODEL_VLM_API_KEY": "",
        "MINERU_LLM_AIDED_FEATURES_TITLE_LEVELING": "false",
        "MINERU_LLM_AIDED_FEATURES_CROSS_PAGE_TABLE_CELL_MERGE": "false",
        "HF_HOME": str(root / "huggingface"), "MODELSCOPE_CACHE": str(root / "modelscope"),
        "HF_HUB_DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1",
        "HF_HUB_OFFLINE": "0" if download_models else "1",
    })
    return env


def _settings(tier: str, max_pages: int, start_page: int) -> dict:
    if tier not in {"basic", "flash"}:
        raise ValueError("MinerU tier must be basic or flash (local execution only)")
    if not 1 <= max_pages <= 300 or not 1 <= start_page <= 10000:
        raise ValueError("max_pages must be 1..300 and start_page 1..10000")
    model_root = ROOT / ".runtime" / "document-parsers" / "models" / "mineru"
    model_files = []
    for path in sorted(model_root.rglob("*")) if model_root.exists() else []:
        if path.is_file():
            stat = path.stat()
            model_files.append((str(path.relative_to(model_root)), stat.st_size, stat.st_mtime_ns))
    model_signature = hashlib.sha256(json.dumps(model_files).encode()).hexdigest()
    return {"tier": tier, "max_pages": max_pages, "start_page": start_page,
            "adapter_version": ADAPTER_VERSION, "mineru_version": MINERU_VERSION,
            "model_files_signature": model_signature}


def _cache_path(sha: str, settings: dict, root: Path | str | None) -> Path:
    key = hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()[:16]
    return Path(root or DEFAULT_CACHE_ROOT) / sha / key


def _read_report(path: Path, sha: str, settings: dict | None = None) -> dict | None:
    try:
        if path.stat().st_size > 32 * 1024 * 1024:
            return None
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (not isinstance(result, dict) or result.get("status") != "complete"
            or result.get("source_sha256") != sha
            or result.get("adapter_version") != ADAPTER_VERSION
            or (settings is not None and result.get("settings") != settings)):
        return None
    return result


def load_cached_mineru_report(pdf_path: Path | str, *, cache_root=None,
                              tier="basic", max_pages=30, start_page=1) -> dict | None:
    settings = _settings(tier, max_pages, start_page)
    try:
        sha = source_sha256(pdf_path)
    except OSError:
        return None
    return _read_report(_cache_path(sha, settings, cache_root) / "evidence.json", sha, settings)


def latest_cached_mineru_report(pdf_path: Path | str, *, cache_root=None) -> dict | None:
    """Read-only API accessor; a replaced PDF can never reuse stale evidence."""
    try:
        sha = source_sha256(pdf_path)
    except OSError:
        return None
    reports = [_read_report(path, sha) for path in
               (Path(cache_root or DEFAULT_CACHE_ROOT) / sha).glob("*/evidence.json")]
    reports = [item for item in reports if item]
    return max(reports, key=lambda item: (item.get("tier") == "basic",
                item.get("created_at", "")), default=None)


def _text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(_text(item) for item in value)
    if isinstance(value, dict):
        return _text(value.get("content", value.get("text", "")))
    return ""


class _TableReader(HTMLParser):
    """Read bounded HTML cells; keep spanning cells explicit for safe rejection."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.row, self.cell = [], [], None
        self.complex_spans = False

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        if tag in {"td", "th"}:
            self.cell = []
            for key, value in attrs:
                if key in {"rowspan", "colspan"} and value not in {None, "", "1"}:
                    self.complex_spans = True
        elif tag == "br" and self.cell is not None:
            self.cell.append(" ")

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self.cell is not None:
            self.row.append(plain_text("".join(self.cell)))
            self.cell = None
        if tag == "tr" and self.row:
            if len(self.rows) >= 1000 or len(self.row) > 100:
                raise ValueError("MinerU table exceeds evidence bounds")
            self.rows.append(self.row)
            self.row = []


def _table_metric(label: str, value: str) -> str:
    """Carry explicit header units to bare cells; do not invent missing units."""
    value, label = plain_text(value), plain_text(label)
    # No numeric ranges, slashes, footnotes or inequality is collapsed to a scalar.
    if re.fullmatch(r"\d+(?:\.\d+)?", value):
        units = re.findall(r"(?:[pf]J\s*/\s*(?:bit|b)(?:\s*/\s*dB)?|Gb/s|Gbps|Gbit/s|GBaud|mW|[µμu]W|nm|dB|GHz)", label, re.I)
        if len(units) == 1:
            value += " " + units[0]
    # Strip units from the label once transferred: preserves `BER` and scope.
    return f"{label}: {value}"


def _table_fragments(block: dict) -> tuple[list[dict], list[str]]:
    html = block.get("table_body") or block.get("html") or _text(block.get("content"))
    parser = _TableReader()
    parser.feed(html)
    rows = parser.rows
    if not rows:
        return [], ["table_has_no_html_cells"]
    if parser.complex_spans:
        return [], ["table_spans_require_manual_review"]
    if any(len(row) != len(rows[0]) for row in rows):
        return [], ["table_ragged_rows_require_manual_review"]
    markers = [(r, c) for r, row in enumerate(rows) for c, cell in enumerate(row)
               if THIS_WORK.search(cell) and not PRIOR_WORK.search(cell)]
    if len(markers) != 1:
        return [], ["table_requires_unique_this_work_label"]
    r, c = markers[0]
    fragments = []
    if r == 0 and c > 0:
        # Common ISSCC comparison: metrics in rows, implementations in columns.
        for ri, row in enumerate(rows[1:], 1):
            if c < len(row):
                fragments.append({"text": _table_metric(row[0], row[c]),
                                  "table_row": ri + 1, "table_column": c + 1,
                                  "table_header": rows[0], "table_cells": row,
                                  "attribution": "this_work"})
    elif c == 0 and r > 0:
        # Transposed comparison: one implementation per row.
        row = rows[r]
        for ci, value in enumerate(row[1:], 1):
            if ci < len(rows[0]):
                fragments.append({"text": _table_metric(rows[0][ci], value),
                                  "table_row": r + 1, "table_column": ci + 1,
                                  "table_header": rows[0], "table_cells": row,
                                  "attribution": "this_work"})
    else:
        return [], ["table_this_work_position_ambiguous"]
    return fragments, []


def _conditions(text: str) -> dict:
    """Keep verbatim conditions beside values; inference never becomes a fact."""
    patterns = {
        "modulation": r"\b(?:PAM[- ]?[2348]|NRZ)\b",
        "supply_voltage": r"\d+(?:\.\d+)?\s*V\b",
        "temperature": r"-?\d+(?:\.\d+)?\s*°?\s*C\b",
        "fec": r"\b(?:pre|post)[- ]?FEC\b|\bFEC\s+(?:threshold|limit)\b",
        "excluded_components": r"\b(?:excluding|without|does not include)\b[^.;]{0,100}",
        "measurement_type": r"\b(?:measured|measurement|simulated|simulation|estimated)\b",
    }
    return {name: list(dict.fromkeys(re.findall(pattern, text, flags=re.I)))
            for name, pattern in patterns.items() if re.search(pattern, text, re.I)}


def build_evidence_report(payload: dict, *, source_hash: str,
                          source_path: str = "", tier: str = "basic") -> dict:
    """Normalize MinerU 4 structured content without flattening page/table scope."""
    if not isinstance(payload, dict) or not isinstance(payload.get("pages"), list):
        raise ValueError("Expected MinerU 4 structured_content.json with pages")
    blocks, candidates, warnings = [], [], []
    for page in payload["pages"]:
        page_idx = page.get("page_idx")
        if not isinstance(page_idx, int) or page_idx < 0:
            raise ValueError("MinerU output is missing its original page index")
        for block_index, source in enumerate(page.get("blocks", [])):
            kind = source.get("type", "text")
            if kind not in {"text", "title", "doc_title", "paragraph_title", "table",
                            "equation", "image", "chart", "list", "ref_text"}:
                continue
            index = source.get("index", block_index)
            block_id = f"p{page_idx + 1}-b{index}"
            caption = _text(source.get("captions", source.get("caption", source.get("table_caption", []))))
            text = plain_text(_text(source.get("content", source.get("text", ""))))
            bbox = source.get("bbox")
            if not (isinstance(bbox, (list, tuple)) and len(bbox) == 4
                    and all(isinstance(x, (int, float)) and math.isfinite(x) for x in bbox)):
                bbox = None
            block = {"block_id": block_id, "page": page_idx + 1,
                     "block_type": kind, "bbox": bbox,
                     "bbox_coordinate_system": "normalized_top_left_0_1",
                     "text": text, "caption": caption,
                     "caption_annotations": source.get("captions", []),
                     "footnote_annotations": source.get("footnotes", []),
                     "image_path": source.get("image_source") or source.get("image_path") or source.get("img_path"),
                     "footnotes": _text(source.get("footnotes", source.get("footnote", source.get("table_footnote", []))))}
            if kind == "table":
                block["table_html"] = source.get("table_body") or source.get("html") or _text(source.get("content"))
            fragments = []
            block_warnings = []
            if kind == "table":
                fragments, block_warnings = _table_fragments(source)
            elif kind == "text":
                # Exclude references sentence by sentence, preserving self-attribution.
                for sentence in re.split(r"(?<=[.;!?])\s+", text):
                    if PRIOR_WORK.search(sentence):
                        block_warnings.append("mixed_attribution_requires_review" if THIS_WORK.search(sentence)
                                              else "prior_work_sentence_excluded")
                        continue
                    fragments.append({"text": sentence,
                                      "attribution": "this_work" if THIS_WORK.search(sentence)
                                      or re.search(r"\bwe\s+(?:measure|demonstrate|achieve|present|report)", sentence, re.I)
                                      else "requires_review"})
            block["warnings"] = list(dict.fromkeys(block_warnings))
            blocks.append(block)
            warnings.extend(f"{block_id}:{warning}" for warning in block["warnings"])
            for fragment_index, fragment in enumerate(fragments):
                local_text = fragment["text"]
                metrics = extract_performance(local_text)
                if not any(metrics.get(field) is not None for field in NUMERIC_FIELDS) and not metrics["ambiguous_fields"]:
                    continue
                conditions_text = " ".join((local_text, caption, block["footnotes"]))
                scope = classify_energy_component_scope(None, [conditions_text])
                flags = ["requires_human_review"]
                if fragment["attribution"] != "this_work":
                    flags.append("implementation_attribution_unconfirmed")
                if metrics["ambiguous_fields"]:
                    flags.append("multiple_values_in_same_fragment")
                if metrics.get("reported_rate_gbps") is not None and re.search(
                        r"\b(?:recovered\s+data|data\s+recovery|demultiplex\w*|deserial\w*)\b", local_text, re.I):
                    flags.append("internal_or_parallel_rate_scope_requires_review")
                if re.search(r"\b(?:simulated|simulation|estimated)\b", conditions_text, re.I):
                    flags.append("not_confirmed_measured")
                if kind == "table":
                    flags.append("table_cell_requires_operating_point_review")
                if tier == "flash":
                    flags.append("flash_native_parse_requires_layout_review")
                identity = {"source_sha256": source_hash, "adapter_version": ADAPTER_VERSION,
                            "tier": tier, "producer": payload.get("metadata", {}).get("producer"),
                            "block": block, "fragment": fragment, "fragment_index": fragment_index}
                candidate_id = hashlib.sha256(json.dumps(
                    identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]
                candidates.append({"candidate_id": candidate_id, **block, **fragment,
                                   "performance": metrics, "conditions": _conditions(conditions_text),
                                   "energy_component_scope": scope["energy_component_scope"],
                                   "review_status": "needs_review", "flags": flags})
    values = {}
    for candidate in candidates:
        for field in NUMERIC_FIELDS:
            value = candidate["performance"].get(field)
            if value is not None:
                values.setdefault(field, {}).setdefault(str(value), []).append(candidate["candidate_id"])
    conflicts = [{"field": field, "values": group,
                  "reason": "different_operating_points_or_extraction_conflict"}
                 for field, group in values.items() if len(group) > 1]
    return {"adapter_version": ADAPTER_VERSION, "status": "complete", "tier": tier,
            "source_sha256": source_hash, "source_path": source_path,
            "producer": payload.get("metadata", {}).get("producer", {}),
            "document_metadata": payload.get("metadata", {}),
            "layout_geometry": payload.get("extensions", {}).get("docvortex_layout", {}),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "review_status": "needs_review", "blocks": blocks, "candidates": candidates,
            "conflicts": conflicts, "warnings": warnings,
            "stats": {"pages": len(payload["pages"]), "blocks": len(blocks),
                      "candidates": len(candidates), "conflicting_fields": len(conflicts)},
            "limitations": ["Parser output requires human verification before benchmark use.",
                            "No metrics are joined across text fragments or table cells.",
                            "Tables with merged cells require manual review."]}


def extract_mineru_evidence(pdf_path: Path | str, *, cache_root=None,
                           python_path=None, tier="basic", max_pages=30,
                           start_page=1, timeout=900, force=False) -> dict:
    settings = _settings(tier, max_pages, start_page)
    if not 1 <= timeout <= 7200:
        raise ValueError("timeout must be 1..7200 seconds")
    pdf_path = Path(pdf_path).resolve()
    if pdf_path.suffix.lower() != ".pdf" or not pdf_path.is_file():
        raise ValueError("MinerU evidence requires an existing local PDF")
    if pdf_path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("MinerU evidence PDF exceeds the local 64 MiB processing limit")
    sha = source_sha256(pdf_path)
    cache_dir = _cache_path(sha, settings, cache_root)
    if not force:
        cached = _read_report(cache_dir / "evidence.json", sha, settings)
        if cached:
            return cached
    python = Path(python_path or os.getenv("IEEE_DOCUMENT_PARSER_PYTHON") or os.getenv("DOCUMENT_PARSER_PYTHON") or
                  ROOT / ".runtime" / "document-parsers" / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
    if not python.is_file():
        raise RuntimeError("Document parser runtime is missing; run scripts/setup_document_parsers.ps1")
    cache_dir.mkdir(parents=True, exist_ok=True)
    # Each attempt is isolated; incomplete outputs never replace a valid cache.
    with tempfile.TemporaryDirectory(prefix="attempt-", dir=cache_dir) as temp:
        output = Path(temp)
        env = mineru_environment()
        command = [str(python), str(ROOT / "scripts" / "mineru_worker.py"),
                   "--pdf", str(pdf_path), "--output", str(output), "--tier", tier,
                   "--start-page", str(start_page), "--max-pages", str(max_pages)]
        try:
            with (cache_dir / "parser.log").open("w", encoding="utf-8") as log:
                result = subprocess.run(command, cwd=ROOT, env=env, stdout=log,
                                        stderr=subprocess.STDOUT, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"MinerU timed out after {timeout}s; see {cache_dir / 'parser.log'}") from exc
        if result.returncode:
            raise RuntimeError(f"MinerU worker failed (exit {result.returncode}); see {cache_dir / 'parser.log'}")
        if source_sha256(pdf_path) != sha:
            raise RuntimeError("PDF changed while MinerU was parsing; discard this extraction and retry")
        payload = json.loads((output / "structured_content.json").read_text(encoding="utf-8"))
        report = build_evidence_report(payload, source_hash=sha, source_path=str(pdf_path), tier=tier)
        report.update({"settings": settings, "cache_dir": str(cache_dir.resolve()),
                       "assets_dir": "parsed", "source_pages_requested": [start_page, start_page + max_pages - 1]})
        # Preserve the parsed source package alongside the normalized evidence.
        import shutil
        assets = cache_dir / "parsed"
        assets.mkdir(exist_ok=True)
        shutil.copytree(output, assets, dirs_exist_ok=True)
        pending = cache_dir / "evidence.pending.json"
        pending.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        pending.replace(cache_dir / "evidence.json")
        return report

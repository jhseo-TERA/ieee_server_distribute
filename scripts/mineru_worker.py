"""Isolated MinerU 4 local SDK worker; never call from an HTTP request."""
from __future__ import annotations

import argparse
from importlib.metadata import version
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from paper_server.serdes.mineru import mineru_environment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--download-models", action="store_true")
    parser.add_argument("--tier", choices=("basic", "flash"), default="basic")
    parser.add_argument("--start-page", type=int, default=1)
    parser.add_argument("--max-pages", type=int, default=30)
    args = parser.parse_args()
    for key in list(os.environ):
        if key.startswith("MINERU_"):
            del os.environ[key]
    os.environ.update(mineru_environment(download_models=args.download_models))
    if args.download_models:
        executable = Path(sys.executable).parent / ("mineru-kit.exe" if os.name == "nt" else "mineru-kit")
        subprocess.run([str(executable), "models", "download", "--tier", "basic",
                        "--small-backend", "onnx", "--source", "huggingface"], check=True)
        subprocess.run([str(executable), "models", "verify", "--tier", "basic",
                        "--small-backend", "onnx"], check=True)
        return
    if args.pdf is None or args.output is None:
        parser.error("--pdf and --output are required for extraction")
    if not 1 <= args.max_pages <= 300 or args.start_page < 1:
        parser.error("Invalid page bounds")
    installed = version("mineru")
    if installed != "4.0.10":
        raise RuntimeError(f"Expected tested MinerU 4.0.10, found {installed}")
    import pypdfium2 as pdfium
    with pdfium.PdfDocument(str(args.pdf)) as pdf:
        count = len(pdf)
    if args.start_page > count:
        raise ValueError("start-page is beyond the PDF's last page")
    end = min(count, args.start_page + args.max_pages - 1)
    from mineru.parser import parse
    from mineru.config import VlmConfig
    from mineru.parser.writer import FileBasedDataWriter
    # Local SDK, no API URL or document-library service is configured.  Basic
    # uses small models only; disabling image analysis avoids descriptive VLM.
    result = parse(args.pdf, tier=args.tier, image_analysis=False,
                   page_range=f"{args.start_page}-{end}", vlm_config=VlmConfig(server_url=""))
    args.output.mkdir(parents=True, exist_ok=True)
    result.save(FileBasedDataWriter(str(args.output)))
    print(f"MinerU {installed}: {args.tier}, pages {args.start_page}-{end}", flush=True)


if __name__ == "__main__":
    main()

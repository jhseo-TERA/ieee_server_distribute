"""Read package/model readiness without importing models or using the network."""
import importlib.metadata
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    packages = {}
    for name in ("paddleocr", "paddlepaddle", "mineru", "docvortex", "onnxruntime"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    models = ROOT / ".runtime/document-parsers/models"
    result = {
        "python": sys.executable,
        "packages": packages,
        "paddle_model_present": (models / "paddle/official_models/PP-DocLayout-L/inference.yml").is_file(),
        "model_cache": str(models),
        "paddle_reports": len(list((ROOT / "outputs/document_parsers/paddle_layout").glob("*.json"))),
        "mineru_reports": len(list((ROOT / "outputs/serdes_mineru").glob("**/evidence.json"))),
        "mineru_onnx_files": len(list((models / "mineru").glob("**/*.onnx"))),
        "note": "Package/model files alone do not prove inference readiness; run the documented sample checks.",
    }
    print(json.dumps(result, indent=2, ensure_ascii=True))
    return 0 if all(packages.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Read-only authenticated checks of the restored SerDes API and PDF route.

Use --local-http to check the existing loopback server as well.
The viewer session is signed in memory with this app's own key; neither the
cookie nor credentials are printed or saved. No write endpoint is called.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from web.app import app


def check_api(get_json, get_pdf, get_html) -> dict:
    assert "serdes" in get_html("/serdes").lower()
    meta = get_json("/api/serdes/meta")
    assert meta["total"] > 0 and meta["structured_total"] > 0, meta
    papers = get_json("/api/serdes/papers?size=5&pdf_only=1")
    assert papers["items"] and papers["total"] > 0, "No PDF-backed survey papers"
    performance = get_json("/api/serdes/performance")
    assert performance["points"], "No structured performance points"
    article = next(p["article_number"] for p in performance["points"] if p.get("article_number"))
    evidence = get_json(f"/api/serdes/papers/{article}/evidence")
    assert evidence["evidence"], "Missing field evidence"
    pdf_article = papers["items"][0]["article_number"]
    assert get_pdf(f"/pdf/{pdf_article}").startswith(b"%PDF-"), "PDF response is not a PDF"
    return {
        "meta": {k: v for k, v in meta.items() if k.endswith("total") or k == "abstract_providers"},
        "paper_response_keys": list(papers),
        "performance_points": len(performance["points"]),
        "chart_sets": {k: len(v) for k, v in performance.get("chart_sets", {}).items()},
        "evidence_article": article, "evidence_fields": len(evidence["evidence"]),
        "pdf_article": pdf_article, "pdf_header_valid": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-http", action="store_true")
    args = parser.parse_args()
    report = {}
    with app.test_client() as client:
        assert client.get("/api/serdes/meta").status_code == 401
        with client.session_transaction() as session:
            session["authenticated"] = True
            session["username"] = os.getenv("AUTH_USERNAME", "recovery-check")
            session["role"] = "viewer"

        def get_local(path):
            response = client.get(path)
            assert response.status_code == 200, (path, response.status_code)
            return response

        report["test_client"] = check_api(
            lambda p: get_local(p).get_json(), lambda p: get_local(p).data[:5],
            lambda p: get_local(p).get_data(as_text=True),
        )
        cookie = client.get_cookie(app.config["SESSION_COOKIE_NAME"]).value
        if args.local_http:
            for base in ("http://127.0.0.1:5001",):
                with requests.Session() as http:
                    http.cookies.set(app.config["SESSION_COOKIE_NAME"], cookie,
                                     domain=base.split("//", 1)[1].split(":")[0], path="/")

                    def fetch(path):
                        response = http.get(base + path, timeout=60, allow_redirects=False)
                        assert response.status_code == 200, (path, response.status_code)
                        return response

                    def pdf_head(path):
                        with http.get(base + path, headers={"Range": "bytes=0-4"},
                                      timeout=30, allow_redirects=False, stream=True) as response:
                            assert response.status_code in (200, 206)
                            assert "application/pdf" in response.headers.get("Content-Type", "")
                            return next(response.iter_content(5))

                    report[base] = check_api(lambda p: fetch(p).json(), pdf_head,
                                            lambda p: fetch(p).text)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = ROOT / f"outputs/serdes_recovery/verification_{stamp}.json"
    with path.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(json.dumps({"report": str(path), "checks": report}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

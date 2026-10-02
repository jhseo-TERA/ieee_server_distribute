"""Authenticated Research Desk routes. No inference endpoint is publicly exposed."""
from __future__ import annotations

from io import BytesIO
import os
import re
import threading

from flask import Blueprint, jsonify, render_template, request, send_file, session
from sqlalchemy.exc import SQLAlchemyError


def create_ai_blueprint(engine, root, csrf_token):
    bp = Blueprint("local_ai", __name__)
    factory_lock = threading.Lock()
    state = {}

    def service():
        if os.getenv("LOCAL_AI_ENABLED", "1") != "1":
            raise PermissionError("로컬 AI 기능이 비활성화되어 있습니다.")
        with factory_lock:
            if "service" not in state:
                from local_ai_service import LocalAIService
                from local_ai_runtime import OllamaClient
                from local_ai_documents import DocumentStore
                from local_ai_repository import LocalAIRepository
                from local_ai_recommendations import RecommendationService
                from local_ai_survey import SurveyAIService
                state["service"] = LocalAIService(
                    LocalAIRepository(engine),
                    OllamaClient(os.getenv("LOCAL_AI_OLLAMA_URL", "http://127.0.0.1:11434")),
                    DocumentStore(root), root,
                )
                from web.app import build_sql_recommendations, build_serdes_performance
                ai = state["service"]
                ai.recommendations = RecommendationService(engine, ai, build_sql_recommendations)
                ai.handlers['recommendations'] = ai.recommendations.run
                ai.survey = SurveyAIService(ai, build_serdes_performance)
                ai.handlers['survey_analyze'] = ai.survey.run
                ai.handlers['survey_compare'] = ai.survey.run
        return state["service"]

    # Exposed only for isolated tests, never as a web endpoint.
    bp.ai_service_factory = service
    bp.ai_state = state

    def admin_required():
        if session.get("role") != "admin":
            raise PermissionError("관리자 권한이 필요합니다.")

    def owner():
        return session.get("username", "")

    def body():
        if request.content_length and request.content_length > 20000:
            raise ValueError("요청 본문이 너무 큽니다.")
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValueError("JSON 객체가 필요합니다.")
        return data

    def public_job(job):
        if not job:
            raise KeyError("job")
        result = job.get("result") or {}
        return {**job, "progress": result.get("progress", "")}

    @bp.errorhandler(ValueError)
    def invalid(exc):
        return jsonify(ok=False, error=str(exc)), 400

    @bp.errorhandler(PermissionError)
    def forbidden(exc):
        return jsonify(ok=False, error=str(exc)), 403

    @bp.errorhandler(KeyError)
    def missing(exc):
        return jsonify(ok=False, error="해당 항목을 찾을 수 없습니다."), 404

    @bp.errorhandler(SQLAlchemyError)
    def database_error(exc):
        return jsonify(ok=False, error="AI 저장소를 사용할 수 없습니다. AI 스키마 마이그레이션과 MySQL 연결을 확인하세요."), 503

    @bp.errorhandler(RuntimeError)
    def runtime_error(exc):
        return jsonify(ok=False, error=str(exc)[:500]), 503

    @bp.route("/ai")
    def research_desk():
        return render_template("ai_research.html", username=owner(), role=session.get("role", "viewer"),
                               csrf_token=csrf_token(), can_edit=session.get("role") == "admin")

    @bp.route("/api/ai/status")
    def ai_status():
        ai = service()
        status = ai.runtime.status()
        status.update(ok=bool(status.get("available", status.get("ok", False))),
                      busy=ai.active is not None, queue_size=ai.queue.qsize(),
                      limits={"concurrent_jobs": 1, "selected_papers": 5, "pdf_pages": 3,
                              "max_pdf_page": 40, "contexts": [4096, 8192, 16384], "max_tokens": 2048})
        return jsonify(status)

    @bp.route("/api/ai/search")
    def ai_search():
        from local_ai_service import FILTER_KEYS
        filters = {k: request.args[k] for k in FILTER_KEYS if request.args.get(k) not in (None, "")}
        query = request.args.get("q", "")
        if len(query) > 500:
            raise ValueError("검색어가 너무 깁니다.")
        return jsonify(ok=True, items=service().repo.search(query, request.args.get("scope", "serdes"), filters, limit=30))

    @bp.route("/api/ai/papers/<article_number>")
    def ai_paper(article_number):
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,180}", article_number):
            raise KeyError(article_number)
        details = service().repo.paper_evidence(article_number)
        if not details or not details.get("paper"):
            raise KeyError(article_number)
        return jsonify(ok=True, **details)

    @bp.route("/api/ai/papers/<article_number>/pages/<int:page>.png")
    def ai_page_image(article_number, page):
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,180}", article_number) or not 1 <= page <= 40:
            raise KeyError(article_number)
        ai = service()
        papers = ai.repo.get_papers([article_number])
        if not papers or not papers[0].get("pdf_available"):
            raise KeyError(article_number)
        image = ai.docs.render_page(papers[0], page)
        response = send_file(BytesIO(image), mimetype="image/png", max_age=0)
        response.headers["Cache-Control"] = "private, no-store"
        return response

    @bp.route("/api/ai/review-queue")
    def ai_review_queue():
        result = service().repo.review_queue(limit=30)
        if isinstance(result, list):
            result = {"items": result}
        result.setdefault("items", result.get("review_queue", []))
        return jsonify(ok=True, **result)

    @bp.route("/api/ai/survey/jobs", methods=["POST"])
    def survey_job():
        job = service().survey.submit(body(), owner(), session.get("role", "viewer"))
        return jsonify(ok=True, job=public_job(job)), 202

    @bp.route("/api/ai/survey/gaps")
    def survey_gaps():
        return jsonify(ok=True, **service().survey.gaps())

    @bp.route("/api/ai/jobs", methods=["GET", "POST"])
    def ai_jobs():
        ai = service()
        if request.method == "GET":
            return jsonify(ok=True, items=[public_job(j) for j in ai.repo.list_jobs(owner(), limit=20)])
        job = ai.submit(body(), owner(), session.get("role", "viewer"))
        return jsonify(ok=True, job=public_job(job)), 202

    @bp.route("/api/ai/jobs/<job_id>")
    def ai_job(job_id):
        return jsonify(ok=True, job=public_job(service().repo.get_job(job_id, owner())))

    @bp.route("/api/ai/jobs/<job_id>/cancel", methods=["POST"])
    def ai_cancel(job_id):
        return jsonify(ok=True, job=public_job(service().cancel(job_id, owner())))

    @bp.route("/api/ai/proposals")
    def ai_proposals():
        admin_required()
        job_id = request.args.get("job_id")
        if job_id and not service().repo.get_job(job_id, owner()):
            raise KeyError(job_id)
        return jsonify(ok=True, items=service().repo.list_proposals(
            job_id=job_id, status=request.args.get("status", "pending"), limit=100))

    @bp.route("/api/ai/proposals/decision", methods=["POST"])
    def ai_decision():
        admin_required()
        data = body()
        ids = data.get("ids")
        if not isinstance(ids, list) or not 1 <= len(ids) <= 24 or any(type(i) is not int or i <= 0 for i in ids):
            raise ValueError("검토할 제안 ID를 최대 24개 선택하세요.")
        decision = data.get("decision")
        if decision not in {"approve", "reject"}:
            raise ValueError("approve 또는 reject를 지정하세요.")
        confirm_visual = data.get("confirm_visual", False)
        if not isinstance(confirm_visual, bool):
            raise ValueError("이미지 근거 확인 여부는 true/false여야 합니다.")
        result = service().repo.decide_proposals(ids, decision, owner(), confirm_visual=confirm_visual)
        return jsonify(ok=True, result=result)

    @bp.after_request
    def no_shared_cache(response):
        response.headers["Cache-Control"] = "private, no-store"
        return response

    return bp

"""Daily random favorites and private review notes, independent of recommendation feedback."""
import hashlib
import random
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from flask import Blueprint, current_app, jsonify, render_template, request, session
from sqlalchemy import (Column, Date, DateTime, ForeignKeyConstraint, Integer,
                        MetaData, String, Table, Text, and_, func, select, text)
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from paper_server.accounts import current_account_id, favorite_sql
from paper_server.obsidian_reviews import REVIEW_WRITER, ObsidianReviewSync, require_review_writer


SEOUL = ZoneInfo('Asia/Seoul')
metadata = MetaData()
batches = Table('daily_review_batches', metadata,
    Column('owner', String(80), primary_key=True),
    Column('review_date', Date, primary_key=True),
    mysql_charset='utf8mb4', mysql_collate='utf8mb4_unicode_ci')
picks = Table('daily_review_picks', metadata,
    Column('owner', String(80), primary_key=True),
    Column('review_date', Date, primary_key=True),
    Column('slot', Integer, primary_key=True),
    Column('article_number', String(80), nullable=False),
    ForeignKeyConstraint(['owner', 'review_date'],
                         ['daily_review_batches.owner', 'daily_review_batches.review_date']),
    mysql_charset='utf8mb4', mysql_collate='utf8mb4_unicode_ci')
reviews = Table('personal_paper_reviews', metadata,
    Column('owner', String(80), primary_key=True),
    Column('article_number', String(80), primary_key=True),
    Column('notes', Text, nullable=False),
    Column('status', String(16), nullable=False),
    Column('version', Integer, nullable=False),
    Column('updated_at', DateTime, nullable=False),
    mysql_charset='utf8mb4', mysql_collate='utf8mb4_unicode_ci')


class ReviewConflict(Exception):
    pass


def today():
    return datetime.now(SEOUL).date()


def review_owner():
    """Only authenticated, server-owned identity can select the private scope."""
    account = current_account_id.get()
    if account is not None:
        return f'account:{account}'
    # Legacy configured logins also keep separate notebooks.
    return 'legacy:' + hashlib.sha256(session['username'].encode('utf-8')).hexdigest()


def serialize(row):
    item = dict(row)
    item.pop('owner', None)
    stamp = item.get('updated_at')
    if stamp:
        if isinstance(stamp, str):
            stamp = datetime.fromisoformat(stamp)
        item['updated_at'] = stamp.replace(tzinfo=timezone.utc).isoformat()
    item['notes'] = item.get('notes') or ''
    item['status'] = item.get('status') or 'pending'
    item['version'] = item.get('version') or 0
    return item


def can_write_reviews():
    return session.get('username') == REVIEW_WRITER


def sync_review(engine, owner, article):
    vault = current_app.config.get('OBSIDIAN_VAULT_PATH', '')
    if not vault:
        return dict(status='disabled')
    try:
        return ObsidianReviewSync(engine, vault, media_dir=current_app.config.get('REVIEW_MEDIA_DIR') or None).sync_one(owner, article)
    except (OSError, ValueError, KeyError, SQLAlchemyError):
        current_app.logger.exception('Obsidian review sync failed')
        return dict(status='failed', message='Obsidian 노트를 준비하지 못했습니다. 보관함 경로와 쓰기 권한을 확인한 뒤 다시 열어 주세요.')


PAPER_FIELDS = ('p.article_number,p.title,p.authors,p.year,p.source_name,p.source_system,'
                'p.url,p.pdf_available,r.notes,r.status,r.version,r.updated_at')
REVIEW_JOIN = ('LEFT JOIN personal_paper_reviews r ON '
               'r.owner=:owner AND r.article_number=p.article_number ')


class DailyReviewStore:
    def __init__(self, engine):
        self.engine = engine

    def daily(self, owner, day):
        """Persist even an empty day. Concurrent first visits share one atomic batch."""
        try:
            with self.engine.begin() as conn:
                exists = conn.execute(select(batches.c.owner).where(
                    batches.c.owner == owner, batches.c.review_date == day)).first()
                if not exists:
                    # IDs only: sampling does not sort or fetch the whole paper corpus.
                    candidates = list(conn.execute(text(
                        'SELECT p.article_number FROM papers p ' + REVIEW_JOIN +
                        f'WHERE {favorite_sql("p")}=1 '
                        "AND COALESCE(r.status,'pending') <> 'completed' "
                        "AND p.article_number IS NOT NULL AND TRIM(p.title) <> ''"),
                        {'owner': owner}).scalars())
                    chosen = random.SystemRandom().sample(candidates, min(2, len(candidates)))
                    conn.execute(batches.insert().values(owner=owner, review_date=day))
                    if chosen:
                        conn.execute(picks.insert(), [dict(owner=owner, review_date=day,
                            slot=slot, article_number=article) for slot, article in enumerate(chosen, 1)])
        except IntegrityError:
            # The unique date/owner key settles races between browser tabs/workers.
            with self.engine.connect() as conn:
                if not conn.execute(select(batches.c.owner).where(
                        batches.c.owner == owner, batches.c.review_date == day)).first():
                    raise
        with self.engine.connect() as conn:
            items = [serialize(row) for row in conn.execute(text(
                'SELECT ' + PAPER_FIELDS + ' FROM daily_review_picks d '
                'JOIN papers p ON p.article_number=d.article_number ' + REVIEW_JOIN +
                'WHERE d.owner=:owner AND d.review_date=:day ORDER BY d.slot'),
                {'owner': owner, 'day': day}).mappings()]
            remaining = conn.execute(text(
                'SELECT COUNT(*) FROM papers p ' + REVIEW_JOIN +
                f'WHERE {favorite_sql("p")}=1 '
                "AND COALESCE(r.status,'pending') <> 'completed'"), {'owner': owner}).scalar()
        return dict(date=day.isoformat(), timezone='Asia/Seoul', items=items,
                    remaining=remaining, daily_limit=2)

    def history(self, owner, page=1):
        # Keep every previously assigned paper reachable, including unfinished days.
        grouped = select(picks.c.article_number,
            func.max(picks.c.review_date).label('last_recommended')).where(
                picks.c.owner == owner).group_by(picks.c.article_number).subquery()
        with self.engine.connect() as conn:
            total = conn.execute(select(func.count()).select_from(grouped)).scalar()
            items = [serialize(row) for row in conn.execute(text(
                'SELECT ' + PAPER_FIELDS + ',d.last_recommended FROM ('
                'SELECT article_number,MAX(review_date) last_recommended FROM daily_review_picks '
                'WHERE owner=:owner GROUP BY article_number) d '
                'JOIN papers p ON p.article_number=d.article_number ' + REVIEW_JOIN +
                'ORDER BY d.last_recommended DESC,p.article_number LIMIT 20 OFFSET :offset'),
                {'owner': owner, 'offset': (page - 1) * 20}).mappings()]
        for item in items:
            item['last_recommended'] = str(item['last_recommended'])
        return dict(items=items, total=total, page=page, page_size=20)

    def save(self, owner, article, notes, status, version):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        predicate = and_(reviews.c.owner == owner, reviews.c.article_number == article)
        try:
            with self.engine.begin() as conn:
                # Only papers actually assigned to this notebook may be reviewed.
                require_review_writer(conn, owner)
                assigned = conn.execute(select(picks.c.article_number).where(
                    picks.c.owner == owner, picks.c.article_number == article).limit(1)).first()
                if not assigned:
                    raise KeyError(article)
                values = dict(status=status, version=version + 1, updated_at=now)
                if notes is not None:
                    values['notes'] = notes
                if version == 0:
                    values.setdefault('notes', '')
                    conn.execute(reviews.insert().values(owner=owner, article_number=article, **values))
                else:
                    result = conn.execute(reviews.update().where(predicate, reviews.c.version == version)
                                          .values(**values))
                    if result.rowcount != 1:
                        raise ReviewConflict()
                row = conn.execute(select(reviews).where(predicate)).mappings().one()
        except IntegrityError as exc:
            raise ReviewConflict() from exc
        return serialize(row)


def create_daily_review_blueprint(get_engine, csrf_token):
    bp = Blueprint('daily_review', __name__)

    def with_note_links(result):
        if can_write_reviews() and current_app.config.get('OBSIDIAN_VAULT_PATH'):
            try:
                notebook = ObsidianReviewSync(get_engine(), current_app.config['OBSIDIAN_VAULT_PATH'])
                for item in result['items']:
                    item['obsidian_note_exists'] = notebook.exists(item['article_number'])
                    if item['obsidian_note_exists']:
                        item['obsidian_url'] = notebook.uri(item['article_number'])
            except (OSError, ValueError):
                pass  # The explicit open action reports configuration errors.
        return result

    @bp.route('/daily-review')
    def page():
        return render_template('daily_review.html', username=session.get('username'),
                               can_write_reviews=can_write_reviews(),
                               obsidian_enabled=bool(current_app.config.get('OBSIDIAN_VAULT_PATH')),
                               csrf_token=csrf_token())

    @bp.route('/api/daily-review')
    def daily():
        result = DailyReviewStore(get_engine()).daily(review_owner(), today())
        return jsonify(ok=True, can_write_reviews=can_write_reviews(), **with_note_links(result))

    @bp.route('/api/daily-review/history')
    def history():
        page = request.args.get('page', '1')
        if not 1 <= len(page) <= 6 or not page.isascii() or not page.isdigit() or not 1 <= int(page) <= 100000:
            return jsonify(ok=False, error='올바른 페이지 번호를 입력하세요.'), 400
        return jsonify(ok=True, **with_note_links(DailyReviewStore(get_engine()).history(review_owner(), int(page))))

    @bp.route('/api/daily-review/review', methods=['PUT'])
    def save():
        if not can_write_reviews():
            return jsonify(ok=False, error='개인 리뷰 작성·수정은 admin 계정만 가능합니다.'), 403
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(ok=False, error='리뷰 내용을 확인하세요.'), 400
        article, notes = data.get('article_number'), data.get('notes')
        status, version = data.get('status'), data.get('version')
        if (not isinstance(article, str) or not 1 <= len(article) <= 80
                or ('notes' in data and (not isinstance(notes, str) or len(notes) > 12000))
                or not isinstance(status, str) or status not in {'reading', 'completed'}
                or type(version) is not int or not 0 <= version <= 2147483646):
            return jsonify(ok=False, error='리뷰는 12,000자 이내로 입력하고 진행 상태를 선택하세요.'), 400
        try:
            item = DailyReviewStore(get_engine()).save(review_owner(), article, notes, status, version)
        except KeyError:
            return jsonify(ok=False, error='내 추천 기록에서 논문을 찾을 수 없습니다.'), 404
        except ReviewConflict:
            return jsonify(ok=False, error='다른 창에서 진행 상태를 수정했습니다. 새로고침한 뒤 다시 저장하세요.'), 409
        except PermissionError:
            return jsonify(ok=False, error='개인 리뷰 작성·수정은 admin 계정만 가능합니다.'), 403
        # Completion tracking never writes to the vault. Legacy clients can still
        # import their old web notes once; existing Obsidian notes remain untouched.
        linked = sync_review(get_engine(), review_owner(), article) if 'notes' in data else dict(status='external')
        return jsonify(ok=True, item=item, obsidian_sync=linked)

    @bp.route('/api/daily-review/obsidian-note', methods=['POST'])
    @bp.route('/api/daily-review/obsidian-sync', methods=['POST'])
    def obsidian_sync():
        if not can_write_reviews():
            return jsonify(ok=False, error='Obsidian 리뷰 노트는 admin 계정만 열 수 있습니다.'), 403
        data = request.get_json(silent=True)
        article = data.get('article_number') if isinstance(data, dict) else None
        if not isinstance(article, str) or not 1 <= len(article) <= 80:
            return jsonify(ok=False, error='논문 ID를 확인하세요.'), 400
        with get_engine().connect() as conn:
            exists = conn.execute(select(picks.c.article_number).where(
                picks.c.owner == review_owner(), picks.c.article_number == article).limit(1)).first()
        if not exists:
            return jsonify(ok=False, error='내 추천 기록에서 논문을 찾을 수 없습니다.'), 404
        result = sync_review(get_engine(), review_owner(), article)
        if result['status'] == 'disabled':
            return jsonify(ok=False, error='Obsidian 보관함 경로가 설정되지 않았습니다.'), 503
        if result['status'] == 'failed':
            return jsonify(ok=False, error=result['message']), 503
        return jsonify(ok=True, obsidian_sync=result)

    @bp.errorhandler(SQLAlchemyError)
    def unavailable(error):
        return jsonify(ok=False, error='리뷰 저장소에 연결할 수 없습니다. 잠시 후 다시 시도하세요.'), 503

    return bp

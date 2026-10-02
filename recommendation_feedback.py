"""Shared, explicit recommendation exclusions; favorites remain independent."""
from sqlalchemy import Column, DateTime, MetaData, String, Table, text
from account_store import current_account_id
from sqlalchemy.exc import ProgrammingError

metadata = MetaData()
feedback_table = Table('recommendation_feedback', metadata,
    Column('article_number', String(255), primary_key=True),
    Column('action', String(20), nullable=False),
    Column('updated_at', DateTime, nullable=False, server_default=text('CURRENT_TIMESTAMP')))


class FeedbackStore:
    def __init__(self, engine):
        self.engine = engine

    def read(self):
        account_id = current_account_id.get()
        if account_id is not None:
            with self.engine.connect() as conn:
                rows = list(conn.execute(text('SELECT article_number,action,updated_at '
                    'FROM account_recommendation_feedback WHERE account_id=:account'),
                    {'account': account_id}).mappings())
            return {'available': True, 'actions': {str(r['article_number']): dict(r) for r in rows}}
        try:
            with self.engine.connect() as conn:
                rows = list(conn.execute(text('SELECT article_number,action,updated_at FROM recommendation_feedback')).mappings())
        except ProgrammingError as exc:
            if getattr(exc.orig, 'args', (None,))[0] != 1146:
                raise
            return {'available': False, 'actions': {}}
        return {'available': True, 'actions': {str(r['article_number']): dict(r) for r in rows}}

    def save(self, article, action):
        if not isinstance(action, str) or action not in {'dismissed', 'reviewed', 'restore'}:
            raise ValueError('지원하지 않는 추천 피드백입니다.')
        with self.engine.begin() as conn:
            account_id = current_account_id.get()
            if account_id is not None:
                account = conn.execute(text('SELECT role,is_active FROM accounts WHERE id=:id FOR UPDATE'),
                                       {'id': account_id}).mappings().first()
                if not account or not account['is_active'] or account['role'] not in {'admin', 'member'}:
                    raise PermissionError('personal settings permission required')
            paper = conn.execute(text('SELECT article_number FROM papers WHERE article_number=:article FOR UPDATE'),
                                 {'article': article}).first()
            if paper is None:
                raise KeyError(article)
            if account_id is not None:
                params = {'account': account_id, 'article': article, 'action': action}
                conn.execute(text('DELETE FROM account_recommendation_feedback '
                    'WHERE account_id=:account AND article_number=:article'), params)
                if action != 'restore':
                    conn.execute(text('INSERT INTO account_recommendation_feedback '
                        '(account_id,article_number,action) VALUES (:account,:article,:action)'), params)
                return
            conn.execute(feedback_table.delete().where(feedback_table.c.article_number == article))
            if action != 'restore':
                conn.execute(feedback_table.insert().values(article_number=article, action=action))

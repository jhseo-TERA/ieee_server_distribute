"""Create review notes once; Obsidian owns all subsequent body edits."""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import unicodedata
from datetime import datetime, timezone
from urllib.parse import quote, unquote, urlencode, urlsplit
from zoneinfo import ZoneInfo

from sqlalchemy import text
from paper_server.review_media import ReviewMedia, article_token, media_root


REVIEW_WRITER = 'admin'
ROOT = Path(__file__).resolve().parents[1]
SEOUL = ZoneInfo('Asia/Seoul')
START = '<!-- paper-server-review:start -->'
END = '<!-- paper-server-review:end -->'
_lock = threading.RLock()


class ObsidianConflict(ValueError):
    pass


def require_review_writer(conn, owner):
    if owner == 'legacy:' + hashlib.sha256(REVIEW_WRITER.encode()).hexdigest():
        return
    account = owner.removeprefix('account:')
    if not owner.startswith('account:') or not account.isascii() or not account.isdigit():
        raise PermissionError('개인 리뷰 작성은 admin 계정만 가능합니다.')
    row = conn.execute(text('SELECT username,is_active FROM accounts WHERE id=:id'),
                       {'id': int(account)}).mappings().first()
    if not row or not row['is_active'] or row['username'] != REVIEW_WRITER:
        raise PermissionError('개인 리뷰 작성은 admin 계정만 가능합니다.')


def _article_key(article):
    key = article if re.fullmatch(r'[A-Za-z0-9._-]{1,80}', article) else hashlib.sha256(article.encode()).hexdigest()[:24]
    return key


def note_title(row):
    """Compact only title words; keep the publisher's full title in YAML."""
    title = _line(row.get('title'))
    title = re.sub(r'^(?:\d+\.\d+\s+)?(?:a|an|the)\s+', '', title, flags=re.I)
    title = re.sub(r'\b(\d+(?:\.\d+)?)\s*([TGMK])(?:b|bit)/s\b',
                   lambda match: match[1]+match[2].upper()+'bps', title, flags=re.I)
    title = re.sub(r'\bPAM[- ](\d+)\b', r'PAM\1', title, flags=re.I)
    for word, compact in (('Transmitter','TX'),('Receiver','RX'),('Transceiver','TRX')):
        title = re.sub(r'\b'+word+r'\b', compact, title, flags=re.I)
    title = re.sub(r'\b(?:with|in|for)\b', '', title, flags=re.I)
    title = re.sub(r'\s+', ' ', title).strip()
    year = str(row.get('year') or '')
    prefix = ' '.join(value for value in (_line(row.get('source_name')),year if re.fullmatch(r'\d{4}',year) else '') if value)
    return (prefix+' - ' if prefix else '') + (title or 'Paper')


def note_filename(article, row=None):
    if row is None:
        return 'paper-server-' + _article_key(article) + '.md'  # Existing notes stay reachable.
    stem = unicodedata.normalize('NFC',note_title(row))
    stem = re.sub(r'[<>:"/\\|?*\[\]#^\x00-\x1f]', ' ', stem)
    stem = re.sub(r'\s+', ' ', stem).strip()[:100].rstrip(' .') or 'Paper'
    if stem.split('.')[0].upper() in {'CON','PRN','AUX','NUL',*(f'COM{i}' for i in range(1,10)),*(f'LPT{i}' for i in range(1,10))}:
        stem = 'Paper - '+stem
    return stem+'.md'


def _digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _line(value):
    return str(value or '').replace('\r', ' ').replace('\n', ' ')


class ObsidianReviewSync:
    def __init__(self, engine, vault, repository=ROOT, media_dir=None):
        self.engine = engine
        self.vault = Path(vault).resolve()
        self.repository = Path(repository).resolve()
        asset_root = media_dir or (media_root() if self.repository == ROOT else self.repository/'py_01_data/06_personal_reviews')
        self.media = ReviewMedia(asset_root)
        if not (self.vault / '.obsidian').is_dir() or not (self.vault / 'templates/paper.md').is_file():
            raise ValueError('기존 Obsidian 보관함과 논문 양식을 확인하세요.')

    def _path(self, relative):
        path = self.vault / relative
        if not path.resolve().is_relative_to(self.vault):
            raise ValueError('보관함 밖의 경로에는 쓸 수 없습니다.')
        return path

    def _write(self, relative, content):
        path = self._path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Recheck after mkdir; do not follow an existing directory link outside the vault.
        self._path(relative)
        temporary = None
        try:
            binary = isinstance(content,bytes)
            options = {} if binary else dict(encoding='utf-8',newline='\n')
            with tempfile.NamedTemporaryFile(mode='wb' if binary else 'w', **options,
                    dir=path.parent, prefix='.paper-server-', suffix='.tmp', delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(content)
            os.replace(temporary, path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    def _state(self):
        path = self._path('work/paper-server-reviews.json')
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'notes': {}}

    def filename(self, article):
        file = self._state()['notes'].get(article,{}).get('file')
        if file is None:
            return note_filename(article)
        if not isinstance(file,str) or re.search(r'[<>:"/\\|?*\x00-\x1f]',file) or not file.endswith('.md'):
            raise ValueError('노트 파일 경로를 확인하세요.')
        self._path('wiki/sources/'+file)
        return file

    def uri(self, article):
        file = 'wiki/sources/' + self.filename(article)
        return 'obsidian://open?' + urlencode({'vault': self.vault.name, 'file': file})

    def exists(self, article):
        return self._path('wiki/sources/' + self.filename(article)).is_file()

    def _new_filename(self, article, row, state):
        if article in state['notes']:
            return self.filename(article)
        legacy = note_filename(article)
        if self._path('wiki/sources/'+legacy).exists():
            return legacy
        desired = note_filename(article,row)
        used = {entry['file'] for key,entry in state['notes'].items() if key!=article}
        candidate = desired
        number = 0
        while candidate.casefold() in {file.casefold() for file in used} or self._path('wiki/sources/'+candidate).exists():
            suffix = ' - '+_article_key(article)+(f'-{number}' if number else '')
            candidate = desired[:-3][:max(1,100-len(suffix))].rstrip(' .')+suffix+'.md'
            number += 1
        return candidate

    def _create_note(self, relative, content):
        """Exclusive creation also protects notes made concurrently in Obsidian."""
        path = self._path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path(relative)
        try:
            with path.open('x', encoding='utf-8', newline='\n') as handle:
                handle.write(content)
            return True
        except FileExistsError:
            if not path.is_file():
                raise ValueError('노트 경로를 확인하세요.')
            return False

    def _pdf(self, row):
        directories = {'ieee':'ieee-pdf', 'optica':'optica-pdf', 'nature':'nature-pdf', 'designcon':'DesignCon'}
        directory = directories.get(row['source_system'])
        if not directory or not row['pdf_available']:
            return None
        base = (self.repository / directory).resolve()
        local = row.get('pdf_local_path')
        candidate = (self.repository / str(local).replace('\\', '/')) if local else base / (row['article_number'] + '.pdf')
        candidate = candidate.resolve()
        return candidate if candidate.is_relative_to(base) and candidate.suffix.lower() == '.pdf' and candidate.is_file() else None

    def _block(self, row, relative, owner):
        pdf = self._pdf(row)
        links = []
        if pdf:
            links.append(f'[원본 PDF]({pdf.as_uri()})')
        url = row.get('url') or ''
        if urlsplit(url).scheme in {'http','https'}:
            links.append(f'[출판사 원문]({quote(url, safe=":/?=&%#@+;,~")})')
        def copy_asset(source):
            data = source.read_bytes()
            if len(data) > 64*1024*1024:
                raise ValueError('리뷰 첨부가 64MB를 초과합니다.')
            filename = hashlib.sha256(data).hexdigest()[:16] + '-' + source.name
            relative_asset = 'raw/assets/paper-reviews/' + article_token(row['article_number']) + '/' + filename
            destination = self._path(relative_asset)
            if destination.exists() and destination.read_bytes() != data:
                raise ObsidianConflict('기존 Obsidian 첨부가 직접 수정되어 덮어쓰지 않았습니다.')
            if not destination.exists():
                self._write(relative_asset,data)
            return quote(os.path.relpath(destination,self._path(relative).parent).replace('\\','/'),safe='/.:')
        if pdf and pdf.stat().st_size <= 64*1024*1024:
            copied = (self._path(relative).parent / unquote(copy_asset(pdf))).resolve()
            links.insert(0,'[Acrobat에서 PDF 열기](' + copied.as_uri() + ')')
        links.append('[웹 추천](http://127.0.0.1:5001/daily-review)')
        notes = self.media.export_notes(owner,row['article_number'],row.get('notes') or '',copy_asset)
        return (' · '.join(links)+'\n\n## 핵심 내용\n\n'+(notes+'\n\n' if notes else '')+
            '## 그림 수식\n\n## 총평\n\n## 외부 편찬\n\n')

    def _new_note(self, row, block):
        values = dict(title=row['title'], venue=row.get('source_name'),
            year=int(row['year']) if re.fullmatch(r'\d{4}', str(row.get('year') or '')) else None,
            doi=row.get('doi'))
        front = '\n'.join(f'{key}: '+json.dumps(value,ensure_ascii=False) for key,value in values.items()
                          if value is not None and (not isinstance(value,str) or value.strip()))
        return f'---\n{front}\n---\n# {note_title(row)}\n\n{block}'

    def _hub(self, state):
        day = datetime.now(SEOUL).date().isoformat()
        entries = sorted(state['notes'].values(), key=lambda row: (row['updated_at'], row['file']), reverse=True)
        lines = []
        for entry in entries:
            title = _line(entry['title']).replace('[', '\\[').replace(']', '\\]')
            lines.append(f'- [{title}](sources/{quote(entry["file"],safe="")})')
        body = '\n'.join(lines) if lines else '아직 만든 리뷰 노트가 없습니다. 웹에서 Obsidian 리뷰를 열면 이곳에 추가됩니다.'
        return (f'# 논문 서버 개인 리뷰\n\n마지막 갱신: {day}\n\n'
            '[웹 개인 리뷰 열기](http://127.0.0.1:5001/daily-review) · [위키 목차](index.md)\n\n'
            f'`{REVIEW_WRITER}`가 웹의 **Obsidian에서 리뷰하기**로 노트를 만들거나 엽니다. '
            '그림·수식·텍스트는 Obsidian에서 직접 편집하고, 완료 체크는 웹에서 관리합니다.\n\n'
            '논문 노트는 처음 한 번만 생성합니다. 다시 열거나 웹에서 완료 상태를 바꿔도 기존 노트는 덮어쓰지 않습니다. '
            'Obsidian의 리뷰 본문을 웹 DB로 가져오지 않습니다.\n\n'
            '원본 PDF는 논문 서버 폴더의 파일을 상대 경로로 연결합니다. 두 프로젝트를 다른 PC로 옮길 때는 경로를 확인하세요.\n\n'
            f'## 저장한 리뷰\n\n{START}\n{body}\n{END}\n')

    def _write_hub(self, state):
        generated = self._hub(state)
        pattern = r'^' + re.escape(START) + r'\n.*?^' + re.escape(END) + r'$'
        block = re.search(pattern, generated, re.M | re.S).group()
        path = self._path('wiki/paper-reviews.md')
        if path.exists():
            current = path.read_text(encoding='utf-8')
            existing = re.search(pattern, current, re.M | re.S)
            if not existing or _digest(existing.group()) != state.get('hub_digest'):
                raise ObsidianConflict('Obsidian 리뷰 목차의 자동 구간이 수정되어 덮어쓰지 않았습니다.')
            generated = current[:existing.start()] + block + current[existing.end():]
            generated = re.sub(r'마지막 갱신: \d{4}-\d{2}-\d{2}', '마지막 갱신: ' + datetime.now(SEOUL).date().isoformat(), generated, count=1)
        self._write('wiki/paper-reviews.md', generated)
        state['hub_digest'] = _digest(block)

    def initialize(self):
        with _lock:
            state = self._state()
            hub = self._path('wiki/paper-reviews.md')
            if hub.exists() and not state.get('initialized'):
                raise ObsidianConflict('기존 개인 리뷰 목차가 있어 자동으로 덮어쓰지 않았습니다.')
            if not state.get('initialized'):
                self._write_hub(state)
                index_path = self._path('wiki/index.md')
                index = index_path.read_text(encoding='utf-8')
                if '(paper-reviews.md)' not in index:
                    section = '## 개인 논문 리뷰\n\n- [논문 서버 개인 리뷰](paper-reviews.md) — Obsidian에서 편집하는 리뷰 노트와 원본 PDF 링크.\n\n'
                    index = index.replace('## 관리', section + '## 관리') if '## 관리' in index else index + '\n' + section
                    index = re.sub(r'마지막 갱신: \d{4}-\d{2}-\d{2}', '마지막 갱신: ' + datetime.now(SEOUL).date().isoformat(), index, count=1)
                    self._write('wiki/index.md', index)
                state['initialized'] = True
                self._write('work/paper-server-reviews.json', json.dumps(state, ensure_ascii=False, indent=2) + '\n')
                self._log('논문 서버 개인 리뷰 연동', '사용자의 직접 요청에 따라 Obsidian 리뷰 노트 열기와 원본 PDF 링크를 연결했다.', 'paper-reviews.md')
            return state

    def _log(self, title, detail, link):
        log_path = self._path('wiki/log.md')
        content = log_path.read_text(encoding='utf-8')
        day = datetime.now(SEOUL).date().isoformat()
        content += f'\n## {day} | {title}\n\n- 수행: {detail}\n- 출처·수정 문서: [개인 리뷰]({link}).\n- 미확인: 원문 내용의 자동 분석·검증은 수행하지 않았다.\n'
        self._write('wiki/log.md', content)

    def _index_note(self, row, file):
        path = self._path('wiki/index.md')
        content = path.read_text(encoding='utf-8')
        target = '(sources/' + quote(file,safe='') + ')'
        if target not in content:
            title = _line(note_title(row)).replace('[', '\\[').replace(']', '\\]')
            line = f'- [{title}]{target} — Obsidian에서 직접 편집하는 개인 리뷰.\n'
            heading = '## 개인 논문 리뷰\n'
            start = content.find(heading)
            end = content.find('\n## ', start + len(heading)) if start >= 0 else -1
            if end >= 0:
                content = content[:end].rstrip() + '\n' + line + '\n' + content[end:]
            else:
                content = content.rstrip() + '\n' + line
        content = re.sub(r'마지막 갱신: \d{4}-\d{2}-\d{2}', '마지막 갱신: ' + datetime.now(SEOUL).date().isoformat(), content, count=1)
        self._write('wiki/index.md', content)

    def sync_one(self, owner, article):
        with _lock:
            return self._sync_one(owner, article)

    def _sync_one(self, owner, article):
        with self.engine.connect() as conn:
            require_review_writer(conn, owner)
            row = conn.execute(text('SELECT p.article_number,p.title,p.authors,p.year,p.source_name,'
                'p.source_system,p.url,p.doi,p.pdf_available,p.pdf_local_path,r.notes,r.status,r.version,r.updated_at '
                'FROM papers p LEFT JOIN personal_paper_reviews r ON p.article_number=r.article_number AND r.owner=:owner '
                'WHERE p.article_number=:article AND EXISTS (SELECT 1 FROM daily_review_picks d '
                'WHERE d.owner=:owner AND d.article_number=p.article_number)'), {'owner':owner, 'article':article}).mappings().first()
        if row is None:
            raise KeyError(article)
        with _lock:
            state = self.initialize()
            file = self._new_filename(article,row,state)
            relative = 'wiki/sources/' + file
            path = self._path(relative)
            created = False
            if not path.exists():
                created = self._create_note(relative, self._new_note(row, self._block(row, relative, owner)))
            elif not path.is_file():
                raise ValueError('노트 경로를 확인하세요.')
            # No reads, checksums or writes of an existing note: all of it belongs to Obsidian.
            if article in state['notes'] and not created:
                return dict(status='synced', url=self.uri(article), created=False)
            state['notes'][article] = dict(file=file, title=note_title(row),
                updated_at=datetime.now(timezone.utc).isoformat())
            self._write_hub(state)
            self._write('work/paper-server-reviews.json', json.dumps(state, ensure_ascii=False, indent=2) + '\n')
            self._index_note(row, file)
            self._log('Obsidian 리뷰 노트 연결', '논문 노트를 처음 생성하거나 기존 노트를 목차에 연결했다. 이후 본문 편집은 Obsidian에서 수행한다.', 'sources/' + file)
            return dict(status='synced', url=self.uri(article), created=created)

    def sync_all(self, owner):
        with self.engine.connect() as conn:
            require_review_writer(conn, owner)
            articles = list(conn.execute(text('SELECT article_number FROM personal_paper_reviews WHERE owner=:owner'), {'owner':owner}).scalars())
        self.initialize()
        for article in articles:
            self.sync_one(owner, article)
        return dict(status='synced', count=len(articles))

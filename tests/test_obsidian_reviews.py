"""Real filesystem + SQL tests for authorized, non-destructive Obsidian exports."""
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, quote, unquote, urlsplit
from urllib.request import url2pathname

from sqlalchemy import text

from paper_server.obsidian_reviews import ObsidianConflict, ObsidianReviewSync, note_filename, note_title
from tests import test_daily_review as fixtures
import web.app as server


class ObsidianReviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.DailyReviewTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        workspace_tmp = Path(__file__).resolve().parents[1] / 'tmp'
        self.directory = tempfile.TemporaryDirectory(prefix='obsidian-review-test-', dir=workspace_tmp)
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.assertTrue(self.root.resolve().is_relative_to(workspace_tmp.resolve()))
        self.vault, self.repository = self.root / 'LLM_Wiki', self.root / 'Paper_Server'
        for relative in ('.obsidian', 'templates', 'wiki/sources'):
            (self.vault / relative).mkdir(parents=True)
        (self.repository / 'ieee-pdf').mkdir(parents=True)
        (self.vault / 'templates/paper.md').write_text('---\ntitle:\nvenue:\nyear:\ndoi:\n---\n', encoding='utf-8')
        (self.vault / 'wiki/index.md').write_text('# 기존 목차\n\n마지막 갱신: 2026-10-11\n\n## 관리\n\n기존 링크 유지.\n', encoding='utf-8')
        (self.vault / 'wiki/log.md').write_text('# 작업 기록\n\n기존 기록 유지.\n', encoding='utf-8')
        with self.fixture.engine.begin() as conn:
            conn.execute(text('ALTER TABLE papers ADD COLUMN doi TEXT'))
            conn.execute(text('ALTER TABLE papers ADD COLUMN pdf_local_path TEXT'))
        self.sync = ObsidianReviewSync(self.fixture.engine, self.vault, self.repository)

    def review(self, notes='사용자가 쓴 리뷰\n다음에 확인할 내용', status='reading'):
        item = self.fixture.daily()['items'][0]
        pdf = self.repository / 'ieee-pdf' / (item['article_number'] + '.pdf')
        pdf.write_bytes(b'%PDF-1.4 test-only path fixture')
        with self.fixture.engine.begin() as conn:
            conn.execute(text('UPDATE papers SET pdf_local_path=:path WHERE article_number=:article'),
                         {'path':'ieee-pdf/' + pdf.name, 'article':item['article_number']})
        result = self.fixture.save(item, notes=notes, status=status)
        self.assertEqual(result.status_code, 200, result.json)
        return result.json['item']

    def test_exports_saved_review_with_pdf_link_template_fields_and_obsidian_uri(self):
        item = self.review(status='completed')
        with self.fixture.engine.begin() as conn:
            conn.execute(text('UPDATE papers SET doi=:doi WHERE article_number=:article'),
                         dict(doi='10.1234/example',article=item['article_number']))
        result = self.sync.sync_one('account:1', item['article_number'])
        note = self.vault / 'wiki/sources' / self.sync.filename(item['article_number'])
        content = note.read_text(encoding='utf-8')
        self.assertIn('사용자가 쓴 리뷰\n다음에 확인할 내용', content)
        self.assertNotRegex(content, re.compile('^status: reviewed$', re.M))
        self.assertNotIn('paper-server-review:start', content)
        front=content.split('---\n',2)[1]
        self.assertEqual(re.findall(r'^(\w+):',front,re.M),['title','venue','year','doi'])
        self.assertEqual(re.findall(r'^## (.+)$',content,re.M),['핵심 내용','그림 수식','총평','외부 편찬'])
        relative_pdf = re.search(r'\[원본 PDF\]\(([^)]+)\)', content).group(1)
        self.assertEqual(urlsplit(relative_pdf).scheme,'file')
        original_pdf=Path(url2pathname(urlsplit(relative_pdf).path))
        self.assertTrue(original_pdf.is_file())
        vault_pdf = re.search(r'\[Acrobat에서 PDF 열기\]\(([^)]+)\)', content).group(1)
        self.assertEqual(urlsplit(vault_pdf).scheme,'file')
        copied = Path(url2pathname(urlsplit(vault_pdf).path)).resolve()
        self.assertTrue(copied.is_relative_to(self.vault))
        self.assertEqual(copied.read_bytes(),original_pdf.read_bytes())
        query = parse_qs(urlsplit(result['url']).query)
        self.assertEqual(query['vault'], ['LLM_Wiki'])
        self.assertEqual(query['file'], ['wiki/sources/' + note.name])
        self.assertIn(quote(note.name,safe=''), (self.vault / 'wiki/paper-reviews.md').read_text(encoding='utf-8'))
        index = (self.vault / 'wiki/index.md').read_text(encoding='utf-8')
        self.assertIn('(paper-reviews.md)', index)
        self.assertIn('(sources/' + quote(note.name,safe='') + ')', index)
        self.assertIn('기존 링크 유지.', index)
        self.assertIn('기존 기록 유지.', (self.vault / 'wiki/log.md').read_text(encoding='utf-8'))

    def test_same_review_is_idempotent_and_does_not_duplicate_log_or_notes(self):
        item = self.review()
        self.sync.sync_one('account:1',item['article_number'])
        note = self.vault / 'wiki/sources' / self.sync.filename(item['article_number'])
        original = note.read_bytes()
        log = (self.vault / 'wiki/log.md').read_bytes()
        self.sync.sync_one('account:1',item['article_number'])
        self.assertEqual(note.read_bytes(), original)
        self.assertEqual((self.vault / 'wiki/log.md').read_bytes(), log)
        self.assertEqual(len(list((self.vault / 'wiki/sources').glob('*.md'))), 1)

    def test_preserves_obsidian_user_sections_and_user_reviewed_status(self):
        item = self.review()
        self.sync.sync_one('account:1',item['article_number'])
        note = self.vault / 'wiki/sources' / self.sync.filename(item['article_number'])
        note.write_text(note.read_text(encoding='utf-8').replace('---\n', '---\nstatus: reviewed\n',1) + '\n## 사용자가 추가한 설계 정리\n원문 대조 내용 유지.\n', encoding='utf-8')
        hub = self.vault / 'wiki/paper-reviews.md'
        hub.write_text(hub.read_text(encoding='utf-8') + '\n추가한 읽기 계획 유지.\n', encoding='utf-8')
        update = self.fixture.save(item, notes='새로운 웹 리뷰', status='completed')
        self.assertEqual(update.status_code, 200)
        self.sync.sync_one('account:1',item['article_number'])
        content = note.read_text(encoding='utf-8')
        self.assertNotIn('새로운 웹 리뷰', content)
        self.assertIn('원문 대조 내용 유지.', content)
        self.assertIn('status: reviewed', content)
        self.assertIn('추가한 읽기 계획 유지.', hub.read_text(encoding='utf-8'))

    def test_all_obsidian_edits_are_preserved_even_in_legacy_managed_sections(self):
        item = self.review()
        self.sync.sync_one('account:1',item['article_number'])
        note = self.vault / 'wiki/sources' / self.sync.filename(item['article_number'])
        changed = '# Obsidian에서 직접 바꾼 제목\n\n<!-- paper-server-review:start -->\n$$x^2$$\n![그림](figures/test.png)\n<!-- paper-server-review:end -->\n'
        note.write_text(changed,encoding='utf-8')
        self.fixture.save(item,notes='새로운 웹 내용')
        result = self.sync.sync_one('account:1',item['article_number'])
        self.assertFalse(result['created'])
        self.assertEqual(note.read_text(encoding='utf-8'),changed)

    def test_empty_notebook_creates_only_index_and_never_placeholder_paper_notes(self):
        result = self.sync.sync_all('account:1')
        self.assertEqual(result['count'],0)
        self.assertEqual(list((self.vault / 'wiki/sources').glob('*.md')),[])
        self.assertIn('아직 만든 리뷰 노트가 없습니다.',(self.vault / 'wiki/paper-reviews.md').read_text(encoding='utf-8'))

    def test_other_accounts_and_unsafe_pdf_paths_do_not_export_private_content(self):
        with self.assertRaises(PermissionError):
            self.sync.sync_all('account:2')
        self.assertFalse((self.vault / 'wiki/paper-reviews.md').exists())
        item = self.review()
        with self.fixture.engine.begin() as conn:
            conn.execute(text('UPDATE papers SET pdf_local_path=:path WHERE article_number=:article'),
                         {'path':'../outside-secret.pdf','article':item['article_number']})
        self.sync.sync_one('account:1',item['article_number'])
        content = (self.vault / 'wiki/sources' / self.sync.filename(item['article_number'])).read_text(encoding='utf-8')
        self.assertNotIn('outside-secret',content)
        self.assertNotIn('[원본 PDF]',content)
        self.assertNotIn('[Acrobat에서 PDF 열기]',content)
        self.assertNotIn('/',note_filename('../../escape'))
        self.assertNotIn('\\',note_filename('../../escape'))

    def test_export_failure_keeps_web_review_saved_and_retry_does_not_resave_it(self):
        item = self.fixture.daily()['items'][0]
        with patch.dict(server.app.config, OBSIDIAN_VAULT_PATH=str(self.vault)):
            with patch('paper_server.daily_review.ObsidianReviewSync', side_effect=OSError('test fixture failure')):
                with self.assertLogs(server.app.logger,level='ERROR'):
                    saved = self.fixture.save(item,notes='동기화 실패해도 보존할 웹 리뷰')
            self.assertEqual(saved.status_code,200)
            self.assertEqual(saved.json['obsidian_sync']['status'],'failed')
            self.assertEqual(saved.json['item']['version'],1)
            with patch('paper_server.daily_review.ObsidianReviewSync',return_value=self.sync):
                retried = self.fixture.admin.post('/api/daily-review/obsidian-sync',
                    json={'article_number':item['article_number']},headers={'X-CSRF-Token':'review-csrf'})
            self.assertEqual(retried.status_code,200)
            self.assertEqual(retried.json['obsidian_sync']['status'],'synced')
            self.assertEqual(self.fixture.daily()['items'][0]['version'],1)

    def test_saving_review_calls_sync_and_returns_a_link_without_changing_wiki_status(self):
        item = self.fixture.daily()['items'][0]
        with patch.dict(server.app.config, OBSIDIAN_VAULT_PATH=str(self.vault)):
            with patch('paper_server.daily_review.ObsidianReviewSync',return_value=self.sync):
                saved = self.fixture.save(item,notes='웹에서 바로 자동 반영',status='completed')
        self.assertEqual(saved.status_code,200)
        self.assertEqual(saved.json['obsidian_sync']['status'],'synced')
        self.assertTrue(saved.json['obsidian_sync']['url'].startswith('obsidian://open?'))
        content = (self.vault / 'wiki/sources' / self.sync.filename(item['article_number'])).read_text(encoding='utf-8')
        self.assertIn('웹에서 바로 자동 반영',content)  # Legacy web notes are imported once.
        self.assertNotRegex(content,re.compile(r'^status:',re.M))

    def test_missing_metadata_is_omitted_instead_of_creating_empty_properties(self):
        content=self.sync._new_note(dict(title='검증 제목',source_name='',year='unknown',doi=None),'## 핵심 내용\n')
        self.assertEqual(content.split('---\n',2)[1],'title: "검증 제목"\n')

    def open_note(self, article, client=None):
        with patch.dict(server.app.config, OBSIDIAN_VAULT_PATH=str(self.vault)):
            with patch('paper_server.daily_review.ObsidianReviewSync',return_value=self.sync):
                return (client or self.fixture.admin).post('/api/daily-review/obsidian-note',
                    json={'article_number':article},headers={'X-CSRF-Token':'review-csrf'})

    def test_open_creates_note_before_any_web_review_and_completion_never_writes_it(self):
        item=self.fixture.daily()['items'][0]
        opened=self.open_note(item['article_number'])
        self.assertEqual(opened.status_code,200,opened.json)
        self.assertTrue(opened.json['obsidian_sync']['created'])
        note=self.vault/'wiki/sources'/self.sync.filename(item['article_number'])
        edited=note.read_text(encoding='utf-8')+'\n## 내가 편집한 리뷰\n$$H(s)=1$$\n'
        note.write_text(edited,encoding='utf-8')
        with self.fixture.engine.connect() as conn:
            self.assertEqual(conn.execute(text('SELECT COUNT(*) FROM personal_paper_reviews')).scalar(),0)
        completed=self.fixture.admin.put('/api/daily-review/review',json=dict(
            article_number=item['article_number'],status='completed',version=0),headers={'X-CSRF-Token':'review-csrf'})
        self.assertEqual(completed.status_code,200,completed.json)
        self.assertEqual(completed.json['obsidian_sync']['status'],'external')
        self.assertEqual(note.read_text(encoding='utf-8'),edited)
        again=self.open_note(item['article_number'])
        self.assertEqual(again.status_code,200)
        self.assertFalse(again.json['obsidian_sync']['created'])
        self.assertEqual(note.read_text(encoding='utf-8'),edited)

    def test_existing_unregistered_note_is_linked_without_reading_or_replacing_it(self):
        article=self.fixture.daily()['items'][0]['article_number']
        note=self.vault/'wiki/sources'/note_filename(article)
        original=b'---\nstatus: reviewed\n---\nMy existing note\n'
        note.write_bytes(original)
        result=self.open_note(article)
        self.assertEqual(result.status_code,200,result.json)
        self.assertFalse(result.json['obsidian_sync']['created'])
        self.assertEqual(note.read_bytes(),original)

    def test_note_created_concurrently_in_obsidian_is_never_replaced(self):
        item=self.fixture.daily()['items'][0]
        article=item['article_number']
        note=self.vault/'wiki/sources'/note_filename(article,item)
        original=self.sync._new_note
        def concurrent_note(row,block):
            note.write_text('Obsidian에서 방금 만든 노트',encoding='utf-8')
            return original(row,block)
        with patch.object(self.sync,'_new_note',side_effect=concurrent_note):
            result=self.open_note(article)
        self.assertEqual(result.status_code,200,result.json)
        self.assertFalse(result.json['obsidian_sync']['created'])
        self.assertEqual(note.read_text(encoding='utf-8'),'Obsidian에서 방금 만든 노트')

    def test_note_open_requires_root_csrf_and_assigned_article(self):
        article=self.fixture.daily()['items'][0]['article_number']
        for client in (self.fixture.member,self.fixture.viewer):
            self.assertEqual(self.open_note(article,client).status_code,403)
        self.assertEqual(self.open_note('5').status_code,404)
        self.assertEqual(self.fixture.admin.post('/api/daily-review/obsidian-note',json={'article_number':article}).status_code,403)
        self.assertFalse(list((self.vault/'wiki/sources').glob('*.md')))

    def test_read_only_list_returns_existing_note_link_for_root_only_and_no_heavy_editor(self):
        article=self.fixture.daily()['items'][0]['article_number']
        self.open_note(article)
        with patch.dict(server.app.config, OBSIDIAN_VAULT_PATH=str(self.vault)):
            with patch('paper_server.daily_review.ObsidianReviewSync',return_value=self.sync):
                result=self.fixture.daily()
                linked=next(item for item in result['items'] if item['article_number']==article)
                self.assertTrue(linked['obsidian_note_exists'])
                self.assertEqual(linked['obsidian_url'],self.sync.uri(article))
                self.assertTrue(all('obsidian_url' not in item for item in self.fixture.daily(self.fixture.member)['items']))
        page=self.fixture.admin.get('/daily-review').get_data(as_text=True)
        for absent in ('review_tools.js','katex','markdown-it','clip-dialog','<textarea'):
            self.assertNotIn(absent,page)
        self.assertIn('Obsidian에서 리뷰하기',page)

    def test_new_note_uses_readable_name_and_heading_but_keeps_original_title(self):
        article=self.fixture.daily()['items'][0]['article_number']
        title='8.1 A 224Gb/s DAC-Based PAM-4 Transmitter with 8-Tap FFE in 10nm CMOS'
        with self.fixture.engine.begin() as conn:
            conn.execute(text("UPDATE papers SET title=:title,source_name='ISSCC',year='2021' WHERE article_number=:article"),dict(title=title,article=article))
        result=self.open_note(article)
        self.assertEqual(result.status_code,200,result.json)
        expected='ISSCC 2021 - 224Gbps DAC-Based PAM4 TX 8-Tap FFE 10nm CMOS'
        self.assertEqual(self.sync.filename(article),expected+'.md')
        note=self.vault/'wiki/sources'/self.sync.filename(article)
        body=note.read_text(encoding='utf-8')
        self.assertIn('# '+expected+'\n',body)
        self.assertIn('title: '+json.dumps(title,ensure_ascii=False),body)
        self.assertIn(quote(expected+'.md',safe=''),(self.vault/'wiki/paper-reviews.md').read_text(encoding='utf-8'))

    def test_note_title_collisions_keep_distinct_papers_and_stable_file_bindings(self):
        articles=[item['article_number'] for item in self.fixture.daily()['items']]
        with self.fixture.engine.begin() as conn:
            conn.execute(text("UPDATE papers SET title='A Shared Transmitter',source_name='ISSCC',year='2021'"))
        for article in articles:
            self.assertEqual(self.open_note(article).status_code,200)
        files=[self.sync.filename(article) for article in articles]
        self.assertNotEqual(files[0],files[1])
        self.assertTrue(all((self.vault/'wiki/sources'/file).is_file() for file in files))
        with self.fixture.engine.begin() as conn:
            conn.execute(text("UPDATE papers SET title='Changed metadata title'"))
        for article,file in zip(articles,files):
            self.assertFalse(self.open_note(article).json['obsidian_sync']['created'])
            self.assertEqual(self.sync.filename(article),file)

    def test_renamed_file_binding_drives_urls_and_never_recreates_legacy_filename(self):
        article=self.fixture.daily()['items'][0]['article_number']
        self.open_note(article)
        old=self.vault/'wiki/sources'/self.sync.filename(article)
        new=old.with_name('직접 정한 리뷰 노트.md')
        old.rename(new)
        state_file=self.vault/'work/paper-server-reviews.json'
        state=json.loads(state_file.read_text(encoding='utf-8'))
        state['notes'][article]['file']=new.name
        state_file.write_text(json.dumps(state),encoding='utf-8')
        contents=new.read_bytes()
        self.assertTrue(self.sync.exists(article))
        self.assertEqual(parse_qs(urlsplit(self.sync.uri(article)).query)['file'],['wiki/sources/'+new.name])
        self.assertFalse(self.open_note(article).json['obsidian_sync']['created'])
        self.assertEqual(new.read_bytes(),contents)
        self.assertFalse(old.exists())

    def test_generated_filenames_are_windows_safe_and_bounded(self):
        file=note_filename('id',dict(title='A TX: [test] / 224Gb/s #notes? '+('long '*100),source_name='ISSCC',year='2021'))
        self.assertLessEqual(len(file),103)
        self.assertNotRegex(file,r'[<>:"/\\|?*\[\]#^]')
        self.assertEqual(note_filename('id',dict(title='CON')),'Paper - CON.md')

    def test_saved_file_binding_cannot_select_a_path_or_ntfs_stream(self):
        for file in ('../secret.md',r'..\secret.md','C:secret.md','/secret.md','note.md:secret.md'):
            with self.subTest(file=file), patch.object(self.sync,'_state',return_value={'notes':{'paper':{'file':file}}}):
                with self.assertRaises(ValueError):
                    self.sync.uri('paper')


if __name__ == '__main__':
    unittest.main()

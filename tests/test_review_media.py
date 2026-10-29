"""PDF clips, saved annotations, uploads and private media authorization."""
import hashlib
import io
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, FloatObject, NameObject, NumberObject, TextStringObject
from sqlalchemy import text

from paper_server.obsidian_reviews import ObsidianReviewSync, note_filename
from paper_server.review_media import DEFAULT_MEDIA_ROOT, ReviewMedia, annotations, article_token, media_root, render_page
from tests import test_daily_review as fixtures
import web.app as server


def sample_pdf(annotated=False):
    writer=PdfWriter()
    page=writer.add_blank_page(width=400,height=300)
    font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
    page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
    stream=DecodedStreamObject()
    stream.set_data(b'BT /F1 18 Tf 35 260 Td (Equalizer transfer function) Tj ET\n'
        b'BT /F1 11 Tf 35 232 Td (Review fixture: figures, highlights and equations.) Tj ET\n'
        b'0.15 0.32 0.63 rg 35 120 90 65 re f 155 120 90 65 re f 275 120 90 65 re f\n'
        b'0 0 0 RG 125 152 m 155 152 l S 245 152 m 275 152 l S\n'
        b'1 1 1 rg BT /F1 14 Tf 52 146 Td (TX FFE) Tj ET BT /F1 14 Tf 173 146 Td (Channel) Tj ET BT /F1 14 Tf 295 146 Td (RX EQ) Tj ET\n'
        b'0 0 0 rg BT /F1 13 Tf 35 70 Td (H(s) = Vout(s) / Vin(s)) Tj ET')
    page[NameObject('/Contents')]=writer._add_object(stream)
    if annotated:
        writer.add_annotation(0,DictionaryObject({NameObject('/Type'):NameObject('/Annot'),NameObject('/Subtype'):NameObject('/Highlight'),
            NameObject('/Rect'):ArrayObject([FloatObject(v) for v in (33,255,320,280)]),
            NameObject('/QuadPoints'):ArrayObject([FloatObject(v) for v in (33,280,320,280,33,255,320,255)]),
            NameObject('/Contents'):TextStringObject('Check gain and bandwidth.'),NameObject('/NM'):TextStringObject('sample-highlight'),
            NameObject('/C'):ArrayObject([FloatObject(1),FloatObject(1),FloatObject(0)]),NameObject('/F'):NumberObject(4)}))
        writer.add_annotation(0,DictionaryObject({NameObject('/Type'):NameObject('/Annot'),NameObject('/Subtype'):NameObject('/Text'),
            NameObject('/Rect'):ArrayObject([FloatObject(v) for v in (20,30,40,50)]),
            NameObject('/Contents'):TextStringObject('Compare equalizer power.'),NameObject('/NM'):TextStringObject('sample-note')}))
    writer.add_blank_page(width=400,height=300)
    output=io.BytesIO(); writer.write(output)
    return output.getvalue()


class ReviewMediaTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.DailyReviewTests(); self.fixture.setUp(); self.addCleanup(self.fixture.tearDown)
        directory=tempfile.TemporaryDirectory(prefix='review-media-test-',dir=Path(__file__).resolve().parents[1]/'tmp')
        self.addCleanup(directory.cleanup); self.root=Path(directory.name)
        self.repository=self.root/'Paper_Server'; (self.repository/'ieee-pdf').mkdir(parents=True)
        self.media_dir=self.root/'private-media'
        self.config=patch.dict(server.app.config,PAPER_REPOSITORY_ROOT=str(self.repository),REVIEW_MEDIA_DIR=str(self.media_dir))
        self.config.start(); self.addCleanup(self.config.stop)
        with self.fixture.engine.begin() as conn:
            conn.execute(text('ALTER TABLE papers ADD COLUMN pdf_local_path TEXT'))
            conn.execute(text('ALTER TABLE papers ADD COLUMN doi TEXT'))
        self.item=self.fixture.daily()['items'][0]
        self.article=self.item['article_number']
        self.original=self.repository/'ieee-pdf'/(self.article+'.pdf')
        self.original.write_bytes(sample_pdf())
        self.original_hash=hashlib.sha256(self.original.read_bytes()).hexdigest()
        self.store=ReviewMedia(self.media_dir)

    def get(self,path,client=None):
        response=(client or self.fixture.admin).get(path)
        self.addCleanup(response.close)
        return response

    def post(self,path,data=None,json=None,client=None):
        response=(client or self.fixture.admin).post(path,data=data,json=json,headers={'X-CSRF-Token':'review-csrf'})
        self.addCleanup(response.close)
        return response

    def test_page_render_and_clip_use_top_left_normalized_coordinates(self):
        response=self.get('/api/daily-review/page?article_number='+self.article+'&page=1')
        self.assertEqual(response.status_code,200,response.json)
        image=Image.open(io.BytesIO(response.data))
        self.assertEqual(image.size,(800,600))
        result=self.post('/api/daily-review/clip',json=dict(article_number=self.article,page=1,box=[.05,.25,.95,.70]))
        self.assertEqual(result.status_code,200,result.json)
        saved=self.get(result.json['item']['url'])
        self.assertEqual(saved.status_code,200)
        cropped=Image.open(io.BytesIO(saved.data))
        self.assertEqual(cropped.size,(720,270))
        self.assertEqual(hashlib.sha256(self.original.read_bytes()).hexdigest(),self.original_hash)
        self.assertIn('/api/daily-review/media/',result.json['item']['markdown'])

    def test_empty_media_environment_uses_the_private_data_directory(self):
        with patch.dict(os.environ,REVIEW_MEDIA_DIR=''):
            self.assertEqual(media_root(),DEFAULT_MEDIA_ROOT.resolve())

    def test_clip_rejects_bad_pages_nan_and_outside_or_empty_regions(self):
        for page,box in ((0,[0,0,1,1]),(3,[0,0,1,1]),(1,[-1,0,1,1]),(1,[.5,0,.5,1]),(1,[0,0,.001,.001]),(1,['bad',0,1,1])):
            response=self.post('/api/daily-review/clip',json=dict(article_number=self.article,page=page,box=box))
            self.assertEqual(response.status_code,400,(page,box,response.json))
        self.assertEqual(self.get('/api/daily-review/page?article_number='+self.article+'&page=９').status_code,400)

    def test_image_upload_normalizes_png_and_refuses_non_images(self):
        output=io.BytesIO(); Image.new('RGB',(32,24),'#af3248').save(output,format='JPEG')
        response=self.post('/api/daily-review/image',data={'article_number':self.article,'image':(io.BytesIO(output.getvalue()),'clip.jpg')})
        self.assertEqual(response.status_code,200,response.json)
        asset=self.get(response.json['item']['url'])
        self.assertEqual(asset.content_type,'image/png')
        self.assertEqual(Image.open(io.BytesIO(asset.data)).size,(32,24))
        rejected=self.post('/api/daily-review/image',data={'article_number':self.article,'image':(io.BytesIO(b'<svg onload="alert(1)"></svg>'),'fake.png')})
        self.assertEqual(rejected.status_code,400)

    def test_acrobat_opens_only_the_private_copy_and_preserves_existing_edits(self):
        with patch('paper_server.review_media.launch_acrobat') as launch:
            response=self.post('/api/daily-review/acrobat',json={'article_number':self.article})
            self.assertEqual(response.status_code,200,response.json)
            path=launch.call_args.args[0]
            self.assertNotEqual(path,self.original)
            self.assertTrue(path.resolve().is_relative_to(self.media_dir.resolve()))
            path.write_bytes(sample_pdf(annotated=True))
            edited_hash=hashlib.sha256(path.read_bytes()).hexdigest()
            self.post('/api/daily-review/acrobat',json={'article_number':self.article})
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),edited_hash)
        self.assertEqual(hashlib.sha256(self.original.read_bytes()).hexdigest(),self.original_hash)

    def test_saved_highlights_and_comments_are_extracted_with_stable_ids(self):
        response=self.post('/api/daily-review/annotated-pdf',data={'article_number':self.article,'pdf':(io.BytesIO(sample_pdf(annotated=True)),'annotated.pdf')})
        self.assertEqual(response.status_code,200,response.json)
        result=self.get('/api/daily-review/annotations?article_number='+self.article)
        self.assertEqual(result.status_code,200,result.json)
        items=result.json['items']
        highlight=next(item for item in items if item['kind']=='Highlight')
        self.assertIn('Equalizer',highlight['text'])
        self.assertEqual(highlight['comment'],'Check gain and bandwidth.')
        self.assertEqual(highlight['page'],1)
        self.assertTrue(any(item['comment']=='Compare equalizer power.' for item in items))
        self.assertEqual(items,self.get('/api/daily-review/annotations?article_number='+self.article).json['items'])
        self.assertEqual(hashlib.sha256(self.original.read_bytes()).hexdigest(),self.original_hash)

    def test_importing_a_new_pdf_keeps_the_previous_edit_copy_intact(self):
        previous=self.store.working_copy('account:1',self.article,self.original)
        previous.write_bytes(sample_pdf(annotated=True))
        saved=previous.read_bytes()
        self.store.import_pdf('account:1',self.article,sample_pdf(),self.original)
        current=self.store.pdf('account:1',self.article,self.original)
        self.assertNotEqual(current,previous)
        self.assertEqual(previous.read_bytes(),saved)
        self.assertEqual(hashlib.sha256(self.original.read_bytes()).hexdigest(),self.original_hash)

    def test_unauthorized_accounts_and_unassigned_papers_cannot_access_media(self):
        image=io.BytesIO(); Image.new('RGB',(20,20),'blue').save(image,format='PNG')
        asset=self.store.image('account:1',self.article,image.getvalue())
        for client in (self.fixture.member,self.fixture.viewer):
            self.assertEqual(self.get(asset['url'],client).status_code,403)
            self.assertEqual(self.get('/api/daily-review/document?article_number='+self.article,client).status_code,403)
            self.assertEqual(self.post('/api/daily-review/clip',json={'article_number':self.article,'page':1,'box':[0,0,1,1]},client=client).status_code,403)
        self.assertEqual(server.app.test_client().get(asset['url']).status_code,401)
        self.assertEqual(self.post('/api/daily-review/acrobat',json={'article_number':'5'}).status_code,404)
        self.assertEqual(self.fixture.admin.post('/api/daily-review/acrobat',json={'article_number':self.article}).status_code,403)
        self.assertEqual(self.get('/api/daily-review/media/'+article_token(self.article)+'/manifest.json').status_code,404)

    def test_obsidian_exports_images_annotated_pdf_and_latex_without_private_web_urls(self):
        image=io.BytesIO(); Image.new('RGB',(30,20),'green').save(image,format='PNG')
        asset=self.store.image('account:1',self.article,image.getvalue())
        self.store.import_pdf('account:1',self.article,sample_pdf(annotated=True),self.original)
        notes='전달함수 $H(s)$\n\n$$\nH(s)=\\frac{V_o}{V_i}\n$$\n\n'+asset['markdown']
        saved=self.fixture.save(self.item,notes=notes)
        self.assertEqual(saved.status_code,200)
        vault=self.root/'LLM_Wiki'
        for directory in ('.obsidian','templates','wiki/sources'): (vault/directory).mkdir(parents=True)
        (vault/'templates/paper.md').write_text('paper template',encoding='utf-8')
        (vault/'wiki/index.md').write_text('# Wiki\n\n## 관리\n',encoding='utf-8')
        (vault/'wiki/log.md').write_text('# 기록\n',encoding='utf-8')
        sync=ObsidianReviewSync(self.fixture.engine,vault,self.repository,media_dir=self.media_dir)
        sync.sync_one('account:1',self.article)
        note=vault/'wiki/sources'/sync.filename(self.article)
        content=note.read_text(encoding='utf-8')
        self.assertIn('H(s)=\\frac{V_o}{V_i}',content)
        self.assertNotIn(asset['url'],content)
        links=re.findall(r'\]\(([^)]+)\)',content)
        local_assets=[Path(url2pathname(urlsplit(link).path)) if urlsplit(link).scheme=='file' else note.parent/unquote(link)
                      for link in links if 'raw/assets/paper-reviews/' in link]
        self.assertEqual(len(local_assets),3)  # Original PDF, edited PDF and the legacy web image.
        self.assertTrue(all(path.is_file() for path in local_assets))
        self.assertTrue(any(path.suffix=='.pdf' and len(PdfReader(path).pages)==2 for path in local_assets))


if __name__=='__main__':
    unittest.main()

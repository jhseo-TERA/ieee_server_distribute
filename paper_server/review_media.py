"""Private review images, PDF clips and Acrobat annotations; source PDFs stay intact."""
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import subprocess
import threading
from urllib.parse import quote
import uuid

from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader
from sqlalchemy import text
from paper_server.pdf_runtime import PDFIUM_LOCK


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MEDIA_ROOT = ROOT / 'py_01_data' / '06_personal_reviews'
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_PDF_BYTES = 64 * 1024 * 1024
MAX_PAGES = 500
_lock = threading.RLock()
_pdf_lock = PDFIUM_LOCK


def media_root():
    return Path(os.getenv('REVIEW_MEDIA_DIR','').strip() or DEFAULT_MEDIA_ROOT).resolve()


def article_token(article):
    return hashlib.sha256(article.encode()).hexdigest()[:32]


def media_url(article, filename):
    return '/api/daily-review/media/' + article_token(article) + '/' + filename


def check_assigned(engine, owner, article):
    if not isinstance(article, str) or not 1 <= len(article) <= 80:
        raise ValueError('논문 ID를 확인하세요.')
    with engine.connect() as conn:
        row = conn.execute(text('SELECT article_number FROM daily_review_picks '
            'WHERE owner=:owner AND article_number=:article LIMIT 1'),
            {'owner':owner, 'article':article}).first()
    if not row:
        raise KeyError(article)


def source_pdf(engine, article, repository=ROOT):
    with engine.connect() as conn:
        row = conn.execute(text('SELECT source_system,pdf_local_path,pdf_available '
            'FROM papers WHERE article_number=:article'), {'article':article}).mappings().first()
    if not row or not row['pdf_available']:
        raise FileNotFoundError('이 논문의 원본 PDF를 찾을 수 없습니다.')
    directories = {'ieee':'ieee-pdf','optica':'optica-pdf','nature':'nature-pdf','designcon':'DesignCon'}
    directory = directories.get(row['source_system'])
    if not directory:
        raise FileNotFoundError('PDF 출처를 확인하세요.')
    base = (Path(repository) / directory).resolve()
    local = row['pdf_local_path']
    if local:
        path = (Path(repository) / str(local).replace('\\','/')).resolve()
    else:
        if not re.fullmatch(r'[A-Za-z0-9._-]{1,80}',article):
            raise ValueError('올바른 PDF 경로가 아닙니다.')
        path = (base / (article + '.pdf')).resolve()
    if not path.is_relative_to(base) or path.suffix.lower() != '.pdf' or not path.is_file():
        raise FileNotFoundError('원본 PDF를 찾을 수 없습니다.')
    return path


def pdf_info(path):
    try:
        reader = PdfReader(path)
        if reader.is_encrypted and not reader.decrypt(''):
            raise ValueError('암호가 걸린 PDF는 주석 가져오기를 지원하지 않습니다.')
        count = len(reader.pages)
        if not 1 <= count <= MAX_PAGES:
            raise ValueError(f'PDF는 1~{MAX_PAGES}페이지까지 지원합니다.')
        return dict(pages=count)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError('읽을 수 있는 PDF 파일인지 확인하세요.') from exc


def render_page(path, page_number):
    import pypdfium2 as pdfium
    with _pdf_lock:
        with pdfium.PdfDocument(str(path)) as document:
            if not 1 <= page_number <= min(len(document), MAX_PAGES):
                raise ValueError('페이지 번호를 확인하세요.')
            page = document[page_number - 1]
            try:
                width, height = page.get_size()
                if width <= 0 or height <= 0 or not math.isfinite(width+height):
                    raise ValueError('PDF 페이지 크기를 확인하세요.')
                scale = min(2.0, 1800 / width, 2400 / height)
                bitmap = page.render(scale=scale, draw_annots=True)
                try:
                    return bitmap.to_pil().copy()
                finally:
                    bitmap.close()
            finally:
                page.close()


class ReviewMedia:
    def __init__(self, root=None):
        self.root = Path(root or media_root()).resolve()

    def folder(self, owner, token):
        if not re.fullmatch(r'[a-f0-9]{32}',token):
            raise ValueError('첨부 경로를 확인하세요.')
        owner_key = hashlib.sha256(owner.encode()).hexdigest()[:32]
        path = self.root / owner_key / token
        if not path.resolve().is_relative_to(self.root):
            raise ValueError('잘못된 첨부 경로입니다.')
        return path

    def manifest(self, owner, article):
        path = self.folder(owner, article_token(article)) / 'manifest.json'
        if not path.resolve().is_relative_to(self.root):
            raise ValueError('첨부 경로를 확인하세요.')
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else dict(article=article, images=[])

    def _write(self, owner, article, filename, data):
        folder = self.folder(owner, article_token(article))
        folder.mkdir(parents=True,exist_ok=True)
        path = folder / filename
        if not path.resolve().is_relative_to(self.root):
            raise ValueError('첨부 경로를 확인하세요.')
        temporary = folder / (uuid.uuid4().hex + '.tmp')
        try:
            temporary.write_bytes(data)
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()
        return path

    def _save_manifest(self, owner, article, manifest):
        self._write(owner, article, 'manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8'))

    def image(self, owner, article, data, caption='그림', page=None):
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError('이미지는 12MB 이내로 첨부하세요.')
        try:
            with Image.open(io.BytesIO(data)) as image:
                if image.width * image.height > 25_000_000 or image.width < 1 or image.height < 1:
                    raise ValueError('이미지는 2,500만 픽셀 이내여야 합니다.')
                if image.format not in {'PNG','JPEG','WEBP'}:
                    raise ValueError('PNG·JPEG·WebP 이미지를 사용하세요.')
                normalized = image.convert('RGBA' if 'A' in image.getbands() else 'RGB')
                output = io.BytesIO()
                normalized.save(output,format='PNG')
                dimensions = normalized.size
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise ValueError('읽을 수 있는 PNG·JPEG·WebP 이미지인지 확인하세요.') from exc
        if len(output.getvalue()) > MAX_IMAGE_BYTES:
            raise ValueError('그림 크기를 줄여 12MB 이내로 첨부하세요.')
        caption = str(caption)[:200].replace('\n',' ').replace('\r',' ').replace('[','(').replace(']',')')
        filename = uuid.uuid4().hex + '.png'
        with _lock:
            manifest = self.manifest(owner,article)
            if len(manifest['images']) >= 100:
                raise ValueError('논문당 그림을 최대 100개까지 첨부할 수 있습니다.')
            self._write(owner,article,filename,output.getvalue())
            info = dict(file=filename,caption=caption,page=page,width=dimensions[0],height=dimensions[1],
                        created_at=datetime.now(timezone.utc).isoformat())
            manifest['images'].append(info)
            self._save_manifest(owner,article,manifest)
        url = media_url(article,filename)
        return dict(**info,url=url,markdown=f'![{caption}]({url})')

    def clip(self, owner, article, path, page, box):
        if (not isinstance(box,list) or len(box)!=4 or any(type(v) not in (int,float)
                or not math.isfinite(v) for v in box)):
            raise ValueError('잘라낼 영역을 다시 선택하세요.')
        x0,y0,x1,y1 = box
        if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
            raise ValueError('PDF 안에서 잘라낼 영역을 선택하세요.')
        image = render_page(path,page)
        bounds = (round(x0*image.width),round(y0*image.height),round(x1*image.width),round(y1*image.height))
        if bounds[2]-bounds[0] < 10 or bounds[3]-bounds[1] < 10:
            raise ValueError('선택 영역을 조금 더 크게 잡아 주세요.')
        cropped = image.crop(bounds)
        output = io.BytesIO()
        cropped.save(output,format='PNG')
        return self.image(owner,article,output.getvalue(),f'p.{page} 그림',page=page)

    def pdf(self, owner, article, original):
        manifest = self.manifest(owner,article)
        if manifest.get('working_pdf'):
            path = self.folder(owner,article_token(article)) / manifest.get('working_file','annotated.pdf')
            if path.resolve().is_relative_to(self.root) and path.is_file():
                return path
        return original

    def working_copy(self, owner, article, original):
        with _lock:
            existing = self.pdf(owner,article,original)
            if existing != original:
                return existing
            if original.stat().st_size > MAX_PDF_BYTES:
                raise ValueError('주석용 PDF는 64MB 이내까지 지원합니다.')
            pdf_info(original)
            key = article if re.fullmatch(r'[A-Za-z0-9._-]{1,80}',article) else article_token(article)
            filename = 'review-' + key + '.pdf'
            path = self._write(owner,article,filename,original.read_bytes())
            manifest = self.manifest(owner,article)
            manifest['working_pdf'] = True
            manifest['working_file'] = filename
            self._save_manifest(owner,article,manifest)
            return path

    def import_pdf(self, owner, article, data, original):
        if len(data) > MAX_PDF_BYTES or not data.lstrip().startswith(b'%PDF-'):
            raise ValueError('64MB 이내의 PDF 파일을 선택하세요.')
        info = pdf_info(io.BytesIO(data))
        if info['pages'] != pdf_info(original)['pages']:
            raise ValueError('원본과 페이지 수가 다릅니다. 같은 논문의 주석 PDF를 선택하세요.')
        with _lock:
            manifest = self.manifest(owner,article)
            key = article if re.fullmatch(r'[A-Za-z0-9._-]{1,80}',article) else article_token(article)
            filename = 'review-' + key + '-' + hashlib.sha256(data).hexdigest()[:12] + '.pdf'
            destination = self.folder(owner,article_token(article))/filename
            if destination.exists() and destination.read_bytes()!=data:
                filename = 'review-' + key + '-' + uuid.uuid4().hex[:12] + '.pdf'
                destination = self.folder(owner,article_token(article))/filename
            # Keep the previous edit copy, including one still open in Acrobat.
            if not destination.exists():
                self._write(owner,article,filename,data)
            manifest['working_pdf'] = True
            manifest['working_file'] = filename
            self._save_manifest(owner,article,manifest)
        return info

    def file(self, owner, token, filename):
        if not re.fullmatch(r'(?:[a-f0-9]{32}\.png|annotated\.pdf)',filename):
            raise KeyError(filename)
        folder = self.folder(owner,token)
        if not (folder/'manifest.json').resolve().is_relative_to(self.root):
            raise ValueError('첨부 경로를 확인하세요.')
        manifest = json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
        allowed = {entry['file'] for entry in manifest['images']}
        if manifest.get('working_pdf'):
            allowed.add('annotated.pdf')
        path = folder / (manifest.get('working_file','annotated.pdf') if filename=='annotated.pdf' else filename)
        if filename not in allowed or not path.resolve().is_relative_to(self.root) or not path.is_file():
            raise KeyError(filename)
        return path

    def export_notes(self, owner, article, notes, copy_asset):
        """Rewrite the owner's private image URLs to immutable vault-relative assets."""
        manifest = self.manifest(owner,article)
        pattern = r'/api/daily-review/media/([a-f0-9]{32})/([a-f0-9]{32}\.png)'
        notes = re.sub(pattern,lambda match: copy_asset(self.file(owner,match[1],match[2])),notes)
        if manifest.get('working_pdf'):
            path = self.file(owner,article_token(article),'annotated.pdf')
            notes += '\n\n[Acrobat 주석 PDF](' + copy_asset(path) + ')'
        return notes


def annotations(path):
    """Read standard PDF highlight quads and comments saved by Acrobat or another editor."""
    import pypdfium2 as pdfium
    data = Path(path).read_bytes()
    if len(data) > MAX_PDF_BYTES:
        raise ValueError('주석 PDF는 64MB 이내까지 지원합니다.')
    info = pdf_info(io.BytesIO(data))
    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        reader.decrypt('')
    result = []
    with _pdf_lock:
        with pdfium.PdfDocument(data) as document:
            for page_index in range(info['pages']):
                page = reader.pages[page_index]
                for reference in page.get('/Annots',[]):
                    item = reference.get_object()
                    kind = str(item.get('/Subtype','')).lstrip('/')
                    if kind not in {'Highlight','Underline','StrikeOut','Text','FreeText','Ink','Square','Circle'}:
                        continue
                    comment = str(item.get('/Contents','')).strip()[:4000]
                    selected = ''
                    quads = item.get('/QuadPoints',[]) if kind in {'Highlight','Underline','StrikeOut'} else []
                    if quads and len(quads) % 8 == 0:
                        pdf_page = document[page_index]
                        text_page = pdf_page.get_textpage()
                        try:
                            chunks = []
                            for i in range(0,min(len(quads),8000),8):
                                points = [float(v) for v in quads[i:i+8]]
                                if not all(math.isfinite(v) for v in points):
                                    continue
                                xs,ys = points[0::2],points[1::2]
                                chunks.append(text_page.get_text_bounded(min(xs),min(ys),max(xs),max(ys)).replace('\r\n',' ').strip())
                            selected = ' '.join(part for part in chunks if part)[:6000]
                        finally:
                            text_page.close()
                            pdf_page.close()
                    if not selected and not comment:
                        continue
                    key = hashlib.sha256(json.dumps([page_index,kind,item.get('/NM'),
                        str(item.get('/Rect')),selected,comment],ensure_ascii=False,sort_keys=True).encode()).hexdigest()[:24]
                    result.append(dict(id=key,page=page_index+1,kind=kind,text=selected,comment=comment))
                    if len(result) >= 200:
                        return result
    return result


def launch_acrobat(path):
    candidates = [os.getenv('ACROBAT_EXECUTABLE',''),
        r'C:\Program Files\Adobe\Acrobat DC\Acrobat\Acrobat.exe',
        r'C:\Program Files (x86)\Adobe\Acrobat DC\Acrobat\Acrobat.exe']
    executable = next((Path(p) for p in candidates if p and Path(p).is_file()),None)
    if executable is None:
        raise FileNotFoundError('이 PC의 Acrobat을 찾을 수 없습니다. 주석 PDF 내려받기·가져오기를 사용하세요.')
    # The user explicitly requests an interactive Acrobat window through this button.
    subprocess.Popen([str(executable),str(path)],cwd=str(path.parent),shell=False)


def create_review_media_blueprint(get_engine):
    from flask import Blueprint, current_app, jsonify, request, send_file
    from paper_server.daily_review import can_write_reviews, review_owner
    from paper_server.obsidian_reviews import require_review_writer
    bp = Blueprint('review_media',__name__)

    @bp.before_request
    def permissions():
        if not can_write_reviews():
            return jsonify(ok=False,error='리뷰 그림·PDF 주석은 admin 계정만 사용할 수 있습니다.'),403
        if request.method == 'POST':
            request.max_content_length = MAX_PDF_BYTES + 1024*1024
        with get_engine().connect() as conn:
            require_review_writer(conn,review_owner())

    def identity(article=None):
        article = article if article is not None else request.args.get('article_number','')
        owner = review_owner()
        check_assigned(get_engine(),owner,article)
        return owner,article

    def store():
        return ReviewMedia(current_app.config.get('REVIEW_MEDIA_DIR') or None)

    def document():
        owner,article = identity()
        original = source_pdf(get_engine(),article,current_app.config.get('PAPER_REPOSITORY_ROOT',ROOT))
        return owner,article,original,store().pdf(owner,article,original)

    @bp.route('/api/daily-review/document')
    def get_document():
        owner,article,original,path = document()
        return jsonify(ok=True,**pdf_info(path),working_copy=path!=original,
            download_url='/api/daily-review/document.pdf?article_number='+quote(article,safe='')+'&download=1',
            viewer_url='/api/daily-review/document.pdf?article_number='+quote(article,safe=''))

    @bp.route('/api/daily-review/document.pdf')
    def get_pdf():
        owner,article,original,path = document()
        key = article if re.fullmatch(r'[A-Za-z0-9._-]{1,80}',article) else article_token(article)
        return send_file(path,mimetype='application/pdf',as_attachment=request.args.get('download')=='1',
                         download_name='review-'+key+'.pdf')

    @bp.route('/api/daily-review/page')
    def get_page():
        owner,article,original,path = document()
        raw = request.args.get('page','1')
        if not raw.isascii() or not raw.isdigit() or not 1 <= len(raw) <= 3:
            raise ValueError('페이지 번호를 확인하세요.')
        image = render_page(path,int(raw))
        output = io.BytesIO()
        image.save(output,format='PNG')
        output.seek(0)
        return send_file(output,mimetype='image/png')

    @bp.route('/api/daily-review/clip',methods=['POST'])
    def crop():
        data = request.get_json(silent=True)
        if not isinstance(data,dict) or type(data.get('page')) is not int:
            raise ValueError('페이지와 영역을 확인하세요.')
        owner,article = identity(data.get('article_number'))
        original = source_pdf(get_engine(),article,current_app.config.get('PAPER_REPOSITORY_ROOT',ROOT))
        path = store().pdf(owner,article,original)
        result = store().clip(owner,article,path,data['page'],data.get('box'))
        return jsonify(ok=True,item=result)

    @bp.route('/api/daily-review/image',methods=['POST'])
    def upload_image():
        owner,article = identity(request.form.get('article_number',''))
        upload = request.files.get('image')
        if not upload:
            raise ValueError('첨부할 그림을 선택하세요.')
        data = upload.stream.read(MAX_IMAGE_BYTES+1)
        return jsonify(ok=True,item=store().image(owner,article,data,request.form.get('caption','첨부 그림')))

    @bp.route('/api/daily-review/annotated-pdf',methods=['POST'])
    def upload_pdf():
        owner,article = identity(request.form.get('article_number',''))
        upload = request.files.get('pdf')
        if not upload:
            raise ValueError('Acrobat에서 저장한 PDF를 선택하세요.')
        original = source_pdf(get_engine(),article,current_app.config.get('PAPER_REPOSITORY_ROOT',ROOT))
        info = store().import_pdf(owner,article,upload.stream.read(MAX_PDF_BYTES+1),original)
        return jsonify(ok=True,**info)

    @bp.route('/api/daily-review/acrobat',methods=['POST'])
    def open_acrobat():
        data = request.get_json(silent=True)
        owner,article = identity(data.get('article_number') if isinstance(data,dict) else '')
        original = source_pdf(get_engine(),article,current_app.config.get('PAPER_REPOSITORY_ROOT',ROOT))
        path = store().working_copy(owner,article,original)
        launch_acrobat(path)
        return jsonify(ok=True,**pdf_info(path))

    @bp.route('/api/daily-review/annotations')
    def get_annotations():
        owner,article,original,path = document()
        return jsonify(ok=True,items=annotations(path))

    @bp.route('/api/daily-review/media/<token>/<filename>')
    def get_media(token,filename):
        path = store().file(review_owner(),token,filename)
        return send_file(path,mimetype='image/png' if filename.endswith('.png') else 'application/pdf')

    @bp.errorhandler(KeyError)
    @bp.errorhandler(FileNotFoundError)
    def not_found(error):
        message = str(error) if isinstance(error,FileNotFoundError) and not error.filename else '내 리뷰의 논문이나 첨부를 찾을 수 없습니다.'
        return jsonify(ok=False,error=message),404

    @bp.errorhandler(ValueError)
    def invalid(error):
        return jsonify(ok=False,error=str(error)),400

    @bp.errorhandler(PermissionError)
    def denied(error):
        return jsonify(ok=False,error='파일을 읽거나 쓸 수 없습니다. Acrobat에서 저장을 마친 뒤 다시 시도하세요.'),403

    @bp.errorhandler(OSError)
    def unavailable(error):
        return jsonify(ok=False,error='리뷰 첨부를 읽거나 저장하지 못했습니다. 잠시 후 다시 시도하세요.'),503

    return bp

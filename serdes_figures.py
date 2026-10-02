"""Extract original PDF architecture figures offline; serve only cached images.

No inference, PDF parsing or image generation occurs during a hover request.
Automatic selections remain unverified; curated selections are source-hash bound.
"""
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
from urllib.parse import quote
import uuid

from local_ai_documents import DocumentStore, DocumentError, _io_path

VERSION = 'architecture-figures-v5'
# Stable storage permits per-paper atomic migration without a cache-wide outage.
STORAGE_VERSION = 'caption-figures-v4'
CAPTION = re.compile(r'^(?:fig(?:ure)?\.?)[ \t]*(\d+(?:\.\d+)*)(?:[.:][ \t]*|[ \t]+)(?![ \t])(?!(?:shows?|illustrates?|depicts?|presents?|is|are|has|can)\b)(.+)', re.I)
CAPTION_START = re.compile(r'\bfig(?:ure)?\.?[ \t]*\d+(?:\.\d+)*(?:[.:][ \t]*|[ \t]+)', re.I)
MAX_SCAN_PAGES = 24
MAX_OBJECTS = 30000


def valid_absence_review(entry):
    """A negative result requires a source-bound, whole-document review."""
    count = entry.get('page_count')
    return (entry.get('disposition') == 'no_top_diagram'
            and type(count) is int and 1 <= count <= 2000
            and entry.get('source_pages_reviewed') == list(range(1, count + 1))
            and all(isinstance(entry.get(k), str) and entry[k].strip()
                    for k in ('evidence', 'absence_reason', 'review_note_ko'))
            and type(entry.get('page')) is int and 1 <= entry['page'] <= count
            and bool(re.fullmatch(r'[a-f0-9]{64}', entry.get('pdf_sha256', ''))))


def normalized_caption(value):
    value = re.sub(r'[\u00ad\ufffe]', '', value.lower())
    value = re.sub(r'architec?utre|architechture|archtecture', 'architecture', value)
    return re.sub(r'\s+', ' ', value)


def caption_role(caption, title=''):
    value = normalized_caption(caption)
    main = re.split(r'\binsets?\b', value)[0]
    subject=re.sub(r'^fig(?:ure)?\.?\s*\d+(?:\.\d+)*[.:]?\s*','',main)
    if re.match(r'(?:overall\s+)?(?:measured|simulated|measurement|simulation)\s+(?:results?|performance|gain|noise|frequency|eye|response)\b',subject):
        return 'measurement'
    if re.search(r'(?:measurement|experimental?|test)(?:\s+\w+){0,2}\s+(?:set[ -]?up|arrangement)|(?:diagram|setup) of (?:the |an? )?experiment\b', main):
        return 'measurement'
    if re.search(r'\b(?:general|generic|conceptual|conventional|previous|prior|existing)\b', main) and not re.search(r'\bproposed\b|this work', main):
        return 'background'
    if re.search(r'channel characteristics|target backplane|transmission medium', main):
        return 'background'
    if re.search(r'\[\d+(?:[ ,–-]\d+)*\]', main) and 'proposed' not in main:
        return 'prior_art'
    if re.search(r'comparison|different (?:types|architectures)|(?:analog|dsp).{0,40}(?:analog|dsp)', main) and 'proposed' not in main:
        return 'comparison'
    return 'implementation'


def figure_scope(caption, title=''):
    value = normalized_caption(re.split(r'\binsets?\b', caption, flags=re.I)[0])
    if re.search(r'transceiver|\btrx\b|serial i/?o|signal conditioner', value): return 'trx'
    if re.search(r'receiver|\brx\b', value): return 'rx'
    if re.search(r'transmitter|\btx\b', value): return 'tx'
    for scope, pattern in [('cdr', r'\bcdr\b|clock.{0,12}recovery'), ('afe', r'\bafe\b|front[ -]?end|preamplifier'),
                           ('driver', r'\bdriver\b'), ('adc', r'\badc\b')]:
        if re.search(pattern, value): return scope
    return 'unknown'


def caption_score(caption, number, title='', context=''):
    value = normalized_caption(caption)
    main = re.split(r'\binsets?\b', value)[0]
    role = caption_role(caption, title)
    if role == 'measurement':
        return 0  # A measurement apparatus is not the implemented main architecture.
    score = 0
    if re.search(r'block[ -]+diagram', value):
        score += 12
    if 'architecture' in value:
        score += 14
    if re.search(r'(?:system|transceiver|receiver|transmitter|link)\s+(?:overview|diagram)', value):
        score += 10
    if re.search(r'\b(?:driver|receiver|transmitter|transceiver|rx|tx|afe|tia)\s+(?:overview|structure|topology)\b', main):
        score += 14
    if re.search(r'\bsystem\s+block[ -]diagram\b', main):
        score += 4
    if re.search(r'(?:top[ -]level|overall|system[ -]level|top)\s+(?:overview|architecture|diagram)', value):
        score += 10
    # Explicit complete signal chains and implementation references also qualify.
    if re.search(r'(?:receiver|transmitter|transceiver|\brx\b|\btx\b).{0,35}\bwith\b', main) and sum(
            bool(re.search(pattern, main)) for pattern in (r'\bctle\b', r'\badc\b', r'\bdsp\b', r'\bcdr\b')) >= 3:
        score += 14
    if context and re.search(r'architecture|block[ -]diagram|top[ -]level', normalized_caption(context)):
        score += 10
    if not score:
        return 0
    if re.search(r'top[ -]level|overall|system[ -]level', value):
        score += 9
    if 'proposed' in value:
        score += 5
    if role in ('background', 'prior_art', 'comparison'):
        score -= 18
    if context and re.search(r'proposed|prototype|implemented|this (?:work|paper)|our', context, re.I):
        score += 8
    if re.search(r'receiver|transmitter|transceiver|\brx\b|\btx\b|\btrx\b', value):
        score += 4
    if re.search(r'timing diagram|die (?:photo|micrograph)|measured (?:performance|result)', value):
        score -= 6
    # A sub-block is less representative than the titled complete implementation.
    for part in ('ctle', 'sampler', 'vga', 'buffer', 'latch', 'differentiator', 'bbpd'):
        # Penalize only when the named part is the subject, not part of a full RX.
        if re.search(r'(?:of (?:the )?(?:proposed )?|proposed )' + part + r'\b|\b' + part + r' (?:architecture|block diagram)', main) and part not in title.lower():
            score -= 8
    if re.search(r'\bafe\b', main):
        score += 5 if re.search(r'front[ -]?end', title, re.I) else -3
    # Figure number must not outweigh actual implementation evidence.
    return score


def union(rects):
    return [min(r[0] for r in rects), min(r[1] for r in rects),
            max(r[2] for r in rects), max(r[3] for r in rects)]


def valid_rect(rect, width, height):
    return (len(rect) == 4 and all(math.isfinite(v) for v in rect)
            and rect[2] > rect[0] and rect[3] > rect[1]
            and rect[0] >= -2 and rect[1] >= -2 and rect[2] <= width + 2 and rect[3] <= height + 2)


def page_rect(rect, page_box):
    """PDF user space -> unrotated visible-page coordinates."""
    return [rect[0]-page_box[0], rect[1]-page_box[1], rect[2]-page_box[0], rect[3]-page_box[1]]


def render_crop(rect, width, height, rotation=0):
    """Unrotated page-local bbox -> PDFium's displayed-view crop distances."""
    if not valid_rect(rect, width, height):
        raise DocumentError('Invalid figure crop.')
    l, b, r, t = rect
    rotation %= 360
    if rotation == 90:
        l, b, r, t = b, width-r, t, width-l
        width, height = height, width
    elif rotation == 180:
        l, b, r, t = width-r, height-t, width-l, height-b
    elif rotation == 270:
        l, b, r, t = height-t, l, height-b, r
        width, height = height, width
    elif rotation:
        raise DocumentError('Unsupported page rotation.')
    return tuple(max(0, value) for value in (l, b, width-r, height-t))


def isolate_reviewed_regions(picture, bbox, regions, rotation=0):
    """Keep source pixels in explicitly reviewed PDF panels, in original positions.

    Irregular layouts may interleave a diagram with an unrelated plot/photo.
    This is deterministic document clipping, never generated/reconstructed art.
    """
    from PIL import Image
    if rotation or not isinstance(regions, list) or not 1 <= len(regions) <= 12:
        raise DocumentError('Invalid reviewed panel regions or rotation.')
    l,b,r,t = bbox
    result = Image.new('RGB', picture.size, 'white')
    for rect in regions:
        if (not isinstance(rect, list) or len(rect) != 4
                or any(type(v) not in (int,float) or not math.isfinite(v) for v in rect)
                or not (l <= rect[0] < rect[2] <= r and b <= rect[1] < rect[3] <= t)):
            raise DocumentError('Reviewed panel region is outside figure bbox.')
        area = (round((rect[0]-l)/(r-l)*picture.width), round((t-rect[3])/(t-b)*picture.height),
                round((rect[2]-l)/(r-l)*picture.width), round((t-rect[1])/(t-b)*picture.height))
        result.paste(picture.crop(area), area[:2])
    return result


def merge_tiles(graphics):
    """Join only edge-touching tiles with identical orthogonal extents."""
    groups = [list(g) for g in graphics]
    # Sorted sweeps avoid quadratic/cubic behavior on PDFs with thousands of Forms.
    for _ in range(3):
        before = len(groups)
        for axis in (1,0):
            other = 1-axis
            ordered = sorted(groups,key=lambda g:(round(g[other]),round(g[other+2]),g[axis]))
            merged = []
            for box in ordered:
                prev = merged[-1] if merged else None
                if (prev and abs(prev[other]-box[other])<1 and abs(prev[other+2]-box[other+2])<1
                        and abs(prev[axis+2]-box[axis])<1):
                    merged[-1]=union([prev,box])
                else: merged.append(box)
            groups=merged
        if len(groups)==before:break
    return groups


def caption_flags(caption):
    value = normalized_caption(caption)
    flags = []
    if re.search(r'\b(?:structure|topology|driver overview)\b',value):
        flags.append('semantic_scope_review')
    if len(set(re.findall(r'\(([a-f])\)', value))) > 1 or re.search(r'\b(?:left|right|top|bottom)\b.*\b(?:photo|waveform|eye|loss|schematic|diagram)\b', value):
        flags.append('multi_panel')
    if caption_role(caption) in ('background', 'comparison', 'prior_art'):
        flags.append(caption_role(caption))
    return flags


def vector_region(lines, paths, caption_box, width, height):
    """Conservative page region, bounded by caption/prose; always review first."""
    l, b, r, t = caption_box
    # Never infer a region on a scan or from text alone.
    if not paths: return None
    full_width = r-l > width*.56
    left, right = (max(0, l-6), min(width, r+8)) if full_width else ((width*.5, width*.96) if l > width*.48 else (width*.04, width*.5))
    upper = height*.95
    for line in lines:
        box = line['box']
        if box[1] <= t+15 or min(box[2], right)-max(box[0], left) < (right-left)*.45:
            continue
        # Body prose/previous caption is a hard boundary; labels are not prose.
        if CAPTION.match(line['text']) or len(line['text'].split()) >= 9:
            upper = min(upper, box[1]-3)
    included = [p for p in paths if p[0] >= left-2 and p[2] <= right+2 and p[1] >= t+2 and p[3] <= upper
                and p[2]-p[0] < width*.85 and p[3]-p[1] < height*.7]
    if len(included) < 12: return None
    area = union(included)
    if area[2]-area[0] < 65 or area[3]-area[1] < 40 or area[1]-t > 65: return None
    # Vector primitives often omit text labels. Include nearby short labels, but
    # never use prose to grow a region or cross the caption/column boundaries.
    labels=[line['box'] for line in lines if len(line['text'].split())<=8
            and not CAPTION.match(line['text'].strip())
            and line['box'][1]>=max(t+2,area[1]-12) and line['box'][3]<=min(upper,area[3]+12)
            and line['box'][0]>=max(left,area[0]-25) and line['box'][2]<=min(right,area[2]+25)]
    return union([area]+labels)


def text_lines(objects):
    """Join font fragments along a baseline, without bridging two columns."""
    bands = []
    for obj in sorted(objects, key=lambda o: (-o['box'][1], o['box'][0])):
        box = obj['box']
        band = next((b for b in bands[-4:] if abs(b[0]['box'][1] - box[1]) <= 2.5), None)
        if band is None:
            band = []
            bands.append(band)
        band.append(obj)
    lines = []
    for band in bands:
        current = None
        for obj in sorted(band, key=lambda o: o['box'][0]):
            if (current is not None and obj['box'][0] - current['box'][2] < 9
                    and not re.match(r'^Fig(?:ure)?\.?\s*\d', obj['text'], re.I)):
                gap = obj['box'][0] - current['box'][2]
                current['text'] += (' ' if gap > 1.5 else '') + obj['text']
                current['box'] = union([current['box'], obj['box']])
            else:
                current = {'text': obj['text'], 'box': list(obj['box'])}
                lines.append(current)
    return sorted(lines, key=lambda o: (-o['box'][3], o['box'][0]))


def recover_caption_lines(textpage, lines, bounds):
    """Recover captions lost by fragmented PDF text objects using native search."""
    # Some PDF text objects concatenate the two columns' figure captions.
    # Replace those objects with char-box-bounded native text fragments.
    recovered = [line for line in lines if len(list(CAPTION_START.finditer(line['text'])))<2]
    known = {m[1] for line in recovered if (m := CAPTION.match(line['text'].strip()))}
    fragments=[]
    for native_line in textpage.get_text_range().splitlines():
        starts=list(CAPTION_START.finditer(native_line))
        if len(starts)>1:
            fragments.extend(native_line[m.start():starts[i+1].start() if i+1<len(starts) else len(native_line)] for i,m in enumerate(starts))
        else:fragments.append(native_line)
    for content in fragments:
        content = content.strip()
        match = CAPTION.match(content)
        if not match or match[1] in known or len(content)>1000: continue
        search = textpage.search(content)
        try:
            found = search.get_next()
            if found is None: continue
            start, count = found
            boxes = [textpage.get_charbox(i) for i in range(start,start+count)]
            boxes = [b for b in boxes if b[2]>b[0] and b[3]>b[1]]
            if not boxes: continue
            box = page_rect(union(boxes), bounds)
            if valid_rect(box,bounds[2]-bounds[0],bounds[3]-bounds[1]):
                recovered.append({'text':content,'box':box});known.add(match[1])
        finally: search.close()
    return sorted(recovered,key=lambda line:(-line['box'][3],line['box'][0]))


def find_candidates(lines, graphics, width, height, page_number, title='', *, paths=None, contexts=None, diagnostics=None):
    candidates = []
    graphics = merge_tiles(graphics)
    for line in lines:
        match = CAPTION.match(line['text'].strip())
        if not match:
            continue
        caption, box = line['text'].strip(), list(line['box'])
        # Captions commonly wrap into two or three lines of the same small font.
        font_height = box[3] - box[1]
        for _ in range(4):
            following = [l for l in lines if 0 < box[1] - l['box'][3] < 8
                         and abs(l['box'][0] - box[0]) < 5
                         and .65 * font_height <= l['box'][3] - l['box'][1] <= 1.15 * font_height
                         and not CAPTION.match(l['text'].strip())]
            if not following:
                break
            extra = min(following, key=lambda l: box[1] - l['box'][3])
            caption += ' ' + extra['text'].strip()
            box = union([box, extra['box']])
        context = (contexts or {}).get(match[1], '')
        score = caption_score(caption, match[1], title, context)
        trace = {'page':page_number, 'figure_number':match[1], 'caption':caption[:1000], 'score':score,
                 'role':caption_role(caption, title), 'context':context[:500], 'reason':'below_threshold'}
        if diagnostics is not None: diagnostics.append(trace)
        if score < 14:
            continue
        # PDF Form/Image objects often contain the complete vector/raster figure.
        above = [g for g in graphics if g[1] >= line['box'][3] - 3
                 and 0 <= g[1] - line['box'][3] < 65
                 and min(g[2], box[2]) - max(g[0], box[0]) > 15
                 and g[2] - g[0] >= 65 and g[3] - g[1] >= 40
                 and (g[2] - g[0]) * (g[3] - g[1]) < width * height * .8]
        method = 'graphic_object'
        if not above:
            graphic = vector_region(lines, paths, box, width, height)
            if graphic is None:
                trace['reason'] = 'no_graphic_region'
                continue
            method = 'vector_region'
        else:
            graphic = min(above, key=lambda g: (abs(g[1] - line['box'][3]), -(g[2]-g[0])*(g[3]-g[1])))
        # Include adjacent figure panels on the same baseline when they share this caption.
        peers = [g for g in graphics if abs(g[1] - graphic[1]) < 8 and abs(g[3] - graphic[3]) < 20
                 and g[0] >= box[0] - 6 and g[2] <= max(box[2], graphic[2]) + 6]
        crop = union([graphic] + peers)
        crop = [max(0, crop[0]-5), max(0, crop[1]-5, line['box'][3]+2), min(width, crop[2]+5), min(height, crop[3]+5)]
        if valid_rect(crop, width, height):
            flags = caption_flags(caption)
            if method == 'vector_region': flags.append('vector_region_review')
            trace['reason'] = 'candidate'
            candidates.append({'page': page_number, 'figure_number': match[1], 'caption': caption[:1000],
                               'bbox': [round(v, 2) for v in crop], 'score': score, 'method':method,
                               'scope':figure_scope(caption, title), 'review_reasons':flags, 'context':context[:500]})
    return candidates


def scan_document(document, title='', diagnostics=None, ocr=None):
    candidates = []
    ocr_pages = 0
    # Cross-page prose references allow a caption without architecture vocabulary.
    contexts = {}
    for index in range(min(len(document), MAX_SCAN_PAGES)):
        page = document[index]
        tp = page.get_textpage()
        try:
            content = re.sub(r'\s+', ' ', tp.get_text_range())
            for match in re.finditer(r'(?:Fig(?:ure)?\.?\s*)(\d+(?:\.\d+)*)(?!\d)(?:[a-z()]*)\s+(?:shows?|illustrates?|depicts?|presents?)\s+([^.!?]{8,240})', content, re.I):
                sentence = match[0]
                if re.search(r'architecture|block[ -]diagram|prototype|proposed|implemented', sentence, re.I):
                    old = contexts.get(match[1], '')
                    if not old or 'proposed' in sentence.lower(): contexts[match[1]] = sentence
        finally:
            tp.close(); page.close()
    for index in range(min(len(document), MAX_SCAN_PAGES)):
        page = document[index]
        try:
            bounds = page.get_bbox()
            width, height = bounds[2]-bounds[0], bounds[3]-bounds[1]
            textpage = page.get_textpage()
            try:
                texts, graphics, paths = [], [], []
                for n, obj in enumerate(page.get_objects(max_depth=1, textpage=textpage)):
                    if n >= MAX_OBJECTS:
                        break
                    box = page_rect(obj.get_bounds(), bounds)
                    if not valid_rect(box, width, height):
                        continue
                    if obj.type == 1:
                        try:
                            content = re.sub(r'[\x00-\x1f\ufffe]', '', obj.extract()).strip()
                        except UnicodeError:
                            continue  # One broken glyph must not discard the remaining pages.
                        if content:
                            texts.append({'text': content, 'box': box})
                    elif obj.type in (3, 5):  # Image or Form XObject: preserve full figure geometry.
                        graphics.append(box)
                    elif obj.type == 2:
                        paths.append(box)
                lines = recover_caption_lines(textpage, text_lines(texts), bounds)
                ocr_used = False
                if diagnostics is not None and not any(CAPTION.match(line['text']) for line in lines):
                    plain = ' '.join(line['text'] for line in lines)
                    language_hits = len(re.findall(r'\b(?:the|and|with|this|of|for|receiver|transmitter)\b',plain,re.I))
                    reason = 'text_encoding_or_scan' if language_hits < 3 else 'no_readable_caption'
                    trace = {'page':index+1, 'reason':reason, 'text_objects':len(texts), 'path_objects':len(paths)}
                    if reason == 'text_encoding_or_scan' and ocr is not None and ocr_pages < 3:
                        ocr_pages += 1
                        recovered, outcome = ocr(page, index)
                        trace['ocr'] = outcome
                        if recovered:
                            lines = recovered; ocr_used = True
                    diagnostics.append(trace)
                page_candidates = find_candidates(lines, graphics, width, height, index+1, title,
                                                  paths=paths, contexts=contexts, diagnostics=diagnostics)
                if ocr_used:
                    for candidate in page_candidates: candidate['review_reasons'].append('ocr_review')
                candidates.extend(page_candidates)
            finally:
                textpage.close()
        finally:
            page.close()
    return sorted(candidates, key=lambda c: (-c['score'], c['page'], c['bbox'][0]))


class FigureStore:
    def __init__(self, root, cache_namespace=STORAGE_VERSION):
        self.root = Path(root).resolve()
        self.documents = DocumentStore(self.root)
        if not re.fullmatch(r'[a-zA-Z0-9-]{1,60}', cache_namespace):
            raise DocumentError('Invalid figure cache namespace.')
        self.directory = self.root / 'outputs' / 'serdes_figures' / cache_namespace
        self.review_path = self.root / 'config' / 'serdes_figure_overrides.json'
        self.flags_path = self.root / 'config' / 'serdes_figure_review_flags.json'

    def review_flags(self, article, digest):
        try:
            entry = json.loads(self.flags_path.read_text(encoding='utf-8')).get(str(article))
        except FileNotFoundError:
            return []
        if not entry: return []
        if entry.get('pdf_sha256') != digest:
            return ['review_source_changed']
        return list(entry['reasons'])

    def reviewed_selection(self, paper, digest):
        try:
            entries = json.loads(self.review_path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return None
        entry = entries.get(str(paper['article_number']))
        if entry is None: return None
        if entry.get('pdf_sha256') != digest:
            raise DocumentError('Reviewed figure source hash changed; re-review required.')
        if entry.get('disposition') == 'no_top_diagram':
            if not valid_absence_review(entry):
                raise DocumentError('Invalid whole-document absence review.')
            return {**entry, 'selection':'source_review', 'verified':True, 'review_reasons':[]}
        if (not isinstance(entry.get('evidence'), str) or not entry['evidence'].strip()
                or type(entry.get('page')) is not int or entry['page'] < 1
                or not re.fullmatch(r'\d+(?:\.\d+)*', str(entry.get('figure_number', '')))
                or not isinstance(entry.get('bbox'), list) or len(entry['bbox']) != 4
                or not isinstance(entry.get('caption'), str)):
            raise DocumentError('Invalid reviewed figure selection.')
        return {**entry, 'selection':'source_review', 'verified':True, 'review_reasons':[], 'score':100,
                'coordinate_space':'page_local'}

    def paper_directory(self, article):
        if not isinstance(article, str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,180}', article):
            raise DocumentError('Invalid paper identifier.')
        key = hashlib.sha256(article.encode()).hexdigest()
        folder = (self.directory / key[:2] / key).resolve()
        if not folder.is_relative_to(self.root / 'outputs'):
            raise DocumentError('Figure cache path escapes the repository.')
        return folder

    def fingerprint(self, path):
        path = Path(path).resolve(strict=True)
        stat = path.stat()
        return {'path': path.relative_to(self.root).as_posix(), 'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}

    def lookup(self, paper):
        try:
            pdf = self.documents.resolve_path(paper)
        except DocumentError:
            return {'status': 'missing_pdf'}
        try:
            folder = self.paper_directory(str(paper['article_number']))
            index = folder / 'index.json'
            if index.stat().st_size > 100000:
                return {'status': 'not_prepared'}
            data = json.loads(index.read_text(encoding='utf-8'))
            if data.get('fingerprint') != self.fingerprint(pdf) or data.get('version') not in (VERSION, STORAGE_VERSION):
                return {'status': 'not_prepared'}
            if data.get('status') not in ('available', 'not_found', 'review_required', 'no_top_diagram'):
                return {'status': 'not_prepared'}
            if data.get('status') == 'no_top_diagram' and (not valid_absence_review(data)
                    or data.get('verified') is not True or data.get('image')):
                return {'status': 'not_prepared'}
            if data.get('status') == 'available' or data.get('image'):
                if (type(data.get('page')) is not int or not 1 <= data['page'] <= MAX_SCAN_PAGES
                        or not isinstance(data.get('caption'), str) or len(data['caption']) > 1000
                        or not re.fullmatch(r'\d+(?:\.\d+)*', str(data.get('figure_number', '')))
                        or any(type(data.get(k)) is not int or not 1 <= data[k] <= 1600 for k in ('width', 'height'))):
                    return {'status': 'not_prepared'}
                if not re.fullmatch(r'[a-f0-9]{32}\.png', data.get('image', '')):
                    return {'status': 'not_prepared'}
                path = (folder / data['image']).resolve()
                if not path.is_relative_to(folder) or not path.is_file():
                    return {'status': 'not_prepared'}
            return data
        except (OSError, ValueError, TypeError, AttributeError):
            return {'status': 'not_prepared'}

    def public(self, paper, measurement=None):
        data = self.lookup(paper)
        article = quote(str(paper['article_number']), safe='')
        result = {'status': data['status']}
        if data['status'] != 'missing_pdf':
            result['pdf_url'] = f'/pdf/{article}#page={data.get("page", 1)}'
        if data['status'] == 'review_required':
            result['review_reasons'] = data.get('review_reasons', [])
        if data['status'] == 'no_top_diagram':
            result.update(verified=True, selection='source_review', page=data['page'],
                          absence_reason=data['absence_reason'], review_note=data['review_note_ko'],
                          source_pages_reviewed=data['source_pages_reviewed'])
        if data['status'] == 'available' and measurement:
            ids = data.get('measurement_ids', [])
            implementations = data.get('implementation_ids', [])
            scope = data.get('scope', 'unknown')
            requested = measurement.get('component_scope', 'unknown')
            if requested in ('tx_rx','full_link'): requested = 'trx'
            if ids and measurement.get('id') not in ids:
                return {**result, 'status':'review_required', 'review_reasons':['different_implementation']}
            if implementations and measurement.get('implementation_id') not in implementations:
                return {**result, 'status':'review_required', 'review_reasons':['different_implementation']}
            reviewed_component = bool(implementations and data.get('verified') and
                                      (requested,scope) in (('tx','driver'),('rx','afe'),('rx','cdr')))
            # A reviewed source figure and a verified measurement association are
            # different facts. Permit a clearly separate paper reference, never
            # silently treat a component/variant as the complete measured system.
            note = self.context_note(paper, data)
            scope_mismatch = requested in (None, '', 'unknown') or scope != requested
            if note or (scope_mismatch and not reviewed_component):
                if not (data.get('verified') and data.get('selection') == 'source_review' and data.get('evidence')):
                    return {**result, 'status':'review_required', 'review_reasons':['scope_mismatch']}
                result.update(status='reference_only', measurement_link='reference_only',
                              measurement_scope=requested or 'unknown', association_verified=False,
                              scope_note=note or (f'성능점 범위: {str(requested or "unknown").upper()} · 그림 범위: {scope.upper()}. '
                                  '원문에서 확인한 논문 참고 그림이며, 이 성능점의 전체 회로·전력 범위와 동일하다는 뜻은 아닙니다.'))
            else:
                result['measurement_link'] = 'reviewed' if ids else 'implementation' if implementations else 'paper_level'
                result['association_verified'] = bool(ids or implementations)
        if data['status'] == 'available':
            result.update({key: data[key] for key in ('page', 'figure_number', 'caption', 'width', 'height')})
            result.update(selection=data.get('selection', 'caption'), verified=bool(data.get('verified',False)),
                          scope=data.get('scope','unknown'),
                          image_url=f'/api/serdes/papers/{article}/block-diagram/image?v={data["image"][:-4]}',
                          pdf_url=f'/pdf/{article}#page={data["page"]}')
        return result

    def context_note(self, paper, record):
        """Optional source/crop-bound qualification, never an inferred binding."""
        try:
            entry=json.loads((self.root/'config/serdes_figure_context_notes.json').read_text(encoding='utf-8')).get(str(paper['article_number']))
        except FileNotFoundError:
            return None
        if not entry: return None
        if any(entry.get(k)!=record.get(k) for k in ('pdf_sha256','page','figure_number','scope','bbox')):
            return '그림 선택이 변경되어 이전 변형 연결을 재사용하지 않습니다. 논문 참고 그림으로만 표시합니다.'
        return entry.get('note')

    def image_path(self, paper):
        data = self.lookup(paper)
        return self.paper_directory(str(paper['article_number'])) / data['image'] if data['status'] == 'available' else None

    def prepare(self, paper, retry=False):
        previous = self.lookup(paper)
        if previous['status'] == 'missing_pdf' or (not retry and previous['status'] != 'not_prepared' and previous.get('version') == VERSION):
            return previous
        import pypdfium2 as pdfium
        path, digest = self.documents._file_info(paper)
        fingerprint = self.fingerprint(path)
        reviewed = self.reviewed_selection(paper, digest)
        with pdfium.PdfDocument(_io_path(path), password='') as document:
            diagnostics = []
            from serdes_ocr import recognize_page
            absence = reviewed and reviewed.get('disposition') == 'no_top_diagram'
            if absence and reviewed['page_count'] != len(document):
                raise DocumentError('Absence review does not cover the actual complete document.')
            candidates = [] if reviewed else scan_document(document, paper.get('title', ''), diagnostics,
                                       ocr=lambda page,index: recognize_page(page,self.root,digest,index))
            record = {'version': VERSION, 'status': 'not_found', 'article_number': str(paper['article_number']),
                      'pdf_sha256': digest, 'fingerprint': fingerprint,
                      'created_at': datetime.now(timezone.utc).isoformat(), 'candidates': candidates[:8],
                      'scanned_pages': min(len(document), MAX_SCAN_PAGES), 'coordinate_space':'page_local',
                      'selection':'caption_context', 'verified':False}
            folder = self.paper_directory(str(paper['article_number']))
            folder.mkdir(parents=True, exist_ok=True)
            if absence:
                record.update(reviewed, status='no_top_diagram', scanned_pages=len(document))
            elif candidates or reviewed:
                chosen = reviewed or candidates[0]
                reasons = list(chosen.get('review_reasons', []))
                if not reviewed and len(candidates)>1 and candidates[0]['score']-candidates[1]['score'] < 3:
                    reasons.append('ambiguous_ranking')
                if not reviewed and chosen.get('scope') == 'unknown':
                    reasons.append('unknown_scope')
                if not reviewed:
                    reasons.extend(self.review_flags(paper['article_number'], digest))
                    if chosen.get('scope') == 'adc' and not re.search(r'\badc\b', paper.get('title',''), re.I):
                        reasons.append('subblock_not_top_level')
                reasons = list(dict.fromkeys(reasons))
                if chosen['page'] > len(document) or chosen['page'] > MAX_SCAN_PAGES:
                    raise DocumentError('Reviewed figure page is outside the document.')
                page = document[chosen['page']-1]
                try:
                    bounds = page.get_bbox()
                    width, height = bounds[2]-bounds[0], bounds[3]-bounds[1]
                    l, b, r, t = chosen['bbox']
                    if not valid_rect(chosen['bbox'],width,height):
                        raise DocumentError('Figure bbox is outside its page.')
                    bitmap = page.render(scale=min(3, 1400/max(r-l,t-b)),
                                         crop=render_crop(chosen['bbox'],width,height,page.get_rotation()), draw_annots=False)
                    try:
                        picture = bitmap.to_pil().convert('RGB')
                        if reviewed and 'include_bboxes' in chosen:
                            picture = isolate_reviewed_regions(picture, chosen['bbox'], chosen['include_bboxes'], page.get_rotation())
                        image_name = uuid.uuid4().hex + '.png'
                        picture.save(folder / image_name, format='PNG', optimize=True)
                        record.update(chosen, status='review_required' if reasons else 'available', review_reasons=reasons,
                                      image=image_name, width=picture.width, height=picture.height)
                    finally:
                        bitmap.close()
                finally:
                    page.close()
        if self.fingerprint(path) != fingerprint:
            raise DocumentError('Source PDF changed during extraction.')
        # Retain each extraction record/image; only the small current pointer changes.
        diagnostic_name = uuid.uuid4().hex + '.diagnostics.json'
        (folder / diagnostic_name).write_text(json.dumps({'version':VERSION, 'pdf_sha256':digest, 'captions':diagnostics},ensure_ascii=False),encoding='utf-8')
        record['diagnostics_file'] = diagnostic_name
        run = folder / (uuid.uuid4().hex + '.json')
        run.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
        temporary = folder / (uuid.uuid4().hex + '.tmp')
        temporary.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
        temporary.replace(folder / 'index.json')
        return record

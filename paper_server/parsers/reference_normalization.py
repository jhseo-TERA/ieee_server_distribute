"""Traceable display-title normalization, after source-grounded extraction.

Never changes raw/title/author/year/DOI. Unsupported math is reviewable, with no
search key, rather than silently deleting symbols or guessing scientific data.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata

VERSION = 'reference-title-normalization-v3'
FORMAT_COMMANDS = {'mathrm', 'mathit', 'mathtt', 'mathbf', 'text', 'textit', 'textrm', 'operatorname'}
SYMBOL_COMMANDS = {'mu': 'μ', 'alpha': 'α', 'beta': 'β', 'gamma': 'γ', 'omega': 'ω',
                   'Omega': 'Ω', 'Delta': 'Δ', 'times': '×', 'pm': '±', 'cdot': '·',
                   'leq': '≤', 'geq': '≥', 'infty': '∞'}


def compact(value):
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', str(value or ''))).strip()


def source_key(value):
    return ''.join(c for c in unicodedata.normalize('NFKD', str(value or '')).casefold() if c.isalnum())


def match_key(value):
    # Keep scientific operators and indices: A+B must not collide with A-B.
    return compact(value).casefold().translate(str.maketrans({'–': '-', '—': '-', '−': '-', '“': '"', '”': '"'}))


class MathUnsupported(ValueError):
    pass


class InlineMath:
    def __init__(self, value):
        if len(value) > 1500:
            raise MathUnsupported('math_too_long')
        self.value, self.index = value, 0

    def skip(self):
        while self.index < len(self.value) and (self.value[self.index].isspace() or self.value[self.index] == '~'):
            self.index += 1

    def group(self, depth):
        self.skip()
        if self.index >= len(self.value) or self.value[self.index] != '{':
            raise MathUnsupported('math_missing_group')
        self.index += 1
        return self.parse(depth + 1, grouped=True)

    def parse(self, depth=0, grouped=False):
        if depth > 16:
            raise MathUnsupported('math_nesting_limit')
        output = ''
        while self.index < len(self.value):
            char = self.value[self.index]
            if char == '}':
                if not grouped:
                    raise MathUnsupported('math_unbalanced_group')
                self.index += 1
                return compact(output)
            if char == '{':
                output += self.group(depth)
            elif char == '\\':
                self.index += 1
                command = re.match(r'[A-Za-z]+', self.value[self.index:])
                if command:
                    name = command.group()
                    self.index += len(name)
                    if name in FORMAT_COMMANDS:
                        output += self.group(depth)
                    elif name in ('bf', 'rm', 'it', 'scriptstyle', 'displaystyle'):
                        pass  # Style switches affect typography only.
                    elif name in ('left', 'right'):
                        self.skip()
                        if self.index >= len(self.value) or self.value[self.index] not in '()[]|.':
                            raise MathUnsupported('math_invalid_delimiter')
                        delimiter = self.value[self.index]
                        output += '' if delimiter == '.' else delimiter
                        self.index += 1
                    elif name in SYMBOL_COMMANDS:
                        output += SYMBOL_COMMANDS[name]
                    elif name in ('quad', 'qquad'):
                        output += ' '
                    else:
                        raise MathUnsupported('math_unknown_command')
                else:
                    if self.index >= len(self.value):
                        raise MathUnsupported('math_incomplete_escape')
                    escaped = self.value[self.index]
                    self.index += 1
                    if escaped in ',;! ': output += ' '
                    elif escaped in '{}%&#$': output += escaped
                    else: raise MathUnsupported('math_ambiguous_escape')
            elif char in '_^':
                self.index += 1
                self.skip()
                if self.index >= len(self.value):
                    raise MathUnsupported('math_missing_index')
                if self.value[self.index] == '{':
                    index = self.group(depth)
                else:
                    index = self.value[self.index]
                    self.index += 1
                if not index:
                    raise MathUnsupported('math_empty_index')
                if re.fullmatch(r'[A-Za-z](?:\s+[A-Za-z]){1,7}', index):
                    index = re.sub(r'\s+', '', index)
                if not re.fullmatch(r'[^\W_]+', index, re.UNICODE):
                    index = '(' + index + ')'
                output = output.rstrip() + char + index
            elif char.isdigit():
                number = re.match(r'\d+(?:\s*\.\s*\d+)?', self.value[self.index:]).group()
                output += re.sub(r'\s+', '', number)
                self.index += len(number)
            elif char == '$':
                raise MathUnsupported('math_nested_dollar')
            else:
                output += ' ' if char == '~' else char
                self.index += 1
        if grouped:
            raise MathUnsupported('math_unbalanced_group')
        return compact(output)


def normalize_math(title):
    # Repeated escaped dollar signs may be an escaped math fence or literal
    # currency. Require independent PDF text before interpreting them.
    if len(re.findall(r'\\\$', title)) >= 2:
        raise MathUnsupported('math_escaped_delimiters')
    if len(re.findall(r'(?<!\\)\$\s*\d', title)) >= 2:
        raise MathUnsupported('math_ambiguous_currency')
    output, steps, cursor = '', [], 0
    opening = re.compile(r'(?<!\\)(\$\$?|\\\()')
    while match := opening.search(title, cursor):
        delimiter = match.group()
        closing = '\\)' if delimiter == '\\(' else delimiter
        end = re.search(r'(?<!\\)' + re.escape(closing), title[match.end():])
        if not end:
            raise MathUnsupported('math_unclosed_span')
        stop = match.end() + end.start()
        content = title[match.end():stop]
        if title[:match.start()].rstrip()[-1:].isdigit() and re.match(r'\s*\.\s*\d', content):
            raise MathUnsupported('math_split_decimal')
        rendered = InlineMath(content).parse()
        if not rendered:
            raise MathUnsupported('math_empty_span')
        output += title[cursor:match.start()] + rendered
        cursor = stop + len(closing)
        steps.append({'rule': 'inline_math_to_text', 'source': title[match.start():cursor], 'display': rendered})
    output += title[cursor:]
    if re.search(r'\\[A-Za-z]+|[_^]\s*\{', output):
        raise MathUnsupported('math_unscoped_tex')
    return compact(output), steps


def strip_reference_type(title, raw):
    match = re.search(r'\s*\[(J|C)\]\s*[.;,]?\s*$', title)
    if not match:
        return title, None, [], []
    raw, literal = compact(raw), compact(title)
    offset = raw.find(literal)
    if offset < 0:
        return title, None, [], ['ambiguous_reference_type_tag']
    following = raw[offset + len(literal):]
    # A closing quote would put the tag inside an explicitly quoted title.
    boundary = re.match(r'^[\s.;,:]*(IEEE\b|Journal\b|Proc\b|Proceedings\b|ACM\b|IET\b)', following, re.I)
    if not boundary or not re.search(r'\b(?:18|19|20)\d{2}\b', following[:600]):
        return title, None, [], ['ambiguous_reference_type_tag']
    display = title[:match.start()].rstrip()
    return display, match.group(1), [{'rule': 'reference_type_suffix', 'source': match.group().strip(),
                                    'reference_type': match.group(1)}], []


def normalize_title(title, raw_reference):
    original = title
    result = {'title_display': title, 'title_match_key': None, 'reference_type': None,
              'normalization_version': VERSION, 'normalization_steps': [], 'normalization_warnings': [],
              'normalization_needs_review': False, 'normalization_status': 'missing' if not title else 'clean'}
    if not title:
        return result
    title = compact(title)
    if len(title) > 5000 or not source_key(title) or source_key(title) not in source_key(raw_reference):
        result.update(title_display=None, normalization_needs_review=True, normalization_status='needs_review',
                      normalization_warnings=['title_source_not_grounded'])
        return result
    display, kind, steps, warnings = strip_reference_type(title, raw_reference)
    try:
        display, math_steps = normalize_math(display)
        steps.extend(math_steps)
    except MathUnsupported as exc:
        warnings.append(str(exc))
        display = None
    result.update(title_display=display, title_match_key=match_key(display) if display and not warnings else None,
                  reference_type=kind, normalization_steps=steps, normalization_warnings=warnings,
                  normalization_needs_review=bool(warnings),
                  normalization_status='needs_review' if warnings else 'normalized' if display != original else 'clean')
    return result


def apply_title_normalization(row, *, source_reference=None):
    # The caller must authenticate alternate PDF evidence before passing it.
    # The original reference string is preserved in the output.
    reference = row.get('raw', '') if source_reference is None else source_reference
    result = {**row, **normalize_title(row.get('title'), reference)}
    result['normalization_reference_sha256'] = hashlib.sha256(reference.encode()).hexdigest()
    result['normalization_source_sha256'] = hashlib.sha256(str(row.get('title') or '').encode()).hexdigest()
    result['needs_review_before_normalization'] = row.get('needs_review_before_normalization', bool(row.get('needs_review')))
    result['needs_review'] = bool(result['needs_review_before_normalization'] or result['normalization_needs_review'])
    return result


def recover_from_pdf(row, evidence, *, expected_pdf_sha256):
    """Recover only flagged math, using independently extracted PDF evidence.

    The evidence worker binds the title to the exact PDF hash/page/ref number.
    Here we additionally check author/year and substantial unchanged title text.
    """
    if not row.get('normalization_needs_review') or not any(w.startswith('math_') for w in row['normalization_warnings']):
        return row
    if (evidence.get('number') != row.get('number') or evidence.get('page') != row.get('page')
            or not re.fullmatch(r'[0-9a-f]{64}', expected_pdf_sha256)
            or evidence.get('source_sha256') != expected_pdf_sha256):
        return row
    title, reference = evidence.get('title', ''), evidence.get('reference_text', '')
    reference_hash = hashlib.sha256(reference.encode()).hexdigest()
    if (not title or not source_key(row.get('author'))
            or source_key(row['author']) not in source_key(evidence.get('author_text'))
            or not row.get('year') or not re.search(r'\b' + re.escape(str(row['year'])) + r'\b', reference)
            or evidence.get('reference_text_sha256') != reference_hash):
        return row
    original = row.get('title') or ''
    fragments = re.split(r'(?<!\\)\$\$?.*?(?<!\\)\$\$?', original)
    anchor_size = sum(len(source_key(f)) for f in fragments if len(source_key(f)) >= 6 and source_key(f) in source_key(title))
    if anchor_size < 35:
        return row
    checked = normalize_title(title, reference)
    if checked['normalization_needs_review']:
        return row
    result = {**row, **checked}
    result['normalization_status'] = 'recovered_from_pdf'
    result['normalization_steps'] = row['normalization_steps'] + [
        {'rule': 'pdf_text_recovery', 'source_title': original, 'display': checked['title_display'],
         'matched_anchor_characters': anchor_size, 'original_warnings': row['normalization_warnings'],
         'recovered_title_steps': checked['normalization_steps']}]
    result['title_recovery_evidence'] = evidence
    result['needs_review'] = bool(row.get('needs_review_before_normalization', False))
    return result

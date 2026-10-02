"""Deterministic relevance gates shared by SQL fallback and AI selection."""
from collections import Counter, defaultdict
from math import log, sqrt
import html
import re
import unicodedata
from difflib import SequenceMatcher

VERSION = 'favorites-ai-4'
INTEREST_INSTRUCTIONS = (
    '사용자는 optical I/O와 electrical I/O 모두에 관심이 있습니다. 광학만 우선하는 것으로 추정하지 마세요. '
    'Electrical I/O에서 DMT(discrete multitone) 또는 multicarrier/OFDM 방식이 주된 연구인 논문은 추천에서 제외하세요. '
    '일반 electrical PAM/NRZ, equalizer, CDR, HBM 및 die-to-die 회로는 관심 분야입니다. '
    '광학 WDM/다중 파장·다중 채널을 이 제외 조건과 혼동하지 마세요. Optical I/O의 DMT/OFDM까지 제외하라는 뜻이 아닙니다. '
    '단순 비교·관련 연구 언급만으로 제외하지 마세요.'
)
MULTICARRIER_RE = re.compile(r'\b(?:dmt|discrete[\s-]+multi[\s-]?tone|multi[\s-]?carrier|ofdm|orthogonal frequency[\s-]+division multiplex\w*)\b', re.I)
ELECTRICAL_RE = re.compile(r'\b(?:electrical|wireline|wired|copper|backplane|die[\s-]+to[\s-]+die|chip[\s-]+to[\s-]+chip|serdes|hbm)\b|\bi\s*/\s*o\b', re.I)
OPTICAL_RE = re.compile(r'\b(?:optical|photonic\w*|fiber|fibre|wdm|vcsel|laser|microring|mzm|im[\s/]dd|pon)\b', re.I)

INTEREST_TOPICS = (
    ('광 I/O', re.compile(r'\b(?:optical|photonic\w*|fiber|fibre|wdm|vcsel|laser|micro[- ]?ring|mzm|modulator|silicon photonics?)\b', re.I)),
    ('메모리 I/O', re.compile(r'\b(?:hbm|ddr\d*|memory interface|gddr|lpddr)\b', re.I)),
    ('클럭·CDR', re.compile(r'\b(?:cdr|clock[ -]?and[ -]?data|clock recovery|jitter|pll|phase[- ]locked|injection[- ]locked)\b', re.I)),
    ('등화·채널', re.compile(r'\b(?:equaliz\w*|dfe|ffe|feed[- ]?forward|decision[- ]?feedback|channel loss|crosstalk)\b', re.I)),
    ('수신기', re.compile(r'\b(?:receiver|transimpedance|\btia\b|sense amplifier|sampler)\b', re.I)),
    ('송신기', re.compile(r'\b(?:transmitter|serializer|output driver|voltage[- ]mode driver|current[- ]mode driver)\b', re.I)),
    ('전기 I/O', re.compile(r'\b(?:electrical|wireline|backplane|die[ -]?to[ -]?die|chiplet|chip[ -]?to[ -]?chip|serdes|transceiver|i\s*/\s*o|interface|interconnect|serial link|data link|source[- ]synchronous|single[- ]ended|pam[- ]?[2348]|nrz|[gt]b/s(?:/pin|/wire)?)\b', re.I)),
)


def classify_interest_topic(paper):
    """Transparent title-based topic used only by deterministic recommendation modes."""
    title = unicodedata.normalize('NFKC', html.unescape(str(paper.get('title') or '')))
    for name, pattern in INTEREST_TOPICS:
        if pattern.search(title):
            return name
    return '기타 관련 연구'


def excluded_electrical_multicarrier(paper):
    """Only exclude a title-centered multicarrier topic with electrical evidence."""
    title = unicodedata.normalize('NFKC', html.unescape(str(paper.get('title') or '')))
    title = re.sub(r'[\u2010-\u2015\u2212]', '-', title)
    # Do not turn absence/comparison of a technique into a positive topic match.
    if re.search(r'\b(?:without|non[- ]|free of)\s*(?:dmt|ofdm|multicarrier)\b|\b(?:dmt|ofdm|multi[- ]?carrier)[- ]free\b', title, re.I):
        return False
    if re.search(r'\b(?:versus|vs\.?|comparison|comparative|compared)\b', title, re.I):
        return False
    if not MULTICARRIER_RE.search(title) or OPTICAL_RE.search(title):
        return False
    if ELECTRICAL_RE.search(title):
        return True
    abstract = str(paper.get('abstract') or '')
    # A generic CMOS/DAC title alone cannot establish the link medium.
    return bool(ELECTRICAL_RE.search(abstract) and not OPTICAL_RE.search(abstract))
SOURCE_PROFILE_CAP = 100
TOTAL_RECOMMENDATIONS = 180
MIN_AI_SCORE = 70
MIN_SIMILARITY = 0.25
GENERIC_TERMS = frozenset({'cmos', 'power', 'bit', 'circuit', 'circuits', 'integrated',
    'frequency', 'voltage', 'data', 'digital', 'analog', 'nm', 'ghz', 'mhz', 'khz', 'db', 'gbps'})


def identity_text(title):
    value = unicodedata.normalize('NFKC', html.unescape(str(title or ''))).casefold()
    value = re.sub(r'^\s*\d+\.\d+\s+', '', value)
    value = re.sub(r'\s*:\s*publisher[’\x27]s note\s*$', '', value)
    value = value.replace(r'\mu', 'u').replace('μ', 'u').replace('µ', 'u')
    return re.sub(r'[^a-z0-9.]', '', value)


def near_duplicate(left, right):
    a, b = identity_text(left), identity_text(right)
    if not a or not b:
        return False
    if a == b:
        return True
    # Keep changes in measured rate/process and serial parts (I/II etc.).
    if re.findall(r'\d+(?:\.\d+)?', a) != re.findall(r'\d+(?:\.\d+)?', b):
        return False
    if re.findall(r'\bpart\s+([ivx]+|\d+)\b', left.casefold()) != re.findall(r'\bpart\s+([ivx]+|\d+)\b', right.casefold()):
        return False
    return SequenceMatcher(None, a, b, autojunk=False).ratio() >= .94


def weighted_profile(rows, tokenize, topn=40):
    """Cap each venue's total contribution without inferring why it was saved."""
    rows = [row for row in rows if not excluded_electrical_multicarrier(row)]
    counts = Counter(row['source_name'] for row in rows)
    frequencies, occurrences = Counter(), Counter()
    for row in rows:
        terms = tokenize(row['title'])
        weight = min(1.0, SOURCE_PROFILE_CAP / counts[row['source_name']])
        occurrences.update(terms)
        for term in terms:
            frequencies[term] += weight
    minimum = 2 if len(rows) >= 5 else 1
    return sorted((t for t in frequencies if occurrences[t] >= minimum),
                  key=lambda t: (-frequencies[t], t))[:topn]


class FavoriteSimilarity:
    """Explain a candidate using its best individual favorite, not a word soup."""
    def __init__(self, favorites, tokenize):
        self.tokenize = tokenize
        self.rows = [row for row in favorites if not excluded_electrical_multicarrier(row)]
        self.terms = [tokenize(row['title']) for row in self.rows]
        frequencies = Counter(t for terms in self.terms for t in terms)
        self.idf = {t: 1 + log((len(self.rows) + 1) / (count + 1)) for t, count in frequencies.items()}
        self.index = defaultdict(set)
        self.norms = []
        for i, terms in enumerate(self.terms):
            self.norms.append(sqrt(sum(self.idf[t] ** 2 for t in terms)))
            for term in terms - GENERIC_TERMS:
                self.index[term].add(i)

    def evidence(self, title):
        terms = self.tokenize(title)
        hits = Counter(i for t in terms - GENERIC_TERMS for i in self.index.get(t, ()))
        norm = sqrt(sum(self.idf.get(t, 1 + log(len(self.rows) + 1)) ** 2 for t in terms))
        best, anchor, shared, duplicate = 0.0, None, set(), False
        for i, count in hits.items():
            if count < 2 or not norm or not self.norms[i]:
                continue
            overlap = terms & self.terms[i]
            if len(overlap) < 3:
                continue
            score = sum(self.idf[t] ** 2 for t in overlap) / (norm * self.norms[i])
            if score >= .7 and near_duplicate(title, self.rows[i]['title']):
                duplicate = True
            if score > best or (score == best and str(self.rows[i].get('article_number', '')) < str((anchor or {}).get('article_number', ''))):
                best, anchor, shared = score, self.rows[i], overlap
        return {'favorite_similarity': round(best, 4),
                'related_favorite_id': str(anchor.get('article_number', '')) if anchor else None,
                'related_favorite_title': anchor['title'] if anchor else None,
                'shared_terms': sorted(shared), 'near_duplicate_favorite': duplicate}


def allocate(rows, total=TOTAL_RECOMMENDATIONS, per_source=20):
    """Global quality budget, with a venue ceiling but no venue minimum."""
    if total <= 0 or per_source <= 0:
        return []
    ordered = sorted(rows, key=lambda r: (-float(r.get('recommendation_score', r.get('favorite_similarity', 0))),
                     -float(r.get('score', 0)), str(r.get('article_number', ''))))
    counts, seen, output = Counter(), set(), []
    for row in ordered:
        if excluded_electrical_multicarrier(row):
            continue
        key = str(row.get('article_number', ''))
        if key in seen or counts[row['source_name']] >= per_source:
            continue
        seen.add(key)
        counts[row['source_name']] += 1
        output.append(row)
        if len(output) >= total:
            break
    return output

"""Deterministic relevance gates shared by SQL fallback and AI selection."""
from collections import Counter, defaultdict
from math import log, sqrt
import html
import re
import unicodedata
from difflib import SequenceMatcher

VERSION = 'favorites-ai-7'
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
    ('전기 I/O', re.compile(r'\b(?:electrical|wireline|backplane|die[ -]?to[ -]?die|d2d|chiplet|chip[ -]?to[ -]?chip|serdes|transceiver|i\s*/\s*o|interface|interconnect|serial link|data link|source[- ]synchronous|single[- ]ended|pam[- ]?[2348]|nrz|[gt]b/s(?:/pin|/wire)?)\b', re.I)),
)


def classify_interest_topic(paper):
    """Transparent title-based topic used only by deterministic recommendation modes."""
    title = technical_text(paper.get('title'))
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

# Replace a full phrase before tokenization so an abbreviation and its expansion
# count as ONE feature. Keep modulation orders distinct (PAM2 != PAM4).
TECHNICAL_ALIASES = (
    ('dfe', r'decision[ -]+feedback equaliz(?:er|ation|ers)|dfe', 'decision feedback equalizer'),
    ('ffe', r'feed[ -]?forward equaliz(?:er|ation|ers)|ffe', 'feed forward equalizer'),
    ('cdr', r'clock[ -]+(?:and[ -]+)?data recovery|cdr', 'clock and data recovery'),
    ('tia', r'transimpedance amplifier(?:s)?|tia', 'transimpedance amplifier'),
    ('pll', r'phase[ -]+locked loop(?:s)?|pll', 'phase locked loop'),
    ('hbm', r'high[ -]+bandwidth memor(?:y|ies)|hbm', 'high bandwidth memory'),
    ('wdm', r'wavelength[ -]+division multiplex(?:ing|ed|er|ers)?|wdm', 'wavelength division multiplexing'),
    ('nrz', r'non[ -]+return[ -]+to[ -]+zero|nrz', 'non return to zero'),
    ('serdes', r'serializer[ /-]+deserializer|serdes', 'serializer deserializer'),
    ('d2d', r'die[ -]+to[ -]+die|d2d', 'die to die'),
    ('microring', r'micro[ -]?ring(?:s)?', 'microring'),
    ('pam2', r'pam[ -]?2', 'pam2 pam-2'),
    ('pam3', r'pam[ -]?3', 'pam3 pam-3'),
    ('pam4', r'pam[ -]?4', 'pam4 pam-4'),
    ('pam8', r'pam[ -]?8', 'pam8 pam-8'),
)
CONCEPT_PATTERNS = tuple((name, re.compile(r'\b(?:' + pattern + r')\b', re.I), expansion)
                         for name, pattern, expansion in TECHNICAL_ALIASES)
CONCEPT_NAMES = frozenset(name for name, _, _ in CONCEPT_PATTERNS)
BROAD_TERMS = GENERIC_TERMS | frozenset({
    'receiver', 'transmitter', 'transceiver', 'optical', 'electrical', 'silicon',
    'photonic', 'photonics', 'link', 'links', 'interface', 'interfaces', 'adaptive',
})


def technical_text(title):
    value = unicodedata.normalize('NFKC', html.unescape(str(title or ''))).casefold()
    value = re.sub(r'[\u2010-\u2015\u2212]', '-', value)
    for name, pattern, _ in CONCEPT_PATTERNS:
        value = pattern.sub(name, value)
    return value


def technical_terms(title, tokenize):
    # Tokenizers used for SQL retrieval may collapse PAM orders. Restore those
    # specific features here without changing the database's lexical tokenizer.
    value = technical_text(title)
    terms = set(tokenize(value)) - GENERIC_TERMS
    orders = set(re.findall(r'\bpam[2348]\b', value))
    if orders:
        terms.discard('pam')
        terms.update(orders)
    return terms


def expanded_search_terms(terms):
    """Bounded lexical expansion for retrieval; relevance is checked afterwards."""
    output = list(dict.fromkeys(terms))
    words = set(re.findall(r'[a-z0-9]+', ' '.join(output).casefold()))
    for name, _, expansion in CONCEPT_PATTERNS:
        expanded = expansion.split()
        if name in words or set(expanded) <= words:
            output.extend([name, *expanded])
    # The legacy SQL profile deliberately uses a generic PAM term.
    if 'pam' in words:
        output.extend(['pam2', 'pam3', 'pam4', 'pam8'])
    return list(dict.fromkeys(output))


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
        self.terms = [technical_terms(row['title'], tokenize) for row in self.rows]
        # Repeated imports must not change the statistics of a user's interests.
        unique = {}
        for row, terms in sorted(zip(self.rows, self.terms), key=lambda pair: (
                pair[0].get('source_name', ''), str(pair[0].get('article_number', '')))):
            # Identity ignores punctuation; tokenization can still distinguish
            # "SAR-ADC" from "SAR ADC". Count all variant terms once per identity.
            _, combined = unique.setdefault(identity_text(row['title']), (row, set()))
            combined.update(terms)
        sources = Counter(row.get('source_name', '') for row, _ in unique.values())
        frequencies, mass = Counter(), 0.0
        for row, terms in unique.values():
            weight = min(1.0, SOURCE_PROFILE_CAP / sources[row.get('source_name', '')])
            mass += weight
            for term in terms:
                frequencies[term] += weight
        self.unknown_idf = 1 + log(mass + 1)
        self.idf = {t: 1 + log((mass + 1) / (count + 1)) for t, count in frequencies.items()}
        self.index = defaultdict(set)
        self.norms = []
        self.identities = {identity_text(row['title']) for row in self.rows}
        for i, terms in enumerate(self.terms):
            self.norms.append(sqrt(sum(self.idf[t] ** 2 for t in terms)))
            for term in terms:
                self.index[term].add(i)

    def evidence(self, title):
        terms = technical_terms(title, self.tokenize)
        hits = Counter(i for t in terms for i in self.index.get(t, ()))
        norm = sqrt(sum(self.idf.get(t, self.unknown_idf) ** 2 for t in terms))
        best, anchor, shared = 0.0, None, set()
        duplicate = identity_text(title) in self.identities
        for i, count in hits.items():
            if count < 2 or not norm or not self.norms[i]:
                continue
            overlap = terms & self.terms[i]
            # Two specific shared features can explain short technical titles;
            # broad words like "CMOS optical receiver" cannot establish a match.
            if not overlap - BROAD_TERMS or (len(overlap) < 3 and not overlap & CONCEPT_NAMES):
                continue
            score = sum(self.idf[t] ** 2 for t in overlap) / (norm * self.norms[i])
            if score >= .7 and near_duplicate(title, self.rows[i]['title']):
                duplicate = True
            if score > best or (score == best and str(self.rows[i].get('article_number', '')) < str((anchor or {}).get('article_number', ''))):
                best, anchor, shared = score, self.rows[i], overlap
        literal = set(self.tokenize(title)) & set(self.tokenize(anchor['title'])) if anchor else set()
        return {'favorite_similarity': round(min(best, 1.0), 4),
                'related_favorite_id': str(anchor.get('article_number', '')) if anchor else None,
                'related_favorite_title': anchor['title'] if anchor else None,
                'shared_terms': sorted(literal), 'shared_concepts': sorted(shared & CONCEPT_NAMES),
                'near_duplicate_favorite': duplicate}


def diverse_select(rows, per_source, total=TOTAL_RECOMMENDATIONS, topic_field='interest_topic'):
    """Greedy relevance ranking with topic, venue and title redundancy penalties.

    Incremental maximum title overlap keeps cost O(candidates * selected), and
    scores/inputs remain independent of incidental SQL ordering.
    """
    remaining = sorted((dict(row) for row in rows if not excluded_electrical_multicarrier(row)),
                       key=lambda r: (-r['recommendation_score'], str(r.get('article_number', ''))))
    if per_source <= 0 or total <= 0:
        return []
    tokenize = lambda value: set(re.findall(r'[a-z][a-z0-9]*(?:-[a-z0-9]+)*', value)) - {
        'a', 'an', 'the', 'with', 'for', 'and', 'of', 'in', 'using', 'based', 'on', 'to'}
    terms = [technical_terms(row.get('title', ''), tokenize) for row in remaining]
    redundancy = [0.0] * len(remaining)
    selected, used, sources, topics = [], set(), Counter(), Counter()
    available = set(range(len(remaining)))
    while available and len(selected) < total:
        eligible = [i for i in sorted(available)
                    if sources[remaining[i]['source_name']] < per_source
                    and str(remaining[i].get('article_number', '')) not in used]
        if not eligible:
            break
        def adjusted(i):
            row = remaining[i]
            topic = row.get(topic_field, '기타 관련 연구')
            topic_penalty = 12 * topics[topic]
            return row['recommendation_score'] - topic_penalty - 4 * sources[row['source_name']] - 25 * redundancy[i]
        chosen = max(eligible, key=lambda i: (adjusted(i), remaining[i]['recommendation_score'], -i))
        item = remaining[chosen]
        item['relevance_score'] = item['recommendation_score']
        item['recommendation_score'] = round(adjusted(chosen), 2)
        item['recommendation_reason'] = '관련성을 바탕으로 비슷한 제목·주제·출처의 반복을 줄인 추천'
        selected.append(item)
        available.remove(chosen)
        used.add(str(item.get('article_number', '')))
        sources[item['source_name']] += 1
        topics[item.get(topic_field, '기타 관련 연구')] += 1
        for i in available:
            union = terms[i] | terms[chosen]
            overlap = len(terms[i] & terms[chosen]) / len(union) if union else 0.0
            redundancy[i] = max(redundancy[i], overlap)
    return selected


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

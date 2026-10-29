"""Grounded, additive retrieval from saved favorite abstracts to corpus titles.

Only recognized technical concepts can expand a query. A new concept AND an
independent title concept/domain must agree; ordinary prose never supplies
either piece of evidence. The shared index contains titles, never user seeds.
"""
from collections import defaultdict
from math import log, sqrt
import html
import re
import time
import unicodedata

from .policy import TECHNICAL_ALIASES, excluded_electrical_multicarrier, identity_text, near_duplicate

MAX_ABSTRACT_POOL = 1500
ABSTRACT_VERSION = 'saved-abstract-terms-1'
# Conservative vocabulary of circuit/link functions and multiword techniques.
# Patterns match complete concepts, not the individual words of a phrase.
PATTERNS = [(name, pattern) for name, pattern, _ in TECHNICAL_ALIASES] + [
    ('ctle', r'continuous[ -]+time[ -]+linear[ -]+equaliz(?:er|ation|ers)|ctle'),
    ('input-referred noise', r'input[ -]+referred[ -]+noise'),
    ('inductive peaking', r'(?:shunt[ -]+|series[ -]+)?inductive[ -]+peaking'),
    ('shunt peaking', r'shunt[ -]+peaking'),
    ('bandwidth extension', r'bandwidth[ -]+(?:extension|enhancement)'),
    ('offset cancellation', r'(?:dc[ -]+)?offset[ -]+(?:cancellation|compensation|calibration)'),
    ('common-mode feedback', r'common[ -]+mode[ -]+feedback|cmfb'),
    ('negative capacitance', r'negative[ -]+capacitance'),
    ('regulated cascode', r'regulated[ -]+cascode'),
    ('avalanche photodiode', r'avalanche[ -]+photodiodes?|apd'),
    ('photodiode', r'photo[ -]?diodes?'),
    ('vcsel', r'vertical[ -]+cavity[ -]+surface[ -]+emitting[ -]+lasers?|vcsel'),
    ('mach-zehnder modulator', r'mach[ -]+zehnder[ -]+modulators?|mzm'),
    ('burst-mode', r'burst[ -]+mode'),
    ('optical coupling', r'(?:grating|optical)[ -]+coupl(?:er|ers|ing)'),
    ('silicon photonics', r'silicon[ -]+photonic(?:s)?'),
    ('injection locking', r'injection[ -]+lock(?:ed|ing)'),
    ('bang-bang', r'bang[ -]+bang'),
    ('phase interpolator', r'phase[ -]+interpolat(?:or|ors|ion)'),
    ('phase detector', r'phase[ -]+detectors?'),
    ('time-to-digital converter', r'time[ -]+to[ -]+digital[ -]+conver(?:ter|ters|sion)|tdc'),
    ('digitally controlled oscillator', r'digitally[ -]+controlled[ -]+oscillators?|dco'),
    ('voltage controlled oscillator', r'voltage[ -]+controlled[ -]+oscillators?|vco'),
    ('ring oscillator', r'ring[ -]+oscillators?'),
    ('lc oscillator', r'lc[ -]+oscillators?'),
    ('fractional-n', r'fractional[ -]+n'),
    ('sub-sampling', r'sub[ -]?sampling'),
    ('clock forwarding', r'forwarded[ -]+clock|clock[ -]+forward(?:ed|ing)'),
    ('source synchronous', r'source[ -]+synchronous'),
    ('duty-cycle correction', r'duty[ -]+cycle[ -]+(?:correction|corrector)'),
    ('jitter tolerance', r'jitter[ -]+tolerance'),
    ('jitter cancellation', r'jitter[ -]+(?:cancellation|compensation)'),
    ('pre-emphasis', r'pre[ -]?emphasis'),
    ('de-emphasis', r'de[ -]?emphasis'),
    ('crosstalk cancellation', r'cross[ -]?talk[ -]+(?:cancellation|compensation)'),
    ('impedance matching', r'impedance[ -]+match(?:ed|ing)'),
    ('termination calibration', r'termination[ -]+calibration'),
    ('time-interleaved', r'time[ -]+interleav(?:ed|ing)'),
    ('successive approximation', r'successive[ -]+approximation|sar[ -]+adc'),
    ('delta-sigma', r'(?:delta[ -]+sigma|sigma[ -]+delta)'),
    ('analog-to-digital converter', r'analog[ -]+to[ -]+digital[ -]+conver(?:ter|ters|sion)|adc'),
    ('digital-to-analog converter', r'digital[ -]+to[ -]+analog[ -]+conver(?:ter|ters|sion)|dac'),
    ('sample-and-hold', r'(?:sample|track)[ -]+and[ -]+hold'),
    ('sense amplifier', r'sense[ -]+amplifiers?'),
    ('low-noise amplifier', r'low[ -]+noise[ -]+amplifiers?|lna'),
    ('power amplifier', r'power[ -]+amplifiers?'),
    ('low-dropout regulator', r'low[ -]+dropout[ -]+regulators?|ldo'),
    ('dc-dc converter', r'dc[ -]+dc[ -]+converters?'),
    ('switched capacitor', r'switched[ -]+capacitors?'),
    ('charge pump', r'charge[ -]+pumps?'),
    ('capacitive coupling', r'capacitiv(?:e|ely)[ -]+coupl(?:ed|ing)'),
    ('inductive coupling', r'inductiv(?:e|ely)[ -]+coupl(?:ed|ing)'),
    ('through-silicon via', r'through[ -]+silicon[ -]+vias?|tsv'),
    ('electrostatic discharge', r'electrostatic[ -]+discharge|esd'),
]
CONCEPT_RE = re.compile(r'\b(?:' + '|'.join(fr'(?P<c{i}>{pattern})' for i, (_, pattern) in enumerate(PATTERNS)) + r')\b')
DOMAINS = {
    'optical': r'\b(?:optical|photonic\w*|photodiodes?|fiber|fibre|vcsel|wdm|microring|tia|transimpedance)\b',
    'wireline': r'\b(?:wireline|serdes|backplane|interconnect\w*|pam[ -]?[2348]|nrz|die[ -]to[ -]die|d2d|serial link|memory interface)\b',
    'clock': r'\b(?:clock|pll|cdr|oscillator\w*|phase[ -]locked|jitter)\b',
    'memory': r'\b(?:memory|hbm|gddr\d*|lpddr\d*|ddr\d*)\b',
    'converter': r'\b(?:adc|dac|analog[ -]to[ -]digital|digital[ -]to[ -]analog)\b',
    'rf': r'\b(?:rf|radio[ -]frequency|millimeter[ -]wave|mm[ -]wave|radar|wireless)\b',
}
DOMAIN_PATTERNS = {key: re.compile(pattern) for key, pattern in DOMAINS.items()}


def normalized(value):
    value = unicodedata.normalize('NFKC', html.unescape(str(value or ''))).casefold()
    return re.sub(r'[\u2010-\u2015\u2212]', '-', value)


def concepts(value, abstract=False):
    """Return concept -> grounded phrase; skip explicit absence/comparison.

    Sentence-local filtering errs on the side of excluding background methods.
    It is a conservative lexical rule, not a claim of semantic understanding.
    """
    result = {}
    value = normalized(value)
    esd_context = bool(re.search(r'\b(?:esd|electrostatic|human[ -]+body|\d+(?:\.\d+)?[ -]*kv|\d{3,}[ -]*v)\b', value))
    for sentence in re.split(r'[.!?;\n]', value):
        if abstract and re.search(r'\b(?:previous|prior|conventional|unlike|compared|comparison|versus)\b', sentence):
            continue
        for match in CONCEPT_RE.finditer(sentence):
            name = PATTERNS[int(match.lastgroup[1:])][0]
            # HBM in ESD measurements means Human Body Model, not memory.
            # Do not use this ambiguous acronym as an expansion/anchor.
            if name == 'hbm' and match.group() == 'hbm' and esd_context:
                continue
            prefix = sentence[max(0, match.start()-45):match.start()]
            if re.search(r'\b(?:without|no|eliminat\w*|avoid\w*|free of)\b[^,;:.]*$', prefix):
                continue
            if re.match(r'[ -]+free\b', sentence[match.end():]):
                continue
            result[name] = match.group()
    return result


def domains(value):
    value = normalized(value)
    if re.search(r'\b(?:esd|electrostatic|human[ -]+body)\b', value):
        value = re.sub(r'\bhbm\b', '', value)
    return {name for name, pattern in DOMAIN_PATTERNS.items() if pattern.search(value)}


class AbstractTitleIndex:
    def __init__(self, rows):
        self.features = [set(concepts(row['title'])) for row in rows]
        self.domains = [domains(row['title']) for row in rows]
        self.postings = defaultdict(set)
        for i, features in enumerate(self.features):
            for name in features:
                self.postings[name].add(i)
        self.weights = {name: 1 + log((len(rows)+1)/(len(ids)+1)) for name, ids in self.postings.items()}
        self.norms = [sqrt(sum(self.weights[term]**2 for term in features)) for features in self.features]

    def supplement(self, index, favorites, abstracts, title_candidates, excluded=(), limit=MAX_ABSTRACT_POOL):
        started = time.monotonic()
        excluded_ids = set(map(str, excluded)) | {str(row['article_number']) for row in favorites}
        used_ids = excluded_ids | {str(row['article_number']) for row in title_candidates}
        used_titles = {identity_text(row['title']) for row in [*favorites, *title_candidates]}
        records, queues = {}, []
        covered = expanded = searched = 0
        from .retrieval import retrieval_terms
        term_cache = {}
        for seed in sorted(favorites, key=lambda row: str(row['article_number'])):
            abstract = abstracts.get(str(seed['article_number']), '')
            if not abstract.strip():
                continue
            covered += 1
            if excluded_electrical_multicarrier(seed):
                continue
            original = set(concepts(seed['title']))
            seed_domains = domains(seed['title'])
            grounded = concepts(abstract, abstract=True)
            extra = set(grounded) - original
            # Corpus specificity determines a bounded expansion, never prose IDF.
            extra = sorted((term for term in extra if term in self.postings),
                           key=lambda term: (-self.weights[term], term))[:8]
            if not extra:
                continue
            expanded += 1
            potential = set().union(*(self.postings[term] for term in extra))
            query = {term: 1.0 for term in original}
            query.update({term: .55 for term in extra})
            query_norm = sqrt(sum((self.weights.get(term, 1)*weight)**2 for term, weight in query.items()))
            title_terms = retrieval_terms(seed['title'], index.tokenize)
            norm = sqrt(sum(index.weights.get(term, 1+log(len(index.rows)+1))**2 for term in title_terms))
            ranked = []
            for i in sorted(potential):
                row = index.rows[i]
                if not index.allowed[i] or str(row['article_number']) in used_ids:
                    continue
                candidate = self.features[i]
                added = sorted(set(extra) & candidate)
                anchors = sorted(original & candidate)
                # The new term alone cannot establish the context. A domain
                # match must remain even after removing expanded concepts.
                context = []
                if not anchors:
                    if not seed_domains & self.domains[i]:
                        continue
                    title_without_extra = normalized(row['title'])
                    for match in reversed(list(CONCEPT_RE.finditer(title_without_extra))):
                        if PATTERNS[int(match.lastgroup[1:])][0] in added:
                            title_without_extra = title_without_extra[:match.start()] + ' ' + title_without_extra[match.end():]
                    context = sorted(seed_domains & domains(title_without_extra))
                if not anchors and not context:
                    continue
                candidate_norm = self.norms[i]
                semantic = sum(self.weights[term]**2 * query[term] for term in candidate & query.keys()) / (query_norm*candidate_norm)
                # Retain title TF-IDF in the score, with a bounded contribution
                # from grounded concepts. This is not a relevance probability.
                if i not in term_cache:
                    term_cache[i] = retrieval_terms(row['title'], index.tokenize)
                row_terms = term_cache[i]
                lexical = sum(index.weights[term]**2 for term in title_terms & row_terms) / max(1e-9, norm*float(index.norms[i]))
                score = .65 * lexical + .35 * semantic
                if score < .18:
                    continue
                if identity_text(row['title']) in used_titles or near_duplicate(row['title'], seed['title']):
                    continue
                ranked.append((score, i, added, anchors, context))
            if ranked:
                searched += 1
            queue = []
            for score, i, added, anchors, context in sorted(ranked, key=lambda r: (-r[0], r[1]))[:8]:
                queue.append(i)
                record = records.setdefault(i, {'score': -1, 'seeds': set()})
                record['seeds'].add(str(seed['article_number']))
                if score > record['score']:
                    record.update(score=score, seed=seed, added=added, anchors=anchors, context=context,
                                  evidence={term: grounded[term] for term in added})
            queues.append(queue)
        output, seen = [], set()
        for depth in range(max(map(len, queues), default=0)):
            for queue in queues:
                if len(output) >= max(0, limit):
                    break
                if depth >= len(queue) or queue[depth] in seen:
                    continue
                i = queue[depth]
                seen.add(i)
                row, record = index.rows[i], records[i]
                key = identity_text(row['title'])
                if key in used_titles:
                    continue
                used_titles.add(key)
                seed = record['seed']
                output.append({**row, 'favorite_similarity': round(record['score'], 4),
                    'related_favorite_id': str(seed['article_number']), 'related_favorite_title': seed['title'],
                    'shared_terms': record['anchors'], 'shared_concepts': record['anchors'],
                    'shared_technical_terms': sorted(set(record['anchors'] + record['added'])),
                    'abstract_expansion_terms': record['added'], 'abstract_expansion_evidence': record['evidence'],
                    'abstract_context': record['context'],
                    'retrieval_channels': ['abstract'], 'retrieval_support': len(record['seeds']),
                    'retrieval_basis': ABSTRACT_VERSION})
        return output, {'abstract_favorites': covered, 'abstract_expanded_favorites': expanded,
            'abstract_favorites_with_neighbors': searched, 'abstract_discovered_candidates': len(records),
            'abstract_added_candidates': len(output), 'abstract_search_seconds': round(time.monotonic()-started, 3)}

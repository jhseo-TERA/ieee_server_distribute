"""Dismiss only claims contradicted by an explicit, readable source field.

This is a triage guard, not a correctness certificate. The original model claim
is retained by the caller. Unquoted titles, source damage and boundary claims
always remain reviewable.
"""
import unicodedata
from paper_server.parsers.reference_roles import personal_name,publication_year,quoted_title
from paper_server.parsers.reference_segmentation import compact,damaged_characters,uncertain_scientific_text
from paper_server.parsers.reference_display import micro_unit_display
from paper_server.parsers.reference_identifiers import literal_doi
from paper_server.parsers.reference_scope import citation_scope


def field_key(value):
    return micro_unit_display(compact(unicodedata.normalize('NFC',str(value or '')))).casefold().strip(' ,;.')


def source_consistency(issue,original):
    source=original.get('independent_source','')
    if not source or damaged_characters(source):return None
    source=citation_scope(source)[0]
    field=issue['field'];candidate=original.get('candidate',{}).get(field)
    expected=None;explicit=False
    if field=='author':
        name=personal_name(source)
        expected=name.get('family');explicit=bool(expected)
    elif field=='title':
        quote=quoted_title(source)
        if quote:expected=quote[1].strip(' ,;');explicit=not uncertain_scientific_text(expected)
    elif field=='year':
        quote=quoted_title(source)
        expected,years=publication_year(source,quote[1] if quote else None)
        explicit=expected is not None and len(years)==1
    elif field=='doi':
        doi=literal_doi(source)
        if doi:
            expected=doi['doi'];explicit=not doi['incomplete'] and not doi.get('ambiguous_discretionary_hyphen')
    if explicit and candidate is not None and field_key(candidate)==field_key(expected):
        return {'disposition':'source_consistent_field','field':field,'literal_expected':expected,
                'rule':'readable_independent_field_matches_candidate','scope':'triage_only_not_verified_correctness'}
    return None

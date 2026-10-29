"""User display policy; keep extracted source fields and mathematical mu intact."""
from __future__ import annotations

import re

from paper_server.parsers.reference_normalization import match_key
from paper_server.parsers.reference_segmentation import damaged_characters

VERSION = 'reference-display-v2'
# Recognize a micro prefix attached to a unit, not a standalone Greek variable.
MICRO_UNIT = re.compile(r'(?<![A-Za-zμµ])[μµ](?=(?:mol|rad|Hz|Pa|[msWAVFHCJgLlTNKSΩ])(?![A-Za-z]))')


def micro_unit_display(value):
    return MICRO_UNIT.sub('u', value) if isinstance(value, str) else value


def apply_display_formatting(row):
    before = row.get('title_display')
    micro = micro_unit_display(before)
    display = micro
    if isinstance(display, str):
        display = re.sub(r'(?<=\d)-[ \t]+(?=\d)', '-', display)
    result = {**row, 'display_policy_version': VERSION}
    if display != before:
        result['title_display'] = display
        # A display policy must not grant a search key to an unresolved title.
        if row.get('title_match_key') is not None:
            result['title_match_key'] = match_key(display)
        steps = []
        if micro != before:
            steps.append({'rule': 'micro_unit_ascii', 'source': before, 'display': micro})
        if display != micro:
            steps.append({'rule': 'numeric_wrap_space', 'source': micro, 'display': display})
        result['normalization_steps'] = row.get('normalization_steps', []) + steps
        if row.get('normalization_status') == 'clean':
            result['normalization_status'] = 'normalized'
    # Keep uncertain glyphs visibly uncertain instead of deleting a possible
    # micro prefix/exponent. The extracted fields and raw source stay intact.
    for field in ('title_display', 'author_display', 'first_author_display'):
        value = result.get(field)
        if isinstance(value, str) and damaged_characters(value):
            invalid = set(damaged_characters(value))
            result[field] = ''.join('\ufffd' if c in invalid else c for c in value)
            result['needs_review'] = result['needs_review_before_normalization'] = True
            result['warnings'] = list(dict.fromkeys(result.get('warnings', []) + ['damaged_reference_text']))
            if field == 'title_display':
                result['title_match_key'] = None
    return result

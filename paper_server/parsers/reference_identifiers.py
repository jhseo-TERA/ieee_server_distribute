"""Recover literal DOI continuations; never infer an identifier from metadata."""
from __future__ import annotations
import re
from paper_server.parsers.reference_scope import citation_scope

VERSION='literal-doi-v1'


def literal_doi(text):
    text=citation_scope(text)[0]
    original=text
    text=re.sub(r'(?<=\w)[\ufffe\u00ad]\s*(?=\w)','',text)
    start=re.search(r'10\.\d{4,9}\s*/\s*',text,re.I)
    if not start:return None
    tail=text[start.end():]
    first=re.match(r'[^\s<>"”]+',tail)
    if not first:return None
    value=re.sub(r'\s+','',start[0])+first[0]
    end=start.end()+first.end();joins=[]
    for _ in range(5):
        following=re.match(r'\s+([^\s<>"”]+)',text[end:])
        if not following:break
        token=following[1]
        # A numeric suffix after an explicit DOI year, or a long literal
        # continuation after a hyphen, cannot absorb a later citation year.
        year_suffix=bool(re.search(r'\.(?:19|20)\d{2}\.$',value)
                         and re.fullmatch(r'\d{4,}[A-Za-z0-9._/-]*[.,;]?',token))
        dot_suffix=bool(re.search(r'\.(?:19|20)\d{2}$',value)
                        and re.fullmatch(r'\.\d{4,}[A-Za-z0-9._/-]*[.,;]?',token))
        separator=following[0][:-len(token)]
        year_start=bool(re.fullmatch(r'10\.\d{4,9}/[A-Za-z][A-Za-z0-9_/-]*\.',value,re.I)
                        and (re.search(r'[\r\n]',separator) or re.match(r'10\.(?:1109|23919)/',value))
                        and re.fullmatch(r'(?:18|19|20)\d{2}\.\d{4,}[A-Za-z0-9._/-]*[.,;]?',token))
        hyphen_suffix=bool(value.endswith('-') and
            re.fullmatch(r'[A-Za-z0-9]{3,}(?:[._/-][A-Za-z0-9]+)+[.,;]?',token))
        conference_suffix=bool(re.search(r'[A-Za-z]$',value) and
            re.fullmatch(r'\d{4,}\.(?:19|20)\d{2}\.\d{4,}[.,;]?',token))
        registry_suffix=bool(value.endswith('/') and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{3,}',token))
        if not any((year_suffix,dot_suffix,year_start,hyphen_suffix,conference_suffix,registry_suffix)):break
        joins.append({'left':value,'right':token,'literal_separator':following[0][:-len(token)]})
        value+=token;end+=following.end()
    value=value.casefold().rstrip('.,;')
    incomplete=bool(re.fullmatch(r'10\.(?:1109|23919)/[A-Za-z0-9._/-]*\.(?:19|20)\d{2}',value))
    return {'doi':value,'literal':text[start.start():end],'joins':joins,
            'incomplete':incomplete,'version':VERSION,
            'ambiguous_discretionary_hyphen':bool(re.search(r'10\.\d{4,9}\s*/[^“”"\n]*(?:[\ufffe\u00ad])',original))}

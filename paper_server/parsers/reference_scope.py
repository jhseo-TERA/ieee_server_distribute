"""Exclude host-document furniture, preserving literal source for provenance."""
import re


def citation_scope(text):
    quotes=[(m.start(),m.end()) for m in re.finditer(r'[“"]([^”"]+)[”"]',text)]
    pattern=(r'\b(?:Authorized licensed use|Downloaded (?:on|from)|Restrictions apply|focus\s*\|\s*review articles)\b'
        r'|\b[A-Za-z][A-Za-z .-]{2,45}\s*\|\s*VOL(?:UME)?\.?\s*\d|\bFig(?:ure)?\.?\s*\d+(?:\.\d+)*\s*:'
        r'|\b\d{1,5}\s+(?:IEEE\s+)?(?:JOURNAL|TRANSACTIONS)\s+(?:OF|ON)\s+[^,]{1,100},\s*VOL\.'
        r'|(?<=[.])\s+[A-Z][a-z]+(?:\s+[A-Z][A-Za-z.-]*){1,3}\s+received\s+(?:the\s+)?(?:B\.?S\.?|M\.?S\.?|Ph\.?D\.?)\s+(?:and|degree|degrees)\b'
        r'|\bResearch Article\s+Vol\.'
        r'|\bVol\.\s*\d+,\s*No\.\s*\d+\s*\|'
        r'|#\d{5,}\s*-\s*\$[\d.]+\s*USD\s+Received\b'
        r'|\bArticle\s+https?://doi\.org/\S+\s+[A-Za-z ]{3,50}\s*\|'
        r'|\bAbout the Authors\b|\bcontributors\s*\(continued from\b'
        r'|\b[A-Z][A-Z -]{3,70}\s*\((?:Member|Senior Member|Fellow),\s*IEEE\)\s+(?:is|was|received)\b'
        r'|\bIEEE\s+(?:A-?SSCC|ASSCC)\s+20\d{2}\s*/\s*Session\b'
        r'|(?<=\.)\s+(?:\d{1,5}\s+)?VOLUME\s+\d+,\s*20\d{2}\b'
        r'|©\s*(?:19|20)\d{2}\s+[^.]{1,100}\b(?:IEEE|All rights reserved)\b')
    for m in re.finditer(pattern,text,re.I):
        if not any(a<m.start()<b for a,b in quotes):
            if '©' in m[0] or re.search(r'\bVOLUME\s+\d+,',m[0],re.I):
                prefix=text[:m.start()]
                prefix=re.sub(r'[“"]([^”"]+)[”"]',' ',prefix)
                if not re.search(r'\b(?:18|19|20)\d{2}\b',prefix):continue
            return text[:m.start()].rstrip(),{'removed':text[m.start():],'rule':'host_document_furniture_after_reference'}
    return text,None

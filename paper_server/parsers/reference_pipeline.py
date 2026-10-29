"""Shared final stage for fresh batches and cached replays."""
from __future__ import annotations

from paper_server.parsers.reference_evidence import align_sources
from paper_server.parsers.reference_roles import apply_roles
from paper_server.parsers.reference_normalization import apply_title_normalization, recover_from_pdf
from paper_server.parsers.reference_authority import apply_author_resolution, load_authorities, verified_author_resolution
from paper_server.parsers.reference_source_reviews import apply_source_reviews, verified_source_review, display_name
from paper_server.parsers.reference_display import apply_display_formatting


def reconcile_rows(rows, *, bounded=None, evidence=None, source_sha256=None):
    aligned = align_sources(rows,bounded) if bounded else {}
    authorities = load_authorities()
    output = []
    prior_review={r['number']:bool(r.get('needs_review_before_normalization') or r.get('needs_review')) for r in rows}
    for row in apply_roles(rows,aligned):
        row['needs_review_before_normalization'] = bool(row.get('needs_review_before_normalization') or row.get('needs_review'))
        use_source = any(change['field']=='title' for change in row['field_changes'])
        normalized = apply_title_normalization(row,source_reference=row['source_view']['raw'] if use_source else None)
        normalized['normalization_uses_source_view'] = use_source
        if row.get('title_recovery_evidence') and source_sha256:
            normalized = recover_from_pdf(normalized,row['title_recovery_evidence'],expected_pdf_sha256=source_sha256)
        normalized = apply_author_resolution(normalized,authorities)
        normalized = apply_source_reviews(normalized,source_sha256)
        reviewed_author=verified_author_resolution(normalized,authorities)
        reviewed_source=verified_source_review(normalized,source_sha256)
        if reviewed_author:
            normalized['warnings']=[w for w in normalized.get('warnings',[]) if w!='personal_name_boundary_uncertain']
        if reviewed_author or reviewed_source:
            active={'personal_name_boundary_uncertain','personal_name_glyph_requires_review',
                'independent_title_text_disagreement','ambiguous_publication_year','possible_lost_scientific_glyph',
                'ocr_source_requires_review','damaged_reference_text','unresolved_field_grounding',
                'local_model_review_failed','unresolved_title_boundary','publication_year_unresolved',
                'compound_family_name_requires_review'}
            review=bool(prior_review[row['number']] or normalized.get('normalization_needs_review')
                        or active.intersection(normalized.get('warnings',[])))
            normalized['needs_review_before_normalization']=normalized['needs_review']=review
        normalized['author_display']=display_name(normalized.get('author'))
        if normalized.get('first_author_name'):
            normalized['first_author_display']=display_name(normalized['first_author_name'])
        normalized = apply_display_formatting(normalized)
        if evidence:
            normalized['source_evidence'] = {'pdf_sha256':source_sha256,'path':evidence['evidence_path'],
                                             'sha256':evidence['evidence_sha256']}
        output.append(normalized)
    return output

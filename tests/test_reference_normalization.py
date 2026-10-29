"""Source-preserving title cleanup and bounded PDF fallback regressions."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paper_server.parsers.reference_normalization import (
    apply_title_normalization, match_key, normalize_title, recover_from_pdf,
)
from paper_server.parsers.reference_title_recovery import recover_titles
from scripts import benchmark_reference_bulk as bulk
from scripts.reference_title_pdf_worker import quoted_titles
from scripts.revalidate_reference_titles import score

ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads((ROOT / 'config/reference_normalization_cases.json').read_text(encoding='utf-8'))['cases']


def evidence_for(case):
    evidence = quoted_titles(case['pdf_reference_text'], case['row']['number'])
    assert len(evidence) == 1
    return {**evidence[0], 'number': case['row']['number'], 'page': case['row']['page'],
            'source_sha256': case['source_sha256']}


class ReferenceNormalizationTests(unittest.TestCase):
    def test_all_reviewed_titles_preserve_original_fields(self):
        recovered = 0
        for case in CASES:
            with self.subTest(pdf=case['pdf'], number=case['row']['number']):
                original = deepcopy(case['row'])
                row = apply_title_normalization(original)
                if row['normalization_needs_review']:
                    row = recover_from_pdf(row, evidence_for(case), expected_pdf_sha256=case['source_sha256'])
                    recovered += row['normalization_status'] == 'recovered_from_pdf'
                self.assertEqual(row['title_display'], case['expected_display'])
                self.assertFalse(row['normalization_needs_review'])
                self.assertTrue(row['title_match_key'])
                self.assertEqual({k: row[k] for k in original}, original)
                self.assertEqual(original, case['row'])
        self.assertEqual(recovered, 5)

    def test_tag_inside_quoted_title_is_preserved_for_review(self):
        title = 'An investigation [J]'
        row = normalize_title(title, 'A. Lee, "An investigation [J]," IEEE Journal, 2020.')
        self.assertEqual(row['title_display'], title)
        self.assertIsNone(row['reference_type'])
        self.assertTrue(row['normalization_needs_review'])
        self.assertIsNone(row['title_match_key'])

    def test_ordinary_brackets_and_non_suffix_tags_are_preserved(self):
        for title in ('A [J] device for high speed links', 'A method [1]', 'Array [C++] design'):
            with self.subTest(title=title):
                row = normalize_title(title, title)
                self.assertEqual(row['title_display'], title)
                self.assertFalse(row['normalization_needs_review'])

    def test_math_preserves_values_units_indices_and_operators(self):
        title = r'A $1 . 3 \\mathrm{V}_{\\mathrm{p p d}}$ driver at $f_T$ with $x^{2}+y^{-1}$'
        # Use actual TeX escapes, not escaped JSON strings.
        title = title.replace('\\\\', '\\')
        row = normalize_title(title, title)
        self.assertEqual(row['title_display'], 'A 1.3 V_ppd driver at f_T with x^2+y^(-1)')
        self.assertNotEqual(match_key('A+B'), match_key('A-B'))
        self.assertNotEqual(match_key('V_pp'), match_key('Vpp'))

    def test_unsupported_or_unbalanced_math_is_never_partially_committed(self):
        for title in (r'A $f_T$ and $\\unknown{x}$ driver', 'A $V_{pp$ driver', 'A $V_pp driver',
                      'A 4 $.8V_{pp}$ driver', r'A $\\left 4-V_{pp}\\right.$ driver'):
            with self.subTest(title=title):
                title = title.replace('\\\\', '\\')
                row = normalize_title(title, title)
                self.assertIsNone(row['title_display'])
                self.assertIsNone(row['title_match_key'])
                self.assertTrue(row['normalization_needs_review'])
        deep = 'A $' + '{' * 20 + 'x' + '}' * 20 + '$ driver'
        self.assertIn('math_nesting_limit', normalize_title(deep, deep)['normalization_warnings'])

    def test_absent_title_and_ungrounded_title_are_not_filled(self):
        self.assertEqual(normalize_title(None, 'IEEE JSSC, 2020')['normalization_status'], 'missing')
        row = normalize_title('Invented driver', 'A. Lee, IEEE JSSC, 2020')
        self.assertIsNone(row['title_display'])
        self.assertIn('title_source_not_grounded', row['normalization_warnings'])

    def test_currency_is_not_silently_interpreted_as_math(self):
        title = 'Comparing $8 and $9 devices'
        row = normalize_title(title, title)
        self.assertIsNone(row['title_display'])
        self.assertIn('math_ambiguous_currency', row['normalization_warnings'])

    def test_pdf_evidence_cannot_cross_page_number_hash_or_author(self):
        case = next(c for c in CASES if c['pdf'] == 'ieee-pdf/7539209.pdf')
        row = apply_title_normalization(case['row'])
        evidence = evidence_for(case)
        for mutation in ({'number': 99}, {'page': 99}, {'source_sha256': '0' * 64},
                         {'author_text': 'A. Somebody Else'}, {'reference_text_sha256': '0' * 64},
                         {'reference_text': evidence['reference_text'].replace('2015', '2016')}):
            with self.subTest(mutation=mutation):
                checked = recover_from_pdf(row, {**evidence, **mutation}, expected_pdf_sha256=case['source_sha256'])
                self.assertTrue(checked['normalization_needs_review'])
                self.assertIsNone(checked['title_display'])
        checked = recover_from_pdf(row, evidence, expected_pdf_sha256=case['source_sha256'])
        self.assertNotIn('Gm2', checked['title_display'])
        self.assertNotIn('IEEE A-SSCC', checked['title_display'])

    def test_pdf_quote_boundaries_and_unknown_numeric_glyphs_fail_closed(self):
        text = '[1] A. Lee, "First driver," IEEE, 2020.\n[2] B. Kim, "Other driver," 2021.'
        self.assertEqual(quoted_titles(text, 1)[0]['title'], 'First driver')
        self.assertEqual(quoted_titles(text, 99), [])
        self.assertEqual(quoted_titles('[1] A. Lee, "A 4\ufffe8V driver," 2020.', 1), [])
        joined = quoted_titles('[1] A. Lee, "A BiC\ufffe\nMOS driver," 2020.', 1)
        self.assertEqual(joined[0]['title'], 'A BiCMOS driver')

    def test_preexisting_review_is_not_cleared_by_successful_recovery(self):
        case = next(c for c in CASES if c['pdf'] == 'ieee-pdf/7539209.pdf')
        row = apply_title_normalization({**case['row'], 'needs_review': True})
        checked = recover_from_pdf(row, evidence_for(case), expected_pdf_sha256=case['source_sha256'])
        self.assertEqual(checked['normalization_status'], 'recovered_from_pdf')
        self.assertTrue(checked['needs_review'])

    def test_source_change_rejects_pdf_cache_without_executing_worker(self):
        case = next(c for c in CASES if c['pdf'] == 'ieee-pdf/7539209.pdf')
        row = apply_title_normalization(case['row'])
        with tempfile.TemporaryDirectory() as directory, patch('paper_server.parsers.reference_title_recovery.subprocess.run') as run:
            root = Path(directory)
            pdf = root / 'changed.pdf'
            pdf.write_bytes(b'changed')
            rows, status = recover_titles([row], pdf, case['source_sha256'], root / 'cache')
            self.assertEqual(rows, [row])
            self.assertEqual(status['error'], 'ValueError')
            run.assert_not_called()

    def test_postprocessing_version_does_not_invalidate_model_answers(self):
        class Guard:
            def wait(self):
                pass
        entries = [{'number': 1, 'raw': 'source'}]
        signature = {'model': 'test', 'model_digest': 'fixed', 'normalization_code': 'old'}
        with tempfile.TemporaryDirectory() as directory, patch.object(bulk, 'llm_fields', return_value=(entries, {'elapsed_seconds': 1})) as model:
            bulk.llm_batch(None, Guard(), entries, Path(directory), signature)
            bulk.llm_batch(None, Guard(), entries, Path(directory), {**signature, 'normalization_code': 'new'})
            self.assertEqual(model.call_count, 1)

    def test_regression_score_uses_display_title_but_preserves_missing_row_penalty(self):
        rows = {'a.pdf': [{'number': 1, 'title': 'Driver[J]', 'title_display': 'Driver'}]}
        gold = {'papers': [{'pdf': 'a.pdf', 'expected_count': 2, 'gold': [
            {'number': 1, 'title': 'Driver'}, {'number': 2, 'doi': None}]}]}
        result = score(rows, gold)
        self.assertEqual((result['correct'], result['total']), (1, 2))


if __name__ == '__main__':
    unittest.main()

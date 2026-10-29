"""Evaluation must count missing rows and keep source review reproducible."""
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.prepare_reference_validation import fresh_sample, sample_numbers, save, sha, source_inventory
from scripts import review_reference_validation as review


class ReferenceValidationTests(unittest.TestCase):
    def test_sample_is_deterministic_stratified_and_does_not_filter_failures(self):
        papers = [{'pdf': str(i), 'sha256': str(i), 'publisher': 'IEEE' if i < 8 else 'Optica',
                   'status': 'no_references'} for i in range(10)]
        first, quota = fresh_sample(papers, {'0'}, 5, 'fixed')
        second, _ = fresh_sample(list(reversed(papers)), {'0'}, 5, 'fixed')
        self.assertEqual(first, second)
        self.assertEqual(quota, {'IEEE': 4, 'Optica': 1})
        self.assertNotIn('0', [p['pdf'] for p in first])
        self.assertEqual(len(first), 5)
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            fresh_sample([papers[1], papers[1]], set(), 2, 'fixed')

    def test_reference_sampling_keeps_four_when_available(self):
        self.assertEqual(sample_numbers(None), [])
        for count in range(100):
            chosen = sample_numbers(count)
            self.assertEqual(len(chosen), min(4, count))
            self.assertEqual(chosen, sorted(set(chosen)))
            if count:
                self.assertEqual(chosen[0], 1)
                self.assertEqual(chosen[-1], count)

    def test_navigation_can_separate_reference_pages_but_is_not_gold(self):
        inv = source_inventory([(1, 'Body [7]\nREFERENCES\n[1] A. Author, title.\n'),
                                (2, '[2] B. Author, title.\nBiography')])
        self.assertEqual(inv['detected_count'], 2)
        self.assertEqual([r['page'] for r in inv['entries']], [1, 2])

    def test_title_comparison_preserves_scientific_meaning(self):
        key = lambda s: review.field_key(s, 'title')
        self.assertEqual(key('High-speed transceiver'), key('high speed transceiver'))
        self.assertEqual(key('4 × 20 Gb/s'), key('4x20 Gb/s'))
        self.assertNotEqual(key('4 × 20 Gb/s'), key('420 Gb/s'))
        self.assertNotEqual(key('-8.2 dBm'), key('8.2 dBm'))
        self.assertNotEqual(key('50+ Gb/s'), key('50 Gb/s'))
        self.assertNotEqual(key('<10 pA/√Hz'), key('>10 pA/Hz'))
        self.assertNotEqual(key('2^11-1 PRBS'), key('211-1 PRBS'))
        self.assertEqual(key('12-Gb/s-16.8-dBm sensitivity'), key('12 Gb/s -16.8 dBm sensitivity'))
        self.assertEqual(review.field_key('García-López', 'author'), review.field_key('Garcia Lopez', 'author'))
        self.assertEqual(review.field_key('https://doi.org/10.1234/ab\ncd.', 'doi'), '10.1234/abcd')
        self.assertIsNone(review.field_key('', 'doi'))

    def fixture(self, root):
        out = root / 'audit'
        source = root / 'sample.pdf'
        source.write_bytes(b'fake fixture source, no real parser invoked')
        prediction = out / 'predictions' / 'sample.json'
        save(prediction, {'status': 'complete', 'rows': []})
        paper = {'id': 'sample', 'audit_id': 'p001', 'pdf': 'sample.pdf', 'publisher': 'IEEE',
                 'sha256': sha(source), 'prediction_sha256': sha(prediction)}
        save(out / 'manifest.json', {'papers': [paper]})
        image = out / 'sources/p001/page-001.png'
        image.parent.mkdir(parents=True)
        image.write_bytes(b'image fixture')
        save(out / 'gold/p001.json', {'audit_id': 'p001', 'pdf': 'sample.pdf', 'reviewed': True,
            'expected_count': 1, 'source_images_viewed': ['page-001.png'],
            'references': [{'number': 1, 'title': None, 'author': None, 'year': None, 'doi': None}]})
        return out, paper

    def scored(self, root, out):
        with patch.object(review, 'ROOT', root), redirect_stdout(StringIO()):
            return review.score(out)

    def test_missing_reference_fails_even_absent_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out, _ = self.fixture(root)
            review.freeze_gold(out)
            result = self.scored(root, out)
            self.assertEqual(result['fields_checked'], 4)
            self.assertEqual(result['field_errors'], 4)
            self.assertEqual(result['fields']['doi']['absent_errors'], 1)

    def test_ambiguous_omitted_author_is_excluded_not_credited_as_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out, paper = self.fixture(root)
            gold = review.read(out / 'gold/p001.json')
            del gold['references'][0]['author']
            gold['references'][0]['unscored_reason'] = 'Surname boundary ambiguous in original citation'
            save(out / 'gold/p001.json', gold)
            review.freeze_gold(out)
            result = self.scored(root, out)
            self.assertEqual(result['fields_checked'], 3)
            self.assertNotIn('author', result['fields'])
            self.assertEqual(len(result['unscored_fields']), 1)

    def test_unreviewed_or_incomplete_gold_cannot_be_scored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out, paper = self.fixture(root)
            with self.assertRaisesRegex(ValueError, 'Freeze'):
                self.scored(root, out)
            gold = review.read(out / 'gold/p001.json')
            gold['reviewed'] = False
            save(out / 'gold/p001.json', gold)
            with self.assertRaisesRegex(ValueError, 'Unreviewed'):
                review.freeze_gold(out)
            (out / 'gold/p001.json').unlink()
            with self.assertRaisesRegex(ValueError, 'incomplete'):
                self.scored(root, out)

    def test_changed_gold_evidence_prediction_or_source_is_rejected(self):
        for relative in ('gold/p001.json', 'sources/p001/page-001.png', 'predictions/sample.json', '../sample.pdf'):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                out, _ = self.fixture(root)
                review.freeze_gold(out)
                path = out / relative
                path.write_bytes(path.read_bytes() + b' ')
                with self.assertRaisesRegex(ValueError, 'changed'):
                    self.scored(root, out)

    def test_duplicate_reference_numbers_are_not_a_valid_sequence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out, paper = self.fixture(root)
            gold = review.read(out / 'gold/p001.json')
            gold['expected_count'] = 2
            gold['references'].append({**gold['references'][0], 'number': 2})
            save(out / 'gold/p001.json', gold)
            save(out / 'predictions/sample.json', {'rows': [{'number': 1}, {'number': 1}]})
            manifest = review.read(out / 'manifest.json')
            manifest['papers'][0]['prediction_sha256'] = sha(out / 'predictions/sample.json')
            save(out / 'manifest.json', manifest)
            review.freeze_gold(out)
            result = self.scored(root, out)
            self.assertTrue(result['papers'][0]['count_match'])
            self.assertFalse(result['papers'][0]['number_sequence_match'])
            self.assertEqual(result['papers_with_detected_error'], 1)


if __name__ == '__main__':
    unittest.main()

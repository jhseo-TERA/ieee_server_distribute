"""Grounding, false-positive regressions, and strictly additive retrieval."""
from copy import deepcopy
import unittest

import web.app as app
from paper_server.recommendation.abstract_expansion import concepts
from paper_server.recommendation.retrieval import TitleIndex
from tests.test_recommendation_retrieval import paper


class AbstractExpansionTests(unittest.TestCase):
    def supplement(self, favorite, candidates, abstract, base=(), excluded=(), limit=1500):
        index = TitleIndex([favorite, *candidates], app._title_terms, app._is_low_quality_title)
        return index.abstract_index.supplement(index, [favorite], {favorite['article_number']: abstract},
                                               list(base), excluded, limit)

    def test_only_complete_technical_concepts_are_extracted(self):
        self.assertEqual(concepts('Similar article presents required overall performance and noise reduction.'), {})
        self.assertEqual(set(concepts('Input–referred noise, clock and data recovery (CDR), phase interpolator.')),
                         {'input-referred noise', 'cdr', 'phase interpolator'})
        self.assertNotIn('input-referred noise', concepts('Input stage with a referred signal and noise'))
        self.assertEqual(concepts('This circuit has a similar presentation and required bandwidth.', abstract=True), {})

    def test_generic_similar_lsb_gamma_regression(self):
        seed = paper(1, 'A 25-Gb/s Single-Ended PAM-4 Receiver With Time-Windowed LSB Decoder for High-Speed Memory Interfaces')
        bad = paper(4145386, 'Implementation of the Gamma Line System Similar to Non-linear Gamma Curve with 2bit Error(LSB)')
        rows, _ = self.supplement(seed, [bad], 'This article presents pulse-amplitude modulation. The MSB reduces proportional error with similar required LSB timing.')
        self.assertEqual(rows, [])

    def test_positive_tia_noise_regression_with_grounded_phrase(self):
        seed = paper(1, 'High-Performance CMOS TIA for Data Center Optical Interconnects')
        good = paper(11058975, 'A 2 pA Input-Referred Noise TIA in 180-nm CMOS With 2.5 GHz Bandwidth for Optical Receiver')
        rows, _ = self.supplement(seed, [good], 'The input-referred noise of the TIA is measured in the optical receiver.')
        self.assertEqual([row['article_number'] for row in rows], ['11058975'])
        self.assertEqual(rows[0]['abstract_expansion_terms'], ['input-referred noise'])
        self.assertEqual(rows[0]['abstract_expansion_evidence']['input-referred noise'], 'input-referred noise')
        self.assertIn('tia', rows[0]['shared_concepts'])

    def test_same_new_technique_in_unrelated_domain_is_rejected(self):
        seed = paper(1, 'High-Performance CMOS TIA for Optical Interconnects')
        bad = paper(2, 'Input-Referred Noise in Biomedical Electrocardiogram Amplifiers')
        self.assertEqual(self.supplement(seed, [bad], 'The circuit reduces input-referred noise.')[0], [])

    def test_new_concept_cannot_also_supply_its_own_domain_anchor(self):
        seed = paper(1, 'Optical Link for Data Centers')
        bad = paper(2, 'Photodiode Design and Optimization')
        self.assertEqual(self.supplement(seed, [bad], 'A photodiode is integrated in this optical link.')[0], [])

    def test_negated_and_prior_work_terms_do_not_expand(self):
        for sentence in ('This TIA works without inductive peaking.',
                         'Unlike prior work, inductive peaking was studied.',
                         'Conventional designs use inductive peaking.',
                         'The design avoids the use of inductive peaking.'):
            self.assertNotIn('inductive peaking', concepts(sentence, abstract=True))
        self.assertIn('inductive peaking', concepts('We use inductive peaking to extend the bandwidth.', abstract=True))

    def test_hbm_esd_is_not_high_bandwidth_memory(self):
        self.assertNotIn('hbm', concepts('Protection withstands 4-kV HBM ESD stress.'))
        self.assertNotIn('hbm', concepts('HBM (human body model) protection.'))
        self.assertIn('hbm', concepts('An HBM memory interface.'))
        self.assertIn('hbm', concepts('A 1.2V HBM memory interface.'))
        seed = paper(1, 'A Co-Designed ESD Protection and Bypass Circuit for Wideband High-Speed TIA')
        bad = paper(2, 'A 16-kV HBM RF ESD Protection Codesign for a CMOS Direct Conversion Receiver')
        self.assertEqual(self.supplement(seed, [bad], 'The TIA withstands 4-kV HBM ESD stress.')[0], [])

    def test_title_results_scores_and_evidence_are_unchanged(self):
        seed = paper(1, 'Optical TIA Receiver')
        rows = [seed, paper(2, 'Optical TIA Receiver with Low Power'),
                paper(3, 'Optical TIA Receiver with Inductive Peaking')]
        index = TitleIndex(rows, app._title_terms, app._is_low_quality_title)
        base, _ = index.retrieve([seed], limit=1)
        before = deepcopy(base)
        extra, _ = index.abstract_index.supplement(index, [seed], {'1': 'This optical TIA uses inductive peaking.'}, base)
        self.assertEqual(base, before)
        self.assertFalse({r['article_number'] for r in extra} & {r['article_number'] for r in base})
        self.assertEqual((base+extra)[:len(base)], before)

    def test_favorites_feedback_duplicates_and_limits_stay_excluded(self):
        seed = paper(1, 'Optical TIA Receiver')
        candidates = [paper(2, 'Optical TIA with Inductive Peaking'),
                      paper(3, 'Optical TIA with Inductive Peaking', 'OFC'),
                      paper(4, 'Broadband Optical TIA with Inductive Peaking')]
        rows, _ = self.supplement(seed, candidates, 'This TIA uses inductive peaking.', excluded=['4'])
        self.assertEqual(len(rows), 1)
        self.assertNotEqual(rows[0]['article_number'], '4')
        self.assertEqual(self.supplement(seed, candidates, 'This TIA uses inductive peaking.', limit=0)[0], [])

    def test_empty_or_other_accounts_abstracts_do_not_expand(self):
        seed = paper(1, 'Optical TIA Receiver')
        index = TitleIndex([seed, paper(2, 'Optical TIA with Inductive Peaking')], app._title_terms)
        for abstracts in ({}, {'99': 'This TIA uses inductive peaking.'}):
            rows, stats = index.abstract_index.supplement(index, [seed], abstracts, [])
            self.assertEqual(rows, [])
            self.assertEqual(stats['abstract_favorites'], 0)


if __name__ == '__main__':
    unittest.main()

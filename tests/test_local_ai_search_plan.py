import unittest
from local_ai_service import grounded_search_plan


class SearchPlanTests(unittest.TestCase):
    def test_invented_numeric_filter_cannot_remove_real_paper(self):
        plan = grounded_search_plan({'search_query': 'time-windowed LSB decoder PAM4 receiver energy efficiency',
                                     'filters': {'process_nm_max': 28}}, 'PAM4 time-windowed LSB 수신기')
        self.assertEqual(plan['filters'], {})
        self.assertEqual(plan['search_query'], 'time-windowed LSB decoder PAM-4')

    def test_literal_requested_numbers_survive(self):
        plan = grounded_search_plan({'search_query': 'receiver', 'filters': {'process_nm_max': 28,
            'max_energy_pj_bit': 5, 'venue': 'JSSC'}}, '28nm 이하, 5 pJ/bit 이하 JSSC receiver')
        self.assertEqual(plan['filters'], {'process_nm_max': 28, 'max_energy_pj_bit': 5, 'venue': 'JSSC'})

    def test_unrequested_medium_and_venue_removed(self):
        plan = grounded_search_plan({'search_query': 'receiver', 'filters': {'medium': 'optical', 'venue': 'ISSCC'}}, '수신기 비교')
        self.assertEqual(plan['filters'], {})

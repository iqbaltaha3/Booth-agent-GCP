"""Station suggestions never silently choose or broaden the user's geography."""

import unittest
from types import SimpleNamespace

from agents.name_matching import resolve_booth, AmbiguousInput


class StationMatchingTests(unittest.TestCase):
    def setUp(self):
        self.data = SimpleNamespace(load_boothlist=lambda slug: [
            {'part_no': '125', 'pooling_station': 'XYZ Primary School Room 1'},
            {'part_no': '126', 'pooling_station': 'XYZ Primary School Room 2'},
            {'part_no': '127', 'pooling_station': 'Other College'},
        ] if slug == 'seat' else [])

    def suggestions(self, text, slug='seat'):
        with self.assertRaises(AmbiguousInput) as caught:
            resolve_booth(text, slug, self.data)
        return str(caught.exception)

    def test_partial_name_offers_rooms_and_parts_without_selecting_one(self):
        message = self.suggestions('XYZ Primary School')
        self.assertIn('XYZ Primary School Room 1 — Part_125', message)
        self.assertIn('XYZ Primary School Room 2 — Part_126', message)
        self.assertNotIn('Other College', message)

    def test_typo_suggests_but_requires_confirmation(self):
        self.assertIn('Part_125', self.suggestions('XYZ Primry Schol Room 1'))
        self.assertEqual(resolve_booth('Part 125', 'seat', self.data)['part_number'], 'Part_125')
        self.assertEqual(resolve_booth(' xyz primary school room 1 ', 'seat', self.data)['part_number'], 'Part_125')

    def test_unrelated_or_unknown_part_does_not_offer_arbitrary_stations(self):
        for value in ('zzzzzzzz', 'Part 999'):
            self.assertNotIn('Part_125', self.suggestions(value))
        self.assertNotIn('XYZ Primary School', self.suggestions('XYZ', 'another-seat'))

    def test_duplicate_exact_name_still_requires_part_choice(self):
        self.data.load_boothlist = lambda slug: [
            {'part_no': str(i), 'pooling_station': 'XYZ School'} for i in range(1, 9)]
        message = self.suggestions('XYZ School')
        self.assertIn('Part_1', message)
        self.assertIn('Part_5', message)
        self.assertNotIn('Part_6', message)

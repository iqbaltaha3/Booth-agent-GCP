"""Coverage guards must distinguish unavailable data from zero-match evidence."""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from app.services import data


class CoverageTests(unittest.TestCase):
    def test_empty_roll_is_unavailable_while_history_still_opens(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seat = root / 'seat'
            seat.mkdir()
            (seat / 'coverage.json').write_text(json.dumps({'voter_available': False}))
            for filename in ('voters.db', 'history.db'):
                with closing(sqlite3.connect(seat / filename)) as conn, conn:
                    conn.execute('CREATE TABLE voters (name TEXT)')
            with patch.object(data, '_local_root', return_value=root):
                with self.assertRaisesRegex(FileNotFoundError, 'not been added'):
                    data.open_sqlite('seat', 'voters.db')
                with closing(data.open_sqlite('seat', 'history.db')) as conn:
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM voters').fetchone()[0], 0)
                self.assertEqual(data.list_portfolio_files('seat'), [])

    def test_legacy_seat_without_coverage_keeps_working(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'seat').mkdir()
            with patch.object(data, '_local_root', return_value=root):
                self.assertEqual(data.load_coverage('seat'), {})


if __name__ == '__main__':
    unittest.main()

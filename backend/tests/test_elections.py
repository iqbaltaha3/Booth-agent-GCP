"""Regression coverage for observed election-year and conversation-bypass errors."""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from agents import local_runner as runner
from agents.elections import election_catalog, electoral_request, resolve_electoral_objective, catalog_evidence, validate_electoral_finish, scope_lok_answer
from agents.contracts import CoordinatorAction
from agents.contracts import SpecialistResult
from app.config import get_settings
from app.services import data


class ElectionTests(unittest.TestCase):
    def test_lok_results_keep_segment_scope_when_synthesis_omits_it(self):
        result = {'task_id': 'h', 'sources': [{'sql': 'SELECT candidate, SUM(votes) FROM form20_2024_lok GROUP BY candidate'}]}
        answer = scope_lok_answer('Candidate Alpha won with 100 votes.', [result], 'Test Seat')
        self.assertIn('Test Seat assembly segment', answer)
        self.assertIn('led with 100 votes', answer)
        result['sources'][0]['sql'] = 'SELECT candidate FROM form20_2022_vidhan'
        self.assertEqual(scope_lok_answer('Candidate Alpha won.', [result], 'Test Seat'), 'Candidate Alpha won.')

    def test_correction_keeps_result_subject_after_coverage_question(self):
        history = [{'role': 'user', 'content': 'Who won this seat in the past?'},
                   {'role': 'user', 'content': 'How many years of election results do you have?'}]
        objective = resolve_electoral_objective('i am talking about 2024 loksabha', history)
        self.assertIn('Who won this seat', objective)
        self.assertIn('2024', objective)
        self.assertIn('selected assembly segment', objective)

    def test_coverage_evidence_cannot_establish_a_winner(self):
        result = catalog_evidence([{'table': 'form20_2024_lok', 'year': 2024,
            'election_type': 'lok', 'row_count': 2, 'scope': 'selected_assembly_segment'}])
        action = CoordinatorAction(action='finish', answer='2024 data is available.', evidence_task_ids=['_history_catalog'])
        validate_electoral_finish(action, True, {'_history_catalog': result}, 'Which years of results are available?')
        with self.assertRaises(ValueError):
            validate_electoral_finish(action, True, {'_history_catalog': result}, 'Who won in 2024?')

    def test_catalog_distinguishes_empty_table_and_segment_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            seat = Path(directory) / 'seat'
            seat.mkdir()
            with closing(sqlite3.connect(seat / 'history.db')) as conn, conn:
                conn.execute('CREATE TABLE form20_2017_vidhan(votes INTEGER)')
                conn.execute('CREATE TABLE form20_2024_lok(votes INTEGER)')
                conn.execute('INSERT INTO form20_2024_lok VALUES(100)')
            with patch.object(get_settings(), 'local_data_root', directory):
                catalog = election_catalog('seat', data)
            self.assertFalse(catalog[0]['has_records'])
            self.assertTrue(catalog[1]['has_records'])
            self.assertEqual(catalog[1]['scope'], 'selected_assembly_segment')

    def test_bare_year_inherits_election_subject_but_not_arbitrary_conversation(self):
        self.assertTrue(electoral_request('in 2024', [{'role': 'user', 'content': 'Who won this seat?'}]))
        self.assertTrue(electoral_request('i am talking about 2024 loksabha'))
        self.assertFalse(electoral_request('in 2024', [{'role': 'user', 'content': 'Population changes'}]))
        self.assertFalse(electoral_request('Hello'))

    def test_election_cannot_bypass_evidence_as_conversation(self):
        replies = [
            {'action': 'finish', 'answer_kind': 'conversation', 'answer': 'The 2024 election has not taken place.'},
            {'action': 'delegate', 'tasks': [{'task_id': 'h', 'agent': 'history',
             'objective': 'Read 2024 Lok Sabha segment results', 'required_outputs': ['leader'],
             'acceptance_criteria': ['Use 2024 Lok Sabha records']}]},
            {'action': 'finish', 'answer': 'Candidate A led this assembly segment in 2024.', 'evidence_task_ids': ['h']},
        ]
        observed = SpecialistResult(task_id='h', agent='history', status='complete',
            facts=[{'columns': ['candidate', 'votes'], 'rows': [['Candidate A', 100]]}])
        with patch.object(runner, 'gemini_chat', side_effect=[json.dumps(r) for r in replies]) as model, \
             patch('agents.runtime.run_specialist', return_value=observed), \
             patch('agents.runtime.election_catalog', return_value=[{'table': 'form20_2024_lok', 'year': 2024, 'election_type': 'lok', 'row_count': 10, 'scope': 'selected_assembly_segment'}]):
            result = runner.run_local_agent('in 2024', 'allahabad-north',
                history=[{'role': 'user', 'content': 'Who won this seat in the past?'}])
        self.assertIn('Candidate A', result['answer'])
        self.assertNotIn('not taken place', result['answer'])
        self.assertIn('not conversation', model.call_args_list[1].args[0][1]['content'])


if __name__ == '__main__':
    unittest.main()

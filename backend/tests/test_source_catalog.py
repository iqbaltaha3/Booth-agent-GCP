"""Regression cases for source-specific grounding, scope and provider failures."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from unittest.mock import patch

from agents import local_runner as runner
from agents.contracts import CoordinatorAction, VoterRequest
from agents.source_catalog import voter_catalog, validate_demographic_intent
from agents.voter import resolve_category
from agents.provider_errors import ProviderReply, retry_after_seconds
from agents.count_answers import render_count
from agents.runtime import run_specialist
from app.config import get_settings
from app.services import data
from backend.tests.test_voter_harness import task
from backend.tests.sql_model_fixture import specialist_reply, with_specialist


class SourceCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for slug, labels in (("alpha", ["यादव", "यादव", "निषाद", "कुर्मी", "राजपूत", "Undetermined"]),
                             ("beta", ["Yadav", "Nishad", "Nishad", "Kurmi", "Rajput", "Undetermined"])):
            seat = self.root / slug
            seat.mkdir()
            with closing(sqlite3.connect(seat / "voters.db")) as c, c:
                c.execute("CREATE TABLE voters(caste TEXT,social_category TEXT,religion TEXT,gender TEXT,age INTEGER,polling_station TEXT)")
                c.executemany("INSERT INTO voters VALUES(?,?,?,?,?,?)", [(label, "OBC" if i < 4 else "General" if i == 4 else "Undetermined", "Muslim" if i == 0 else "Hindu", "Female" if i % 2 else "Male", 20 + i, "School") for i, label in enumerate(labels)])
            (seat / 'boothlist.csv').write_text('part_no,pooling_station,region,total_voters\n1,School,East,6\n')
        (self.root / 'empty').mkdir()
        (self.root / 'empty/coverage.json').write_text('{"voter_available":false,"status":"history_only"}')
        self.settings = patch.object(get_settings(), 'local_data_root', str(self.root))
        self.settings.start()

    def tearDown(self):
        self.settings.stop()
        self.temp.cleanup()

    def execute(self, slug, request, question=''):
        with patch.object(runner, 'gemini_chat', side_effect=lambda messages, **kwargs: specialist_reply(messages)):
            return run_specialist(task(request), slug, {}, runner, {}, question)

    def test_catalog_is_per_seat_and_source_version(self):
        alpha, beta = voter_catalog('alpha', data), voter_catalog('beta', data)
        self.assertIn('यादव', alpha['labels']['caste']['values'])
        self.assertNotIn('यादव', beta['labels']['caste']['values'])
        self.assertEqual(alpha['constituency_id'], 'alpha')
        self.assertNotEqual(alpha['data_version'], beta['data_version'])
        with closing(sqlite3.connect(self.root / 'alpha/voters.db')) as c, c:
            c.execute("UPDATE voters SET caste='New stored label' WHERE caste='कुर्मी'")
        updated = voter_catalog('alpha', data)
        self.assertNotEqual(alpha['data_version'], updated['data_version'])
        self.assertIn('New stored label', updated['labels']['caste']['values'])
        self.assertNotIn('New stored label', voter_catalog('beta', data)['labels']['caste']['values'])

    def test_missing_source_and_missing_columns_are_explicit(self):
        self.assertFalse(voter_catalog('empty', data)['available'])
        with closing(sqlite3.connect(self.root / 'beta/voters.db')) as c, c:
            c.execute('ALTER TABLE voters DROP COLUMN caste')
        catalog = voter_catalog('beta', data)
        self.assertNotIn('caste', catalog['labels'])
        self.assertNotIn('caste', [col['name'] for col in catalog['columns']])

    def test_large_catalog_marks_inventory_incomplete_but_exact_resolution_works(self):
        with closing(sqlite3.connect(self.root / 'alpha/voters.db')) as c, c:
            c.executemany('INSERT INTO voters(caste) VALUES(?)', [(f'Label {i}',) for i in range(205)])
            self.assertEqual(resolve_category(c, 'caste', 'Label 204', runner), ['Label 204'])
        self.assertFalse(voter_catalog('alpha', data)['labels']['caste']['complete'])

    def test_translations_execute_different_correct_counts_in_multiple_seats(self):
        for slug, expected in [('alpha', [2, 4, 1]), ('beta', [1, 4, 2])]:
            for (field, value), total in zip([('caste', 'Yadav'), ('social_category', 'OBC'), ('caste', 'Nishad')], expected):
                result = self.execute(slug, {'operation':'count','filters':[{'field':field,'value':value}]}, f'How many {value} voters?')
                self.assertEqual(result.status, 'complete', result.errors)
                self.assertEqual(result.facts[0]['total'], total)
                scope = result.facts[0]['count_scope']
                self.assertEqual(scope['constituency_id'], slug)
                self.assertEqual(scope['filters'][0]['field'], field)

    def test_wrong_field_and_broad_substitution_are_rejected(self):
        with closing(data.open_sqlite('alpha', 'voters.db')) as c:
            with patch.object(runner, 'gemini_chat', side_effect=AssertionError('Must not guess a broader group')):
                with self.assertRaisesRegex(ValueError, 'Intent mismatch'):
                    resolve_category(c, 'social_category', 'Yadav', runner)
        request = VoterRequest(operation='count', filters=[{'field':'social_category','value':'OBC'}])
        with self.assertRaisesRegex(ValueError, 'Intent mismatch'):
            validate_demographic_intent(request, 'How many Yadav caste voters?', voter_catalog('alpha', data))

    def test_coordinator_cannot_silently_correct_unknown_term(self):
        request = VoterRequest(operation='count', filters=[{'field':'caste','value':'Nishad'}])
        with self.assertRaisesRegex(ValueError, 'Unconfirmed demographic label'):
            validate_demographic_intent(request, 'How many ishad?', voter_catalog('alpha', data))
        validate_demographic_intent(request, 'How many nishad?', voter_catalog('alpha', data))

    def test_unknown_cannot_become_undetermined(self):
        with closing(data.open_sqlite('alpha', 'voters.db')) as c:
            with patch.object(runner, 'gemini_chat', return_value='{"values":["Undetermined"],"ambiguous":false}'):
                from agents.name_matching import AmbiguousInput
                with self.assertRaises(AmbiguousInput):
                    resolve_category(c, 'caste', 'ishad', runner)
            self.assertEqual(resolve_category(c, 'caste', 'Undetermined', runner), ['Undetermined'])

    def test_scope_bound_rendering_cannot_relabel_obc_as_nishad(self):
        result = self.execute('alpha', {'operation':'count','filters':[{'field':'social_category','value':'OBC'}]})
        answer = render_count([result.model_dump()])
        self.assertIn('4 matching', answer)
        self.assertIn('social category = OBC', answer)
        self.assertNotIn('Nishad', answer)

    def test_repair_wrong_assignment_then_render_executed_scope(self):
        wrong = task({'operation':'count','filters':[{'field':'social_category','value':'OBC'}]})
        right = task({'operation':'count','filters':[{'field':'caste','value':'Yadav'}]})
        replies = [{'action':'delegate','tasks':[wrong.model_dump()]},
                   {'action':'delegate','tasks':[right.model_dump()]},
                   {'action':'finish','answer':'There are 2 Nishad voters.','evidence_task_ids':['v']}]
        seen = []
        reply = with_specialist([json.dumps(r) for r in replies])
        def model(messages, **kwargs):
            if kwargs.get('response_schema', {}).get('title') == 'CoordinatorAction':
                context, _ = json.JSONDecoder().raw_decode(messages[1]['content'])
                seen.append(context['source_catalog'])
            return reply(messages, **kwargs)
        with patch.object(runner, 'gemini_chat', side_effect=model):
            result = runner.run_local_agent('How many Yadav caste voters?', 'alpha')
        self.assertEqual(result['status'], 'complete')
        self.assertIn('caste = यादव', result['answer'])
        self.assertNotIn('Nishad', result['answer'])
        self.assertIn('social_category', seen[0]['labels'])
        self.assertTrue(result['request_id'])

    def test_correction_cannot_bypass_evidence_as_conversation(self):
        with patch.object(runner, 'gemini_chat', return_value=json.dumps({'action':'finish','answer_kind':'conversation','answer':'All OBC records are Yadav.'})):
            result = runner.run_local_agent('OBC is a category while Yadav is caste', 'alpha', history=[{'role':'user','content':'How many Yadav?'}])
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('All OBC records', result['answer'])

    def test_four_tasks_are_valid_but_total_budget_remains_bounded(self):
        action = CoordinatorAction.model_validate({'action':'delegate','tasks':[task({'operation':'count'}, str(i)).model_dump() for i in range(4)]})
        self.assertEqual(len(action.tasks), 4)
        with self.assertRaises(ValueError):
            CoordinatorAction.model_validate({'action':'delegate','tasks':[task({'operation':'count'}, str(i)).model_dump() for i in range(9)]})

    def test_mixed_union_keeps_distinct_dimensions_and_deduplicates(self):
        request = {'operation':'count','any_of':[{'field':'caste','value':'Yadav'}, {'field':'caste','value':'Rajput'}, {'field':'religion','value':'Muslim'}]}
        result = self.execute('alpha', request, 'How many Rajput + Yadav + Muslim voters?')
        self.assertEqual(result.status, 'complete', result.errors)
        self.assertEqual(result.facts[0]['total'], 3)
        self.assertEqual(len(result.facts[0]['count_scope']['any_of']), 3)


class ProviderResilienceTests(unittest.TestCase):
    def test_429_retries_with_backoff_then_succeeds(self):
        with patch.object(runner, 'GEMINI_API_KEY', ''), patch.object(runner, 'MAX_ATTEMPTS', 3), \
             patch.object(runner, 'vertex_deadline_chat', side_effect=[ProviderReply('<<LLM_ERROR: Vertex HTTP 429>>',429,3), '<<LLM_ERROR: Vertex HTTP 429>>', '{}']) as model, \
             patch.object(runner.time, 'sleep') as sleep, patch.object(runner.random, 'uniform', return_value=0):
            self.assertEqual(runner.gemini_chat([]), '{}')
            self.assertEqual(model.call_count, 3)
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [3, 2])

    def test_exhausted_capacity_reports_real_cause_without_query(self):
        with patch.object(runner, 'load_constituency_meta', return_value={}), \
             patch('agents.runtime.voter_catalog', return_value={'available':False}), \
             patch.object(runner, 'gemini_chat', return_value='<<LLM_ERROR: Vertex HTTP 429>>'), \
             patch('agents.runtime.run_specialist') as specialist:
            result = runner.run_local_agent('How many Yadav caste voters?', 'alpha')
        specialist.assert_not_called()
        self.assertEqual(result['error_code'], 'provider_capacity')
        self.assertIn('temporarily busy', result['answer'])

    def test_nonretryable_error_is_not_retried(self):
        with patch.object(runner, 'GEMINI_API_KEY', ''), patch.object(runner, 'vertex_deadline_chat', return_value='<<LLM_ERROR: Vertex HTTP 400>>') as model:
            runner.gemini_chat([])
        self.assertEqual(model.call_count, 1)
        self.assertEqual(retry_after_seconds('12'), 12)

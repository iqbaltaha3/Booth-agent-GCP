"""Meaning/operation checks using fictional roll records, never production PII."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from unittest.mock import patch

from agents import local_runner as runner
from agents.contracts import SpecialistTask, CoordinatorAction, ResearchAction
from agents.runtime import run_specialist
from backend.tests.sql_model_fixture import specialist_reply, with_specialist
from agents.research import SearchPlan, Ranking, Findings
from app.config import get_settings
from app.services import data
from app.services.sessions import MemorySessions


def task(request, identifier='v'):
    return SpecialistTask(task_id=identifier, agent='voter', objective='Perform the requested voter operation',
        required_outputs=['Requested result'], acceptance_criteria=['Preserve source and filters'], voter_request=request)


class VoterHarnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        seat = self.root / 'seat'
        seat.mkdir()
        self.path = seat / 'voters.db'
        with closing(sqlite3.connect(self.path)) as c, c:
            c.execute('CREATE TABLE voters(name TEXT,relative_name TEXT,house_no TEXT,age INTEGER,gender TEXT,religion TEXT,caste TEXT,epic_id TEXT,polling_station TEXT,region TEXT,relation_type TEXT)')
            c.executemany('INSERT INTO voters VALUES(?,?,?,?,?,?,?,?,?,?,?)', [
                ('श्याम','राम','1',65,'Female','Muslim','Test A','TEST1','School','East','Husband'),
                ('श्याम','राम','1',40,'Male','Hindu','Test B','TEST2','School','East','Father'),
                ('लीला','राम','2',70,'Male','Muslim','Test A','TEST3','School','East','Father'),
                ('राधा','राम','3',25,'Female','Hindu','Test B','TEST4','College','West','Father'),
                ('आरती','राम','4',35,'Female','Muslim','Test A','TEST5','College','West','Father')])
        (seat / 'boothlist.csv').write_text('part_no,pooling_station,region,total_voters\n1,School,East,3\n2,College,West,2\n')
        self.patch = patch.object(get_settings(), 'local_data_root', str(self.root))
        self.patch.start()
        self.context = {}

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def execute(self, request):
        previous_chat = runner.gemini_chat
        def chat(messages, **kwargs):
            if kwargs.get('response_schema', {}).get('title') == 'ResearchAction':
                return specialist_reply(messages)
            return previous_chat(messages, **kwargs)
        with patch.object(runner, 'gemini_chat', side_effect=chat):
            return run_specialist(task(request), 'seat', {}, runner, self.context)

    def test_station_suggestions_then_confirmed_scoped_name_lookup(self):
        request = {'operation':'lookup', 'person':'श्याम', 'booth':'Schol'}
        result = self.execute(request)
        self.assertEqual(result.status, 'needs_clarification')
        self.assertEqual(result.facts, [])
        self.assertIn('School — Part_1', result.missing_inputs[0])
        result = self.execute({**request, 'booth':'Part 1'})
        self.assertEqual(result.status, 'complete')
        self.assertEqual(result.facts[0]['total'], 2)
        self.assertEqual({r['polling_station'] for r in next(f['records'] for f in result.facts if 'records' in f)}, {'School'})

    def test_direct_station_filter_also_offers_suggestions_without_zero(self):
        result = self.execute({'operation':'count', 'filters':[{'field':'polling_station','value':'Schol'}]})
        self.assertEqual(result.status, 'needs_clarification')
        self.assertEqual(result.facts, [])
        self.assertIn('School', result.missing_inputs[0])

    def test_unconfirmed_station_precedes_name_transliteration(self):
        with patch.object(runner, 'gemini_chat', side_effect=AssertionError('No name-model call before station choice')):
            result = self.execute({'operation':'lookup', 'person':'Taha Iqbal', 'booth':'Schol'})
        self.assertEqual(result.status, 'needs_clarification')
        self.assertIn('School — Part_1', result.missing_inputs[0])

    def test_total_religion_gender_and_reset_preserve_the_population(self):
        for request, expected in [
            ({'operation':'count'},5),
            ({'operation':'count','population_mode':'refine','filters':[{'field':'religion','value':'Muslim'}]},3),
            ({'operation':'count','population_mode':'refine','filters':[{'field':'gender','value':'Female'}]},2),
            ({'operation':'count','population_mode':'new'},5)]:
            result=self.execute(request)
            self.assertEqual(result.status,'complete')
            self.assertEqual(result.facts[0]['total'],expected)
        self.assertEqual(self.context['population']['filters'],[])

    def test_full_coordinator_keeps_its_answer_from_sql_evidence(self):
        assignment=task({'operation':'count','filters':[{'field':'religion','value':'Muslim'}]})
        replies=[{'action':'delegate','tasks':[assignment.model_dump()]},
                 {'action':'finish','answer':'There are 3 Muslim voters.','evidence_task_ids':['v']}]
        with patch.object(runner,'gemini_chat',side_effect=with_specialist([json.dumps(r) for r in replies])):
            result=runner.run_local_agent('How many Muslim voters?','seat')
        self.assertEqual(result['status'],'complete')
        self.assertEqual(result['answer'],'There are 3 Muslim voters.')
        self.assertEqual(result['voter_context']['population']['filters'][0]['value'],'Muslim')

    def test_provider_failure_does_not_create_population(self):
        with patch.object(runner,'gemini_chat',return_value='<<LLM_ERROR: provider unavailable>>'):
            result=self.execute({'operation':'count','filters':[{'field':'religion','value':'Translated category'}]})
        self.assertEqual(result.status,'failed')
        self.assertNotIn('population',self.context)

    def test_category_alternatives_use_union_not_contradictory_equalities(self):
        result=self.execute({'operation':'count','filters':[{'field':'religion','operator':'in','values':['Muslim','Hindu']},{'field':'gender','value':'Female'}]})
        self.assertEqual(result.facts[0]['total'],3)
        result=self.execute({'operation':'count','population_mode':'refine','filters':[{'field':'age','operator':'gt','value':'60'}]})
        self.assertEqual(result.facts[0]['total'],1)

    def test_replacing_religion_retains_gender(self):
        self.execute({'operation':'count','filters':[{'field':'religion','value':'Muslim'},{'field':'gender','value':'Female'}]})
        result=self.execute({'operation':'count','population_mode':'refine','filters':[{'field':'religion','value':'Hindu'}]})
        self.assertEqual(result.facts[0]['total'],1)
        self.assertEqual(len(self.context['population']['filters']),2)

    def test_unresolved_category_is_passed_to_agent_for_inspection(self):
        replies = [
            '{"values":[],"ambiguous":false}',
            json.dumps({'action':'sql','sql':'SELECT DISTINCT religion FROM voters ORDER BY religion'}),
            json.dumps({'action':'finish','status':'partial','gaps':['Requested category was not found in the observed labels.']}),
        ]
        with patch.object(runner,'gemini_chat',side_effect=replies):
            result=run_specialist(task({'operation':'aggregate','filters':[{'field':'religion','value':'Unknown category'}]}), 'seat', {}, runner, self.context)
        self.assertEqual(result.status,'partial')
        self.assertEqual(result.facts[0]['rows'], [['Hindu'], ['Muslim']])
        self.assertNotIn('population',self.context)

    def test_valid_zero_has_executed_resolved_filters(self):
        result=self.execute({'operation':'count','filters':[{'field':'religion','value':'Muslim'},{'field':'age','operator':'gt','value':'100'}]})
        self.assertEqual(result.status,'complete')
        self.assertEqual(result.facts[0]['total'],0)
        self.assertIn("'Muslim'", result.sources[0]['sql'])
        self.assertIn('100.0', result.sources[0]['sql'])

    def test_category_translation_is_constrained_to_stored_labels(self):
        with patch.object(runner,'gemini_chat',return_value='{"values":["Muslim"],"ambiguous":false}'):
            result=self.execute({'operation':'count','filters':[{'field':'religion','value':'मुस्लिम'}]})
        self.assertEqual(result.facts[0]['total'],3)

    def test_demographic_count_does_not_invoke_name_model(self):
        with patch.object(runner,'gemini_chat') as model:
            result=self.execute({'operation':'count','filters':[{'field':'religion','value':'Muslim'}]})
        self.assertEqual(result.facts[0]['total'],3)
        model.assert_not_called()

    def test_name_plus_demographic_and_numeric_filters(self):
        result=self.execute({'operation':'count','person':'श्याम','filters':[{'field':'gender','value':'Female'},{'field':'age','operator':'gt','value':'60'}]})
        self.assertEqual(result.status,'complete')
        self.assertEqual(result.facts[0]['total'],1)

    def test_house_number_lookup_uses_voter_records_not_reports(self):
        result=self.execute({'operation':'lookup','filters':[{'field':'epic_id','value':'TEST1'}]})
        self.assertEqual(result.status,'complete')
        self.assertEqual(next(f['records'] for f in result.facts if 'records' in f)[0]['house_no'],'1')
        self.assertEqual(result.facts[0]['total'],1)

    def test_verified_reference_lookup_does_not_expand_to_all_records(self):
        from agents.households import data_version
        version=data_version('seat',data)
        self.context['references']={'ref':{'constituency_id':'seat','data_version':version,'epic_id':'TEST3'}}
        self.context['selected_reference']='ref'
        result=self.execute({'operation':'lookup','reference':'ref'})
        self.assertEqual(result.facts[0]['total'],1)
        self.assertEqual(next(f['records'] for f in result.facts if 'records' in f)[0]['epic_id'],'TEST3')
        self.assertEqual(next(f['records'] for f in result.facts if 'records' in f)[0]['house_no'],'2')
        self.context['references']['ref']['data_version']='old-source'
        with self.assertRaisesRegex(ValueError,'stale'):
            self.execute({'operation':'lookup','reference':'ref'})

    def test_exact_booth_count(self):
        result=self.execute({'operation':'count','booth':'1','filters':[{'field':'religion','value':'Muslim'}]})
        self.assertEqual(result.facts[0]['total'],2)
        self.assertIn("'Muslim'", result.sources[0]['sql'])
        self.assertIn("'School'", result.sources[0]['sql'])

    def test_source_change_invalidates_population(self):
        self.execute({'operation':'count'})
        with closing(sqlite3.connect(self.path)) as c,c:
            c.execute("UPDATE voters SET age=66 WHERE epic_id='TEST1'")
        with self.assertRaisesRegex(ValueError,'No current verified population'):
            self.execute({'operation':'count','population_mode':'refine'})

    def test_refine_without_verified_population_is_not_silently_whole_seat(self):
        with self.assertRaisesRegex(ValueError,'No current verified population'):
            self.execute({'operation':'count','population_mode':'refine'})

    def test_scalar_count_remains_available_to_coordinator(self):
        result=self.execute({'operation':'count','filters':[{'field':'religion','value':'Muslim'}]})
        self.assertEqual(result.facts[0]['total'], 3)

    def test_agent_sql_is_not_rejected_for_task_filter_shape(self):
        assignment = task({'operation': 'count', 'filters': [{'field': 'region', 'value': 'East'}]})
        replies = [{'action': 'sql', 'sql': 'SELECT COUNT(*) AS total FROM voters'},
                   {'action': 'finish', 'status': 'complete'}]
        with patch.object(runner, 'gemini_chat', side_effect=[json.dumps(reply) for reply in replies]):
            result = run_specialist(assignment, 'seat', {}, runner, {})
        self.assertEqual(result.status, 'complete')
        self.assertEqual(result.facts[0]['rows'], [[5]])

    def test_agent_can_run_several_queries_before_finishing(self):
        assignment = task({'operation': 'count', 'filters': [{'field': 'region', 'value': 'East'}]})
        replies = [
            {'action': 'sql', 'sql': 'SELECT COUNT(*) AS total FROM voters'},
            {'action': 'sql', 'sql': "SELECT COUNT(*) AS total FROM voters WHERE region='East'"},
            {'action': 'finish', 'status': 'complete'},
        ]
        with patch.object(runner, 'gemini_chat', side_effect=[json.dumps(reply) for reply in replies]):
            result = run_specialist(assignment, 'seat', {}, runner, {})
        self.assertEqual(result.status, 'complete')
        self.assertIsNone(result.failure_code)
        self.assertEqual([fact['rows'] for fact in result.facts], [[[5]], [[3]]])

    def test_percentage_uses_all_voters_denominator_without_a_task_filter(self):
        assignment = task({'operation': 'aggregate'})
        replies = [
            {'action': 'sql', 'sql': "SELECT COUNT(*) AS all_voters, SUM(CASE WHEN religion='Muslim' THEN 1 ELSE 0 END) AS muslim_voters FROM voters"},
            {'action': 'finish', 'status': 'complete'},
        ]
        with patch.object(runner, 'gemini_chat', side_effect=[json.dumps(reply) for reply in replies]):
            result = run_specialist(assignment, 'seat', {}, runner, {}, 'find total Muslim percentage')
        self.assertEqual(result.status, 'complete')
        self.assertEqual(result.facts[0]['columns'], ['all_voters', 'muslim_voters'])
        self.assertEqual(result.facts[0]['rows'], [[5, 3]])

    def test_agent_can_list_regions_with_distinct_and_blank_filter(self):
        assignment = task({'operation': 'aggregate', 'group_by': ['region']})
        replies = [
            {'action': 'sql', 'sql': 'SELECT DISTINCT region FROM voters WHERE region IS NOT NULL ORDER BY region'},
            {'action': 'finish', 'status': 'complete'},
        ]
        with patch.object(runner, 'gemini_chat', side_effect=[json.dumps(reply) for reply in replies]):
            result = run_specialist(assignment, 'seat', {}, runner, {}, 'find all regions in voter list')
        self.assertEqual(result.status, 'complete')
        self.assertEqual(result.facts[0]['rows'], [['East'], ['West']])

    def test_coordinator_preserves_percentage_answer(self):
        assignment = task({'operation': 'aggregate'})
        replies = [
            {'action': 'delegate', 'tasks': [assignment.model_dump()]},
            {'action': 'sql', 'sql': "SELECT COUNT(*) AS all_voters, SUM(CASE WHEN religion='Muslim' THEN 1 ELSE 0 END) AS muslim_voters FROM voters"},
            {'action': 'finish', 'status': 'complete'},
            {'action': 'finish', 'answer': '3 of 5 voter records are Muslim: 60%.', 'evidence_task_ids': ['v']},
        ]
        with patch.object(runner, 'gemini_chat', side_effect=[json.dumps(reply) for reply in replies]):
            result = runner.run_local_agent('find total Muslim percentage', 'seat')
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['answer'], '3 of 5 voter records are Muslim: 60%.')

    def test_failed_sql_retries_return_earlier_observations(self):
        assignment = task({'operation': 'aggregate'})
        replies = [
            {'action': 'sql', 'sql': 'SELECT COUNT(*) AS all_voters FROM voters'},
            {'action': 'sql', 'sql': 'SELECT missing_column FROM voters'},
            {'action': 'sql', 'sql': 'SELECT missing_column FROM voters'},
        ]
        with patch.object(runner, 'gemini_chat', side_effect=[json.dumps(reply) for reply in replies]):
            result = run_specialist(assignment, 'seat', {}, runner, {}, 'find total Muslim percentage')
        self.assertEqual(result.status, 'partial')
        self.assertEqual(result.facts[0]['rows'], [[5]])
        self.assertTrue(any('missing_column' in error for error in result.errors))

    def test_provider_schema_hides_internal_helpers_but_legacy_tasks_parse(self):
        from agents.execution import provider_schema
        schema=provider_schema(CoordinatorAction.model_json_schema())
        fields=schema['properties']['tasks']['items']['properties']
        self.assertNotIn('name_query',fields)
        self.assertNotIn('household_query',fields)
        self.assertEqual(fields['voter_request']['properties']['filters']['type'],'array')
        assignment=SpecialistTask(task_id='old',agent='voter',objective='name',required_outputs=['count'],acceptance_criteria=['match'],name_query={'operation':'count','person':'श्याम'})
        self.assertIsNotNone(assignment.name_query)

    def test_few_shot_outputs_match_their_actual_runtime_contracts(self):
        examples=json.loads((Path(__file__).resolve().parents[2]/'agents/prompts/few_shot_examples.json').read_text())
        schemas={'coordinator_system.txt':CoordinatorAction,'voter_agent_system.txt':ResearchAction,'history_agent_system.txt':ResearchAction,'internet_SearchPlan':SearchPlan,'internet_Ranking':Ranking,'internet_Findings':Findings}
        for key,cases in examples.items():
            for case in cases:
                with self.subTest(key=key,input=case['input']):schemas[key].model_validate(case['output'])

    def test_private_voter_identity_is_not_forwarded_to_internet(self):
        from agents.research import validate_public_assignment
        from agents.contracts import WebResearchQuery
        context={'references':{'ref':{'name':'Fictional Citizen','epic_id':'TEST0001'}}}
        for question in ['Current news about TEST0001','Find Fictional Citizen online']:
            with self.assertRaisesRegex(ValueError,'private voter identity'):
                validate_public_assignment(WebResearchQuery(public_question=question),context)
        validate_public_assignment(WebResearchQuery(public_question='Public road notices in Test District'),context)

    def test_rehydrated_reference_can_remain_selected_in_a_cited_answer(self):
        from agents.households import data_version
        context={'selected_reference':'ref','references':{'ref':{'constituency_id':'seat','data_version':data_version('seat',data),'epic_id':'TEST3'}}}
        reply={'action':'finish','answer':'The recorded house number is 2.','evidence_task_ids':['_verified_person'],'selected_voter_reference':'ref'}
        with patch.object(runner,'gemini_chat',return_value=json.dumps(reply)):
            result=runner.run_local_agent('What is her house number?','seat',voter_context=context)
        self.assertEqual(result['status'],'complete')
        self.assertEqual(result['voter_context']['selected_reference'],'ref')
        self.assertEqual(result['agent_path'],['voter_agent'])

    def test_verified_context_cannot_answer_a_population_count(self):
        from agents.households import data_version
        version=data_version('seat',data)
        context={'selected_reference':'ref','references':{'ref':{'constituency_id':'seat','data_version':version,'epic_id':'TEST3'}}}
        replies=[{'action':'finish','answer':'There is 1 voter.','evidence_task_ids':['_verified_person']},
                 {'action':'delegate','tasks':[task({'operation':'count'}).model_dump()]},
                 {'action':'finish','answer':'There are 5 voters.','evidence_task_ids':['v']}]
        with patch.object(runner,'gemini_chat',side_effect=with_specialist([json.dumps(r) for r in replies])):
            result=runner.run_local_agent('How many voters?','seat',voter_context=context)
        self.assertEqual(result['answer'],'There are 5 voters.')
        self.assertEqual(result['status'],'complete')

    def test_per_turn_diagnostics_survive_later_turns_and_are_bounded(self):
        store=MemorySessions()
        for i in range(26):
            with store.turn('user','sid','seat') as state:
                state['_turn_traces']=[{'request_id':str(i),'status':'complete','events':[{'sql':'SELECT COUNT(*) FROM voters'}]}]
        self.assertEqual(len(store.turn_records[('user','sid')]),24)
        self.assertIn('2',store.turn_records[('user','sid')])
        self.assertNotIn('_turn_traces',store.read('user','sid'))
        self.assertIsNone(store.read('other','sid'))

"""Meaning/operation checks using fictional roll records, never production PII."""
import copy
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
from agents.voter import validate_count_answer
from agents.query_validation import check_query
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
            self.assertTrue(result.facts[0]['validated_count'])
        self.assertEqual(self.context['population']['filters'],[])

    def test_full_coordinator_uses_new_payload_and_repairs_wrong_synthesis(self):
        assignment=task({'operation':'count','filters':[{'field':'religion','value':'Muslim'}]})
        replies=[{'action':'delegate','tasks':[assignment.model_dump()]},
                 {'action':'finish','answer':'There are 0 Muslim voters.','evidence_task_ids':['v']},
                 {'action':'finish','answer':'There are 3 Muslim voters.','evidence_task_ids':['v']}]
        with patch.object(runner,'gemini_chat',side_effect=with_specialist([json.dumps(r) for r in replies])):
            result=runner.run_local_agent('How many Muslim voters?','seat')
        self.assertEqual(result['status'],'complete')
        self.assertEqual(result['answer'],'There are 3 matching voter records in the available data.\nRecorded filters: religion = Muslim.')
        self.assertEqual(result['voter_context']['population']['filters'][0]['value'],'Muslim')
        self.assertTrue(any(e.get('event')=='coordinator_rejected' for e in result['turn_trace']['events']))

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

    def test_unknown_category_is_not_a_zero(self):
        with patch.object(runner,'gemini_chat',return_value='{"values":[],"ambiguous":false}'):
            result=self.execute({'operation':'count','filters':[{'field':'religion','value':'Unknown category'}]})
        self.assertEqual(result.status,'needs_clarification')
        self.assertFalse(result.facts)
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
        with patch.object(runner,'gemini_chat',return_value='{"values":["Invented"],"ambiguous":false}'):
            result=self.execute({'operation':'count','filters':[{'field':'religion','value':'अज्ञात'}]})
        self.assertEqual(result.status,'needs_clarification')

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

    def test_numeric_and_booth_filters_survive_analytical_queries(self):
        from agents.voter import compile_filters,population_filters
        assignment=task({'operation':'aggregate','booth':'1','filters':[{'field':'age','operator':'gt','value':'60'}]})
        with closing(data.open_sqlite('seat','voters.db')) as c:
            compiled=compile_filters(c,population_filters(assignment.voter_request,'seat',data),runner)
            check_query("SELECT gender,COUNT(*) FROM voters WHERE age>60 AND polling_station='School' GROUP BY gender",assignment,c,runner,compiled)
            with self.assertRaises(ValueError):
                check_query("SELECT gender,COUNT(*) FROM voters WHERE age>60 GROUP BY gender",assignment,c,runner,compiled)

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

    def test_synthesis_cannot_replace_the_validated_count_with_zero(self):
        result=self.execute({'operation':'count','filters':[{'field':'religion','value':'Muslim'}]})
        with self.assertRaises(ValueError):
            validate_count_answer('There are 0 Muslim voters.',[result.model_dump()])
        validate_count_answer('There are 3 Muslim voters.',[result.model_dump()])

    def test_analytical_sql_must_keep_filters_even_inside_ctes(self):
        assignment=task({'operation':'aggregate','filters':[{'field':'religion','value':'Muslim'}]})
        with closing(data.open_sqlite('seat','voters.db')) as c:
            check_query("WITH selected AS (SELECT * FROM voters v WHERE v.religion='Muslim') SELECT gender,COUNT(*) FROM selected GROUP BY gender",assignment,c,runner)
            for sql in ["SELECT gender,COUNT(*) FROM voters GROUP BY gender", "SELECT COUNT(*) FROM voters WHERE religion='Muslim' OR gender='Male'", "SELECT COUNT(*) FROM voters WHERE religion='muslim'", "SELECT COUNT(*) FROM voters WHERE religion='Muslim' AND gender='Male'"]:
                with self.assertRaisesRegex(ValueError,'retain every'):
                    check_query(sql,assignment,c,runner)

    def test_wrong_election_type_is_rejected(self):
        assignment=SpecialistTask(task_id='h',agent='history',objective='2024 results',required_outputs=['leader'],acceptance_criteria=['2024 Lok Sabha'],history_request={'operation':'winner','year':2024,'election_type':'lok'})
        with closing(sqlite3.connect(':memory:')) as c:
            c.execute('CREATE TABLE form20_2022_vidhan(votes INTEGER)')
            with self.assertRaisesRegex(ValueError,'wrong election'):
                check_query('SELECT SUM(votes) FROM form20_2022_vidhan',assignment,c,runner)

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
        self.assertEqual(result['answer'],'There are 5 matching voter records in the available data.')
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

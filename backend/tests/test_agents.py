from backend.tests.sql_model_fixture import with_specialist, run_voter_task

"""Behavioral agent checks with synthetic rolls and scripted model responses."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import closing

from agents import local_runner as runner
from agents.contracts import SpecialistTask, SpecialistResult
from agents.name_matching import name_matches
from agents.runtime import sql_specialist, portfolio_specialist
from app.config import get_settings


def task(agent="voter", task_id="t1", **changes):
    return SpecialistTask.model_validate({
        "task_id": task_id, "agent": agent, "objective": "Count matching voters within booth 2",
        "required_outputs": ["total"], "acceptance_criteria": ["Preserve booth and name filters"],
        **changes,
    })


def delegate(value):
    return json.dumps({"action": "delegate", "tasks": [value.model_dump()]}, ensure_ascii=False)


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.settings = get_settings()
        self.previous_root = self.settings.local_data_root
        self.settings.local_data_root = self.directory.name
        seat = Path(self.directory.name) / "seat"
        seat.mkdir()
        (seat / "boothlist.csv").write_text("part_no,pooling_station\n2,School\n3,College\n", encoding="utf-8")
        with closing(sqlite3.connect(seat / "voters.db")) as conn, conn:
            conn.execute("CREATE TABLE voters(name TEXT, relative_name TEXT, polling_station TEXT, house_no TEXT, gender TEXT)")
            conn.executemany("INSERT INTO voters VALUES(?,?,?,?,?)", [
                ("श्याम कुमार", "राम लाल", "School", "5", "M"),
                ("श्याम", "राम लाल", "School", "6", "M"),
                ("घनश्याम", "राम लाल", "School", "5", "M"),
                ("श्यामलाल", "राम लाल", "School", "5", "M"),
                ("श्याम", "राम लाल", "College", "5", "M"),
                ("श्‍याम देवी", "राम लाल", "School", "5", "F"),
                ("शयाम", "मोहन", "School", "7", "M"),
            ])

    def tearDown(self):
        self.settings.local_data_root = self.previous_root
        self.directory.cleanup()

    def name_result(self, query, candidates=None, **kwargs):
        return run_voter_task(
            task(name_query=query, **kwargs), "seat", runner, variants=candidates
        )

    def test_word_boundaries_and_unicode(self):
        self.assertTrue(name_matches("श्‍याम कुमार", "श्याम", "word"))
        self.assertFalse(name_matches("घनश्याम", "श्याम", "word"))
        self.assertFalse(name_matches("श्यामलाल", "श्याम", "word"))
        self.assertFalse(name_matches("श्याम कुमार", "श्याम", "full"))
        self.assertTrue(name_matches("घनश्याम", "श्याम", "substring"))

    def test_count_scoped_to_booth_not_limited_records(self):
        result, model = self.name_result(
            {"operation": "count", "person": "Shyam", "booth": "2"}
        )
        self.assertEqual(result.status, "complete")
        self.assertEqual(result.facts[0]["total"], 3)
        self.assertNotIn("records", result.facts[0])
        self.assertTrue(
            any(
                '"name": "Shyam"' in call.args[0][1]["content"]
                for call in model.call_args_list
            )
        )

    def test_hindi_input_and_every_filter_preserved(self):
        result, model = self.name_result(
            {
                "operation": "count",
                "person": "श्याम",
                "relative": "राम लाल",
                "booth": "Part_2",
                "filters": {"house_no": "5", "gender": "M"},
            }
        )
        self.assertEqual(result.facts[0]["total"], 1)
        self.assertTrue(
            all(
                call.kwargs.get("response_schema", {}).get("title") != "NameCandidates"
                for call in model.call_args_list
            )
        )

    def test_ambiguous_spellings_require_selection(self):
        query = {"operation": "count", "person": "Shyam", "booth": "School"}
        result, _ = self.name_result(query, ["श्याम", "शयाम"])
        self.assertEqual(result.status, "needs_clarification")
        self.assertFalse(result.facts)
        result, _ = self.name_result(
            {**query, "confirmed_person_variants": ["श्याम", "शयाम"]}
        )
        self.assertEqual(result.facts[0]["total"], 4)  # OR alternatives, not AND

    def test_zero_matches_is_evidence(self):
        result, _ = self.name_result({"operation": "count", "person": "नरेश", "booth": "2"})
        self.assertEqual(result.status, "complete")
        self.assertEqual(result.facts[0]["total"], 0)

    def test_missing_booth_and_unsupported_filters_never_broaden(self):
        result, _ = self.name_result({"operation": "count", "person": "श्याम", "booth": "Unknown"})
        self.assertEqual(result.status, "needs_clarification")
        result, _ = self.name_result({"operation": "count", "person": "श्याम", "filters": {"unknown": "5"}})
        self.assertEqual(result.status, "failed")
        result, _ = self.name_result({"operation": "count", "person": "श्याम"}, constraints={"booth": "2"})
        self.assertEqual(result.status, "failed")

    def test_full_name_alternatives_keep_words_together(self):
        result, _ = self.name_result(
            {
                "operation": "lookup",
                "person": "Shyam Kumar",
                "booth": "2",
                "confirmed_person_variants": ["श्याम कुमार", "शयाम कुमार"],
            }
        )
        self.assertEqual(result.facts[0]["total"], 1)
        self.assertEqual(
            dict(zip(result.facts[1]["columns"], result.facts[1]["rows"][0]))["name"],
            "श्याम कुमार",
        )

    def test_total_not_capped_when_lookup_rows_are_truncated(self):
        with (
            closing(
                sqlite3.connect(Path(self.directory.name) / "seat/voters.db")
            ) as conn,
            conn,
        ):
            conn.executemany(
                "INSERT INTO voters VALUES(?,?,?,?,?)",
                [("श्याम", "राम", "School", "5", "M")] * 100,
            )
        result, _ = self.name_result(
            {"operation": "lookup", "person": "श्याम", "booth": "2"}
        )
        self.assertEqual(result.facts[0]["total"], 103)
        self.assertEqual(len(result.facts[1]["rows"]), 80)
        self.assertTrue(result.facts[1]["truncated"])

    def test_specialist_corrects_error_and_returns_actual_rows(self):
        replies = [
            '{"action":"sql","sql":"SELECT COUNT(*) FROM missing_table"}',
            '{"action":"sql","sql":"SELECT COUNT(*) AS total FROM voters"}',
            '{"action":"finish","status":"complete"}',
        ]
        with patch.object(runner, "gemini_chat", side_effect=replies):
            result = sql_specialist(task(), "seat", {}, runner)
        self.assertEqual(result.status, "complete")
        self.assertEqual(result.facts[0]["rows"], [[7]])
        self.assertEqual(len(result.sources), 1)

    def test_coordinator_inspects_results_and_delegates_followup(self):
        first = task("portfolio", constraints={"booth": "2"})
        followup = task("history", "t2", objective="Calculate winner for the resolved School booth in 2022", constraints={"year": 2022, "booth": "School"})
        observations = [
            SpecialistResult(task_id="t1", agent="portfolio", status="complete", facts=[{"booth": "School"}]),
            SpecialistResult(task_id="t2", agent="history", status="complete", facts=[{"winner": "Candidate A"}]),
        ]
        replies = [delegate(first), delegate(followup), '{"action":"finish","answer":"Candidate A won.","evidence_task_ids":["t2"]}']
        with patch.object(runner, "gemini_chat", side_effect=replies) as model, patch("agents.runtime.run_specialist", side_effect=observations) as specialists:
            result = runner.run_local_agent("Compare the booth and result", "seat")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["agent_path"], ["portfolio_agent", "history_agent"])
        self.assertEqual(specialists.call_args_list[1].args[0].objective, followup.objective)
        self.assertIn("School", model.call_args_list[1].args[0][1]["content"])

    def test_clarification_resumes_original_task(self):
        with patch.object(runner, "gemini_chat", return_value='{"action":"clarify","answer":"Which year?"}'):
            initial = runner.run_local_agent("Who won at this booth?", "seat", "sid")
        revised = task("history", objective="Find the 2022 winner at booth 2", constraints={"year": 2022, "booth": "2"})
        observation = SpecialistResult(task_id="t1", agent="history", status="complete", facts=[{"winner": "A"}])
        with patch.object(runner, "gemini_chat", side_effect=[delegate(revised), '{"action":"finish","answer":"A won in 2022.","evidence_task_ids":["t1"]}']) as model, patch("agents.runtime.run_specialist", return_value=observation):
            result = runner.run_local_agent("2022", "seat", "sid", history=[{"role": "user", "content": "Who won at this booth?"}], pending_state=initial["pending_state"])
        self.assertEqual(result["status"], "complete")
        self.assertIn("Who won at this booth?", model.call_args_list[0].args[0][1]["content"])
        self.assertIsNone(result["pending_state"])

    def test_malformed_routing_is_not_a_greeting(self):
        with patch.object(runner, "gemini_chat", return_value="broken output") as model:
            result = runner.run_local_agent("How many voters?", "seat")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(model.call_count, 2)  # One targeted repair, then stop.
        self.assertNotIn("I am", result["answer"])

    def test_unsubstantiated_finish_is_rejected(self):
        with patch.object(runner, "gemini_chat", side_effect=[
            '{"action":"finish","answer":"There are 900 voters."}',
            '{"action":"clarify","answer":"Which booth should I check?"}',
        ]):
            result = runner.run_local_agent("How many voters at that booth?", "seat")
        self.assertEqual(result["status"], "needs_clarification")
        self.assertNotIn("900", result["answer"])

    def test_repeat_task_cannot_spin(self):
        assignment = task()
        observation = SpecialistResult(task_id="t1", agent="voter", status="failed", errors=["Missing data"])
        with patch.object(runner, "gemini_chat", return_value=delegate(assignment)), patch("agents.runtime.run_specialist", return_value=observation) as specialist:
            result = runner.run_local_agent("Count voters", "seat")
        self.assertEqual(specialist.call_count, 1)
        self.assertEqual(result["status"], "failed")

    def test_filtered_count_not_rewritten_as_constituency_total(self):
        answer = "There are 3 voters named Shyam at School."
        self.assertEqual(runner.clean_user_answer(answer, "Total voters named Shyam at booth 2?"), answer)

    def test_scoped_name_count_through_coordinator_and_real_specialist(self):
        assignment = task(name_query={"operation": "count", "person": "Shyam", "booth": "2"})
        replies = [delegate(assignment), '{"variants":["श्याम"]}',
                   '{"action":"finish","answer":"3 voters named Shyam are recorded at School, using the checked spelling श्याम.","evidence_task_ids":["t1"]}']
        with patch.object(runner, "gemini_chat", side_effect=with_specialist(replies)) as model:
            result = runner.run_local_agent("How many people named Shyam in booth 2?", "seat")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["agent_path"], ["voter_agent"])
        self.assertIn('"total": 3', model.call_args.args[0][1]["content"])

    def test_ambiguous_name_task_resumes_with_user_selected_spellings(self):
        assignment = task(name_query={"operation": "count", "person": "Shyam", "booth": "2"})
        with patch.object(runner, "gemini_chat", side_effect=with_specialist([delegate(assignment), '{"variants":["श्याम","शयाम"]}',
                         '{"action":"clarify","answer":"Should I include श्याम, शयाम, or both?"}'])):
            initial = runner.run_local_agent("How many Shyam in booth 2?", "seat", "sid")
        self.assertEqual(initial["status"], "needs_clarification")
        assignment.name_query.confirmed_person_variants = ["श्याम", "शयाम"]
        with patch.object(runner, "gemini_chat", side_effect=with_specialist([delegate(assignment),
                         '{"action":"finish","answer":"4 voters match the two spellings at booth 2.","evidence_task_ids":["t1"]}'])) as model:
            result = runner.run_local_agent("Both", "seat", "sid", pending_state=initial["pending_state"])
        self.assertEqual(result["status"], "complete")
        self.assertEqual(model.call_count, 4)  # SQL, specialist finish and two coordinator calls; no transliteration.
        self.assertIn('"total": 4', model.call_args.args[0][1]["content"])

    def test_failed_evidence_can_finish_with_explicit_failure(self):
        assignment = task()
        failure = SpecialistResult(task_id="t1", agent="voter", status="failed", errors=["Missing roll"])
        with patch.object(runner, "gemini_chat", side_effect=[delegate(assignment),
                         '{"action":"finish","status":"failed","answer":"The voter roll is unavailable."}']), patch("agents.runtime.run_specialist", return_value=failure):
            result = runner.run_local_agent("How many voters?", "seat")
        self.assertEqual(result["status"], "failed")

    def test_portfolio_part_matching_and_sections(self):
        reports = Path(self.directory.name) / "seat/booth_analysis"
        reports.mkdir()
        (reports / "part_2.json").write_text(json.dumps({"booth_metadata": {"booth_name": "Part_2"}, "demographics": {"total": 7}}))
        (reports / "part_20.json").write_text(json.dumps({"booth_metadata": {"booth_name": "Part_20"}, "demographics": {"total": 999}}))
        result = portfolio_specialist(task("portfolio", constraints={"booth": "2", "sections": ["demographics"]}), "seat", runner)
        self.assertEqual(result.status, "complete")
        self.assertEqual(result.facts[0]["portfolio"], {"demographics": {"total": 7}})
        self.assertEqual(result.sources[0]["source_file"], "part_2.json")
        # Explicit metadata must override a misleading filename.
        (reports / "part_2.json").write_text(json.dumps({"booth_metadata": {"booth_name": "Part_20"}}))
        result = portfolio_specialist(task("portfolio", constraints={"booth": "2"}), "seat", runner)
        self.assertEqual(result.status, "needs_clarification")

    def test_coordinator_cannot_finish_over_pending_spelling_ambiguity(self):
        assignment = task(name_query={"operation": "count", "person": "Shyam", "booth": "2"})
        observation = SpecialistResult(task_id="t1", agent="voter", status="needs_clarification", facts=[{"candidate_spellings": ["श्याम", "शयाम"]}], missing_inputs=["Which spelling?"])
        replies = [delegate(assignment), '{"action":"finish","answer":"4 voters.","evidence_task_ids":["t1"]}',
                   '{"action":"clarify","answer":"Which spelling should I include?"}']
        with patch.object(runner, "gemini_chat", side_effect=replies), patch("agents.runtime.run_specialist", return_value=observation):
            result = runner.run_local_agent("Count Shyam", "seat")
        self.assertEqual(result["status"], "needs_clarification")
        self.assertNotIn("4 voters", result["answer"])

    def test_coordinator_has_a_total_specialist_call_budget(self):
        counter = 0

        def next_plan(*args, **kwargs):
            nonlocal counter
            counter += 1
            return json.dumps({"action": "delegate", "tasks": [
                task(task_id=f"{counter}-{i}", objective=f"Inspect distinct slice {counter}-{i}").model_dump() for i in range(3)
            ]})

        def observe(assignment, *args):
            return SpecialistResult(task_id=assignment.task_id, agent="voter", status="partial", facts=[{"total": 1}])

        with patch.object(runner, "gemini_chat", side_effect=next_plan), patch("agents.runtime.run_specialist", side_effect=observe) as specialist:
            result = runner.run_local_agent("Count voters", "seat")
        self.assertEqual(specialist.call_count, 8)
        self.assertEqual(result["status"], "partial")


if __name__ == "__main__":
    unittest.main()

"""Regression checks for task meaning, recovery, and the native model boundary."""

import json
import time
import sqlite3
import tempfile
from pathlib import Path
from contextlib import closing
import unittest
from unittest.mock import patch

from agents import execution, local_runner as runner, model as model_boundary
from langchain_core.messages import AIMessage
from agents.completion import (
    infer_requirements,
    ranking_rows,
    resolve_requirements,
    render_ranking,
    validate_zero_claim,
    validate_clarification,
    source_policy,
)
from agents.contracts import (
    AnswerRequirements,
    CoordinatorAction,
    SpecialistTask,
    SpecialistResult,
    WebResearchQuery,
)
from agents.runtime import compact_results
from agents.runtime import sql_specialist
from app.config import get_settings
from agents.presentation import partial_reason


def task(agent="history", **kwargs):
    return SpecialistTask(task_id="t1", agent=agent, objective="Resolve the requested comparison",
                          required_outputs=["requested evidence"], acceptance_criteria=["Match the selected seat"], **kwargs)


def action(kind, **kwargs):
    return json.dumps({"action": kind, **kwargs})


class HarnessTests(unittest.TestCase):
    def ranking(self, rows=None, **req):
        requirements = AnswerRequirements(intent="election_ranking", year=2024, geography="region", **req)
        assignment = task(answer_requirements=requirements)
        result = SpecialistResult(task_id="t1", agent="history", status="complete",
            facts=[{"source_id": "q1", "columns": ["geography_name", "party_votes", "opponent_name", "opponent_votes", "margin"],
                    "rows": rows or [["North region", 400, "Candidate A", 100, 300], ["West region", 250, "Candidate B", 200, 50]], "truncated": False}],
            sources=[{"source_id": "q1", "sql": "SELECT * FROM form20_2024_lok"}])
        return requirements, assignment, result

    def test_native_schema_inlines_refs_without_changing_validator(self):
        schema = execution.provider_schema(CoordinatorAction.model_json_schema())
        encoded = json.dumps(schema)
        self.assertNotIn("$ref", encoded)
        self.assertNotIn("$defs", encoded)
        self.assertNotIn("additionalProperties", encoded)
        self.assertNotIn("household_query", encoded)
        self.assertNotIn('"name_query"', encoded)
        self.assertIn("voter_request", encoded)
        self.assertNotIn("answer_requirements", schema["required"])
        self.assertIn("objective_mode", schema["required"])
        def check_order(node):
            if isinstance(node, dict):
                if "propertyOrdering" in node:
                    self.assertEqual(set(node["propertyOrdering"]), set(node["properties"]))
                for child in node.values():
                    check_order(child)
            elif isinstance(node, list):
                for child in node:
                    check_order(child)
        check_order(schema)
        with self.assertRaises(ValueError):
            CoordinatorAction.model_validate({"action": "delegate", "tasks": [], "extra": 1})

    def test_langchain_binds_native_schema_and_preserves_system_message(self):
        with patch.object(model_boundary, "ChatGoogleGenerativeAI") as factory:
            factory.return_value.bind.return_value.invoke.return_value = AIMessage(
                content="{}", response_metadata={"finish_reason": "STOP"}
            )
            value = model_boundary.chat(
                [
                    {"role": "system", "content": "Be concise"},
                    {"role": "user", "content": "Hello"},
                ],
                response_schema=CoordinatorAction.model_json_schema(),
            )
        self.assertEqual(value, "{}")
        self.assertEqual(
            factory.return_value.bind.call_args.kwargs["response_schema"],
            execution.provider_schema(CoordinatorAction.model_json_schema()),
        )
        self.assertEqual(
            factory.return_value.bind.return_value.invoke.call_args.args[0][0].content,
            "Be concise",
        )
        self.assertEqual(factory.call_args.kwargs["max_retries"], 0)

    def test_real_langchain_adapter_builds_provider_request_without_network(self):
        from google.genai import types

        provider_response = types.GenerateContentResponse(
            candidates=[
                types.Candidate(
                    content=types.Content(parts=[types.Part(text="{}")]),
                    finish_reason="STOP",
                )
            ]
        )
        with (
            patch.dict(
                "os.environ",
                {"GEMINI_API_KEY": "fictional", "GOOGLE_GENAI_USE_VERTEXAI": "false"},
            ),
            patch("langchain_google_genai.chat_models.Client") as client,
        ):
            client.return_value.models.generate_content.return_value = provider_response
            value = model_boundary.chat(
                [{"role": "user", "content": "Hello"}],
                response_schema=CoordinatorAction.model_json_schema(),
            )
        self.assertEqual(value, "{}")
        request = client.return_value.models.generate_content.call_args.kwargs
        self.assertEqual(request["config"].response_mime_type, "application/json")
        self.assertIn("properties", request["config"].response_json_schema)

    def test_wrapped_provider_errors_keep_status_without_exposing_body(self):
        cause = RuntimeError("private provider response")
        cause.code = 429
        wrapped = RuntimeError("wrapped private request")
        wrapped.__cause__ = cause
        with patch.object(
            model_boundary, "ChatGoogleGenerativeAI", side_effect=wrapped
        ):
            value = model_boundary._invoke_model(
                [], 0, 512, time.monotonic() + 10, None
            )
        self.assertEqual(value.status_code, 429)
        self.assertNotIn("private", value)

    def test_truncated_and_blocked_generations_never_parse_as_complete(self):
        with (
            patch.object(model_boundary, "ChatGoogleGenerativeAI") as factory,
            execution.execution_scope(),
        ):
            invoke = factory.return_value.bind.return_value.invoke
            invoke.return_value = AIMessage(
                content="{}", response_metadata={"finish_reason": "MAX_TOKENS"}
            )
            result = model_boundary._invoke_model(
                [], 0, 512, time.monotonic() + 10, None
            )
            self.assertIn("output_truncated", result)
            invoke.return_value = AIMessage(
                content="{}", response_metadata={"finish_reason": "SAFETY"}
            )
            self.assertIn(
                "generation_blocked",
                model_boundary._invoke_model([], 0, 512, time.monotonic() + 10, None),
            )
            self.assertEqual(execution.events()[0]["finish_reason"], "MAX_TOKENS")

    def test_langchain_transport_retries_truncation_with_larger_budget(self):
        with patch.object(
            model_boundary,
            "_invoke_model",
            side_effect=["<<LLM_ERROR: output_truncated>>", "{}"],
        ) as invoke:
            self.assertEqual(model_boundary.chat([], max_tokens=512), "{}")
        self.assertEqual([call.args[2] for call in invoke.call_args_list], [512, 1024])

    def test_request_deadline_is_isolated_and_stops_new_calls(self):
        with (
            execution.execution_scope(-1),
            patch.object(model_boundary, "_invoke_model") as provider,
        ):
            self.assertIn("deadline", model_boundary.chat([]))
            provider.assert_not_called()
        self.assertIsNone(execution.deadline())

    def test_valid_ranking_is_rendered_from_checked_numbers(self):
        req, assignment, result = self.ranking()
        replies = [action("delegate", tasks=[assignment.model_dump()]),
                   action("finish", answer="Booth 1, Booth 2", evidence_task_ids=["t1"])]
        with patch.object(runner, "gemini_chat", side_effect=replies), patch("agents.runtime.run_specialist", return_value=result):
            response = runner.run_local_agent("Top regions where BJP was ahead in 2024", "allahabad-south")
        self.assertEqual(response["status"], "complete")
        self.assertIn("North region: BJP 400", response["answer"])
        self.assertIn("margin 300", response["answer"])
        self.assertNotIn("Booth 1", response["answer"])

    def test_booth_list_cannot_complete_regional_ranking(self):
        _, assignment, result = self.ranking()
        result.facts = [{"columns": ["part_number"], "rows": [[1], [2]]}]
        finish = action("finish", answer="Booth 1, Booth 2", evidence_task_ids=["t1"])
        with patch.object(runner, "gemini_chat", side_effect=[action("delegate", tasks=[assignment.model_dump()]), finish, finish]), patch("agents.runtime.run_specialist", return_value=result):
            response = runner.run_local_agent("Top regions where BJP was ahead in 2024", "allahabad-south")
        self.assertEqual(response["status"], "partial")
        self.assertNotIn("Booth 1", response["answer"])
        self.assertIn("did not establish", response["answer"])

    def test_invalid_rankings_are_rejected(self):
        for rows in ([["A", 100, "B", 90, 50]], [["A", 200, "B", 150, 50], ["C", 400, "D", 100, 300]],
                     [["", 200, "B", 150, 50]], [["A", float("nan"), "B", 100, 10]]):
            req, assignment, result = self.ranking(rows)
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                ranking_rows(req, [result.model_dump()], {"t1": assignment.model_dump()})

    def test_wrong_year_cannot_support_ranking(self):
        req, assignment, result = self.ranking()
        result.sources[0]["sql"] = "SELECT * FROM form20_2022_vidhan"
        with self.assertRaisesRegex(ValueError, "year"):
            ranking_rows(req, [result.model_dump()], {"t1": assignment.model_dump()})

    def test_vote_share_requires_correct_denominator(self):
        req, assignment, result = self.ranking(metric="vote_share")
        result.facts[0]["columns"] += ["total_valid_votes", "vote_share"]
        result.facts[0]["rows"] = [["A", 400, "B", 100, 300, 1000, 40]]
        self.assertEqual(len(ranking_rows(req, [result.model_dump()], {"t1": assignment.model_dump()})), 1)
        result.facts[0]["rows"][0][-1] = 80
        with self.assertRaises(ValueError):
            ranking_rows(req, [result.model_dump()], {"t1": assignment.model_dump()})

    def test_public_history_uses_historical_research_mode(self):
        assignment = task("research", research_query={"public_question": "Historical background of Allahabad South"})
        result = SpecialistResult(task_id="t1", agent="research", status="failed", errors=["Unavailable"])
        with patch.object(runner, "gemini_chat", side_effect=[action("delegate", tasks=[assignment.model_dump()]),
                action("finish", answer="Research is unavailable.", status="failed")]), patch("agents.runtime.run_specialist", return_value=result) as specialist:
            runner.run_local_agent("Tell me the history of this constituency", "allahabad-south")
        self.assertFalse(specialist.call_args.args[0].research_query.time_sensitive)
        self.assertEqual(specialist.call_args.args[0].research_query.time_mode, "historical")

    def test_explicit_research_mode_overrides_legacy_default(self):
        self.assertFalse(WebResearchQuery(public_question="History", time_mode="historical").time_sensitive)
        self.assertTrue(WebResearchQuery(public_question="News", time_mode="recent", time_sensitive=False).time_sensitive)

    def test_explicit_metric_and_limit_survive_model_contract_omission(self):
        req = resolve_requirements("Top 5 regions for BJP by vote share in 2024", AnswerRequirements())
        self.assertEqual((req.metric, req.limit, req.year, req.geography), ("vote_share", 5, 2024, "region"))

    def test_other_parties_are_not_rendered_as_bjp(self):
        req, _, result = self.ranking(party="SP")
        answer = render_ranking([dict(zip(result.facts[0]["columns"], result.facts[0]["rows"][0]))], req, "Seat")
        self.assertIn("SP 400", answer)
        self.assertNotIn("BJP", answer)

    def test_new_objective_discards_stale_pending_results(self):
        pending = {"objective": "Count Shyam", "tasks": {}, "results": {"old": {"status": "needs_clarification", "facts": [], "missing_inputs": ["Which spelling?"]}}}
        with patch.object(runner, "gemini_chat", return_value=action("clarify", answer="Which election year?", objective_mode="new")):
            result = runner.run_local_agent("Compare election results", "allahabad-south", pending_state=pending)
        self.assertEqual(result["pending_state"]["objective"], "Compare election results")
        self.assertEqual(result["pending_state"]["results"], {})

    def test_duplicate_research_does_not_consume_another_call(self):
        assignment = task("research", research_query={"public_question": "Public notices"})
        result = SpecialistResult(task_id="t1", agent="research", status="failed")
        with patch.object(runner, "gemini_chat", return_value=action("delegate", tasks=[assignment.model_dump()])) as model, patch("agents.runtime.run_specialist", return_value=result) as specialist:
            runner.run_local_agent("Latest public notices", "allahabad-south")
        self.assertEqual(specialist.call_count, 1)
        self.assertIn('"remaining_research_calls": 1', model.call_args.args[0][1]["content"])

    def test_evidence_budget_signals_omission_without_invalid_json(self):
        value = {"t1": {"status": "complete", "facts": [{"text": "x" * 10000}], "limitations": []}}
        compact = compact_results(value, budget=1000)
        self.assertTrue(compact["t1"]["evidence_omitted"])
        self.assertEqual(compact["t1"]["facts"], [])

    def test_history_and_election_history_have_distinct_defaults(self):
        self.assertEqual(infer_requirements("history of this constituency").intent, "public_history")
        self.assertEqual(infer_requirements("history of election results").intent, "electoral_history")

    def test_specialist_returns_executed_ranking_for_coordinator_to_assess(self):
        req, assignment, _ = self.ranking()
        with tempfile.TemporaryDirectory() as root:
            seat = Path(root) / "seat"
            seat.mkdir()
            with closing(sqlite3.connect(seat / "history.db")) as connection, connection:
                connection.execute("CREATE TABLE form20_2024_lok(area TEXT, votes INTEGER)")
                connection.execute("INSERT INTO form20_2024_lok VALUES('North',400)")
            invalid = "SELECT area AS geography_name,votes AS party_votes,'Opponent' AS opponent_name,100 AS opponent_votes,999 AS margin FROM form20_2024_lok"
            replies = [action("sql", sql=invalid), action("finish")]
            with patch.object(get_settings(), "local_data_root", root), patch.object(runner, "gemini_chat", side_effect=replies) as model:
                result = sql_specialist(assignment, "seat", {}, runner)
            self.assertEqual(result.status, "complete")
            self.assertEqual(result.facts[-1]["rows"][0][-1], 999)
            self.assertEqual(model.call_count, 2)

    def test_expired_deadline_interrupts_sql(self):
        with closing(sqlite3.connect(":memory:")) as connection, execution.execution_scope(-1):
            connection.set_progress_handler(lambda: int(execution.remaining() <= 0), 100)
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<1000000) SELECT SUM(x) FROM n").fetchone()

    def test_failed_synthesis_recovers_verified_ranking(self):
        _, assignment, result = self.ranking()
        with patch.object(runner, "gemini_chat", side_effect=[action("delegate", tasks=[assignment.model_dump()]), "<<LLM_ERROR: deadline_exceeded>>"]), patch("agents.runtime.run_specialist", return_value=result):
            response = runner.run_local_agent("Top regions where BJP was ahead in 2024", "allahabad-south")
        self.assertEqual(response["status"], "partial")
        self.assertIn("margin 300", response["answer"])
        self.assertIn("verified partial", response["answer"])

    def test_no_verified_findings_can_finish_honestly_without_retry_loop(self):
        assignment = task("research", research_query={"public_question": "Historical background of Allahabad South"})
        result = SpecialistResult(task_id="t1", agent="research", status="partial",
                                  facts=[{"no_verified_findings": True}])
        with patch.object(runner, "gemini_chat", side_effect=[action("delegate", tasks=[assignment.model_dump()]),
                action("finish", answer="The retrieved sources did not establish a reliable historical account.", status="partial")]) as model, patch("agents.runtime.run_specialist", return_value=result):
            response = runner.run_local_agent("History of this constituency", "allahabad-south")
        self.assertEqual(response["status"], "partial")
        self.assertEqual(model.call_count, 2)
        self.assertIn("did not establish", response["answer"])

    def test_invalid_contract_gets_one_targeted_repair(self):
        invalid = action("clarify", answer="Which election year?", answer_requirements={"limit": 0})
        valid = action("clarify", answer="Which election year?", answer_requirements={"limit": 10})
        with patch.object(runner, "gemini_chat", side_effect=[invalid, valid]) as model:
            response = runner.run_local_agent("Who won?", "allahabad-south")
        self.assertEqual(response["status"], "needs_clarification")
        self.assertIn("greater_than_equal", model.call_args_list[1].args[0][1]["content"])

    def test_ranking_discovers_data_before_asking_redundant_question(self):
        _, assignment, result = self.ranking()
        replies = [action("clarify", answer="Which region?"), action("delegate", tasks=[assignment.model_dump()]),
                   action("finish", answer="Completed", evidence_task_ids=["t1"])]
        with patch.object(runner, "gemini_chat", side_effect=replies) as model, patch("agents.runtime.run_specialist", return_value=result):
            response = runner.run_local_agent("Top regions where BJP was ahead in 2024", "allahabad-south")
        self.assertEqual(response["status"], "complete")
        self.assertIn("inspect available election", model.call_args_list[1].args[0][1]["content"])

    def test_server_downgrade_explains_the_observed_gap(self):
        assignment = task()
        observation = SpecialistResult(task_id="t1", agent="history", status="partial",
            facts=[{"total": 3}], gaps=["The 2022 results could not be verified; only 2024 is covered."])
        with patch.object(runner, "gemini_chat", side_effect=[action("delegate", tasks=[assignment.model_dump()]),
                action("finish", answer="Three results are available.", evidence_task_ids=["t1"])]), patch("agents.runtime.run_specialist", return_value=observation):
            response = runner.run_local_agent("Compare 2022 and 2024 results", "allahabad-south")
        self.assertEqual(response["status"], "partial")
        self.assertEqual(response["partial_reason"], observation.gaps[0])

    def test_partial_explanation_handles_timeouts_and_hides_diagnostics(self):
        self.assertIn("time limit", partial_reason("", {}, "deadline_exceeded"))
        self.assertNotIn("SQL", partial_reason("SQL schema failed", {}, "repeated_invalid_action"))
        self.assertIn("समय सीमा", partial_reason("", {}, "deadline_exceeded", hindi=True))

    def test_complete_answers_do_not_carry_a_partial_reason(self):
        with patch.object(runner, "gemini_chat", return_value=action("finish", answer_kind="conversation", answer="Hello", partial_reason="Unused")):
            response = runner.run_local_agent("Hello", "allahabad-south")
        self.assertIsNone(response["partial_reason"])

    def test_varied_introductions_finish_without_specialists(self):
        for question in ("hey, what can you do for me?", "Tell me a little about yourself", "आप कौन हैं और मेरी कैसे मदद करेंगे?"):
            with self.subTest(question=question), patch.object(runner, "gemini_chat", return_value=action(
                    "finish", answer_kind="conversation", answer="Hi, I'm Arjun, your AI booth agent.")), patch("agents.runtime.run_specialist") as specialist:
                result = runner.run_local_agent(question, "phulpur")
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["agent_path"], [])
            specialist.assert_not_called()

    def test_conversation_can_answer_while_retaining_pending_data_task(self):
        pending = {"objective": "Compare election results", "tasks": {}, "results": {}, "clarification_question": "Which year?"}
        with patch.object(runner, "gemini_chat", return_value=action("finish", answer_kind="conversation", answer="I can help with booth information and election results.")):
            result = runner.run_local_agent("What can you do for me?", "phulpur", pending_state=pending)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["pending_state"]["objective"], pending["objective"])

    def test_conversation_contract_cannot_claim_tool_references(self):
        with self.assertRaises(ValueError):
            CoordinatorAction(action="finish", answer_kind="conversation", answer="Hello", evidence_task_ids=["invented"])

    def test_explicit_whole_scope_is_not_asked_again(self):
        clarification = CoordinatorAction(action="clarify", answer="Do you want specific booths or a general overview?")
        for question in ("Count booths in the whole constituency", "Across the constituency"):
            with self.subTest(question=question), self.assertRaisesRegex(ValueError, "explicitly requested"):
                validate_clarification(clarification, AnswerRequirements(), False, question)
        validate_clarification(CoordinatorAction(action="clarify", answer="Which election year?"),
                               AnswerRequirements(), False, "Across the constituency")

    def test_repeated_clarification_is_rejected_after_reply(self):
        with self.assertRaisesRegex(ValueError, "already replied"):
            validate_clarification(CoordinatorAction(action="clarify", answer="Which booths?"),
                                   AnswerRequirements(), True, "Across the constituency", "Which booths?")

    def test_source_only_request_blocks_other_specialists(self):
        wrong = task("portfolio", constraints={"booth": "2"})
        right = task("voter")
        result = SpecialistResult(task_id="t1", agent="voter", status="complete", facts=[{"total": 100}])
        with patch.object(runner, "gemini_chat", side_effect=[action("delegate", tasks=[wrong.model_dump()]),
                action("delegate", tasks=[right.model_dump()]), action("finish", answer="100 matching records.", evidence_task_ids=["t1"])]), patch("agents.runtime.run_specialist", return_value=result) as specialist:
            response = runner.run_local_agent("Count records across the constituency, answer from voter db only", "allahabad-south")
        self.assertEqual(specialist.call_count, 1)
        self.assertEqual(specialist.call_args.args[0].agent, "voter")
        self.assertEqual(response["status"], "complete")
        self.assertEqual(source_policy("history database only"), ["history"])

    def test_sparse_or_truncated_observations_do_not_establish_zero(self):
        for fact in ({"rows": [["Station", 1]], "truncated": False}, {"rows": [["Station", 20]], "truncated": True}):
            with self.subTest(fact=fact), self.assertRaisesRegex(ValueError, "cannot establish"):
                validate_zero_claim("No booths were found with matching records.", [{"facts": [fact]}])
        validate_zero_claim("No reliable public sources were found; the answer could not be verified.", [])

    def test_full_zero_count_can_support_absence_despite_prior_sample(self):
        result = {"facts": [{"rows": [["Station", 1]], "truncated": True}, {"source_id": "q2", "rows": [[0]], "truncated": False}],
                  "sources": [{"source_id": "q2", "sql": "SELECT COUNT(*) FROM voters WHERE age > 130"}]}
        validate_zero_claim("No matching records were found.", [result])
        result["sources"][0]["sql"] += " LIMIT 1"
        with self.assertRaises(ValueError):
            validate_zero_claim("No matching records were found.", [result])

    def test_partial_tag_cannot_qualify_an_unsupported_zero(self):
        assignment = task("voter")
        result = SpecialistResult(task_id="t1", agent="voter", status="partial",
            facts=[{"rows": [["Station", 1]], "truncated": True}], gaps=["The complete comparison could not be established."])
        replies = [action("delegate", tasks=[assignment.model_dump()]),
                   action("finish", status="partial", answer="No booths were found.", evidence_task_ids=["t1"]),
                   action("finish", status="partial", answer="I could not determine the matching booth count.", evidence_task_ids=["t1"])]
        with patch.object(runner, "gemini_chat", side_effect=replies), patch("agents.runtime.run_specialist", return_value=result):
            response = runner.run_local_agent("Count matching booths across the constituency", "allahabad-south")
        self.assertIn("could not determine", response["answer"])
        self.assertNotIn("No booths", response["answer"])

    def test_final_aggregate_excludes_truncated_exploration(self):
        with tempfile.TemporaryDirectory() as root:
            seat = Path(root) / "seat"; seat.mkdir()
            with closing(sqlite3.connect(seat / "voters.db")) as connection, connection:
                connection.execute("CREATE TABLE voters(age INTEGER)")
                connection.executemany("INSERT INTO voters VALUES(?)", [(30,)] * 100)
            replies = [action("sql", sql="SELECT age FROM voters"),
                       action("sql", sql="SELECT COUNT(*) AS total FROM voters"),
                       action("finish", evidence_source_ids=["t1:q2"])]
            with patch.object(get_settings(), "local_data_root", root), patch.object(runner, "gemini_chat", side_effect=replies):
                result = sql_specialist(task("voter"), "seat", {}, runner)
            self.assertEqual(result.status, "complete")
            self.assertEqual(len(result.facts), 1)
            self.assertEqual(result.facts[0]["rows"], [[100]])

    def test_source_restriction_supersedes_irrelevant_pending_clarification(self):
        previous = task("portfolio", constraints={})
        pending = {"objective": "Count matching booths across the constituency", "tasks": {"t1": previous.model_dump()},
                   "results": {"t1": SpecialistResult(task_id="t1", agent="portfolio", status="needs_clarification",
                       missing_inputs=["Which booth?"]).model_dump()}, "clarification_question": "Which booth?"}
        current = task("voter").model_copy(update={"task_id": "t2"})
        result = SpecialistResult(task_id="t2", agent="voter", status="complete", facts=[{"total": 10}])
        with patch.object(runner, "gemini_chat", side_effect=[action("delegate", tasks=[current.model_dump()]),
                action("finish", answer="10 matching records.", evidence_task_ids=["t2"])]), patch("agents.runtime.run_specialist", return_value=result):
            response = runner.run_local_agent("Answer from voter db only", "allahabad-south", pending_state=pending)
        self.assertEqual(response["status"], "complete")
        self.assertIsNone(response["pending_state"])


if __name__ == "__main__":
    unittest.main()

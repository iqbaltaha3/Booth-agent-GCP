"""Exercise real LangGraph transitions with fictional evidence and mocked models."""

import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from agents import execution, local_runner as runner
from agents.contracts import SpecialistResult
from agents.runtime import build_graph, Coordinator
from app.auth import get_current_user
from app.config import get_settings
from fastapi import HTTPException


class GraphTests(unittest.TestCase):
    def test_preparation_discards_evidence_from_a_replaced_source(self):
        pending = {"objective": "Describe records", "results": {"old": {
            "agent": "voter", "status": "complete", "facts": [{"total": 9}],
            "sources": [{"database": "voters.db", "data_version": "old-version"}],
        }}}
        with (patch.object(runner, "load_constituency_meta", return_value={}),
              patch("agents.runtime.voter_catalog", return_value={"available": False}),
              patch("agents.runtime.data_version", return_value="new-version"),
              patch.object(runner, "gemini_chat", return_value=json.dumps({
                  "action": "finish", "answer_kind": "conversation", "answer": "Hello"})) as model):
            result = runner.run_local_agent("Hello", "fictional", pending_state=pending)
        self.assertEqual(result["status"], "complete")
        context = json.loads(model.call_args.args[0][-1]["content"].split("\nSpecialist observations:")[0])
        self.assertEqual(result["pending_state"]["results"], {})
        self.assertEqual(context["original_objective"], "Describe records")

    def test_graph_stream_exposes_each_specialist_and_answer_validation(self):
        tasks = [
            {
                "task_id": name,
                "agent": name,
                "objective": "Read fictional source",
                "required_outputs": ["verified facts"],
                "acceptance_criteria": ["source matches"],
            }
            for name in ("voter", "history")
        ]
        replies = [
            json.dumps({"action": "delegate", "tasks": tasks}),
            json.dumps(
                {
                    "action": "finish",
                    "answer": "Two sources checked.",
                    "evidence_task_ids": ["voter", "history"],
                }
            ),
        ]

        def specialist(task, *args):
            return SpecialistResult(
                task_id=task.task_id,
                agent=task.agent,
                status="complete",
                facts=[{"verified": True}],
            )

        with (
            patch.object(runner, "load_constituency_meta", return_value={}),
            patch("agents.runtime.voter_catalog", return_value={"available": False}),
            patch.object(runner, "gemini_chat", side_effect=replies),
            patch("agents.runtime.run_specialist", side_effect=specialist),
            execution.execution_scope(),
        ):
            updates = list(
                build_graph(runner).stream(
                    {
                        "question": "Describe the available records",
                        "constituency_id": "fictional",
                        "session_id": "test",
                        "conversation": [],
                        "pending_state": None,
                        "voter_context": {},
                        "turns": 0,
                        "pending_tasks": [],
                    },
                    config={"recursion_limit": 128},
                    stream_mode="updates",
                )
            )
        nodes = [next(iter(update)) for update in updates]
        self.assertEqual(
            nodes,
            [
                "prepare",
                "coordinator",
                "validate",
                "dispatch",
                "voter",
                "dispatch",
                "history",
                "dispatch",
                "coordinator",
                "validate",
            ],
        )
        result = updates[-1]["validate"]["output"]
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["agent_path"], ["voter_agent", "history_agent"])

    def test_concurrent_invocations_do_not_share_conversation_state(self):
        def answer(messages, **kwargs):
            context = json.loads(
                messages[-1]["content"].split("\nSpecialist observations:")[0]
            )
            return json.dumps(
                {
                    "action": "finish",
                    "answer_kind": "conversation",
                    "answer": "Hello " + context["latest_user_message"],
                }
            )

        with (
            patch.object(runner, "load_constituency_meta", return_value={}),
            patch("agents.runtime.voter_catalog", return_value={"available": False}),
            patch.object(runner, "gemini_chat", side_effect=answer),
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(
                    pool.map(
                        lambda name: runner.run_local_agent(
                            name, "fictional", session_id=name
                        ),
                        ["Alice", "Bob"],
                    )
                )
        self.assertEqual([r["answer"] for r in results], ["Hello Alice", "Hello Bob"])
        self.assertEqual([r["session_id"] for r in results], ["Alice", "Bob"])
        self.assertNotEqual(results[0]["request_id"], results[1]["request_id"])

    def test_fallback_reports_cause_and_request_id_without_raw_errors(self):
        from types import SimpleNamespace
        for terminal, code, phrase in [
            ('turn_limit', 'data_unavailable', 'records needed'),
            ('turn_limit', None, 'step limit'),
            ('deadline_exceeded', None, 'time limit'),
            ('repeated_invalid_action', None, 'repeatedly'),
        ]:
            with self.subTest(terminal=terminal, code=code):
                context = Coordinator({
                    'results': {'v': {'status': 'failed', 'failure_code': code,
                                     'facts': [], 'errors': ['private SQL detail']} } if code else {},
                    'requirements': SimpleNamespace(intent='other'),
                    'terminal': terminal, 'question': 'Describe records',
                    'request_id': 'test-request',
                }, runner)
                context.response = lambda answer, status: {'answer': answer, 'status': status}
                result = context.fallback()
                self.assertIn(phrase, result['answer'])
                self.assertIn('test-request', result['answer'])
                self.assertNotIn('private SQL detail', result['answer'])
                self.assertEqual(result['status'], 'failed')

    def test_authentication_is_required_independently_of_graph_runtime(self):
        with patch.object(get_settings(), "local_dev_auth", False):
            with self.assertRaises(HTTPException) as error:
                get_current_user(None)
        self.assertEqual(error.exception.status_code, 401)
        with patch.object(get_settings(), "local_dev_auth", True):
            self.assertEqual(get_current_user(None)["uid"], "local-dev")


if __name__ == "__main__":
    unittest.main()

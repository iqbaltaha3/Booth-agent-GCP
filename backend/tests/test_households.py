from backend.tests.sql_model_fixture import with_specialist, run_voter_task

"""Synthetic follow-up conversations, household matching and shared session checks."""

import copy
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from agents import local_runner as runner
from agents.contracts import SpecialistTask
from app.config import get_settings
from app.services.sessions import (
    FirestoreSessions,
    SessionBusy,
    SessionSeatMismatch,
)


def assignment(**fields):
    return SpecialistTask.model_validate({"task_id": "t1", "agent": "voter", "objective": "Find household records",
                                         "required_outputs": ["records", "total"],
                                         "acceptance_criteria": ["Use the verified household, preserving scope"], **fields})


class HouseholdTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.settings = get_settings()
        self.previous = self.settings.local_data_root
        self.settings.local_data_root = self.directory.name
        path = Path(self.directory.name) / "seat"
        path.mkdir()
        self.database = path / "voters.db"
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.execute("CREATE TABLE voters(name TEXT,relative_name TEXT,house_no TEXT,polling_station TEXT,epic_id TEXT,region TEXT)")
            conn.executemany("INSERT INTO voters VALUES(?,?,?,?,?,?)", [
                ("ताहा इकबाल", "इकबाल", "796A/1A", "school room no.7", "TEST-1", "east"),
                ("दूसरा व्यक्ति", "अन्य", "796A/1A", "school room no.7", "TEST-2", "east"),
                ("अलग बूथ", "अन्य", "796A/1A", "school room no.8", "TEST-3", "east"),
                ("अलग मकान", "अन्य", "796A1A", "school room no.7", "TEST-4", "east"),
            ])

    def tearDown(self):
        self.settings.local_data_root = self.previous
        self.directory.cleanup()

    def household_result(self, task, slug, data, context):
        return run_voter_task(task, slug, runner, context)[0]

    def test_explicit_station_case_and_spacing_preserve_house_punctuation(self):
        result = self.household_result(
            assignment(
                household_query={
                    "house_no": "796A/1A",
                    "polling_station": " School  Room NO.7 ",
                }
            ),
            "seat",
            runner.gcs_data,
            {},
        )
        self.assertEqual(result.status, "complete")
        self.assertEqual(result.facts[0]["total"], 2)
        self.assertEqual(
            {r["epic_id"] for r in result.facts[1]["records"]}, {"TEST-1", "TEST-2"}
        )

    def test_anchor_exclusion_and_missing_anchor(self):
        result = self.household_result(
            assignment(household_query={"epic_id": "TEST-1", "exclude_anchor": True}),
            "seat",
            runner.gcs_data,
            {},
        )
        self.assertEqual(result.facts[0]["total"], 1)
        self.assertEqual(result.facts[1]["records"][0]["epic_id"], "TEST-2")
        result = self.household_result(
            assignment(household_query={"epic_id": "UNKNOWN"}),
            "seat",
            runner.gcs_data,
            {},
        )
        self.assertEqual(result.status, "needs_clarification")
        self.assertEqual(result.facts, [])

    def test_unknown_station_never_broadens_household(self):
        result = self.household_result(
            assignment(
                household_query={"house_no": "796A/1A", "polling_station": "Unknown"}
            ),
            "seat",
            runner.gcs_data,
            {},
        )
        self.assertEqual(result.status, "needs_clarification")
        self.assertFalse(result.facts)

    def test_name_lookup_station_filter_uses_canonical_values(self):
        query = assignment(
            name_query={
                "operation": "lookup",
                "person": "ताहा इकबाल",
                "filters": {"polling_station": "School  ROOM NO.7"},
            }
        )
        result, _ = run_voter_task(query, "seat", runner)
        self.assertEqual(result.status, "complete")
        self.assertEqual(result.facts[0]["total"], 1)

    def test_parameterized_house_filter_cannot_broaden_the_query(self):
        result = self.household_result(
            assignment(
                household_query={
                    "house_no": "x' OR 1=1 --",
                    "polling_station": "School Room No.7",
                }
            ),
            "seat",
            runner.gcs_data,
            {},
        )
        self.assertEqual(result.status, "complete")
        self.assertEqual(result.facts[0]["total"], 0)

    def lookup_context(self):
        lookup = assignment(name_query={"operation": "lookup", "person": "ताहा इकबाल"})
        with patch.object(runner, "gemini_chat", side_effect=with_specialist([
            json.dumps({"action": "delegate", "tasks": [lookup.model_dump()]}),
            '{"action":"finish","answer":"Found the requested voter.","evidence_task_ids":["t1"]}',
        ])):
            return runner.run_local_agent("Show details of the person", "seat", "sid")

    def test_completed_lookup_retains_reference_and_next_turn_resolves_household(self):
        first = self.lookup_context()
        self.assertIsNone(first["pending_state"])
        context = first["voter_context"]
        selected = context["selected_reference"]
        self.assertEqual(context["references"][selected]["epic_id"], "TEST-1")
        followup = assignment(household_query={"reference": selected})
        with patch.object(runner, "gemini_chat", side_effect=with_specialist([
            json.dumps({"action": "delegate", "tasks": [followup.model_dump()]}),
            '{"action":"finish","answer":"Two matching voter records.","evidence_task_ids":["t1"]}',
        ])) as model:
            second = runner.run_local_agent("Find people in the same house", "seat", "sid", voter_context=context)
        self.assertEqual(second["status"], "complete")
        self.assertEqual(second["voter_context"]["selected_reference"], selected)
        self.assertIn('"total": 2', model.call_args.args[0][1]["content"])
        self.assertTrue(any(item.get("operation") == "household_lookup" for item in second["diagnostics"]))

    def test_household_total_includes_anchor_and_accepts_existing_reference(self):
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.execute("DELETE FROM voters WHERE epic_id='TEST-2'")
        context = self.lookup_context()["voter_context"]
        selected = context["selected_reference"]
        followup = assignment(voter_request={"operation": "household", "reference": selected})
        finish = {"action": "finish", "answer": "There is 1 voter record in this household.",
                  "evidence_task_ids": ["t1"], "selected_voter_reference": selected}
        with patch.object(runner, "gemini_chat", side_effect=with_specialist([
            json.dumps({"action": "delegate", "tasks": [followup.model_dump()]}),
            json.dumps(finish),
        ])):
            result = runner.run_local_agent("Find total voters in the same household", "seat", "sid", voter_context=context)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["answer"], finish["answer"])
        self.assertEqual(result["voter_context"]["selected_reference"], selected)
        self.assertTrue(any(1 in item.get("counts", []) for item in result["diagnostics"]))

    def test_stale_and_cross_constituency_references_are_rejected(self):
        context = self.lookup_context()["voter_context"]
        selected = context["selected_reference"]
        context["references"][selected]["constituency_id"] = "other"
        result = self.household_result(
            assignment(household_query={"reference": selected}),
            "seat",
            runner.gcs_data,
            context,
        )
        self.assertEqual(result.status, "needs_clarification")
        context["references"][selected]["constituency_id"] = "seat"
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.execute("UPDATE voters SET house_no='changed' WHERE epic_id='TEST-1'")
        result = self.household_result(
            assignment(household_query={"reference": selected}),
            "seat",
            runner.gcs_data,
            context,
        )
        self.assertEqual(result.status, "needs_clarification")
        self.assertIn("stale", result.missing_inputs[0])

    def test_multi_person_result_never_selects_first_voter(self):
        query = assignment(name_query={"operation": "lookup", "person": "अन्य", "relative": "", "match_mode": "substring"})
        query.name_query.person = "अलग"
        with patch.object(runner, "gemini_chat", side_effect=with_specialist([json.dumps({"action": "delegate", "tasks": [query.model_dump()]}),
                         '{"action":"finish","answer":"Two people match.","evidence_task_ids":["t1"]}',
                         '{"action":"clarify","answer":"Two voter records match. Which person do you mean?"}'])):
            result = runner.run_local_agent("Find matching people", "seat")
        self.assertEqual(result["status"], "needs_clarification")
        self.assertEqual(len(result["voter_context"]["references"]), 2)
        self.assertIsNone(result["voter_context"]["selected_reference"])


class FakeDocument:
    def __init__(self, records, key):
        self.records, self.key = records, key

    def get(self, transaction=None):
        value = copy.deepcopy(self.records.get(self.key))
        class Snapshot:
            def to_dict(self):
                return value
        return Snapshot()


class FakeFirestore:
    """Storage double; transactional decorator is bypassed only in these unit checks."""
    def __init__(self):
        self.records = {}

    def collection(self, name):
        return self

    def document(self, key):
        return FakeDocument(self.records, key)

    def transaction(self):
        return self

    def set(self, document, value):
        self.records[document.key] = copy.deepcopy(value)

    def update(self, document, values):
        self.records[document.key].update(copy.deepcopy(values))


class SharedSessionTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeFirestore()
        self.first, self.second = FirestoreSessions(self.client), FirestoreSessions(self.client)
        self.patch = patch("google.cloud.firestore.transactional", side_effect=lambda f: f)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def test_other_instance_reads_context_and_cannot_switch_seat(self):
        with self.first.turn("alice", "sid", "seat") as state:
            state["voter_context"] = {"selected_reference": "verified"}
            state["pending_state"] = {"rows": [[1, 2]]}  # JSON avoids Firestore nested-array restrictions.
        with self.second.turn("alice", "sid", "seat") as state:
            self.assertEqual(state["voter_context"]["selected_reference"], "verified")
            self.assertEqual(state["pending_state"]["rows"], [[1, 2]])
        self.assertIsNone(self.second.read("bob", "sid"))
        with self.assertRaises(SessionSeatMismatch):
            with self.second.turn("alice", "sid", "other"):
                pass

    def test_lease_blocks_overlap_and_failed_turn_does_not_commit(self):
        with self.first.turn("alice", "sid", "seat"):
            with self.assertRaises(SessionBusy):
                with self.second.turn("alice", "sid", "seat"):
                    pass
        with self.assertRaises(ValueError):
            with self.first.turn("alice", "sid", "seat") as state:
                state["voter_context"] = {"uncommitted": True}
                raise ValueError("model unavailable")
        self.assertEqual(self.second.read("alice", "sid")["voter_context"], {})
        with self.second.turn("alice", "sid", "seat"):
            pass  # Lease released after failure.

    def test_expired_session_does_not_reuse_old_context(self):
        with self.first.turn("alice", "sid", "seat") as state:
            state["voter_context"] = {"old": True}
        key = self.first.document("alice", "sid").key
        self.client.records[key]["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.assertIsNone(self.second.read("alice", "sid"))


if __name__ == "__main__":
    unittest.main()

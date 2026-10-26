"""Structured lookup checks using fictional records and local artifacts only."""

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from agents.execution import execution_scope
from app.auth import get_current_user
from app.config import get_settings
from app.main import app
from app.services import data


class VoterSearchTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.settings = get_settings()
        self.previous_root = self.settings.local_data_root
        self.settings.local_data_root = str(self.root)
        self.user = {"uid": "alice", "constituency_slugs": ["seat"]}
        app.dependency_overrides[get_current_user] = lambda: self.user
        self.client = TestClient(app)
        seat = self.root / "seat"
        seat.mkdir()
        self.db = seat / "voters.db"
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("CREATE TABLE voters(serial_no INTEGER,name TEXT,relative_name TEXT,relation_type TEXT,house_no TEXT,age INTEGER,gender TEXT,epic_id TEXT,polling_station TEXT,region TEXT,caste TEXT)")
            conn.executemany("INSERT INTO voters VALUES (?,?,?,?,?,?,?,?,?,?,?)", [
                (1, "श्याम लाल", "राम लाल", "Father", "012/क", 40, "Male", "ABC001", "School", "East", "private"),
                (2, "श्याम लाल", "मोहन", "Father", "19", 30, "Male", "ABC002", "School", "East", "private"),
                (3, "श्याम लाल", "राम लाल", "Father", "88", 35, "Male", "ABC003", "College", "West", "private"),
                (4, "फ़रीद", None, None, "12", 45, "Male", "ABC004", "School", "East", "private"),
                (5, "Asha", "Sita", "Mother", "11", 50, "Female", "ABC005", "Unlisted", "East", "private"),
            ])
        (seat / "boothlist.csv").write_text(
            "part_no,pooling_station,region\n1,School,East\n2,College,West\n3,Missing,East\n", encoding="utf-8"
        )
        self.write_coverage({"voter_available": True, "status": "partial"})

    def tearDown(self):
        self.settings.local_data_root = self.previous_root
        app.dependency_overrides.clear()
        self.directory.cleanup()

    def write_coverage(self, value):
        (self.root / "seat" / "coverage.json").write_text(json.dumps(value))

    def search(self, **kwargs):
        return self.client.post("/voter/search", json={"constituency_id": "seat", **kwargs})

    def options(self):
        return self.client.get("/voter/search-options", params={"constituency_id": "seat"})

    def test_epic_alone_normalized_and_private(self):
        response = self.search(epic_id=" abc 001 ")
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(len(result["candidates"]), 1)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["match_tier"], "epic_exact")
        self.assertEqual(candidate["voter"]["house_no"], "012/क")
        self.assertNotIn("caste", candidate["voter"])
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-request-id"], result["request_id"])

    def test_exact_epic_keeps_conflicting_details(self):
        result = self.search(epic_id="ABC001", name="Wrong", house_no="other").json()
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["candidates"][0]["field_matches"], {
            "epic_id": "exact", "name": "different", "house_no": "different"})
        self.assertEqual(self.search(epic_id="ABC001", name="!!!").json()["outcome"], "matches")

    def test_duplicate_normalized_epic_is_not_selected(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE voters SET epic_id='abc 001' WHERE serial_no=2")
        result = self.search(epic_id="ABC001").json()
        self.assertTrue(result["duplicate_epic"])
        self.assertEqual(len(result["candidates"]), 2)
        self.assertNotEqual(result["candidates"][0]["record_id"], result["candidates"][1]["record_id"])

    def test_all_details_before_fallback(self):
        result = self.search(name="श्याम लाल", relative_name="राम लाल", house_no="012/क").json()
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["candidates"][0]["match_tier"], "all_details")

    def test_each_missing_detail_can_be_relaxed(self):
        base = {"name": "श्याम लाल", "relative_name": "राम लाल", "house_no": "012/क"}
        for field in base:
            with self.subTest(field=field):
                result = self.search(**{**base, field: "Wrong"}, booth_id="Part_1").json()
                self.assertEqual(result["outcome"], "matches")
                self.assertEqual(result["candidates"][0]["voter"]["epic_id"], "ABC001")
                self.assertEqual(result["candidates"][0]["field_matches"][field], "different")
                self.assertEqual(result["candidates"][0]["matched_fields"], 2)

    def test_epic_failure_can_use_other_fields(self):
        result = self.search(epic_id="WRONG", name="श्याम लाल", house_no="012/क").json()
        self.assertEqual(result["candidates"][0]["match_tier"], "partial_details")
        self.assertEqual(result["candidates"][0]["field_matches"]["epic_id"], "different")

    def test_scope_never_relaxes_even_for_epic(self):
        for scope in ({"region": "West"}, {"booth_id": "Part_2"}):
            self.assertEqual(self.search(epic_id="ABC001", **scope).json()["outcome"], "no_matches")

    def test_minimum_inputs_and_forbidden_keys(self):
        for payload in ({}, {"name": "श्याम"}, {"house_no": "12"}, {"region": "East"},
                        {"epic_id": "ABC001", "sql": "SELECT * FROM voters"},
                        {"name": "श्याम", "search_mode": "name_only"},
                        {"epic_id": "ABC001", "page_size": 100}):
            self.assertEqual(self.search(**payload).status_code, 422)
        self.assertEqual(self.search(name="श्याम", booth_id="Part_1").status_code, 200)
        self.assertEqual(self.search(name="!!!", house_no="12").json()["error_code"], "invalid_name")

    def test_single_name_requires_explicit_relaxation(self):
        request = dict(name="श्याम लाल", relative_name="Wrong", booth_id="Part_1")
        result = self.search(**request).json()
        self.assertEqual(result["outcome"], "no_matches")
        self.assertIn("name_only", result["suggested_modes"])
        broad = self.search(**request, search_mode="name_only").json()
        self.assertEqual(len(broad["candidates"]), 2)
        self.assertTrue(all(c["match_tier"] == "single_name" for c in broad["candidates"]))
        self.assertTrue(all(c["field_matches"]["relative_name"] == "different" for c in broad["candidates"]))

    def test_name_words_nukta_and_house_preservation(self):
        result = self.search(name="श्याम", house_no="012/क").json()
        self.assertEqual(result["candidates"][0]["field_matches"]["name"], "name_words")
        result = self.search(name="फरीद", house_no="12").json()
        self.assertEqual(result["candidates"][0]["field_matches"]["name"], "exact")
        self.assertEqual(self.search(name="श्याम", house_no="12/क").json()["outcome"], "no_matches")

    def test_missing_relative_is_not_a_disagreement(self):
        result = self.search(epic_id="ABC004", relative_name="राम").json()
        self.assertEqual(result["candidates"][0]["field_matches"]["relative_name"], "not_recorded")

    def test_sql_input_is_literal(self):
        self.assertEqual(self.search(epic_id="' OR 1=1 --").json()["outcome"], "no_matches")
        self.assertEqual(self.search(name="' OR 1=1 --", house_no="12").json()["outcome"], "no_matches")

    def test_options_use_roll_regions_and_report_mapping_state(self):
        response = self.options()
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["regions"], ["East", "West"])
        self.assertEqual([b["mapping_status"] for b in result["booths"]], ["mapped", "mapped", "unmapped"])
        self.assertEqual(result["coverage"]["completeness"], "partial")
        self.assertEqual(self.search(epic_id="ABC001", booth_id="Part_3").json()["error_code"], "booth_mapping_unavailable")
        self.assertEqual(self.search(epic_id="ABC001", booth_id="Part_1", region="West").json()["error_code"], "booth_region_mismatch")

    def test_duplicate_booth_labels_are_ambiguous(self):
        with (self.root / "seat" / "boothlist.csv").open("a") as file:
            file.write("4,School,East\n")
        result = self.options().json()
        self.assertEqual([b["mapping_status"] for b in result["booths"]], ["ambiguous", "mapped", "unmapped", "ambiguous"])
        self.assertEqual(self.search(epic_id="ABC001", booth_id="Part_1").status_code, 422)

    def test_no_booth_list_does_not_prevent_epic_search(self):
        (self.root / "seat" / "boothlist.csv").unlink()
        self.assertFalse(self.options().json()["booth_list_available"])
        self.assertEqual(self.search(epic_id="ABC001").json()["outcome"], "matches")

    def test_coverage_unavailable_unknown_and_missing_database(self):
        self.write_coverage({"voter_available": False})
        with patch.object(data, "open_sqlite", side_effect=AssertionError("must not open")):
            self.assertEqual(self.search(epic_id="ABC001").json()["outcome"], "unavailable")
            self.assertEqual(self.options().json()["coverage"]["availability"], "unavailable")
        (self.root / "seat" / "coverage.json").unlink()
        self.assertEqual(self.search(epic_id="ABC001").json()["coverage"], {"availability": "unknown", "completeness": "unverified"})
        self.db.unlink()
        response = self.search(epic_id="ABC001")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error_code"], "data_unavailable")

    def test_access_checked_before_data_read(self):
        self.user["constituency_slugs"] = ["other"]
        with patch.object(data, "load_coverage", side_effect=AssertionError("must not read")):
            self.assertEqual(self.search(epic_id="ABC001").status_code, 403)
            self.assertEqual(self.options().status_code, 403)
        app.dependency_overrides.clear()
        with patch.object(self.settings, "local_dev_auth", False):
            self.assertEqual(self.search(epic_id="ABC001").status_code, 401)

    def test_spelling_assistance_is_optional_and_bounded(self):
        with patch("agents.model.chat", return_value='{"variants":["श्याम लाल"]}') as model:
            plain = self.search(name="Shyam Lal", house_no="012/क").json()
            self.assertEqual(plain["outcome"], "no_matches")
            model.assert_not_called()
            assisted = self.search(name="Shyam Lal", house_no="012/क", spelling_assistance=True).json()
            self.assertEqual(assisted["outcome"], "matches")
            self.assertEqual(assisted["candidates"][0]["field_matches"]["name"], "spelling_variant")
            self.assertNotIn("012/क", json.dumps(model.call_args.args, ensure_ascii=False))
            self.assertIn("deadline", model.call_args.kwargs)
        with patch("agents.model.chat", return_value="<<LLM_ERROR: unavailable>>"):
            result = self.search(name="Asha", house_no="11", spelling_assistance=True).json()
            self.assertEqual(result["outcome"], "matches")
            self.assertEqual(result["spelling_status"], "unavailable")
        with patch("agents.model.chat", side_effect=RuntimeError("provider failed")):
            result = self.search(name="Asha", house_no="11", spelling_assistance=True).json()
            self.assertEqual(result["outcome"], "matches")
            self.assertEqual(result["spelling_status"], "unavailable")

    def test_exact_names_rank_before_word_matches(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO voters(name,relative_name,house_no,epic_id,polling_station,region) VALUES ('श्याम','राम लाल','19','ABC006','School','East')")
        result = self.search(name="श्याम", relative_name="राम लाल", booth_id="Part_1").json()
        self.assertEqual([c["voter"]["epic_id"] for c in result["candidates"]], ["ABC006", "ABC001"])
        self.assertEqual([c["match_tier"] for c in result["candidates"]], ["all_details", "partial_name"])

    def test_result_cap_never_claims_global_ranking(self):
        with patch("app.services.voter_search.CANDIDATE_LIMIT", 1):
            result = self.search(name="श्याम", booth_id="Part_1").json()
        self.assertEqual(result["outcome"], "refine_search")
        self.assertTrue(result["truncated"])
        self.assertEqual(result["candidates"], [])
        result = self.search(name="श्याम", booth_id="Part_1", page_size=1).json()
        self.assertEqual(len(result["candidates"]), 1)
        self.assertTrue(result["truncated"])

    def test_deadline_errors_do_not_become_no_matches(self):
        with patch("app.services.voter_search.search", side_effect=sqlite3.OperationalError("interrupted")):
            result = self.search(epic_id="ABC001")
        self.assertEqual(result.status_code, 504)
        self.assertEqual(result.json()["error_code"], "deadline_exceeded")
        with execution_scope(seconds=0):
            with closing(data.open_sqlite("seat", "voters.db")) as conn:
                with self.assertRaises(sqlite3.OperationalError):
                    conn.execute("WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<100000) SELECT SUM(x) FROM n").fetchall()

    def test_source_change_rejects_results(self):
        with patch("app.services.voter_search.data_version", side_effect=["before", "after"]):
            result = self.search(epic_id="ABC001")
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.json()["error_code"], "source_changed")


if __name__ == "__main__":
    unittest.main()

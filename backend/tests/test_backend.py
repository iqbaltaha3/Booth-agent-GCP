"""Regression checks run without cloud credentials or real voter data."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import closing

from app.auth import get_current_user
from app.config import get_settings
from app.main import app
from app.routes import chat
from app.services import data
from fastapi.testclient import TestClient


class BackendTests(unittest.TestCase):
    """Exercise API contracts and access boundaries with temporary fixtures."""

    def setUp(self):
        """Create two isolated constituency folders and a fake signed-in user."""
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.settings = get_settings()
        self.previous_root = self.settings.local_data_root
        self.settings.local_data_root = str(self.root)
        self.user = {"uid": "alice", "constituency_slugs": ["seat"]}
        app.dependency_overrides[get_current_user] = lambda: self.user
        self.client = TestClient(app)
        seat = self.root / "seat"
        (seat / "booth_analysis").mkdir(parents=True)
        for filename in ("voters.db", "history.db"):
            with closing(sqlite3.connect(seat / filename)) as connection, connection:
                connection.execute("CREATE TABLE voters (gender TEXT)")
                connection.executemany("INSERT INTO voters VALUES (?)", [("F",), ("M",)])
        (seat / "boothlist.csv").write_text(
            "part_no,pooling_station,region,total_voters\n2,School,East,100\n10,College,East,200\n"
        )
        (seat / "booth_analysis/part2.json").write_text(
            json.dumps({"booth_metadata": {"booth_name": "Part_2"}})
        )

    def tearDown(self):
        """Restore application settings and clear test conversations."""
        self.settings.local_data_root = self.previous_root
        app.dependency_overrides.clear()
        chat.get_store().clear()
        self.directory.cleanup()

    def test_account_manifest(self):
        """Keep profile aliases and filter the seat selector using permissions."""
        with patch("app.routes.account.allowed_constituencies", return_value=[{"slug": "seat"}]):
            for method, path in (("get", "/auth/me"), ("post", "/auth/bootstrap")):
                response = getattr(self.client, method)(path)
                self.assertEqual(response.json()["uid"], "alice")
            self.assertEqual(self.client.get("/constituencies").json(), [{"slug": "seat"}])

    def test_invalid_token_never_grants_local_admin(self):
        """An authentication initialization failure returns an error even locally."""
        from fastapi import HTTPException

        with patch("app.auth.firebase_app", side_effect=RuntimeError("unavailable")):
            with self.assertRaises(HTTPException) as error:
                get_current_user("Bearer fake")
        self.assertEqual(error.exception.status_code, 503)

    def test_cloud_download_cache(self):
        """Reuse downloads without contacting Google or reading real cloud data."""
        from unittest.mock import Mock

        client = Mock()

        def download(filename, **kwargs):
            Path(filename).write_text("fixture")

        client.bucket.return_value.blob.return_value.download_to_filename.side_effect = download
        data._download.cache_clear()
        with (
            patch("app.services.data._local_root", return_value=None),
            patch("app.services.data._gcs_client", return_value=client),
        ):
            first = data.resolve_data_path("seat", "boothlist.csv")
            self.assertEqual(first, data.resolve_data_path("seat", "boothlist.csv"))
            client.bucket.return_value.blob.return_value.download_to_filename.assert_called_once()
        first.unlink()
        first.parent.rmdir()
        data._download.cache_clear()

    def test_query_contract_and_access(self):
        """Preserve both query URLs and deny an unauthorized seat."""
        for prefix in ("voter", "history"):
            response = self.client.post(
                f"/{prefix}/query",
                json={"constituency_id": "seat", "sql": "SELECT COUNT(*) AS total FROM voters"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response.json(), {"columns": ["total"], "rows": [[2]], "row_count": 1, "truncated": False}
            )
        response = self.client.get("/voter/schema", params={"constituency_id": "other"})
        self.assertEqual(response.status_code, 403)

    def test_read_only_and_truncation(self):
        """Writes fail at SQLite level and exactly-full results are not truncated."""
        connection = data.open_sqlite("seat", "voters.db")
        try:
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("DELETE FROM voters")
            self.assertFalse(data.execute_select(connection, "SELECT * FROM voters", 2)["truncated"])
            self.assertTrue(data.execute_select(connection, "SELECT * FROM voters", 1)["truncated"])
        finally:
            connection.close()

    def test_no_shared_folder_fallback(self):
        """A missing seat cannot silently read a shared database."""
        (self.root / "voters.db").touch()
        with self.assertRaises(FileNotFoundError):
            data.resolve_data_path("missing", "voters.db")
        with self.assertRaises(ValueError):
            data.resolve_data_path("seat", "../voters.db")

    def test_booth_contracts(self):
        """Numeric sorting, lookup, and report retrieval work with numeric CSV parts."""
        params = {"constituency_id": "seat"}
        booths = self.client.get("/portfolio/booths", params=params).json()
        self.assertEqual([item["part_number"] for item in booths["regions"][0]["booths"]], ["2", "10"])
        matches = self.client.get("/portfolio/search", params={**params, "q": "Part_2"}).json()
        self.assertEqual(matches["matches"][0]["booth_name"], "School")
        report = self.client.get("/portfolio/2", params=params)
        self.assertEqual(report.status_code, 200)
        self.assertEqual(report.json()["source_file"], "part2.json")

    def test_history_is_private(self):
        """Another user cannot retrieve a conversation by knowing its id."""
        with patch(
            "app.routes.chat.run_chat", return_value={"answer": "Two voters", "session_id": "session"}
        ):
            self.assertEqual(
                self.client.post(
                    "/agent/chat", json={"question": "How many?", "constituency_id": "seat", "session_id": "session"}
                ).status_code,
                200,
            )
        self.assertEqual(len(self.client.get("/agent/sessions/session/history").json()["turns"]), 2)
        self.user["uid"] = "bob"
        self.assertEqual(self.client.get("/agent/sessions/session/history").json()["turns"], [])

    def test_clarification_state_is_private_and_resumed(self):
        """A reply gets its own prior turns and pending state, never another user's."""
        pending = {"objective": "Who won?", "tasks": {}, "results": {}}
        with patch("app.routes.chat.run_chat", side_effect=[
            {"answer": "Which year?", "status": "needs_clarification", "pending_state": pending},
            {"answer": "A won.", "status": "complete"},
            {"answer": "Hello", "status": "complete"},
        ]) as runtime:
            response = self.client.post("/agent/chat", json={"question": "Who won?", "constituency_id": "seat"})
            sid = response.json()["session_id"]
            self.assertEqual(response.json()["status"], "needs_clarification")
            self.assertNotIn("pending_state", response.json())
            response = self.client.post("/agent/chat", json={"question": "2022", "constituency_id": "seat", "session_id": sid})
            self.assertEqual(response.json()["status"], "complete")
            self.assertEqual(runtime.call_args.kwargs["pending_state"], pending)
            self.assertEqual(len(runtime.call_args.kwargs["history"]), 2)
            self.assertIsNone(chat.get_store().read("alice", sid)["pending_state"])
            self.user["uid"] = "bob"
            self.client.post("/agent/chat", json={"question": "Hello", "constituency_id": "seat", "session_id": sid})
            self.assertEqual(runtime.call_args.kwargs["history"], [])
            self.assertIsNone(runtime.call_args.kwargs["pending_state"])

    def test_session_cannot_switch_constituency(self):
        """Even access to both seats cannot move a pending task to a different seat."""
        self.user["constituency_slugs"] = ["seat", "other"]
        with patch("app.routes.chat.run_chat", return_value={"answer": "Which year?"}) as runtime:
            self.client.post("/agent/chat", json={"question": "Who won?", "constituency_id": "seat", "session_id": "sid"})
            response = self.client.post("/agent/chat", json={"question": "2022", "constituency_id": "other", "session_id": "sid"})
            self.assertEqual(response.status_code, 403)
            self.assertEqual(runtime.call_count, 1)

    def test_completed_voter_context_survives_the_next_api_turn(self):
        context = {"selected_reference": "verified-person", "references": {}}
        with patch("app.routes.chat.run_chat", side_effect=[
            {"answer": "Found the person", "status": "complete", "voter_context": context},
            {"answer": "Two records", "status": "complete", "voter_context": context},
        ]) as runtime:
            self.client.post("/agent/chat", json={"question": "Show details", "constituency_id": "seat", "session_id": "sid"})
            response = self.client.post("/agent/chat", json={"question": "Same house", "constituency_id": "seat", "session_id": "sid"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(runtime.call_args.kwargs["voter_context"], context)
            self.assertNotIn("voter_context", response.json())

    def test_storage_failure_never_falls_back_to_empty_memory(self):
        with patch("app.routes.chat.get_store", side_effect=RuntimeError("storage unavailable")), patch("app.routes.chat.run_chat") as runtime:
            response = self.client.post("/agent/chat", json={"question": "Same house", "constituency_id": "seat"})
        self.assertEqual(response.status_code, 503)
        runtime.assert_not_called()

    def test_partial_explanation_survives_chat_and_history(self):
        explanation = "Booth results are available, but their region mapping could not be verified."
        with patch("app.routes.chat.run_chat", return_value={"answer": "Available booth results.", "status": "partial", "partial_reason": explanation}):
            result = self.client.post("/agent/chat", json={"question": "Top regions?", "constituency_id": "seat", "session_id": "partial-sid"})
        self.assertEqual(result.json()["partial_reason"], explanation)
        history = self.client.get("/agent/sessions/partial-sid/history").json()
        self.assertEqual(history["turns"][-1]["partial_reason"], explanation)
        self.assertEqual(history["turns"][-1]["status"], "partial")

    def test_web_sources_survive_chat_and_history_without_extracted_content(self):
        source = {"source_id": "R123456789abc", "title": "Fictional public notice", "url": "https://example.com/notice",
                  "published_at": None, "searched_at": "2026-10-16T00:00:00Z", "read_status": "page_read",
                  "source_type": "official", "page_text": "Do not expose raw extraction"}
        with patch("app.routes.chat.run_chat", return_value={"answer": "Notice [R123456789abc].", "sources": [source]}):
            result = self.client.post("/agent/chat", json={"question": "Public notices?", "constituency_id": "seat", "session_id": "web-sid"})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["sources"][0]["url"], "https://example.com/notice")
        self.assertNotIn("page_text", result.json()["sources"][0])
        history = self.client.get("/agent/sessions/web-sid/history").json()
        self.assertNotIn("page_text", history["turns"][-1]["sources"][0])

    def test_voice_contracts(self):
        """Voice endpoints accept browser uploads and return WAV bytes."""
        with patch("app.routes.voice.speech.transcribe", return_value=("Hello", "en-IN")):
            response = self.client.post(
                "/voice/transcribe", files={"file": ("voice.wav", b"audio", "audio/wav")}
            )
            self.assertEqual(response.json(), {"text": "Hello", "language": "en-IN"})
        with patch("app.routes.voice.speech.synthesize", return_value=b"audio"):
            response = self.client.post("/voice/synthesize", json={"text": "Hello"})
            self.assertEqual(response.content, b"audio")
            self.assertEqual(response.headers["content-type"], "audio/wav")


if __name__ == "__main__":
    unittest.main()

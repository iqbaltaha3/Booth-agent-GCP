"""Research integration checks use fictional sources and mocked APIs, never live keys."""

import json
import time
import unittest
from datetime import datetime
from unittest.mock import patch, Mock
from zoneinfo import ZoneInfo

from agents import local_runner as runner
from agents import research
from agents.contracts import SpecialistTask, SpecialistResult
from app.config import get_settings

META = {"name": "Allahabad North", "district": "Prayagraj", "state": "Uttar Pradesh"}


def task(**query):
    return SpecialistTask(task_id="web1", agent="research", objective="Research public water notices",
                          required_outputs=["findings", "sources"], acceptance_criteria=["Cite read sources; preserve locality and dates"],
                          research_query={"public_question": "What water notices were reported in Prayagraj?", **query})


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.settings = get_settings()
        self.previous = self.settings.tavily_api_key
        self.settings.tavily_api_key = "synthetic-key"
        self.calls = []
        self.today = datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()

    def tearDown(self):
        self.settings.tavily_api_key = self.previous

    def provider(self, endpoint, payload, key, deadline):
        self.calls.append((endpoint, payload))
        if endpoint == "search":
            return {"results": [{"url": "https://prayagraj.nic.in/fictional-water-notice", "title": "Fictional water notice",
                                 "content": "Unverified lead", "published_date": self.today}]}
        return {"results": [{"url": "https://prayagraj.nic.in/fictional-water-notice", "raw_content": "Fictional district notice: water maintenance is planned."}]}

    def model(self, messages, **kwargs):
        system = messages[0]["content"]
        if '"title": "SearchPlan"' in system:
            return '{"queries":["Prayagraj water notice","प्रयागराज पानी सूचना"]}'
        if '"title": "Ranking"' in system:
            return '{"ranking":[{"index":0,"score":90}]}'
        context = json.loads(messages[1]["content"])
        source_id = context["evidence"][0]["source_id"]
        return json.dumps({"findings": [{"text": "A fictional district notice describes planned water maintenance.",
                         "source_ids": [source_id], "kind": "official_statement", "geography_scope": "district", "event_date": self.today}],
                         "limitations": ["District scope does not establish constituency coverage."]})

    def execute(self, query=None, provider=None, model=None, meta=None):
        with patch.object(research, "tavily", side_effect=provider or self.provider), patch.object(runner, "gemini_chat", side_effect=model or self.model):
            return research.research_specialist(query or task(), "allahabad-north", meta or META, runner)

    def test_retrieval_uses_existing_model_and_read_sources(self):
        result = self.execute()
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.sources[0]["read_status"], "page_read")
        self.assertEqual(result.facts[0]["source_ids"], [result.sources[0]["source_id"]])
        self.assertEqual(result.facts[0]["geography_scope"], "district")
        searches = [payload for endpoint, payload in self.calls if endpoint == "search"]
        self.assertEqual(len(searches), 4)
        self.assertTrue(all("start_date" in p and "end_date" in p for p in searches))
        self.assertNotIn("page_text", result.sources[0])

    def test_historical_research_does_not_add_news_window(self):
        self.execute(task(time_sensitive=False, public_question="What historical demographic tables describe Prayagraj in 2011?"))
        self.assertTrue(all("start_date" not in p for endpoint, p in self.calls if endpoint == "search"))

    def test_explicit_window_is_preserved(self):
        self.execute(task(start_date="2020-01-01", end_date="2020-01-31"))
        self.assertTrue(all(p["start_date"] == "2020-01-01" and p["end_date"] == "2020-01-31"
                            for endpoint, p in self.calls if endpoint == "search"))

    def test_unread_snippets_never_become_findings(self):
        def provider(endpoint, *args):
            return self.provider(endpoint, *args) if endpoint == "search" else {"results": [], "failed_results": [{}]}
        result = self.execute(provider=provider)
        self.assertEqual(result.status, "partial")
        self.assertTrue(result.facts[0]["search_leads_only"])
        self.assertEqual(result.sources[0]["read_status"], "snippet_only")

    def test_invalid_finding_citations_are_rejected(self):
        def model(messages, **kwargs):
            value = json.loads(self.model(messages, **kwargs))
            if "findings" in value:
                value["findings"][0]["source_ids"] = ["invented"]
            return json.dumps(value)
        result = self.execute(model=model)
        self.assertTrue(result.facts[0]["no_verified_findings"])
        self.assertTrue(any("Rejected" in warning for warning in result.limitations))

    def test_unknown_dates_not_called_recent(self):
        def provider(endpoint, *args):
            value = self.provider(endpoint, *args)
            if endpoint == "search":
                value["results"][0]["published_date"] = None
            return value
        def model(messages, **kwargs):
            value = json.loads(self.model(messages, **kwargs))
            if "findings" in value:
                value["findings"][0]["event_date"] = None
            return json.dumps(value)
        result = self.execute(provider=provider, model=model)
        self.assertTrue(result.facts[0]["no_verified_findings"])

    def test_missing_key_wrong_region_and_private_lookup_do_not_search(self):
        self.settings.tavily_api_key = ""
        self.assertEqual(self.execute().status, "failed")
        self.settings.tavily_api_key = "synthetic-key"
        self.assertEqual(self.execute(meta={**META, "district": "Bhadohi"}).status, "failed")
        self.assertEqual(self.execute(task(public_question="Find EPIC ABC1234567 and house number details")).status, "failed")
        self.assertEqual(self.calls, [])

    def test_provider_failures_do_not_establish_absence(self):
        def fail(*args):
            raise RuntimeError("Tavily unavailable")
        result = self.execute(provider=fail)
        self.assertEqual(result.status, "failed")
        self.assertIn("does not establish", result.errors[0])

    def test_invalid_ranking_indices_are_ignored(self):
        def model(messages, **kwargs):
            if '"title": "Ranking"' in messages[0]["content"]:
                return '{"ranking":[{"index":999,"score":100},{"index":0,"score":90},{"index":0,"score":80}]}'
            return self.model(messages, **kwargs)
        result = self.execute(model=model)
        self.assertEqual(len(result.sources), 1)

    def test_public_url_validation(self):
        for url in ("file:///etc/passwd", "http://localhost/a", "http://127.0.0.1/a", "http://10.0.0.1/a", "http://[::1]/a", "https://user:secret@example.com/a"):
            self.assertIsNone(research.canonical(url))
        self.assertEqual(research.canonical("https://example.com/a/?utm_source=x&v=1#top"), "https://example.com/a?v=1")

    def test_deadline_stops_new_requests_and_credential_failures_are_sanitized(self):
        with patch.object(research.requests, "post") as request:
            with self.assertRaises(TimeoutError):
                research.tavily("search", {}, "synthetic-secret", time.monotonic() - 1)
            request.assert_not_called()
        with patch.object(research.requests, "post", return_value=Mock(ok=False, status_code=401)):
            with self.assertRaises(RuntimeError) as error:
                research.tavily("search", {}, "synthetic-secret", time.monotonic() + 30)
        self.assertNotIn("synthetic-secret", str(error.exception))
        self.assertIn("401", str(error.exception))

    def test_historical_reference_period_does_not_filter_publication_dates(self):
        self.execute(task(time_sensitive=False, start_date="2011-01-01", end_date="2011-12-31"))
        self.assertTrue(all("start_date" not in p for endpoint, p in self.calls if endpoint == "search"))

    def test_ghaziabad_locality_is_not_substituted_with_kaushambi_district(self):
        result = self.execute(task(locality="Kaushambi, Ghaziabad", scope="locality"))
        self.assertEqual(result.status, "failed")
        self.assertEqual(self.calls, [])

    def test_coordinator_preserves_citations_and_public_input_isolated(self):
        source = {"source_id": "R123456789abc", "url": "https://example.com/notice", "title": "Fictional notice",
                  "published_at": self.today, "searched_at": self.today, "source_type": "official", "read_status": "page_read"}
        observation = SpecialistResult(task_id="web1", agent="research", status="partial",
                    facts=[{"text": "Fictional maintenance planned", "source_ids": [source["source_id"]]}], sources=[source])
        plans = [json.dumps({"action": "delegate", "tasks": [task().model_dump(mode="json")]}),
                 '{"action":"finish","answer":"Maintenance is planned [R123456789abc].","evidence_task_ids":["web1"]}']
        with patch.object(runner, "gemini_chat", side_effect=plans), patch("agents.runtime.research_specialist", return_value=observation) as specialist:
            result = runner.run_local_agent("Any current water notices?", "allahabad-north", voter_context={"private": "not-for-research"})
        self.assertEqual(result["sources"], [source])
        self.assertEqual(result["agent_path"], ["research_agent"])
        self.assertEqual(len(specialist.call_args.args), 4)  # No history, voter context or private observations.

    def test_final_missing_or_invented_citations_require_correction(self):
        source = {"source_id": "R123456789abc", "url": "https://example.com/notice", "title": "Fictional",
                  "published_at": None, "searched_at": self.today, "read_status": "page_read", "source_type": "reporting"}
        observation = SpecialistResult(task_id="web1", agent="research", status="partial",
                      facts=[{"text": "Fictional report", "source_ids": [source["source_id"]]}], sources=[source])
        replies = [json.dumps({"action": "delegate", "tasks": [task().model_dump(mode="json")]}),
                   '{"action":"finish","answer":"A report exists.","evidence_task_ids":["web1"]}',
                   '{"action":"finish","answer":"A report exists [R000000000000].","evidence_task_ids":["web1"]}',
                   '{"action":"finish","answer":"A report exists [R123456789abc].","evidence_task_ids":["web1"]}']
        with patch.object(runner, "gemini_chat", side_effect=replies) as model, patch("agents.runtime.research_specialist", return_value=observation):
            result = runner.run_local_agent("Recent reports?", "allahabad-north")
        self.assertEqual(model.call_count, 4)
        self.assertEqual(len(result["sources"]), 1)


if __name__ == "__main__":
    unittest.main()

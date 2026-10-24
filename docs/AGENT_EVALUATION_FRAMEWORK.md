# Evaluation framework for Arjun

Research and repository review: 19 October 2026.

This is an evaluation design proposal, not an implemented evaluation service. The architecture description was updated during the LangChain/LangGraph migration; the historical observations below predate that migration. Findings below come from source inspection. No live model, speech, search, production database, or deployed service evaluation was run. Existing unrelated working-tree changes were left alone.

Migration validation: 162 offline regression tests pass under Python 3.11 with
LangChain 1.4.6, LangGraph 1.2.16 and langchain-google-genai 4.4.2. Dependency
compatibility checks pass. These tests include graph transitions, concurrent
invocation isolation and a real LangChain adapter with a mocked Google client;
they do not measure live model/search/speech quality. Deployment configurations
now run offline tests before backend builds. Production versions are pinned in
`backend/requirements.lock`.

## 1. What the system actually contains

The implementation now uses LangGraph orchestration in `agents/runtime.py` and
LangChain's Gemini integration in `agents/model.py`. FastAPI invokes that graph
in-process; the private API session store remains responsible for durable state.
The earlier proposed remote-engine integration has been removed.

```text
Typed question                  Audio upload
      |                              |
      |                         Sarvam STT
      +------------------------------+
                     |
           POST /agent/chat
           Auth + seat access
           Private session store
                     |
          run_chat / local_runner
                     |
       Coordinator: run_coordinator
         |         |          |          |
       Voter    History    Portfolio   Research
         |         |          |          |
     voters.db history.db  booth JSON Tavily search/extract
         +---------+----------+----------+
                     |
       Evidence checks + final synthesis
                     |
          Text, sources, status
                     |
             Optional Sarvam TTS
```

Implementation anchors:

| Part | Source | Actual responsibility |
|---|---|---|
| Coordinator | `agents/runtime.py:build_graph`, `Coordinator`, `run_coordinator` | Interprets intent and context, delegates typed tasks, validates evidence, clarifies, synthesizes and reports completion. Eight coordinator iterations and eight specialist calls are the configured caps. |
| Voter | `agents/runtime.py:run_specialist`, `sql_specialist`; `agents/voter.py` | Resolves record filters, populations, names and household anchors; writes SQL through the model; validates and executes it; updates verified context. |
| History | `agents/runtime.py:sql_specialist`; `agents/elections.py` | Reads Form-20 sources, checks requested election period/type, supports comparisons and rankings, and preserves assembly-segment scope. |
| Booth Analysis / Portfolio | `agents/runtime.py:portfolio_specialist` | Resolves a booth, loads the matching prepared JSON and selects sections. Retrieval is largely deterministic; the coordinator explains the report. |
| Internet / Research | `agents/research.py:research_specialist` | Plans searches, calls Tavily, ranks leads, reads pages, extracts attributed findings and checks dates, references and geography. Currently limited to a district pilot in Uttar Pradesh. |
| Contracts and completion | `agents/contracts.py`, `completion.py`, `presentation.py` | Defines the agent message shapes, handles conversation state and explains partial results. Voter and History choose their own SQL; the runtime executes read-only queries and returns observations or database errors for repair. |
| Data and isolation | `backend/app/auth.py`, `services/data.py`, `services/sql_safety.py` | Seat authorization, scoped paths, read-only SQLite, bounded results, coverage and cloud caching. |
| Conversation state | `backend/app/services/sessions.py`, `routes/chat.py`; `agents/households.py` | User/seat isolation, leases, pending clarification, verified identities, source versions, history and private traces. |
| Voice | `backend/app/routes/voice.py`, `services/speech.py`; frontend voice/API/page code | Authenticated audio upload and playback around the text agent. Defaults name Sarvam `saaras:v3` and `bulbul:v3`; these are repository defaults, not verified deployed provider settings. |

Names, households, category resolution and SQL validation are components inside the Voter graph node, not additional independently routed agents. Charts are currently returned as an empty list by the local coordinator; there is no implemented chart-generation agent to score yet.

## 2. What current evaluation practice contributes

Use several methods together. Each answers a different question:

| Evaluation method | Meaning | Suitable here |
|---|---|---|
| Deterministic reference grading | Code compares actual values, sets, statuses or state with an independent expected result. | Counts, filters, election arithmetic, row identity, contracts, authorization. |
| Trajectory evaluation | Inspect observable decisions, tool arguments and evidence flow before the answer. | Routing, source restrictions, repair loops, duplicate calls and search stages. |
| Multi-turn evaluation | Score a whole conversation and state changes. | Refinement, resets, clarification, pronouns, selected people and changing years. |
| Evidence-grounded model judging | A separate model applies explicit criteria to response and evidence. | Unsupported claims, omitted qualifications, research attribution and explanation quality. |
| Human evaluation | Domain experts or native speakers review blind examples. | Ambiguity, factual source interpretation, language and speech intelligibility. |
| Adversarial evaluation | Deliberately attempt to break boundaries. | Prompt injection, private-data export, source spoofing, cross-seat access and malicious SQL. |
| Metamorphic/property testing | Check relationships that should remain true after controlled changes. | Reordering rows, changing names, stronger filters, threshold boundaries and equivalent paraphrases. |
| Fault injection | Simulate unavailable, slow, malformed or incomplete dependencies. | Provider errors, storage failures, truncated output and unavailable data. |
| Operational evaluation | Measure quality with latency, usage, load and costs. | Performance budgets and cost per successful task. |

Anthropic's agent-evaluation guidance separates tasks, trials, transcripts, outcomes and graders, and recommends combining code, model and human grading. Apply this by keeping task definitions separate from runner and grader implementations. [Source](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents).

LangGraph exposes explicit state, nodes and conditional edges. Evaluate the
coordinator, validation and specialist nodes individually, then score the whole
graph's final response and conversation state. [Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api).


The τ-bench paper evaluates conversation outcomes against reference state and introduces pass^k for repeated reliability. For this application, a complete conversation must pass all required turns and state assertions; report repeated success, not only the best run. [Paper](https://arxiv.org/abs/2406.12045).

Ragas defines faithfulness as the fraction of response claims supported by retrieved context. This is relevant to research and synthesis, but supported claims can still come from the wrong geography or an inaccurate source. Grade source validity, task scope and answer coverage separately. [Faithfulness](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/faithfulness/).

For speech, WER measures transcription edits against reference words; intelligibility and listener ratings such as MOS assess synthesis quality. Add application-specific checks for numbers, names and places because these can change a query's meaning despite a low overall WER. [NIST ASR evaluation](https://www.nist.gov/system/files/documents/itl/iad/mig/OpenKWS13-evalplan-v4-comp-v3.pdf), [Microsoft TTS evaluation](https://learn.microsoft.com/en-us/azure/foundry/responsible-ai/speech-service/text-to-speech/transparency-note).

The framework below is a repository-specific recommendation, rather than a claim that any source prescribes these exact datasets or thresholds.

## 3. Complete inventory of evaluation surfaces

P0 means a correctness or boundary failure that should block release. P1 means core task quality. P2 means experience and optimization. Existing tests should be retained; an item appearing here does not imply it is entirely untested.

### Coordinator and agent interactions

| ID / priority | Part and description | Evaluation kind | What to measure / reference |
|---|---|---|---|
| C1 / P1 | Intent and route: stored election results, public history, live records and prepared reports require different sources. | Labeled routing + trajectory | Route-set accuracy, per-agent precision/recall and confusion matrix. Allow several valid routes for genuinely mixed requests. |
| C2 / P0 | Request interpretation: preserve operation, person, filters, year/type, party, metric, limit and geography. | Structured semantic comparison | Compare parsed domain requests with human-authored expected fields, including defaults and omissions. Schema validity alone is insufficient. |
| C3 / P1 | Task decomposition: split multi-source questions into sufficient assignments. | Trajectory + reference rubric | Required subtasks covered, dependencies respected, no unnecessary calls; all requested outputs supported. |
| C4 / P1 | Clarification: ask when ambiguity prevents a correct answer. | Labeled cases + multi-turn | Necessary clarification recall, avoidable clarification rate, ambiguity resolution and successful resume. Include acceptable answer/clarify alternatives. |
| C5 / P0 | Evidence gate: data answers must follow successful observations. | Deterministic trace assertions + adversarial | Unknown task/source references, unsupported finish, conversational bypass and rejected-source reuse. |
| C6 / P0 | Synthesis: the final answer must preserve exact numbers, entities, scope and caveats. | Exact claim checks + evidence-grounded judge + human audit | Numerical/identity accuracy, supported-claim precision, required-fact recall and contradiction rate. A correct number in a wrong sentence fails. |
| C7 / P0 | Completion: complete, partial, clarification and failure must match evidence. | Labeled status grading + fault injection | False-complete rate, false-zero/absence rate, useful partial explanation and missing requested outputs. |
| C8 / P1 | Bounded execution and repair: recover without repeated unproductive actions. | Trace assertions + fault injection | First-attempt schema validity, repair success, duplicate-task rate, call/turn caps and budget compliance. |
| C9 / P1 | Conversation knowledge and language: product help, Hindi, English and Hinglish. | Reference cases + bilingual human/model rubric | Helpful self-description without unnecessary specialists; equivalent factual content and appropriate response language. |

### Voter specialist

| ID / priority | Part and description | Evaluation kind | What to measure / reference |
|---|---|---|---|
| V1 / P0 | Category resolution: map requests to actual stored labels, preserving distinctions between fields. | Fixture labels + structured exact grading | Resolution precision/recall; unknown/ambiguous labels require appropriate clarification, not fabricated zero. Recorded labels are not proof of individual traits. |
| V2 / P0 | Filter semantics: AND, OR, NOT, NULL, age thresholds and booth scope. | Independent execution oracle + property tests | Exact selected record IDs and aggregate values; exercise age 59/60/61, missing values, alternatives and compound predicates. |
| V3 / P0 | Population context: refine the verified population or explicitly start a new one. | Stateful multi-turn + mutation cases | Expected filter state at each turn; preserve earlier restrictions for refinements, remove them for resets, reject stale/cross-seat context. |
| V4 / P0 | Name matching and transliteration: names and relatives must match the requested mode. | Gold record sets + human-reviewed variants | Precision/recall for word/full/substring matching; nukta variants, short/long vowel variants, duplicate names and multiword names. Search safe transliteration alternatives together; clarify only among actual matching records. |
| V5 / P0 | Booth/station resolution: part numbers and names must resolve uniquely. | Mapping fixtures + adversarial ambiguity | Exact mapping and clarification behavior for shared school names, rooms, suffixes and misspellings. |
| V6 / P0 | Person selection: references identify a verified, unique current record. | State assertions + adversarial | No first-row selection; duplicate identifiers, fabricated references, expired references and source changes handled safely. |
| V7 / P0 | Household lookup: preserve exact house identifier and station boundary. | Exact record-set oracle + multi-turn | Same house number at another station excluded; punctuation and leading zeros retained; explicit anchor inclusion/exclusion correct. Shared address must not be asserted as proven kinship. |
| V8 / P0 | Generated SQL: query meaning must match the request, beyond being valid/read-only. | Execution accuracy + differential fixtures + trajectory | Compare with independently implemented reference results on multiple datasets; include CTEs, joins, constants, grouping, hidden restrictions and omitted filters. Do not require identical SQL strings. |
| V9 / P0 | Counts and analytics: counts, grouping, comparisons, ranking and denominators. | Exact numeric/row-set grading + properties | Count unaffected by row display limits; grouped totals reconcile under stated NULL rules; ranks and ties obey the task; denominator explicit. |
| V10 / P0 | Coverage and provenance: available records differ from full-seat coverage. | Missing/partial coverage fixtures + claim grading | Zero versus unavailable, partial-population wording, source hash and stale-context invalidation. Truth about stored records and truth about the real world are distinct. |

Voter's current routed path prepares the request and calls `sql_specialist`; the model writes the SELECT, with structural predicate checks. Duplicate `count_request`, `run_name_task` and `household_lookup` execution paths have been removed; their regression cases now exercise the routed Voter specialist. Passing helper tests must not be counted as proof of equivalent coverage of the currently routed path.

### History specialist

| ID / priority | Part and description | Evaluation kind | What to measure / reference |
|---|---|---|---|
| H1 / P0 | Source period/type and availability: choose the correct Form-20 table. | Exact source assertions + execution oracle | Requested year and Lok/Vidhan preserved; missing/empty period distinguished from zero votes; catalog evidence cannot prove a winner. |
| H2 / P0 | Electoral geography: an assembly segment in a Lok election is not the entire parliamentary seat. | Structured scope checks + claim review | No wider-seat winner claims from segment-only results; booth/region/constituency boundaries preserved. |
| H3 / P0 | Arithmetic and winners: totals, margins, shares, turnout where supported. | Independent numerical oracle | Correct aggregation grain, denominator, party/candidate mapping, ties, missing candidates, duplicate rows and rounding. |
| H4 / P1 | Comparisons and rankings: interpret period changes and requested ordering. | Multi-turn + exact ranked result grading | “In 2024” changes the period while keeping relevant subject; last-two-election ambiguity handled; top-N, metric and geography correct. |
| H5 / P0 | Evidence selection: retain final answer observations rather than exploratory samples. | Trajectory + exact result provenance | Correct executed source IDs, no sample-count substitution, no unsupported electoral claim in synthesis. |

### Booth Analysis / Portfolio specialist

| ID / priority | Part and description | Evaluation kind | What to measure / reference |
|---|---|---|---|
| B1 / P0 | Booth-to-report identity: metadata and filename can disagree. | Deterministic mapping + adversarial fixtures | Correct unique report; Part 1 versus Part 10; conflicting explicit metadata rejected; duplicate/missing reports surfaced. |
| B2 / P1 | Section retrieval and size limits: retrieve requested report portions. | Exact JSON comparison + truncation cases | Section recall, missing-section reporting, valid structured evidence and explicit limits for oversized reports. |
| B3 / P0 | Explanation and provenance: report content must not become unsupported current truth. | Claim-based grading + source consistency audit | Correct numbers, labels, units and source period; no invented causal interpretation or conflation with the current voter roll. |
| B4 / P0 | Prepared report data quality: the agent may faithfully repeat an incorrect artifact. | Artifact validation + independent recomputation where possible | Totals reconcile, percentages use documented denominators, required keys/types present; distinguish upstream artifact errors from agent errors. |

### Internet / Research specialist

| ID / priority | Part and description | Evaluation kind | What to measure / reference |
|---|---|---|---|
| R1 / P0 | Public assignment and pilot eligibility: only suitable public tasks leave the backend. | Captured outbound payloads + adversarial | No private voter/household identities exported; test previously known identities and identities newly introduced in the current question; unsupported geography handled explicitly. |
| R2 / P1 | Search planning: preserve exact place, issue and requested period. | Labeled queries + retrieval outcome grading | Scope/date preservation and retrieval of required reference evidence; do not grade literal query text. |
| R3 / P1 | Retrieval: obtain relevant, credible and sufficiently diverse sources. | Frozen source relevance labels + live audits | Precision@k, reference-evidence recall, ranking quality and domain diversity. Recall is relative to a curated reference set, not the unknowable entire web. |
| R4 / P0 | Geography: distinguish district background from constituency/locality evidence. | Gold geographic labels + counterexamples | False locality/seat attribution rate; ambiguous names such as district versus a similarly named locality resolved correctly. |
| R5 / P0 | Recency: event date, publication date and update date differ. | Frozen clock + date-labeled sources | In-window factual claims, unknown-date handling, future windows and historical requests. Exercise old events republished recently. |
| R6 / P0 | Page reading: snippets are leads, not verified findings. | Provider replay + extraction faults | Finding sources must be successfully read pages; unavailable extraction produces honest partial/no-findings behavior. |
| R7 / P1 | Reranking and selection: choose relevant pages under page/domain caps. | Labeled rankings + fault injection | Relevant source retention, diversity and degraded behavior when ranking fails. |
| R8 / P0 | Finding extraction: claims need evidence and appropriate attribution. | Claim/source entailment judge + human audit | Citation precision, required-claim coverage, conflicting-source preservation, allegations/party claims labeled rather than stated as established fact. |
| R9 / P0 | Final citations: references must exist and support nearby assertions. | Deterministic ID/URL checks + claim entailment | Citation validity and coverage, no fabricated links, no uncited expansion during coordinator synthesis. Valid source IDs alone do not prove support. |
| R10 / P0 | Untrusted content and URL handling. | Injection fixtures + URL/network boundary tests | Instructions embedded in pages/snippets cannot alter task or export data. Test URL validation separately from provider fetching, redirects and host resolution; canonicalization alone is not a complete network security proof. |
| R11 / P1 | Stage failure and empty evidence: no retrieval is not proof of no issue. | Fault injection at each stage | Planning/search/ranking/extraction/findings failures classified honestly; useful partials and no unsupported absence claims. |

### Shared runtime, data, API and voice

| ID / priority | Part and description | Evaluation kind | What to measure / reference |
|---|---|---|---|
| S1 / P0 | Contracts and Gemini schema conversion. | Schema conformance + live structured-output eval | First-response validity, extra fields, cross-field rules, defaults, enum errors and repair behavior. Provider grammar conversion must not bypass server validation. |
| S2 / P0 | Data path and SQL boundaries. | Integration/security tests + adversarial | Cross-seat reads denied; traversal, attachments, destructive operations and expensive queries blocked or bounded. Exercise read-only connections as well as text validation. |
| S3 / P0 | Authentication mode and authorization. | Deployed-configuration + API checks | Missing/invalid/expired token behavior, user seat claims and production access settings. Local mode permits header-free local-admin access, so the runtime mode is also an authentication-sensitive setting. |
| S4 / P0 | Sessions and concurrency. | Stateful integration + parallel requests | User/seat isolation, conflicting turns, lease expiry, failure rollback, source version changes, storage outage and restart behavior. |
| S5 / P1 | Context and evidence compaction. | Long conversations + invariant checks | Required identity, filters, citations and pending objective survive limits; omitted evidence is flagged; no fabricated reconstruction after compaction. |
| S6 / P1 | Provider transport and budgets. | Fault injection + load tests | 429/5xx, TLS/network errors, malformed/truncated/blocked output, bounded retries, admission waiting and deadline propagation. |
| S7 / P0 | Data cache and freshness. | Versioned artifact integration tests | New source files and coverage are picked up according to documented refresh behavior; stale cached data cannot silently be labeled fresh. Current cloud downloads are cached per process. |
| S8 / P0 | Trace quality and privacy. | Trace-schema and access assertions | Request-to-agent-to-source correlation; no credentials, hidden reasoning or raw private evidence in public responses. Private traces themselves require access control and retention. |
| S9 / P1 | API/frontend fidelity. | Contract + browser integration | Status, sources, Unicode, partial reasons and session identity survive transport and history; errors or seat switches do not display stale answers. |
| S10 / P1 | STT meaning and transcription. | Human-transcribed audio corpus + semantic comparison | WER/CER with documented normalization, name/place/number exactness, language accuracy, silence/noise/dialect/code-switching and negation. |
| S11 / P1 | TTS content and playback. | Listening tests + exact critical-token checks + media validation | Intelligibility, listener MOS, names/numbers/negation, language, audio completeness, actual codec versus declared WAV, long answers and browser playback. |
| S12 / P1 | Whole voice task. | Paired text/audio end-to-end evaluation | Compare the same intent as text and audio; measure task-success loss caused by STT, correct spoken response and total audio-to-audio latency. |
| S13 / P2 | Latency, usage and cost. | Repeated benchmarking + load | Per-stage and end-to-end p50/p95/p99, tokens, model/search/extract/speech calls, retries, cost per successful conversation and concurrency saturation. |
| S14 / P1 | Model/prompt/data changes and environment parity. | Paired regression experiments | Run baseline and candidate on identical held-out cases and snapshots. Treat model ID, prompts, schema, source versions, runtime mode and configuration as versioned inputs. The same graph runs locally and in the cloud; verify configuration and infrastructure differences. |

## 4. Existing evaluation assets and gaps

`backend/tests/` contains deterministic and mocked integration checks for names, households, station matching, source catalogs, voter filters, election scope, coverage, research, contracts, runtime budgets, session behavior and API behavior. These establish important invariants, but mocked model success does not measure a live model's decision reliability.

`scripts/evaluate_harness.py` contains 45 routing cases with repeated opt-in live trials. It explicitly grades routing/intent, not final factual correctness. Extend its labels to include acceptable alternatives, complete typed arguments and multi-source requests.

`scripts/evaluate_voter_harness.py` contains ten fictional-data conversation scenarios in the inspected version. It runs the current agent loop with a live model, including population refinements, categories, names and an election follow-up. Several passes depend on the expected integer appearing somewhere in the answer or a substring being present. Additional checks exist for some scenarios, but this can still reward an incorrect statement containing the right value. Replace this with structured claim and state checks, retaining secondary semantic review for prose.

For example, if the expected answer is “3 matching records,” “There are 30 matching records, compared with 3 elsewhere” must fail even though `3` appears. Also, allowing `partial` unconditionally must not pass a task whose required deliverables were actually answerable and complete in the fixture.

A historical pre-migration release report recorded 135 offline tests passing, 35/35 fictional conversations, 15/15 focused conversations and 132/135 routing trials for an earlier release evaluation. These are historical reported observations, not new measurements from this review and not proof of broad correctness. The live routing score is about 97.8%, with three clarification outcomes recorded as failures; adjudicate whether their prompts genuinely required clarification before changing that label.

Verification attempt in this review: `PYTHONPATH=backend:. python3 -m unittest discover -s backend/tests -q` failed during module import because the available Python environment lacks dependencies including `pydantic`, `pydantic_settings` and `firebase_admin`. Ten module-load errors occurred; no substantive test results were obtained. Dependencies were not installed and no billed evaluations were triggered.

The voice API test mocks both speech operations. It checks request/response shape and declared media type, not transcription, pronunciation, waveform validity or provider behavior.

## 5. Proposed framework design

Build around the LangGraph nodes and existing Python entry points. Use graph updates for observable trajectories; keep exact reference and state grading independent of the graph.

```text
Versioned cases + immutable fixtures + source snapshots
                          |
              Runner adapters by test level
        component / specialist / coordinator / API / voice
                          |
             Results + state + observable trace
                          |
          Independent deterministic graders first
                          |
       Calibrated evidence judges / human adjudication
                          |
          Per-slice report + baseline comparison
                          |
          Release gates + production failure intake
```

Suggested new structure, not created by this proposal:

```text
evals/
  datasets/       # dev, regression and sealed holdout cases
  fixtures/       # synthetic SQLite, booth CSV/JSON, coverage and audio
  snapshots/      # frozen public pages and search/extract responses
  oracles/        # independent expected values, row sets and state
  runners/        # adapters for existing runtime/API/speech entry points
  graders/        # exact, trajectory, evidence, semantic and audio grading
  rubrics/        # versioned judge instructions and human annotation guide
  reports/        # summaries; sensitive artifacts belong in controlled storage
```

Each case should contain ID, component, language, risk level, user question or scripted turns, initial server state, source snapshot versions, expected structured request, required facts, forbidden claims, acceptable statuses and routes, expected final state, rubric version and performance budget. Expected outputs must be authored independently of the production helper being tested; otherwise the same bug can make both sides agree.

Illustrative case:

```json
{
  "case_id": "population_refine_reset_001",
  "fixture": "synthetic_seat_v2",
  "language": "en",
  "turns": [
    {"question": "How many voters are there?", "expected_total": 120},
    {"question": "How many of these are over 60?", "expected_total": 30},
    {"question": "How many women among them?", "expected_total": 18},
    {"question": "Total voters in the constituency", "expected_total": 120}
  ],
  "assertions": [
    "each numerical claim has the correct population and source",
    "age > 60 remains active on turn 3",
    "turn 4 removes the age and gender restrictions",
    "no source outside this fixture's constituency is read"
  ]
}
```

This is a proposed dataset format, not an existing runnable contract. The corresponding fixture must actually contain these totals and boundary cases.

Three execution modes isolate different causes:

1. Offline: mocked providers and deterministic fixtures, fast enough for every change.
2. Live model with frozen tools: model decisions vary, while databases/pages/search responses remain constant. Best for paired prompt/model regressions.
3. Live integration: real search/speech/model services on controlled test inputs. Detect provider drift, formats and current retrieval problems. Keep separate from frozen-evidence quality results.

Use scripted human-authored conversations first. Add user simulation for exploration, with separate grading of simulator consistency; simulated users can accidentally help the agent or change the goal. Simulator success is not a substitute for measured human task success.

## 6. Dataset and grader design

Start with approximately 300 independently reviewed cases: 40 coordinator, 80 voter, 45 history, 25 portfolio, 45 research, 35 cross-cutting boundary/recovery and 30 voice cases. These are proposed starting counts, not evidence of sufficient statistical power. Expand sparse and high-risk slices before using them as release evidence.

Include Hindi, English and Hinglish, short follow-ups, typos, multiple valid interpretations, missing/partial data, boundary values, malformed providers and conflicting sources. Report results by language, agent, operation, coverage, risk and conversation length; do not let easy English counts dominate the score.

Split by scenario family and fixture family, not random near-duplicate wording. Keep tuning cases separate from a sealed holdout and production regression set. Use fictional private identities and controlled audio; public evidence can be snapshotted with retrieval date and source URL. Evaluate ingestion and prepared-data quality independently from agent correctness.

Use metamorphic checks where ground truth relationships are sound: row ordering should not alter counts; adding a known nonmatching record should not change a filtered count; adding a conjunctive filter cannot increase the selected population; replacing a source version must invalidate its old references. Avoid claiming that replacing an existing filter must always shrink a population.

For model judges, provide question, expected requirements, allowed evidence and response. Ask for criterion-level supported/unsupported/missing decisions, claim references and brief evidence-grounded explanations. Treat the answer and retrieved text as untrusted input. Do not ask the judge to infer private model reasoning.

Calibrate judges on human-labeled examples including plausible wrong numbers, correct numbers with wrong scope, citation mismatches and honest partial answers. Track agreement and especially false passes of critical errors. Keep the judge model, prompt and calibration set versioned. Blind candidate identities, randomize pairwise order and review disagreements. Automated judges cannot establish authorization or replace exact numerical oracles.

## 7. Metrics and release decisions

Use a scorecard, with individual hard gates, rather than one weighted quality percentage.

| Dimension | Proposed measurement |
|---|---|
| Task success | All required outputs correct, adequate and within allowed behavior. Score an entire conversation as well as turns. |
| Structured intent | Required argument-field accuracy and complete request match. |
| Data correctness | Exact scalar values, selected IDs, aggregate rows and ranked results against independent oracles. |
| Grounding | Supported-claim precision and required-fact recall; audit citations separately. |
| Context correctness | Expected population, selected entity, pending objective and source-version state after each turn. |
| Honesty | False-complete, false-zero and unsupported-absence rates; clarification precision/recall. |
| Boundaries | Observed unauthorized reads/exports and forbidden operations. |
| Reliability | Per-task repeated success and fraction of tasks passing all repeated attempts; include uncertainty. |
| Operations | p95 end-to-end latency, calls, tokens, provider retries and total cost divided by successful tasks. |
| Speech | WER/CER, critical entity/number accuracy, listener intelligibility/MOS and audio task success. |

Distinguish pass@k, which rewards at least one success in k attempts, from pass^k, which asks for consistent success across k attempts. An empirical all-five-trials pass rate is useful; do not confuse it with a guaranteed production reliability level. Use paired comparisons, uncertainty intervals and enough independent scenarios. Five repeats of seven scenarios still cover seven scenario families.

Proposed initial gates, to be calibrated with a baseline:

- All deterministic P0 assertions pass; any observed cross-user/seat leak, private search export or destructive operation blocks release.
- Exact numerical/identity reference suites have no unexplained regression; unsupported critical numbers or wrong source scopes block release even if prose scores improve.
- Report false-complete and missing-coverage-as-zero failures explicitly. Do not average them away with greetings or style scores.
- Core live success and Hindi/Hinglish slices must not materially regress from the paired baseline. Choose the permitted margin and sample size before comparing candidates.
- Set latency and cost budgets by task class after measurement. The current 240-second runtime deadline is a safety cap, not an acceptable user-experience target.
- Passing a finite adversarial suite means no violation was observed in those cases; it is not a proof that all attacks are impossible.

Suggested cadence: offline suites on every relevant change; bounded live model regression on prompt/model/schema changes; scheduled full specialist and multi-turn runs; separate live provider checks; controlled staging API/voice evaluation before promotion. Label infrastructure failures separately for diagnosis, but include them in availability and whole-product success reporting.

## 8. Instrumentation and production feedback

Current code already records request IDs, deployment revision, model timing/attempts, some token usage, coordinator actions, specialist results, resolved scopes and terminal status. Preserve this foundation.

Extend the private evaluation capture to include case/trial IDs, code revision, graph node transitions, prompt/schema hashes, model and generation settings, source hashes, clock, runtime mode, expected/actual structured arguments, source evidence references, stage timing, usage totals, cost assumptions and grader versions/results. Aggregate usage incrementally: bounded event buffers can discard early model-call records, making a sum over the final trace incomplete.

Do not expose evaluation evidence or traces through the public chat response. Current trace storage is deliberately bounded and private; evaluations need their own controlled, versioned artifacts rather than assuming session retention is a durable experiment archive. Record observable actions and evidence, not hidden chain-of-thought. Minimize private content and use synthetic records for routine runs.

Sample production failures and low-quality/partial answers for authorized review. Convert confirmed failures into minimized fictional or de-identified regression cases, label root cause, and rerun the relevant suite. Keep explicit user feedback, evaluator scores and infrastructure failures distinct. Any live experiment must continue enforcing the same access and data-export boundaries.

## 9. Recommended implementation order

1. Improve existing graders: exact claim scope and values, acceptable completion status, full state assertions and independent reference outcomes.
2. Add a shared case/result schema and adapters around the existing routing and fictional conversation scripts.
3. Cover current routed Voter/History behavior, election arithmetic, booth identity, source coverage and P0 boundaries first.
4. Add frozen-page research datasets, claim/citation judging and calibrated human review.
5. Add real Hindi/English/Hinglish speech corpora and paired text/audio end-to-end cases.
6. Add baseline/candidate reports, task-class performance budgets and release gates, then production failure intake.

The objective is explainable failure localization: identify whether an answer failed because the request was misunderstood, the wrong specialist/source was selected, SQL or retrieval was wrong, state was stale, evidence was misread, synthesis invented a claim, or transport/speech failed. That makes evaluation actionable for every agent and for the complete product.

# Arjun Booth Agent

Arjun answers questions using constituency voter records, Form-20 election results,
prepared booth reports and public research. The backend uses **LangGraph** for
agent orchestration and **LangChain** for Gemini model calls. FastAPI serves the
API; the Next.js frontend uses `POST /agent/chat`.

The **Find Voter** tab provides direct EPIC lookup and structured multi-field
search with explicit mismatch explanations, region/booth scope, and coverage
notices. See [voter search](docs/VOTER_SEARCH.md) for matching rules and limits.

## Runtime

```text
Frontend text / Sarvam transcription
                 |
       FastAPI auth + seat access
                 |
       Private conversation store
                 |
         LangGraph prepare
                 |
     coordinator -> validate -> dispatch
          ^                       |
          +---- voter / history / portfolio / research
                 |
      verified final answer or fallback
                 |
        Text / optional Sarvam playback
```

The coordinator delegates typed tasks. Voter and History execute validated,
read-only SQL. Portfolio resolves a booth and loads its prepared JSON. Research
uses Tavily search/extraction for public information in the Prayagraj/Kaushambi
pilot. The validation node checks evidence, source restrictions, counts, ranking,
citations and completion status before returning an answer.

LangGraph owns the transitions and exposes individual nodes for inspection and
evaluation. Domain-specific validation remains application code. The graph runs
inside the backend in every environment; there is one runtime implementation.

## Local development

Use Python 3.11 or newer. From the repository root:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements-dev.txt
export PYTHONPATH="$PWD/backend:$PWD"
export LOCAL_DATA_ROOT="$PWD/legacy-data"
export LOCAL_DEV_AUTH=true
export GEMINI_API_KEY="your-development-key"
uvicorn app.main:app --port 8080 --reload
```

`LOCAL_DEV_AUTH=true` explicitly enables header-free development access. The
setting defaults to false. Current Cloud Run deployment configurations set it to
true to support the demo frontend without Firebase credentials. This allows
header-free access to every constituency and voter endpoint. Configure Firebase
sign-in and set it to false before using the service as an authenticated app.
Agent orchestration is independent of this setting.

For the frontend:

```sh
cd frontend
npm ci
npm run dev
```

Configure frontend environment variables as described in [frontend/README.md](frontend/README.md).

## Model and service configuration

| Setting | Purpose |
|---|---|
| `GEMINI_MODEL` | Gemini model ID; default `gemini-2.5-flash` |
| `GEMINI_API_KEY` / `GOOGLE_API_KEY` | Developer API key; omit for service-account/ADC Vertex access |
| `GOOGLE_GENAI_USE_VERTEXAI` | Optional explicit `true`/`false` backend selection |
| `GOOGLE_CLOUD_PROJECT` / `GCP_PROJECT` | Project for Vertex access |
| `GOOGLE_CLOUD_LOCATION` / `VERTEX_LOCATION` | Vertex location; falls back to region settings, then `asia-south1` |
| `AGENT_TIMEOUT_SECONDS` | Whole-turn deadline; default 240, bounded to 30–540 seconds |
| `GEMINI_TIMEOUT` | Maximum individual model-call timeout; default 120 seconds |
| `GEMINI_MAX_ATTEMPTS` | Bounded retry attempts; default 3 |
| `GEMINI_MAX_CONCURRENT_CALLS` | Per-process model admission limit; default 4 |
| `GEMINI_THINKING_BUDGET` | Gemini 2.5 Flash thinking-token budget; default 1024 |
| `LOCAL_DEV_AUTH` | Explicit local auth bypass; defaults to false |
| `LOCAL_DATA_ROOT` | Local folder containing per-seat data |
| `GCS_DATA_BUCKET` | Private cloud artifact bucket |
| `SESSION_STORE` | `auto`, `memory` or `firestore` |
| `SESSION_FIRESTORE_DATABASE` | Defaults to `booth-agent-sessions` |
| `SESSION_PROJECT` | Optional Firestore project override |
| `SESSION_TTL_HOURS` | Session expiry; default 24 |
| `TAVILY_API_KEY` | Enables public research |
| `RESEARCH_TIMEOUT_SECONDS` | Research budget; default 90 seconds |
| `SARVAM_API_KEY` | Enables speech transcription and synthesis |

Gemini is a model provider. Vertex credentials remain supported through the
LangChain Google integration. Model requests and private graph state are not
exported to external tracing services by the application.

## Data and onboarding

Private artifacts are organized per constituency:

```text
legacy-data/<slug>/
  voters.db
  history.db
  boothlist.csv
  booth_analysis/*.json
  coverage.json             # optional availability/coverage metadata
```

Cloud storage mirrors this layout. Configuration lives in
`constituencies/<slug>/constituency.yaml`; the account API uses
`constituencies/manifest.json`. Missing records and partial coverage must not be
reported as zero or complete constituency coverage.

Maintained administration scripts:

| Script | Purpose |
|---|---|
| `scripts/add_constituency.py` | Import a constituency folder and update configuration/manifest |
| `scripts/discover_constituencies.py` | Rebuild the manifest; optionally check GCS artifacts |
| `scripts/import_artifacts.py` | Audit/import prepared artifacts with coverage metadata |
| `scripts/upload_artifacts.py` | Upload selected constituency artifacts |
| `scripts/evaluate_harness.py` | Opt-in live routing evaluation on fictional prompts |
| `scripts/evaluate_voter_harness.py` | Opt-in live conversations against fictional data |

Use each script's `--help` before operational use. Private databases and prepared
upload folders are excluded from Git/cloud build context. Existing local data and
user-maintained preprocessing files are not removed by the framework migration.

## Conversations and boundaries

The API persists recent history, pending clarification and verified voter context
under the authenticated user and session. A session cannot switch constituency.
Selected voter references are checked against current source versions; ambiguous
name/person matches require clarification. A household address match does not
establish kinship or current residence.

`SESSION_STORE=auto` uses memory locally and Firestore on Cloud Run. Configure the
Firestore database and backend `roles/datastore.user` permission. Browser clients
must not access `booth_agent_sessions` or its private trace records. Configure TTL
on `expires_at`; the application also ignores expired sessions. Concurrent turns
are protected by leases; storage failures return an error rather than resetting
state. LangGraph does not introduce a separate shared conversation checkpointer.

SQL runs on read-only connections with source/predicate checks and bounded rows.
These controls are not a proof of arbitrary query semantics. Public research
accepts public tasks, distinguishes snippets from read pages and keeps evidence
attribution, dates and geography. Validate its behavior using the evaluation plan.

## Dependency versions

`backend/requirements.txt` declares supported dependency ranges;
`backend/requirements.lock` pins the resolved production environment. Docker and
development installs use that lock. After changing dependency ranges, regenerate
and validate the lock:

```sh
uv pip compile backend/requirements.txt --python-version 3.11 --no-annotate --no-header -o backend/requirements.lock
```

## Verification

```sh
PYTHONPATH=backend:. python -m unittest discover -s backend/tests -v
pip check
```

Offline tests use fictional records and mocked model/speech/search boundaries.
Live evaluations require `--live` and make billed provider calls. They are separate
from offline tests and do not establish universal factual accuracy.

See [architecture](docs/ARCHITECTURE.md), [evaluation design](docs/AGENT_EVALUATION_FRAMEWORK.md)
and [deployment](docs/CICD.md). Changes to this repository do not themselves deploy
or migrate an existing Cloud Run service.

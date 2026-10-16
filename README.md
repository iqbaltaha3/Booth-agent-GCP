# Arjun — Booth Agent Backend

**Booth-level electoral intelligence for Indian Vidhan Sabha seats.**

This is the production backend for **Arjun**: a multi-agent political analyst that answers campaign questions from the voter roll, Form-20 election history, and precomputed booth portfolios.

- **Region:** `asia-south1` (Mumbai)
- **Frontend always calls** `POST /agent/chat` (never talks to Agent Engine directly)
- **Auth:** Firebase Email/Password + custom claims for constituency access
- **Data:** one private GCS folder per constituency (physical isolation)
- **Adding a seat:** upload files to GCS + one YAML + push. Zero code changes.

---

## Quick start (local, 5 minutes)

### 1. Prerequisites

- Python 3.11+
- A Gemini API key (for local agent mode)
- Optional: Sarvam API key (for voice)

### 2. Prepare Gyanpur data

```bash
# From the repo root
chmod +x scripts/prepare_local_data.sh

# Point at the original MVP folder that contains the .db files
./scripts/prepare_local_data.sh /path/to/Arjun-Booth-Agent
```

This creates:

```
legacy-data/gyanpur/
├── voters.db
├── history.db
├── boothlist.csv
└── booth_analysis/*.json
```

### 3. Configure environment

```bash
cp backend/.env.example backend/.env
# Edit backend/.env:
#   GEMINI_API_KEY=...
#   LOCAL_DATA_ROOT=/absolute/path/to/booth-agent/legacy-data
#   AGENT_LOCAL_MODE=true
```

### 4. Run the API

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export PYTHONPATH="$(pwd):$(pwd)/../agents"
export LOCAL_DATA_ROOT="$(pwd)/../legacy-data"
export AGENT_LOCAL_MODE=true
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

Open http://localhost:8080/docs for the interactive Swagger UI.

### 5. Try a question (no auth needed in local mode)

```bash
curl -s -X POST http://localhost:8080/agent/chat \
  -H "Content-Type: application/json" \
  -d '{
    "question": "How many voters are there in total?",
    "constituency_id": "gyanpur"
  }' | jq .
```

---

## Architecture in one picture

The implemented runtime runs inside the backend when `AGENT_LOCAL_MODE=true`
or no Agent Engine resource is configured:

```text
Frontend → POST /agent/chat → authorize constituency + load session state
                               ↓
                         ReAct coordinator
                               ↓ focused, validated tasks
                  Voter / History / Portfolio specialists
                               ↓ structured facts and sources
                         inspect → refine / clarify / finish
```

Specialists read the selected constituency's files through the shared data service.
The Vertex AI Agent Engine topology below is a deployment target: its client exists,
but `agents/deploy_agent_engine.py` is still a placeholder and does not deploy an
ADK graph. Configuring an external engine does not automatically install this runtime.

```
Frontend
   │  Firebase ID token
   ▼
Cloud Run  (booth-backend)          asia-south1
   ├── /auth  /constituencies  /voice
   ├── /voter /history /portfolio     ← tools used by the agents
   └── /agent/chat  ─────────────────► Vertex AI Agent Engine (ADK)
                                            │
                                            └── tools call back into
                                                the same Cloud Run service

GitHub: constituencies/<slug>/constituency.yaml   (config only, no PII)
GCS:    gs://booth-agent-data-<env>/<slug>/       (voters.db, history.db, …)
```

**Design rule:** one login session → one GCS folder. Other constituencies’ files are never opened.

### Agent coordination and contracts

The coordinator owns task completion. Each model step returns a validated action:
`delegate`, `clarify`, or `finish`. It designs separate assignments for the relevant
specialists instead of forwarding the original question to everyone. Specialists
run sequentially; subsequent coordinator steps can use their results to design
follow-up tasks. Limits: 8 coordinator steps and 8 specialist calls per user turn;
SQL specialists have 5 voter or 6 history research steps per call.

Assignments include `task_id`, `agent`, `objective`, `constraints`,
`required_outputs`, and `acceptance_criteria`. Voter name tasks also include
`name_query`. Results include `status`, `facts`, `sources`, `limitations`,
`missing_inputs`, and `errors`. Status is `complete`, `partial`,
`needs_clarification`, or `failed`. SQL facts come from executed queries rather
than model-generated summaries. Final data answers cite supporting task IDs
internally; the user receives a plain-language answer.

When clarification is necessary, `/agent/chat` returns the question in `answer`
and `status: "needs_clarification"`. Send the user's reply with the same
`session_id`. The backend supplies recent conversation history and pending tasks
to resume the original objective. State is private to the user and locked to the
selected constituency. Completed individual lookups retain server-verified voter
references, including original identifiers and a voter-file checksum. A unique
lookup can become the selected reference; a multi-person result is not silently
reduced to its first person. Household follow-ups use this reference instead of
reconstructing addresses from formatted answers.

`SESSION_STORE=auto` (default) uses Firestore on Cloud Run (`K_SERVICE` is present)
and process memory locally. Firestore sessions survive restarts and are shared
between instances. A short transaction acquires a 15-minute lease, then model
calls run outside the transaction; overlapping turns receive HTTP 409. Failed
turns release the lease without overwriting previous context. Storage failure
returns HTTP 503 rather than silently starting a conversation with lost memory.

Sessions retain 24 messages (the coordinator receives the latest 12), pending
tasks, up to 40 verified voter references, and 16 bounded task diagnostics. Sessions
expire after 24 hours by default. Diagnostics include supplied filters, counts,
source identifiers and bounded query text/parameters; they remain server-side and
are not returned by the chat API.

### Shared session setup

Before deploying this version, enable Firestore Native mode in the application
project, provision the selected database, and grant the backend runtime service
account `roles/datastore.user`. The collection `booth_agent_sessions` must remain
unreadable and unwritable by browser/Firebase clients; only the server uses it.
Session payloads are JSON strings to support query results containing nested arrays.

Configuration:

```text
SESSION_STORE=auto                 # firestore on Cloud Run; memory locally
SESSION_PROJECT=my-booth-agent      # optional; otherwise Google credentials/project discovery
SESSION_FIRESTORE_DATABASE=booth-agent-sessions
SESSION_TTL_HOURS=24
```

For physical expiry cleanup, configure a Firestore TTL policy on `expires_at` for
the `booth_agent_sessions` collection group. The application ignores expired
sessions even before Firestore deletes them. `SESSION_STORE=memory` is available
for local tests; it does not provide shared memory across server instances.

### Household follow-ups

`household_query` accepts a verified voter reference or an EPIC, or explicit
`house_no` plus `polling_station`. It cannot be combined with a name query or extra
task constraints. Explicit station names are resolved using case and whitespace
normalization; ambiguous or unknown stations require clarification. House-number
punctuation is preserved. An anchored lookup reads the original stored address and
verifies the anchor is included before optionally excluding that person.

Counts cover all matching records; display is capped at 20. The original person's
name, age or gender is not retained as a household filter. A voter-file checksum
change invalidates old references and requires a fresh lookup. Matching voter-roll
records do not prove current residence or family relationships.

### Hindi voter-name matching

Name tasks explicitly preserve the operation (`count` or `lookup`), booth,
person/relative names, matching mode and exact roll-column filters. The model
transliterates only isolated name phrases. Parameterized SQL checks candidate
spellings against the scoped roll; it never treats the entire question as a name.
Booths must map uniquely to an actual polling station; fuzzy matches are not
silently accepted.

Default `word` matching includes `श्याम कुमार` for `श्याम`, but excludes
`घनश्याम` and `श्यामलाल`. `full` matches the entire name; `substring` is used
only when explicitly requested. Alternate spellings are OR alternatives; words
inside a multiword name stay together. Unicode combining marks are preserved and
joiner differences are normalized. If multiple candidate spellings match different
scoped names, the coordinator asks which spellings to include. A user can select
one or all. Counts use `COUNT(*)`; lookup returns at most 20 records with a separate
total and truncation flag. Transliteration is not exhaustive, so zero matches mean
zero for the spellings and rule checked, not proof that every possible spelling
was searched. Schema and spelling interpretation still require model judgment.

Contracts: `agents/contracts.py`. Coordinator and specialists: `agents/runtime.py`.
Name matching: `agents/name_matching.py`.
Verified references and households: `agents/households.py`.
Shared session adapters: `backend/app/services/sessions.py`.

### Public Research Agent

The fourth specialist, `research`, is adapted from the supplied Local Research
Agent demo. The coordinator selects it automatically for public news, recent civic
issues, public candidate activities and official announcements. It can combine web
reporting with local aggregate/election evidence; private voter records, household
addresses, EPICs, conversation history and voter context are not passed to search
providers. Database-only questions keep using the existing specialists.

The pilot is limited to constituencies configured in Prayagraj/Allahabad or
Kaushambi/Kausambi district, Uttar Pradesh. Gyanpur/Bhadohi is outside this pilot.
District relevance does not establish constituency or village membership; the
registry is a sampled source guide, not a validated boundary gazetteer. Ambiguous
places require clarification. District-wide or uncertain findings retain those
labels rather than being presented as constituency facts.

`research_query` includes `public_question`, `scope`, optional `locality`,
`time_sensitive`, optional paired `start_date`/`end_date`, and `max_pages` (1–8).
For recent tasks the default is the last 30 days through today in Asia/Kolkata.
Historical/general questions skip publication-date filtering and must state data
years explicitly. The coordinator resolves follow-up subjects before delegating.

Retrieval uses Tavily only: up to four Hindi/English planned queries plus two
source-directed searches, three concurrent searches, at most two HTTP attempts,
domain-diverse reranking and up to eight page extractions. Existing Gemini/GCP
authentication is reused for planning, ranking and structured findings. The default
research deadline is 90 seconds (`RESEARCH_TIMEOUT_SECONDS`, capped at 180), with
at most two research calls per user turn. Interrupted retrieval returns partial
evidence or an explicit failure. Research coverage is not exhaustive.

Source IDs survive coordinator synthesis and become numbered, clickable citations
in the UI. Copy/share include source URLs; audio omits citation identifiers. Chat
responses and session history include a `sources` list with title, public URL,
publication date (possibly unknown), search timestamp, category and read status.
Raw page extractions stay within the specialist call. `snippet_only` sources are
search leads, not confirmed findings. Unknown citations are rejected; factual web
answers require citations to findings from read pages. Identifier validation does
not independently prove claim support. Reporting, official statements, allegations,
conflicts, geographic uncertainty and missing dates must remain distinguishable.

### Tavily configuration

Store the Tavily key in Secret Manager as `tavily-api-key` in the deployment project.
Grant the backend runtime account `roles/secretmanager.secretAccessor` on that
secret. The backend Cloud Build configurations bind it to `TAVILY_API_KEY` and use
`--update-env-vars` so other runtime configuration is preserved. Provision this
secret before deploying in another project. Local development can set
`TAVILY_API_KEY` in the process environment or the backend's local `.env`.
Missing credentials disable research with an explicit result; electoral data tools
continue to operate. Keys and extracted pages are not written into source files.

API references: [Tavily Search](https://docs.tavily.com/documentation/api-reference/endpoint/search)
and [Tavily Extract](https://docs.tavily.com/documentation/api-reference/endpoint/extract).
Implementation: `agents/research.py`; pilot registry: `agents/research_sources.json`.

Run deterministic regression checks without credentials or real voter data:

```bash
PYTHONPATH=backend:. python -m unittest discover -s backend/tests -v
```

---

## Add Or Update A Constituency

Use one folder per constituency. The folder name becomes the constituency id.

Example folder:

```text
gyanpur/
├── voters.db
├── history.db
├── boothlist.csv
└── booth_analysis/
    ├── booth1.json
    └── booth2.json
```

Required files:

| File | Meaning |
|------|---------|
| `voters.db` | Voter SQLite database |
| `history.db` | Election history SQLite database |
| `boothlist.csv` | Booth list CSV |
| `booth_analysis/*.json` | Booth portfolio JSON files |

### Add a new constituency

Run this from the repo root:

```bash
python3 scripts/add_constituency.py /path/to/gyanpur \
  --name "Gyanpur" \
  --district "Bhadohi" \
  --state "Uttar Pradesh"
```

What this does:

- Uses folder name `gyanpur` as the slug.
- Copies data to `legacy-data/gyanpur/`.
- Creates `constituencies/gyanpur/constituency.yaml`.
- Updates `constituencies/manifest.json`.

### Update an existing constituency

Make a fresh folder with the same name:

```text
gyanpur/
├── voters.db
├── history.db
├── boothlist.csv
└── booth_analysis/
    ├── updated-booth1.json
    └── updated-booth2.json
```

Run:

```bash
python3 scripts/add_constituency.py /path/to/gyanpur
```

That updates `legacy-data/gyanpur/` with the new files. It keeps the old YAML details like name, district, state, and tagline.

### Upload data to GCS

For dev:

```bash
gsutil -m rsync -r ./legacy-data/gyanpur/ \
  gs://booth-agent-data-dev/gyanpur/
```

For staging:

```bash
gsutil -m rsync -r ./legacy-data/gyanpur/ \
  gs://booth-agent-data-staging/gyanpur/
```

For prod:

```bash
gsutil -m rsync -r ./legacy-data/gyanpur/ \
  gs://booth-agent-data-prod/gyanpur/
```


## e4xample - gcloud storage cp legacy-data/allahabad-north/booth_analysis/ gs://booth-agent-data-$ENV/allahabad-north/booth_analysis/ s

### Commit only config

Do not commit `legacy-data/` or `.db` files. They are private data.

```bash
git add constituencies/gyanpur constituencies/manifest.json scripts/add_constituency.py README.md
git commit -m "Onboard gyanpur"
git push
```

### Give user access

Add the slug to the Firebase user's custom claims:

```json
{
  "constituency_slugs": ["gyanpur"],
  "role": "campaign_worker"
}
```

### Test

```bash
curl -X POST https://<backend>/agent/chat \
  -H "Authorization: Bearer <firebase-id-token>" \
  -H "Content-Type: application/json" \
  -d '{"question":"How many voters?","constituency_id":"gyanpur"}'
```

That is all. No code change, no new backend service, no new agent deployment.

---

## API surface (all except `/healthz` require a Firebase Bearer token)

| Method | Path | Who calls it |
|--------|------|--------------|
| GET | `/healthz` | Cloud Run |
| POST | `/auth/bootstrap` | Frontend after login |
| GET | `/auth/me` | Frontend |
| GET | `/constituencies` | Frontend (dropdown) |
| POST | `/voter/query` | ADK VoterSubAgent |
| GET | `/voter/schema` | ADK VoterSubAgent |
| POST | `/history/query` | ADK HistorySubAgent |
| GET | `/history/schema` | ADK HistorySubAgent |
| GET | `/portfolio/{id}` | ADK PortfolioSubAgent |
| GET | `/portfolio/search?q=` | ADK PortfolioSubAgent |
| POST | `/voice/transcribe` | Frontend |
| POST | `/voice/synthesize` | Frontend |
| **POST** | **`/agent/chat`** | **Frontend (main entry)** |
| GET | `/agent/sessions/{id}/history` | Frontend |

### Example chat request

```json
{
  "question": "Who won in Khokhar in 2022?",
  "constituency_id": "gyanpur",
  "session_id": "optional-existing-session"
}
```

### Example chat response

```json
{
  "answer": "In the 2022 Vidhan Sabha election, ...",
  "charts": [],
  "session_id": "uuid",
  "agent_path": ["history_agent"],
  "status": "complete",
  "latency_ms": 4200
}
```

---

## Auth model

1. Frontend signs the user in with Firebase **Email/Password**.
2. Frontend sends the Firebase ID token as `Authorization: Bearer <token>`.
3. Backend verifies the token and reads custom claims:

```json
{
  "constituency_slugs": ["gyanpur", "another-seat"],
  "role": "campaign_worker"
}
```

4. Every data route checks that the requested `constituency_id` is in the user’s claim list.  
   The agent/LLM never controls the data path.

**Local mode:** set `AGENT_LOCAL_MODE=true` and omit the header → synthetic admin that can see every constituency.

---

## Security notes

- **SQL safety** — every agent-generated query is re-validated server-side (`SELECT`/`WITH` only, single statement, no mutating keywords).
- **Path derivation** — GCS/SQLite path is built only from the authenticated session’s validated slug.
- **Prompt injection** — live data is wrapped in `<electoral_data>` tags; system prompts instruct the model to treat that content as values, never as instructions.
- **PII** — voter rolls live only in the private GCS bucket, never in the GitHub repo.

---

## Project layout

```
booth-agent/
├── backend/                 # FastAPI service (Cloud Run)
│   ├── app/
│   │   ├── main.py
│   │   ├── routers/         # auth, voter, history, portfolio, voice, agent, constituencies
│   │   ├── services/        # gcs_data, sql_safety, sarvam, agent_engine_client
│   │   └── middleware/      # firebase_auth
│   ├── Dockerfile
│   └── requirements.txt
├── agents/                  # ADK definitions + local runner + shared prompts
│   ├── prompts/             # SHARED templates (filled from constituency.yaml)
│   ├── local_runner.py      # in-process ReAct agents for local/dev
│   ├── contracts.py         # validated coordinator/specialist messages
│   ├── runtime.py           # task planning, research, clarification and completion
│   ├── name_matching.py     # scoped Hindi name matching and exact counts
│   └── deploy_agent_engine.py
├── constituencies/
│   └── gyanpur/
│       └── constituency.yaml
├── scripts/
│   ├── discover_constituencies.py
│   └── prepare_local_data.sh
├── clouddeploy/
├── cloudbuild.yaml
├── docs/
│   └── ARCHITECTURE.md
└── README.md
```

---

## Deploy overview

1. Enable APIs and create service accounts (see `docs/ARCHITECTURE.md`).
2. Create GCS buckets `booth-agent-data-dev|staging|prod` in `asia-south1`.
3. Upload Gyanpur artifacts under `gyanpur/`.
4. Build & push image, create Cloud Deploy pipeline.
5. Deploy Agent Engine (separate trigger on `agents/**`).
6. Point Secret Manager at the Agent Engine resource name and Sarvam key.
7. Frontend uses the Cloud Run URL + Firebase.

---

## Identity

Arjun is a **professional political analyst**. Answers are grounded only in the data for the selected constituency. The product never invents numbers, never crosses constituency boundaries, and always shows which specialist agent produced the evidence.

---

## License / data

Electoral roll and Form-20 data are sensitive. Keep the GCS buckets private. Do not commit `.db` files or credentials to git.

---

## Frontend (website)

The user-facing website lives in **`frontend/`**.

- Product name: **My Booth Agent**
- Tagline: **हर बूथ की समझ**
- Domain: **www.myboothagent.com**
- Hosting: Firebase Hosting
- Languages: English + Hindi

**If you have never deployed a website before**, open this file and follow it step by step:

→ **[frontend/README.md](frontend/README.md)**

It explains installing Node.js, Firebase login, building, deploying, and connecting your domain in plain language.

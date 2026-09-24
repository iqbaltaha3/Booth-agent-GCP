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

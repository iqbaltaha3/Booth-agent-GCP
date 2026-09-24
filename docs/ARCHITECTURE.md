# System & Architecture Report  
**Arjun Booth Agent Backend — Production Design**

Region: **asia-south1 (Mumbai)**  
Auth: **Firebase Email/Password**  
Frontend entry: **always `POST /agent/chat`**

---

## 1. Purpose

Arjun is a multi-agent electoral intelligence product for Indian assembly constituencies.  
A campaign worker asks natural-language questions; specialised agents query the voter roll, Form-20 history, and precomputed booth portfolios, then synthesise a grounded answer.

The original MVP was a single-process Streamlit app for one seat (Gyanpur).  
This backend turns that into a multi-tenant, multi-constituency, GCP-native service with physical data isolation and zero-code onboarding for new seats.

---

## 2. High-level architecture

```
┌─────────────┐
│  Frontend   │  (out of scope — any SPA / mobile)
└──────┬──────┘
       │ HTTPS + Firebase ID token
       ▼
┌──────────────────────────────────────────────────────────────┐
│  Cloud Run: booth-backend  (asia-south1)                     │
│  FastAPI                                                    │
│  • /auth  /constituencies  /voice                           │
│  • /voter /history /portfolio   ← tool endpoints for agents │
│  • /agent/chat                  ← only Q&A entry for UI     │
└────────────┬─────────────────────────────┬───────────────────┘
             │                             │
             │ resolves slug from          │ Vertex SDK
             │ Firebase claims             ▼
             ▼                    ┌────────────────────────────┐
    ┌─────────────────┐           │ Vertex AI Agent Engine     │
    │ Private GCS     │           │ (ADK)                      │
    │ booth-agent-    │           │  BoothCoordinatorAgent     │
    │ data-<env>      │           │   ├─ VoterSubAgent         │
    │   <slug>/       │           │   ├─ HistorySubAgent       │
    │     voters.db   │           │   └─ PortfolioSubAgent     │
    │     history.db  │           │  Gemini 2.5 Flash          │
    │     booth_…     │           │  tools → backend routes    │
    └─────────────────┘           └────────────────────────────┘

GitHub repo
  constituencies/<slug>/constituency.yaml   ← config only (no PII)
  Cloud Build discovers YAMLs, validates GCS, builds image, creates
  Cloud Deploy release (dev → staging → prod, asia-south1)
```

### Design invariants

| Invariant | Why it matters |
|-----------|----------------|
| One session → one GCS folder | Physical isolation; cross-constituency leakage is impossible even under prompt injection |
| Constituency path derived only from Firebase claims + server-side validation | Agent/LLM never controls which data is opened |
| Shared prompt templates filled from YAML | HARD BOUNDARIES text cannot drift across seats |
| SQL re-validated on the backend | LLM-generated SQL is never trusted |
| Frontend only talks to `/agent/chat` | Single chokepoint for logging, auth, and rate limits |

---

## 3. Data model

### 3.1 Config (Git)

```
constituencies/
└── gyanpur/
    └── constituency.yaml
```

```yaml
slug: gyanpur
name: Gyanpur
seat_type: Vidhan Sabha (Assembly)
district: Bhadohi
state: Uttar Pradesh
product_name: Arjun
product_tagline: "..."
```

### 3.2 Data (private GCS, per environment)

```
gs://booth-agent-data-<env>/<slug>/
├── voters.db          # table: voters
├── history.db         # form20_2017_vidhan, form20_2019_lok, ...
├── booth_analysis/
│   └── *.json
└── boothlist.csv
```

Local development mirrors the same layout under `legacy-data/<slug>/`.

### 3.3 Why not a shared Postgres?

- Physical isolation is a stronger boundary against prompt injection and accidental cross-tenant reads.
- Onboarding becomes “upload files + one YAML” instead of a migration.
- At the scale of tens of constituencies × ~100k rows, SQLite-per-seat is simpler and cheaper than Cloud SQL + VPC.

---

## 4. Backend (FastAPI on Cloud Run)

### Responsibility split

| Component | Role |
|-----------|------|
| `middleware/firebase_auth` | Verify ID token, extract custom claims |
| `deps.require_constituency` | Enforce slug ∈ user’s allowed list |
| `services/gcs_data` | Resolve slug → local/GCS path, open SQLite, run validated SELECT |
| `services/sql_safety` | `validate_select()` — SELECT/WITH only, single statement |
| `routers/voter|history|portfolio` | Tool surface for the agents |
| `routers/agent` | `/agent/chat` — the only frontend Q&A entry |
| `routers/voice` | Sarvam STT/TTS proxy |
| `services/agent_engine_client` | Calls Vertex Agent Engine, or falls back to local runner |

### Request path for a chat turn

1. Frontend sends `POST /agent/chat` with Firebase Bearer token + `{question, constituency_id, session_id?}`.
2. Middleware verifies token → user claims.
3. `require_constituency` confirms the slug is allowed.
4. Backend calls Agent Engine (or local runner) with the validated slug.
5. Sub-agents call `/voter/query`, `/history/query`, `/portfolio/...` (or open files in-process in local mode). Those routes again enforce the same slug from the session.
6. Answer + optional chart specs returned to the frontend.

---

## 5. Agent layer

### Structure (mirrors the original Gyanpur agents)

- **BoothCoordinatorAgent** — routes to specialists, synthesises the final answer.
- **VoterSubAgent** — ReAct loop over `voters.db`.
- **HistorySubAgent** — ReAct loop over Form-20 tables.
- **PortfolioSubAgent** — resolves part number / booth name → JSON portfolio.
- **Visualization** — optional chart-spec generation (kept for later wiring).

### Prompt handling

- All system prompts live under `agents/prompts/` as **shared templates**.
- At request time they are filled with `{name}`, `{district}`, `{state}`, `{product_name}` from `constituency.yaml`.
- Live schema / sample values are fetched at runtime and wrapped in `<electoral_data>...</electoral_data>` so the model treats them as data, not instructions.

### Local vs production

| Mode | When | How |
|------|------|-----|
| `AGENT_LOCAL_MODE=true` | Local dev / CI | `agents/local_runner.py` runs the same ReAct logic in-process and opens SQLite via `gcs_data` |
| Agent Engine | staging / prod | ADK agents deployed once per environment; tools are HTTP clients that call the backend |

One Agent Engine deployment serves **every** constituency. `constituency_id` is a runtime parameter only.

---

## 6. Auth & tenancy

- Firebase Auth, **Email/Password only**.
- Custom claims on the user:

  ```json
  { "constituency_slugs": ["gyanpur"], "role": "campaign_worker" }
  ```

- Backend never trusts a client-supplied slug that is not in the claim set.
- No self-signup; an admin provisions the small user set.
- Optional Firestore collection for query audit log (question, agent_path, latency).

---

## 7. Secrets

| Secret | Consumer |
|--------|----------|
| `sarvam-api-key` | `/voice/*` |
| `firebase-service-account` (or Workload Identity) | auth middleware |
| `agent-engine-resource-name` | `/agent/chat` |
| `gcs-data-bucket` | `gcs_data` service |

No long-lived Gemini API key is required in production (Agent Engine uses the runtime service account). Local mode uses `GEMINI_API_KEY` from the environment.

---

## 8. CI/CD

```
git push → Cloud Build (asia-south1)
  1. pytest
  2. discover constituencies/*.yaml + validate matching GCS folders
  3. docker build & push → Artifact Registry
  4. gcloud deploy releases create → Cloud Deploy pipeline
       dev (auto) → staging (approval) → prod (approval)
```

Agent Engine has a separate path-filter trigger on `agents/**`.

---

## 9. Observability

- Cloud Run request logs + latency (default).
- Agent Engine tracing (`enable_tracing=True`).
- Optional Firestore `query_log` for “which agent answered what”.

---

## 10. Threat model (prompt injection)

| Risk | Mitigation |
|------|------------|
| Malicious booth name / caste string steers the model | Data is wrapped in `<electoral_data>`; system prompt forbids treating it as instructions |
| Model tries to query another constituency | Impossible: path is derived only from Firebase claims; other files are never opened |
| Model generates destructive SQL | `validate_select()` rejects anything that is not a single SELECT/WITH |
| Model leaks system prompt | Explicit refusal instruction in every agent prompt |

The load-bearing control is **server-side path derivation + SQL validation**. Prompt-level defences are defense-in-depth only.

---

## 11. Onboarding a new constituency (architecture view)

1. Operator prepares the four artifact types.
2. Uploads them to `gs://booth-agent-data-<env>/<new-slug>/`.
3. Adds `constituencies/<new-slug>/constituency.yaml` and pushes.
4. Cloud Build discovers the YAML, checks GCS, regenerates `manifest.json`.
5. Admin adds the slug to the relevant users’ Firebase custom claims.
6. Frontend dropdown shows the new seat; `/agent/chat` works immediately.

No schema migration, no new service, no new agent deployment, no prompt edit.

---

## 12. Build order (implementation sequence)

1. GCS buckets + upload Gyanpur data.
2. FastAPI skeleton + local GCS/SQLite access + auth middleware.
3. Manual Cloud Run deploy; verify `/voter`, `/history`, `/portfolio`.
4. Port agents (local runner first).
5. Wire `/agent/chat`.
6. Deploy Agent Engine; switch off local mode.
7. Voice routes.
8. Full CI/CD + Cloud Deploy.
9. Promote staging → prod.

---

## 13. Identity

Arjun is presented as a **professional political analyst**.  
Answers are always grounded in the selected constituency’s data. The product does not invent numbers, does not cross seat boundaries, and surfaces which specialist agent produced the evidence (`agent_path` in the chat response).

# Build and deployment

The repository supplies three Cloud Build configurations:

| File | Scope |
|---|---|
| `cloudbuild.yaml` | Backend tests, backend build/deploy and frontend build/deploy |
| `cloudbuild.deploy.yaml` | Backend tests and backend build/deploy |
| `cloudbuild.frontend.yaml` | Frontend build/deploy |

Which configuration runs on push depends on triggers configured in GCP. Trigger
existence, branch filters, IAM and current deployment state are not established by
these files. Do not configure both combined and separate triggers for the same
push unless duplicate deployment is intended.

The backend runs the LangGraph harness in-process. Gemini access uses LangChain's
Google integration with API keys or runtime service-account credentials. There is
no separate agent deployment step.

## Backend prerequisites

- A private artifact bucket with per-constituency folders.
- Firebase Auth and users with allowed `constituency_slugs` claims.
- A Firestore database for shared sessions, backend `roles/datastore.user` access,
  and a TTL policy on `expires_at` for private session/turn records.
- Gemini provider access for the runtime service account or a secret API key.
- Tavily/Sarvam secret bindings if public research/voice are enabled.
- Cloud Build permissions to push images and deploy Cloud Run using its runtime
  service account.

Deploy files explicitly set `LOCAL_DEV_AUTH=false`. Cloud Run's public ingress
allows the browser to reach FastAPI; application authentication still requires
Firebase tokens. Keep CORS aligned with the frontend origin.

Cloud files are cached for the process lifetime. Restart/redeploy the backend
after artifact replacement. Data import/upload scripts are maintained separately
from image deployment; inspect their `--help` and preview before executing.

## Local verification

```sh
pip install -r backend/requirements-dev.txt
PYTHONPATH=backend:. python -m unittest discover -s backend/tests -v
pip check
```

Use the maintained routing and fictional conversation scripts for opt-in billed
model evaluations. Offline tests do not establish live provider availability.
The framework migration changes local source only; it does not deploy a service,
change IAM or rewrite private datasets.

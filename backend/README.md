# Backend walkthrough

The backend receives website requests, checks access, and reads the chosen constituency's data.

```text
app/
  main.py       Creates FastAPI and registers routes
  config.py     Reads environment settings
  auth.py       Verifies login and constituency access
  models.py     Validates API inputs and outputs
  routes/       account, chat, elections, booths, voice
  services/     data, agent, speech, SQL validation
```

## Explain a request in an interview

1. FastAPI matches the URL to a function in `routes/`.
2. `auth.py` verifies the Firebase token and constituency permissions.
3. Pydantic models check the request fields.
4. The route calls a service to read data or contact an AI provider.
5. FastAPI returns the response in the existing website format.

For example, `/voter/query` checks access, opens `voters.db` read-only, validates the SQL, returns a limited number of rows, and closes the connection. `/history/query` reuses that flow for `history.db`.

`services/gcs_data.py` is a small compatibility module: the unchanged `agents/local_runner.py` imports these names. The implementation lives in `services/data.py`.

## Local setup

From the repository root, install `backend/requirements.txt` in your virtual environment, then run:

```sh
export PYTHONPATH="$PWD/backend:$PWD"
export LOCAL_DATA_ROOT="$PWD/legacy-data"
export AGENT_LOCAL_MODE=true
uvicorn app.main:app --port 8080 --reload
```

Store each constituency under `legacy-data/<slug>/`. The backend no longer uses a shared flat data folder. The committed constituency manifest is required for the account endpoints.

For cloud use, set `AGENT_LOCAL_MODE=false` to require login. Set `AGENT_ENGINE_RESOURCE_NAME` for the deployed engine; without it, chat still uses the local runner. Set `GCS_DATA_BUCKET` and Google application credentials for cloud storage. Firebase initialization failures now return an error instead of granting local access.

Cloud file downloads are cached for the process lifetime; restart the service to read updated files. Conversation history is in memory, belongs to a user and constituency, and disappears on restart. It is not shared between server instances.

## Verification

Install `httpx` in the development environment for FastAPI's test client, then run from the repository root:

```sh
PYTHONPATH=backend:. python -m unittest discover -s backend/tests -v
```

Tests use temporary fake records and mocked AI/voice calls. They do not verify live Google or Sarvam services.

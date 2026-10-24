# Backend

FastAPI validates authentication, constituency access and request contracts. Chat
calls the LangGraph harness through `app/services/agent.py`; Gemini calls use
LangChain through `agents/model.py`. There is one agent runtime in local and cloud
environments.

```text
app/main.py          API registration
app/config.py        Environment settings
app/auth.py          Firebase authentication and constituency access
app/models.py        API contracts
app/routes/          Account, chat, election data, booths and voice
app/services/        Data access, sessions, agent entry point and speech
```

See the [root README](../README.md) for setup, environment variables, data layout,
Firestore prerequisites and test commands. Development dependencies are in
`requirements-dev.txt`; production ranges are in `requirements.txt` and deployment versions are pinned in
`requirements.lock`.

Authentication defaults to required. For local development only, explicitly set
`LOCAL_DEV_AUTH=true`. This setting does not change the agent runtime. Cloud Run
configurations currently set `LOCAL_DEV_AUTH=true` for the demo frontend. This
allows header-free access to all constituency and voter endpoints until Firebase
sign-in is configured in the frontend.

`services/data.py` is the shared data implementation. Data access uses a
seat-specific folder and read-only SQLite;
cloud downloads are cached for the process lifetime, so restart after replacing
artifacts. Coverage metadata controls whether missing data is available to agents.

The API session store owns user/seat isolation, clarification state, verified
references and concurrency leases. LangGraph invocation state is private and is
not returned through the public chat response.

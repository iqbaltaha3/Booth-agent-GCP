"""Create the API and connect its feature routes."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routes import account, booths, chat, elections, voice

app = FastAPI(title="Booth Agent Backend", version="2.0.0")
# Browsers send Firebase tokens in headers rather than cross-origin cookies.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
for module in (account, booths, chat, elections, voice):
    app.include_router(module.router)


@app.get("/healthz")
def healthz():
    """Let the hosting platform check that the API is running."""
    return {"status": "ok"}

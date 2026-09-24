"""
Booth Agent Backend — FastAPI entrypoint.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers import (
    agent,
    auth,
    constituencies,
    history,
    portfolio,
    voice,
    voter,
)

logging.basicConfig(level=logging.INFO)

app = FastAPI(
    title="Booth Agent Backend",
    description="Arjun — multi-constituency booth-level electoral intelligence",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(constituencies.router)
app.include_router(voter.router)
app.include_router(history.router)
app.include_router(portfolio.router)
app.include_router(voice.router)
app.include_router(agent.router)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}

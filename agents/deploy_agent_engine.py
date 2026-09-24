#!/usr/bin/env python3
"""
Deploy the ADK coordinator to Vertex AI Agent Engine (asia-south1).

Run after the backend is live so the agent tools can call back into it.
"""

from __future__ import annotations

import os

import vertexai
from vertexai.preview import reasoning_engines

# In a full ADK port you would import the LlmAgent graph here.
# For now this script documents the deployment shape.

PROJECT = os.environ.get("GCP_PROJECT", "booth-agent-prod")
LOCATION = os.environ.get("GCP_REGION", "asia-south1")
DISPLAY_NAME = os.environ.get("AGENT_DISPLAY_NAME", "booth-coordinator-prod")


def main() -> None:
    vertexai.init(project=PROJECT, location=LOCATION)

    # Placeholder: replace with the real AdkApp once the ADK agents are wired.
    # from agents.coordinator import coordinator
    # app = reasoning_engines.AdkApp(agent=coordinator, enable_tracing=True)
    # remote = app.deploy(
    #     display_name=DISPLAY_NAME,
    #     requirements=["google-adk", "httpx"],
    # )
    # print(remote.resource_name)

    print(
        f"Would deploy to project={PROJECT} location={LOCATION} "
        f"display_name={DISPLAY_NAME}"
    )
    print(
        "Wire agents/coordinator.py to google.adk.agents.LlmAgent, then "
        "uncomment the deploy block above."
    )


if __name__ == "__main__":
    main()

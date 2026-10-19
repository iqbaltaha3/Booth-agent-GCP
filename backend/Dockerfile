# Build from the repository root:
#   docker build -f backend/Dockerfile -t booth-backend .
FROM python:3.11-slim

WORKDIR /app

# Install dependencies before copying code so builds can reuse this layer.
COPY backend/requirements.lock ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Keep the existing runner and constituency configuration available to the API.
COPY backend/app ./app
COPY agents /agents
COPY constituencies /constituencies

# Make both the API package and bundled agent runner importable.
ENV PYTHONPATH=/app:/
ENV CONSTITUENCIES_MANIFEST_PATH=/constituencies/manifest.json
EXPOSE 8080

# Start the API on the port used by Cloud Run.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]

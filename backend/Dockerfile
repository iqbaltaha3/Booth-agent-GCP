# Build from the repository root:
#   docker build -f backend/Dockerfile -t booth-backend .
FROM python:3.11-slim

WORKDIR /app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app ./app
COPY agents /agents
COPY constituencies /constituencies

ENV PYTHONPATH=/app:/
ENV CONSTITUENCIES_MANIFEST_PATH=/constituencies/manifest.json
EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]

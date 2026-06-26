FROM python:3.12-slim

ARG DUO2API_COMMIT=unknown
ARG DUO2API_BRANCH=main

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DUO2API_COMMIT=${DUO2API_COMMIT} \
    DUO2API_BRANCH=${DUO2API_BRANCH}

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY context.py gitlab_duo_client.py model_catalog.py responses_api.py security.py server.py ./
COPY config.example.json README.md LICENSE ./

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).read()"

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]

FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Worker processes come from $WEB_CONCURRENCY (uvicorn's default for --workers; compose
# defaults it to 1). Each worker is a separate process with its own DB pool, so all
# correctness must hold across processes: it lives in Postgres (row lock, one-snapshot
# fast path, constraints), never in process memory.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]

# Portable across Railway, Render, and Fly.io — all three run a plain
# Docker container the same way. Not tied to any one platform.
FROM python:3.12-slim

WORKDIR /app

# Only api.py's dependencies matter in production — demo.py/demo_real_llm.py
# are dev-only tools and aren't run here.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Most platforms inject $PORT at runtime; 8000 is the fallback for local
# `docker run` testing. Binding to 0.0.0.0 (not 127.0.0.1) is required —
# container platforms route traffic to the container's network interface,
# not localhost.
ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uvicorn api:app --host 0.0.0.0 --port ${PORT}"]

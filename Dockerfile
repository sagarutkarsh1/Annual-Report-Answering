# ReportLens - container image for Render's free web service (512 MB RAM, 0.1 CPU) and any other Docker host
# (Hugging Face Spaces on a paid plan, Fly, Cloud Run, ...).  Render injects PORT; the default 7860 is for everything else.
#
#   docker build -t reportlens .
#   docker run --rm -p 7860:7860 -e PUBLIC_MODE=1 -e REPORTLENS_DEMO_MOCK=1 -e ACCESS_CODE=test reportlens
#   # Render-like limits:  --memory=512m --memory-swap=512m --cpus=0.1   (scripts/render_limits_test.py automates this)
#
# Secrets (OPENAI_API_KEY, ACCESS_CODE, ...) are never baked in: the host injects them as environment variables at run time.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    RAGAS_DO_NOT_TRACK=true \
    OPENAI_AGENTS_DISABLE_TRACING=1 \
    LITELLM_LOCAL_MODEL_COST_MAP=True \
    MALLOC_ARENA_MAX=2

# A non-root user (uid 1000, which Hugging Face also requires).
RUN useradd --create-home --uid 1000 user

# Dependencies first (cached unless requirements-deploy.txt changes). Wheels only: no compiler is needed or installed.
WORKDIR /app
COPY requirements-deploy.txt .
RUN pip install --no-cache-dir -r requirements-deploy.txt \
 && python -c "from importlib import metadata as m; v = m.version('litellm'); assert v not in ('1.82.7', '1.82.8') and tuple(map(int, v.split('.')[:2])) >= (1, 97), 'unsafe litellm ' + v; print('litellm', v)"

# The product (read-only for the runtime user: owned by root) + the offline demo support (mock OpenAI server, sample report).
COPY reportlens ./reportlens
COPY devtools ./devtools
COPY samples ./samples
RUN python -m compileall -q reportlens devtools

# Run-time defaults. Everything here can be overridden by the host (Space variables / Render environment).
#  - data lives on the container's temporary disk and disappears when the host restarts or sleeps the app
#  - PORT: Hugging Face expects 7860; Render injects its own PORT, which wins over this value
#  - PUBLIC_MODE: safe defaults for budget, chat count, per-IP rate limit, upload size and page count (see docs/DEPLOY.md)
ENV HOME=/home/user \
    REPORTLENS_DATA_DIR=/tmp/reportlens \
    REPORTLENS_HOST=0.0.0.0 \
    PORT=7860 \
    PUBLIC_MODE=1

USER user
WORKDIR /home/user
EXPOSE 7860

# /api/health is open and cheap (no OpenAI call). start-period covers the cold start (imports take several seconds).
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('PORT', '7860'), timeout=4)"]

# Exec form: python is PID 1 and receives SIGTERM (sent when the host sleeps or redeploys the service) for a graceful shutdown.
ENV PYTHONPATH=/app
CMD ["python", "-m", "reportlens", "--host", "0.0.0.0"]

FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (layer-cached until requirements change)
COPY tools/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY tools/ tools/

# SQLite database lives on a mounted volume — this dir is the default location
RUN mkdir -p data

EXPOSE 8000

ENV PYTHONUNBUFFERED=1
ENV LOG_LEVEL=INFO

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["uvicorn", "tools.api_server:app", "--host", "0.0.0.0", "--port", "8000"]

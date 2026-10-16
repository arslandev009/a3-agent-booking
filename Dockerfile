# BrightSmile Dental assistant API (sample data only).
# Before building:  python scripts/vendor_llm_client.py     (copies the shared llm-client package into ./vendor)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src:/app/vendor \
    CLINIC_DB=/data/clinic.db

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY data/clinic_faq.md ./data/clinic_faq.md
COPY vendor ./vendor

RUN useradd --create-home --uid 10001 app && mkdir /data && chown app /data
USER app
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status==200 else 1)"

# ONE worker: the calendar is SQLite guarded by an in-process lock (see README, "Scaling").
CMD ["uvicorn", "clinicagent.api:build_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

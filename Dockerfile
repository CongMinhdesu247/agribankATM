FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt ./
RUN apt-get update \
    && apt-get install --no-install-recommends -y gosu \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 appuser \
    && mkdir -p /data /app/uploads \
    && chown -R appuser:appuser /app /data

COPY --chown=appuser:appuser . .
COPY --chmod=755 entrypoint.sh /usr/local/bin/agribank-entrypoint

USER root
EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/agribank-entrypoint"]
CMD ["sh", "-c", "python init_db.py && exec uvicorn app:app --host 0.0.0.0 --port \"${PORT:-8000}\" --workers 1"]


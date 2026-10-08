FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 appuser \
    && mkdir -p /data /app/uploads \
    && chown -R appuser:appuser /app /data

COPY --chown=appuser:appuser . .
USER appuser

EXPOSE 8000
CMD ["sh", "-c", "python init_db.py && exec uvicorn app:app --host 0.0.0.0 --port 8000 --workers 1"]


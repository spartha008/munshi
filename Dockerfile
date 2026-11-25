FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MUNSHI_DB=/var/data/munshi.db \
    PORT=8000

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY prompts ./prompts
COPY data ./data

RUN useradd --create-home --uid 10001 munshi \
    && mkdir -p /var/data && chown -R munshi /var/data /app
USER munshi

EXPOSE 8000
# One worker on purpose: the message worker and scheduler run in-process against one SQLite file.
CMD ["sh", "-c", "uvicorn src.server:app --host 0.0.0.0 --port ${PORT} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]

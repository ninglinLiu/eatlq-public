FROM python:3.11-slim

WORKDIR /app

COPY services/strategy/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY scripts/ /app/scripts/
COPY services/strategy/ /app/services/strategy/

RUN mkdir -p /app/models /app/logs

ENV PYTHONPATH=/app/services/strategy/src:/app
ENV PYTHONUNBUFFERED=1

CMD ["python", "-u", "services/strategy/src/main.py"]

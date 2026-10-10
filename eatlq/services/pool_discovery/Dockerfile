FROM python:3.11-slim

WORKDIR /app

COPY services/pool_discovery/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY services/pool_discovery/ /app/services/pool_discovery/

ENV PYTHONPATH=/app/services/pool_discovery/src:/app
ENV PYTHONUNBUFFERED=1

CMD ["python", "-u", "services/pool_discovery/src/main.py"]

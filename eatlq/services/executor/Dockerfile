FROM python:3.11-slim

WORKDIR /app

COPY services/executor/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY services/executor/ /app/services/executor/

ENV PYTHONPATH=/app/services/executor/src:/app
ENV PYTHONUNBUFFERED=1

CMD ["python", "-u", "services/executor/src/main.py"]

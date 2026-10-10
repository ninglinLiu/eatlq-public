FROM python:3.11-slim
WORKDIR /app

COPY services/signal_bridge/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/signal_bridge/ .
CMD ["python", "-u", "bridge.py"]

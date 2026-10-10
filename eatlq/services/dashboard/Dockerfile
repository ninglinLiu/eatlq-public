FROM python:3.11-slim
WORKDIR /app
COPY services/dashboard/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY services/dashboard/ /app/services/dashboard/
ENV PYTHONUNBUFFERED=1
CMD ["python", "-u", "services/dashboard/server.py"]

FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir redis==5.2.1

COPY services/telegram_bot/bot.py .

CMD ["python", "-u", "bot.py"]

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Moscow

# libgomp1 нужен onnxruntime (зависимость PyMuPDF), tzdata — для корректной
# даты отчётного периода.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py imgToFilds.py report.py storage.py ./
COPY db ./db

# .env копируется, чтобы `docker run portfolio-bot:latest` работал без
# --env-file. Если .env нет, шаблон .env.example всё равно даст собрать образ.
# Переменные из docker compose / --env-file имеют приоритет над файлом.
COPY .env* ./

RUN useradd --create-home --uid 1000 bot \
    && mkdir -p /app/data/photos /app/data/tmp \
    && chown -R bot:bot /app

USER bot

# База и фото переживают перезапуск контейнера.
VOLUME ["/app/data"]

CMD ["python", "-u", "bot.py"]

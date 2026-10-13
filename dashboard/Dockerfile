FROM python:3.12-slim

WORKDIR /app

# ffmpeg renders the pan-and-zoom video for TikTok (/internal/render-video).
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/image_cache

EXPOSE 5000

# --timeout must exceed the 120s relay timeout in app.py; the default 30s kills
# workers mid-decision. --threads keeps an approve from occupying a whole worker
# while it waits on this same process to render its video.
CMD ["gunicorn", "-b", "0.0.0.0:5000", "--workers", "2", "--threads", "4", "--timeout", "180", "app:app"]

FROM python:3.11-slim
WORKDIR /app

# Install system deps (ffmpeg) and cleanup caches
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg build-essential \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8080
EXPOSE 8080
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "app:app"]

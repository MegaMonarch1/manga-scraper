# Bu Dockerfile SADECE scraper servisi icin.
# Render'in normal "Python" ortami root yetkisi vermiyor, bu yuzden
# "playwright install --with-deps" (apt-get gerektirir) hata veriyordu.
# Docker build'i root olarak calistigi icin bu sorunu cozer.

FROM python:3.11-slim

WORKDIR /app

# Playwright/Chromium'un ihtiyac duydugu sistem kutuphaneleri
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    ca-certificates \
    fonts-liberation \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libcairo2 \
    libatspi2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-scraper.txt .
RUN pip install --no-cache-dir -r requirements-scraper.txt

# Chromium'u indir (sistem kutuphaneleri zaten yukarida kuruldu,
# bu yuzden --with-deps yerine sade "install chromium" yeterli)
RUN playwright install chromium

COPY scraper_service.py .

# Render $PORT ortam degiskenini disaridan verir; shell formunda CMD
# kullanmak bu degiskenin okunmasini saglar.
CMD ["sh", "-c", "uvicorn scraper_service:app --host 0.0.0.0 --port $PORT"]

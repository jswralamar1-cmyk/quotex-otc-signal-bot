FROM python:3.11-slim-bullseye

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PLAYWRIGHT_BROWSERS_PATH=/root/.cache/ms-playwright

WORKDIR /app

# تثبيت Chromium وكل المتطلبات النظامية
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    chromium-driver \
    libnss3 \
    libnspr4 \
    libdbus-1-3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libcairo2 \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# تثبيت Python packages
RUN pip install --no-cache-dir numpy requests

# تثبيت pyquotex
RUN pip install --no-cache-dir "pyquotex @ git+https://github.com/cleitonleonel/pyquotex.git"

# تثبيت playwright وربطه بـ Chromium النظامي
RUN pip install --no-cache-dir playwright
RUN playwright install chromium --with-deps 2>/dev/null || true

# نسخ كود البوت
COPY bot.py .

CMD ["python", "bot.py"]

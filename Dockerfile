FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# تثبيت Python وGit والمتطلبات
RUN apt-get update && apt-get install -y \
    python3.11 \
    python3-pip \
    python3.11-dev \
    git \
    curl \
    wget \
    && rm -rf /var/lib/apt/lists/*

# تثبيت Playwright system deps
RUN apt-get update && apt-get install -y \
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
    libwayland-client0 \
    libx11-6 \
    libxext6 \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# تثبيت المكتبات
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# تثبيت Playwright Chromium
RUN python3.11 -m playwright install chromium

# نسخ الكود
COPY . .

# تشغيل البوت
CMD ["python3.11", "bot.py"]

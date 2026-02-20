FROM mcr.microsoft.com/playwright/python:v1.49.0-noble

WORKDIR /app

# تثبيت المتطلبات
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# تثبيت Playwright Chromium
RUN playwright install chromium

# نسخ الكود
COPY . .

# تشغيل البوت
CMD ["python", "bot.py"]

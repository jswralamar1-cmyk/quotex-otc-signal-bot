# استخدام الصورة الرسمية لـ Playwright مع Python
# هذه الصورة تحتوي على Chromium وكل المتطلبات جاهزة
FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy

# منع التفاعل أثناء التثبيت
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# تثبيت numpy أولاً (مطلوب لـ pyquotex)
RUN pip install --no-cache-dir numpy

# نسخ ملف المتطلبات
COPY requirements.txt .

# تثبيت باقي المتطلبات
RUN pip install --no-cache-dir -r requirements.txt

# نسخ كود البوت
COPY bot.py .

# تشغيل البوت
CMD ["python", "bot.py"]

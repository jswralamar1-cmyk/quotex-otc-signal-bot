# Quotex OTC Signal Bot 🤖

بوت إشارات تداول Quotex OTC ذكي يعمل على Telegram.

## المميزات
- مسار P1: ارتداد/إعادة اختبار S/R على فريم 30 دقيقة
- مسار P2: خطوط الترند + EMA Pullback + Micro-Channel على فريم 1 دقيقة
- تحليل AI بـ GPT-4o-mini مع صور الشارت
- متابعة حية للنتائج مع نظام المارتينجال
- تنبيهات مبكرة عند اقتراب السعر من المستويات

## النشر على Railway

### متغيرات البيئة المطلوبة

| المتغير | الوصف |
|---------|-------|
| `TELEGRAM_TOKEN` | توكن بوت Telegram |
| `TELEGRAM_CHAT_ID` | Chat ID لإرسال الإشارات |
| `QUOTEX_EMAIL` | إيميل حساب Quotex |
| `QUOTEX_PASSWORD` | كلمة مرور Quotex (للتجديد التلقائي) |
| `QUOTEX_TOKEN` | توكن Quotex (احتياطي) |
| `OPENAI_API_KEY` | مفتاح OpenAI للتحليل الذكي |
| `PORT` | المنفذ (Railway يضبطه تلقائياً) |
| `SESSION_DB_PATH` | مسار قاعدة بيانات الجلسة (اختياري، الافتراضي: `session_store.db`) |

### خطوات النشر
1. Fork أو اربط الـ repo بـ Railway
2. أضف المتغيرات أعلاه في **Variables** بلوحة تحكم Railway
3. Railway سيشغّل البوت تلقائياً عبر `Procfile`

## الفروع
- `main` — النسخة المستقرة (Replit)
- `vision-ai` — بيئة التطوير (Railway + جلسة SQLite + تجديد تلقائي)

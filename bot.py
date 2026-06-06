"""
=============================================================
  Quotex OTC Ultimate Bot v7
  - RSI + Bollinger Bands
  - WebSocket مباشر (تجاوز Cloudflare HTTP)
  - واجهة ويب لتحديث التوكن (المتصفح)
  - تجديد التوكن عبر Telegram: /token <VALUE>
  - إعادة اتصال تلقائية فور استلام توكن جديد
=============================================================
"""

import asyncio
import time
import os
import re
import json
import logging
import random
import io
from datetime import datetime
from logging.handlers import RotatingFileHandler
import numpy as np

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.gridspec import GridSpec

import requests
from aiohttp import web
from pyquotex.stable_api import Quotex

from ai import AIAdvisor, build_chart_text
import agent_tools

ANALYZE_ASSET_SPEC = {
    "type": "function", "function": {
        "name": "analyze_asset",
        "description": "يجلب بيانات شموع OHLC الحقيقية المخزّنة لأصل OTC ويعيد ملخصاً (السعر، الاتجاه، الدعم/المقاومة، المؤشرات) لتحليله.",
        "parameters": {"type": "object", "properties": {
            "asset": {"type": "string", "description": "اسم الأصل مثل EUR/USD أو EURUSD"},
        }, "required": ["asset"]},
    },
}

VERIFY_ASSET_SPEC = {
    "type": "function", "function": {
        "name": "verify_asset",
        "description": "يعرض بيانات التحقق عبر التوكن لأصل OTC: نسبة العائد (payout %) وهل تتجاوز الحد الأدنى 80%، وحداثة البيانات (عمر آخر شمعة) للتأكد من عدم وجود تأخير.",
        "parameters": {"type": "object", "properties": {
            "asset": {"type": "string", "description": "اسم الأصل مثل EUR/USD أو EURUSD"},
        }, "required": ["asset"]},
    },
}

RESTART_BOT_SPEC = {
    "type": "function", "function": {
        "name": "restart_bot",
        "description": "لا تستخدمه: إعادة تشغيل البوت ذاتياً توقفه على المنصّة. تعديلات شروط الإشارة عبر relax_conditions تُطبَّق حيّاً بلا إعادة تشغيل. أي إعادة تشغيل حقيقية يتولّاها المالك.",
        "parameters": {"type": "object", "properties": {}},
    },
}

RELAX_CONDITIONS_SPEC = {
    "type": "function", "function": {
        "name": "relax_conditions",
        "description": ("يخفّف شروط الإشارة برمجياً لزيادة عدد الإشارات: يوسّع نطاق RSI المرن "
                        "(يرفع حد التشبع البيعي RSI_FLEX_SELL ويخفض حد التشبع الشرائي RSI_FLEX_BUY) بمقدار نقاط، "
                        "ويقلّل انحراف بولينجر BB_STD قليلاً (يؤثر على analyze القديم للوكيل). "
                        "يُطبَّق التعديل حيّاً فوراً بلا إعادة تشغيل. "
                        "استخدمه عندما يطلب المستخدم «خفّف الشروط» أو «زِد عدد الإشارات». "
                        "لتشديد الشروط مرّر قيماً سالبة."),
        "parameters": {"type": "object", "properties": {
            "rsi_points": {"type": "number",
                           "description": "عدد نقاط توسيع نطاق RSI لكل جهة (الافتراضي 2)."},
            "bb_delta": {"type": "number",
                         "description": "مقدار تقليل انحراف بولينجر BB_STD (الافتراضي 0.2)."},
        }},
    },
}

# ─────────────────────────────────────────────
#  إعدادات
# ─────────────────────────────────────────────
QUOTEX_EMAIL     = os.getenv("QUOTEX_EMAIL",     "")
QUOTEX_PASSWORD  = os.getenv("QUOTEX_PASSWORD",  "")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN",   "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
QUOTEX_TOKEN     = os.getenv("QUOTEX_TOKEN",     "")

WEB_PORT       = int(os.getenv("PORT", 3000))   # 3000 تطوير، 80 نشر (Replit VM يضبطها)
RSI_PERIOD     = 14
RSI_OVERSOLD   = 35
RSI_OVERBOUGHT = 65
BB_PERIOD      = 20
BB_STD         = 2.0
CANDLE_PERIOD  = 60
HISTORY_OFFSET = 7200
CHECK_INTERVAL = 5
MAX_CONN_RETRY = 5
MIN_CANDLES      = max(RSI_PERIOD, BB_PERIOD) + 1   # أقل عدد شموع مغلقة لازم للتحليل (RSI/BB)
BACKFILL_SECONDS = 2400        # نافذة الجلب العميق أول مرة (~40 شمعة) لتجاوز سقف history/load (~9 شموع فقط)
BACKFILL_TIMEOUT = 8           # مهلة قصيرة للجلب العميق: يفشل الأصل غير السائل بسرعة بدل تجميد الحلقة 30 ثانية
CANDLE_FETCH_TIMEOUT = 12      # سقف صارم لجلب الشموع الخفيف: يمنع تجمّد الحلقة للأبد إذا أُعيد تدوير السوكِت أثناء الطلب
CANDLE_BUFFER_MAX = 90         # أقصى عدد شموع محفوظة لكل أصل في الذاكرة

RSI_APPROACH   = 6          # هامش "الاقتراب" من حدود RSI لتوليد الإشارة الاستباقية
BB_PROXIMITY   = 0.0010     # نسبة القرب من حد بولينجر تُعد "اقتراباً" (0.10%)
SIGNAL_LEAD    = 50         # أدنى زمن (ثوانٍ) قبل افتتاح شمعة الدخول لإرسال الإشارة. الزمن المتبقي محدود بـ60ث
                            # (آخر شمعة مغلقة تضمن مرور 60ث على الأقل)، فالإرسال يجب أن يصل قبل الافتتاح بـ≥50ث كما طلب المالك
AI_BUDGET         = 5       # ميزانية زمنية (ثوانٍ/timeout) لاستدعاء الذكاء داخل مسار الإشارة. إن تجاوزها يسقط
                            # تحليل الذكاء فقط (fallback) وتُرسَل الرسالة الفنية في وقتها — لا نكسر شرط الـ50ث أبداً
SEND_BUDGET       = 1       # هامش زمني تقديري لنقل رسالة تيليجرام، يُحجَز ضمن البداية لضمان وصول فعلي ≥ SIGNAL_LEAD
SIGNAL_START_LEAD = SIGNAL_LEAD + AI_BUDGET + SEND_BUDGET  # =56: نبدأ المعالجة هنا ليبقى ≥ SIGNAL_LEAD بعد رد الذكاء + النقل
MIN_SEND_LEAD     = SIGNAL_LEAD  # حد أدنى صارم: لا تُرسل أبداً إن قلّ المتبقي عن 50ث (شرط المالك الصريح)
PRE_SIGNAL_MULT   = 3.5          # مضاعف البافر للتنبيه المبكر: السعر ضمن 3.5× buffer = اقتراب واضح
PRE_ALERT_COOLDOWN = 5           # عدد الشموع الحد الأدنى بين تنبيهَين لنفس المستوى على نفس الأصل
# ── المتابعة الحية لنتائج الإشارة والمضاعفات (تعديل نفس الرسالة) ──
MARTINGALE_MAX    = 5       # أقصى عدد مضاعفات تُتابَع بعد الصفقة الأساسية (المستوى 1..5)
LIVE_GRACE        = 8       # ثوانٍ بعد إغلاق شمعة النتيجة قبل قراءتها (لتحديث مخزّن الشموع)
LIVE_FETCH_RETRIES = 3      # محاولات جلب الشموع إن لم تتوفّر شمعة النتيجة في المخزّن
LIVE_RETRY_SLEEP  = 4       # ثوانٍ بين محاولات الجلب
TG_MAX_LEN        = 4000    # هامش آمن تحت حد تيليجرام (4096) — نقتطع أي رسالة أطول لتفادي فشل الإرسال/التعديل
TG_CAPTION_MAX    = 950     # الحد الآمن للـ caption في الصور (تيليجرام يسمح 1024)
TG_EDIT_RETRIES   = 3       # محاولات إعادة عند 429/5xx مع احترام retry_after
# ── فلتر الاتجاه (مقترح خفيف لحماية خطة المضاعفات من الاتجاهات المستمرة) ──
EMA_FAST       = 5          # متوسط أسّي سريع لقياس الاتجاه القصير
EMA_SLOW       = 20         # متوسط أسّي بطيء لقياس الاتجاه العام
TREND_RUN_MIN  = 4          # عدد الشموع المتتالية بنفس اللون الذي يُعد زخماً/اتجاهاً قوياً مستمراً
TREND_EMA_GAP  = 0.0006     # فجوة EMA النسبية التي تُعد اتجاهاً واضحاً (0.06% — واضح وموثوق)
MIN_SCAN_PAYOUT = 80        # الحد الأدنى لنسبة العائد: يُستبعد أي أصل دونه من الفحص وإرسال الإشارة (%) — مخفَّف من 85
CHAT_HISTORY_MAX = 25       # أقصى عدد رسائل محفوظة في ذاكرة المحادثة لكل مستخدم
CHAT_HISTORY_PATH = os.path.join(os.getcwd(), "chat_history.json")  # تُحفظ ذاكرة المحادثة هنا لتبقى بعد إعادة التشغيل
LAG_MAX_AGE    = CANDLE_PERIOD * 2  # أقصى عمر مقبول لآخر شمعة (ثوانٍ) لتفادي التأخير
# ── الاستمرارية عبر إعادة التشغيل: حفظ الشموع والمتابعات على القرص لتفادي فترة الإحماء وفقدان المتابعة ──
CANDLES_PATH   = os.path.join(os.getcwd(), "candles.json")   # مخزّن الشموع يُحفظ هنا لإقلاع فوري بعد إعادة التشغيل
TRACKS_PATH    = os.path.join(os.getcwd(), "tracks.json")    # المتابعات الجارية تُحفظ هنا لاستئنافها بعد إعادة التشغيل
CANDLE_RELOAD_MAX_AGE = 8 * CANDLE_PERIOD   # إن كانت أحدث شمعة محفوظة أقدم من هذا (ثوانٍ) نتجاهلها ونجلب من جديد
TRACK_RESUME_WINDOW   = 30 * 60             # نستأنف فقط المتابعات التي ما زالت جارية أو انتهت حديثاً خلال هذه المدة (ثوانٍ)

# ── فريم 30 دقيقة (Multi-Timeframe Analysis) ────────────────────────────
PERIOD_30M        = 1800              # مدة شمعة 30 دقيقة بالثوانٍ
SR_30M_LIMIT      = 40               # أقصى عدد شموع 30 دقيقة يُجلب (= آخر 20 ساعة تقريباً)
SR_30M_OFFSET     = SR_30M_LIMIT * PERIOD_30M  # = 72000 ثانية
SR_30M_REFRESH    = PERIOD_30M // 2  # تحديث كل 15 دقيقة فقط لتخفيف الضغط على السيرفر

# ── ATR والمناطق الديناميكية ────────────────────────────────────────────
ATR_PERIOD        = 14               # فترة مؤشر ATR على فريم 1 دقيقة
ATR_SR_FACTOR     = 1.4             # هامش حول الدعم/المقاومة = 1.4 × ATR (منطقة، مو خط)
ATR_TREND_FACTOR  = 1.3             # هامش حول خط الترند = 1.3 × ATR (خط الترند له انحراف طبيعي)
PROX_PIPS_FLOOR   = 0.0004          # حد أدنى للبافر = 4 نقاط/Pips (بدلاً من 0.0001 = 1 نقطة)

# ── فلتر الزخم الانفجاري (اختراق حقيقي ≠ ارتداد) ────────────────────
MOMENTUM_CANDLES  = 5               # شموع مرجعية لحساب متوسط جسم الشمعة
MOMENTUM_MULT     = 2.5             # إذا جسم الشمعة > 2.5× متوسط → اختراق → لا إشارة ارتداد

# ── نظام نقاط القوة ─────────────────────────────────────────────────────
SCORE_30M_BASE    = 50              # نقاط المستوى من فريم 30 دقيقة (أساسي)
SCORE_TOUCHES     = 30              # حد أقصى نقاط الارتدادات التاريخية (3+ ارتدادات)
SCORE_RSI_BONUS   = 20              # نقاط تأكيد RSI
RSI_FLEX_BUY      = 48              # حد RSI للتأكيد الشرائي — RSI < 48 = تحيّز شرائي
RSI_FLEX_SELL     = 52              # حد RSI للتأكيد البيعي — RSI > 52 = تحيّز بيعي
SCORE_MIN         = 52              # حد أدنى للنقاط — يرفض الإشارات الضعيفة جداً فقط
SR_MIN_TOUCHES    = 2               # أدنى ارتدادات تاريخية لقبول مستوى S/R

# ── خطوط الترند (فريم 1 دقيقة) ─────────────────────────────────────────
SWING_WINDOW      = 1               # ±شمعة واحدة لرصد القمم/القيعان
MIN_TREND_TOUCHES = 3               # أدنى نقاط ارتداد لتأكيد خط الترند (3 نقاط = ترند حقيقي)

# ── إعادة الاختبار (Role Reversal Retest) ────────────────────────────
RETEST_WINDOW     = 1800            # نافذة مراقبة إعادة الاختبار بالثوانٍ (30 دقيقة)
RETEST_SCORE_BASE = 60              # نقاط أساسية لإشارة إعادة الاختبار

# ── وضع المضاربة: EMA Pullback + Micro-Channel ───────────────────────
EMA_PB_SCORE_BASE = 55             # نقاط أساسية لإشارة الارتداد عند EMA (مرفوعة من 52)
CHANNEL_MIN_TOUCHES = 2            # لمسات الحد الأدنى لتأكيد القناة السعرية
CHANNEL_ATR_WIDTH   = 1.5          # عرض القناة ≤ هذا × ATR لقبولها (ضيقة ومتماسكة)
CHANNEL_SCORE_BASE  = 52           # نقاط أساسية لإشارة القناة

# ── لمسات هندسية ────────────────────────────────────────────────────────
SCORE_PATTERN_BONUS = 25            # نقاط نموذج الشمعة المؤكِّد (Pin Bar / Engulfing / Doji)
SCORE_CONFLUENCE    = 15            # نقاط التقاطع عند توافق مؤشرَين في آنٍ واحد
ATR_DEAD_ZONE       = 0.00004       # نسبة ATR/سعر — أقل منها = سوق ميت (مخفَّفة من 0.00008)

LOG_PATH = os.path.join(os.getcwd(), "bot.log")
_LOG_FMT = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
# ملف السجل يبقى نظيفاً (INFO فأعلى) حتى لا تطغى ضوضاء WebSocket على الأخطاء المهمة
_file_handler = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
_file_handler.setLevel(logging.INFO)
_file_handler.setFormatter(_LOG_FMT)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(), _file_handler],
)
logger = logging.getLogger("QuotexBot")

# ─────────────────────────────────────────────
#  User-Agents
# ─────────────────────────────────────────────
MOBILE_USER_AGENTS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPad; CPU OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.82 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.6312.118 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; Samsung Galaxy S23) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.6261.105 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 12; Redmi Note 11) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.6167.144 Mobile Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Safari/605.1.15",
]


def get_random_ua() -> str:
    return random.choice(MOBILE_USER_AGENTS)


# ─────────────────────────────────────────────
#  session.json
# ─────────────────────────────────────────────
SESSION_PATH = os.path.join(os.getcwd(), "session.json")


def setup_session(token: str, ua: str) -> None:
    data = {QUOTEX_EMAIL: {"token": token, "cookies": f"token={token}; lang=en", "user_agent": ua}}
    with open(SESSION_PATH, "w") as f:
        json.dump(data, f, indent=4)
    logger.info("✅ session.json محدَّث")


def load_current_token() -> str:
    if os.path.exists(SESSION_PATH):
        try:
            with open(SESSION_PATH) as f:
                d = json.load(f)
            t = d.get(QUOTEX_EMAIL, {}).get("token", "")
            if t:
                return t
        except Exception:
            pass
    return QUOTEX_TOKEN


# ─────────────────────────────────────────────
#  Telegram
# ─────────────────────────────────────────────
def _split_msg(text: str, limit: int = 3800):
    """يقسّم الرسائل الطويلة لتجنّب حد Telegram (4096 حرف)."""
    text = text or ""
    if len(text) <= limit:
        return [text]
    chunks, cur = [], ""
    for line in text.split("\n"):
        # سطر واحد أطول من الحد → قسّمه بالقوة
        while len(line) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            if cur:
                chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return chunks


def send_telegram(msg: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
        return r.status_code == 200
    except Exception as e:
        logger.error(f"Telegram: {e}")
        return False


def _tg_clip(msg: str) -> str:
    """يقتطع الرسالة تحت حد تيليجرام (4096 محرفاً) لتفادي فشل الإرسال/التعديل."""
    return msg if len(msg) <= TG_MAX_LEN else msg[:TG_MAX_LEN - 1] + "…"


def tg_send_tracked(msg: str):
    """يرسل رسالة ويعيد message_id (لتعديلها حياً لاحقاً في لوحة المتابعة) أو None عند الفشل."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return None
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": _tg_clip(msg), "parse_mode": "HTML"},
            timeout=10,
        )
        if r.status_code == 200:
            return r.json().get("result", {}).get("message_id")
        logger.error(f"Telegram send: HTTP {r.status_code} {_tg_desc(r)}")
        return None
    except Exception as e:
        logger.error(f"Telegram send: {e}")
        return None


def _tg_desc(r) -> str:
    try:
        return r.json().get("description", "")
    except Exception:
        return ""


def tg_edit_caption(message_id, caption: str) -> bool:
    """يعدّل الـ caption لرسالة صورة مُرسَلة سابقاً. يعيد المحاولة عند 429/5xx."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID or not message_id:
        return False
    text = caption[:TG_CAPTION_MAX]
    for _ in range(TG_EDIT_RETRIES):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageCaption",
                data={"chat_id": TELEGRAM_CHAT_ID, "message_id": message_id,
                      "caption": text, "parse_mode": "HTML"},
                timeout=10,
            )
            if r.status_code == 200:
                return True
            desc = _tg_desc(r)
            if "not modified" in desc.lower():
                return True
            if r.status_code == 429:
                try:
                    wait = int(r.json().get("parameters", {}).get("retry_after", 2))
                except Exception:
                    wait = 2
                time.sleep(min(wait, 10))
                continue
            if 500 <= r.status_code < 600:
                time.sleep(1)
                continue
            logger.debug(f"Telegram editCaption HTTP {r.status_code}: {desc}")
            return False
        except Exception as e:
            logger.debug(f"Telegram editCaption: {e}")
            time.sleep(1)
    return False


def tg_edit(message_id, msg: str) -> bool:
    """يعدّل نص رسالة مُرسَلة سابقاً (التحديث الحي للنتائج). يعيد المحاولة عند 429/5xx
    ويحترم retry_after؛ ويعتبر "not modified" نجاحاً (لا تغيير فعلي)."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID or not message_id:
        return False
    text = _tg_clip(msg)
    for _ in range(TG_EDIT_RETRIES):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText",
                data={"chat_id": TELEGRAM_CHAT_ID, "message_id": message_id,
                      "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
            if r.status_code == 200:
                return True
            desc = _tg_desc(r)
            if "not modified" in desc.lower():
                return True
            if r.status_code == 429:
                try:
                    wait = int(r.json().get("parameters", {}).get("retry_after", 2))
                except Exception:
                    wait = 2
                time.sleep(min(wait, 10))
                continue
            if 500 <= r.status_code < 600:
                time.sleep(1)
                continue
            logger.debug(f"Telegram edit HTTP {r.status_code}: {desc}")
            return False
        except Exception as e:
            logger.debug(f"Telegram edit: {e}")
            time.sleep(1)
    return False


def tg_send_photo(photo_bytes: bytes, caption: str = "") -> int | None:
    """يرسل صورة PNG إلى تيليجرام من الذاكرة (BytesIO) ويعيد message_id أو None."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID or not photo_bytes:
        return None
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
            data={"chat_id": TELEGRAM_CHAT_ID,
                  "caption": caption[:1020] if caption else "",
                  "parse_mode": "HTML"},
            files={"photo": ("chart.png", photo_bytes, "image/png")},
            timeout=20,
        )
        if r.status_code == 200:
            return r.json().get("result", {}).get("message_id")
        logger.debug(f"Telegram photo: HTTP {r.status_code} {_tg_desc(r)}")
        return None
    except Exception as e:
        logger.debug(f"Telegram photo: {e}")
        return None


def _ema_series(values: list, period: int) -> list:
    """EMA بسيطة على قائمة قيم — تُعيد قائمة بنفس الطول."""
    if not values:
        return []
    k, out = 2 / (period + 1), [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def _rsi_series(closes: list, period: int = 14) -> tuple[list, list]:
    """يحسب RSI ويعيد (x_indices, rsi_values) للرسم."""
    if len(closes) < period + 2:
        return [], []
    gains  = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    ag = sum(gains[:period])  / period
    al = sum(losses[:period]) / period
    rsi_vals = []
    for i in range(period, len(closes) - 1):
        ag = (ag * (period - 1) + gains[i])  / period
        al = (al * (period - 1) + losses[i]) / period
        rs = ag / al if al > 1e-9 else 100.0
        rsi_vals.append(100 - 100 / (1 + rs))
    x = list(range(period + 1, len(closes)))
    return x, rsi_vals


def draw_signal_chart(candles_1m: list, result: dict,
                      direction: str, sig_type: str, asset: str) -> bytes | None:
    """يرسم شارت OHLC احترافي بخلفية داكنة مع مستويات الإشارة ويعيد PNG كـ bytes.

    الرسم يتضمّن:
      • شموع يابانية ملوّنة (أخضر/أحمر)
      • EMA5 و EMA20
      • مستويات S/R / قناة / خط ترند خاصة بالإشارة
      • سهم اتجاه عند آخر شمعة
      • نافذة RSI(14) في اللوح السفلي
    """
    try:
        now    = time.time()
        closed = [c for c in candles_1m
                  if isinstance(c, dict) and c.get("time", 0) + CANDLE_PERIOD <= now]
        if len(closed) < 20:
            return None
        seg = closed[-45:]                           # نافذة 45 شمعة للعرض

        def _f(c, k, fallback=0.0):
            return float(c.get(k, fallback))

        idx    = list(range(len(seg)))
        opens  = [_f(c, "open",  _f(c, "close")) for c in seg]
        closes = [_f(c, "close", _f(c, "open"))  for c in seg]
        highs  = [_f(c, "high",  max(_f(c,"open"),  _f(c,"close"))) for c in seg]
        lows   = [_f(c, "low",   min(_f(c,"open"), _f(c,"close"))) for c in seg]

        if not closes or closes[-1] <= 0:
            return None

        ema5  = _ema_series(closes, EMA_FAST)
        ema20 = _ema_series(closes, EMA_SLOW)
        rsi_x, rsi_v = _rsi_series(closes, RSI_PERIOD)

        px_hi = max(highs);  px_lo = min(lows)
        px_rng = max(px_hi - px_lo, closes[-1] * 0.0001)
        last_i = len(seg) - 1

        # ── إعداد الشكل ──────────────────────────────────────────
        fig = plt.figure(figsize=(10, 5.8), facecolor="#0d0d0d")
        gs  = GridSpec(2, 1, height_ratios=[2.8, 1.0], hspace=0.04, figure=fig,
                       top=0.91, bottom=0.07, left=0.04, right=0.93)
        ax1 = fig.add_subplot(gs[0])
        ax2 = fig.add_subplot(gs[1], sharex=ax1)
        for ax in (ax1, ax2):
            ax.set_facecolor("#111111")
            for sp in ax.spines.values():
                sp.set_edgecolor("#2a2a2a")

        # ── شموع OHLC ────────────────────────────────────────────
        W_BODY, W_WICK = 0.55, 0.9
        for i, (o, h, l, c) in enumerate(zip(opens, highs, lows, closes)):
            col = "#26a69a" if c >= o else "#ef5350"
            ax1.plot([i, i], [l, h], color=col, linewidth=W_WICK, zorder=1)
            ax1.bar(i, max(abs(c - o), px_rng * 0.001),
                    bottom=min(o, c), width=W_BODY, color=col, zorder=2)

        # ── EMA ──────────────────────────────────────────────────
        ax1.plot(idx, ema5,  color="#f0c030", linewidth=1.1,
                 label=f"EMA{EMA_FAST}", zorder=3, alpha=0.9)
        ax1.plot(idx, ema20, color="#4fc3f7", linewidth=1.1,
                 label=f"EMA{EMA_SLOW}", zorder=3, alpha=0.9)

        # ── مستويات الإشارة ───────────────────────────────────────
        def _hline(ax, y, color, label="", ls="--", lw=1.5):
            ax.axhline(y, color=color, linestyle=ls, linewidth=lw, alpha=0.85, zorder=4)
            if label:
                fmt_y = f"{y:.5f}" if y < 10 else f"{y:.4f}" if y < 100 else f"{y:.2f}"
                ax.text(last_i + 0.6, y, fmt_y,
                        color=color, fontsize=7.5, va="center", fontweight="bold")

        arrow_col = "#4caf50" if direction == "CALL" else "#ef5350"
        current_px = closes[-1]

        # ── مستويات S/R الخلفية (من 30م) ─────────────────────────────
        sr_levels = result.get("sr_levels", [])
        signal_level = (result.get("level") or result.get("channel_hi")
                        or result.get("channel_lo") or result.get("tl_value")
                        or result.get("ema_fast") or 0)
        drawn_sr = 0
        for lvl_info in sr_levels[:8]:
            lp = lvl_info.get("price", 0)
            if not lp or drawn_sr >= 5:
                break
            # تجنّب رسم مستوى الإشارة الرئيسي مرة ثانية (هامش 2 pip)
            if signal_level and abs(lp - signal_level) < (result.get("atr", 0) or 0) * 0.3:
                continue
            # لون خافت: أخضر للدعم، أحمر للمقاومة
            sr_col = "#2e7d52" if lp < current_px else "#7b2b2b"
            ax1.axhline(lp, color=sr_col, linestyle=":", linewidth=0.9,
                        alpha=0.6, zorder=2)
            fmt_lp = f"{lp:.5f}" if lp < 10 else f"{lp:.4f}" if lp < 100 else f"{lp:.2f}"
            ax1.text(last_i + 0.6, lp, fmt_lp,
                     color=sr_col, fontsize=6.5, va="center", alpha=0.75)
            drawn_sr += 1

        # ── مستوى الإشارة الرئيسي (بارز) ─────────────────────────────
        if sig_type == "reversal":
            lvl   = result.get("level", 0)
            lcolor = "#4caf50" if direction == "CALL" else "#ef5350"
            if lvl:
                _hline(ax1, lvl, lcolor, "S/R")
        elif sig_type == "retest":
            lvl = result.get("level", 0)
            if lvl:
                _hline(ax1, lvl, "#ff9800", "Retest")
        elif sig_type == "micro_channel":
            ch_hi = result.get("channel_hi", 0)
            ch_lo = result.get("channel_lo", 0)
            if ch_hi and ch_lo:
                _hline(ax1, ch_hi, "#ef5350", "CH Hi")
                _hline(ax1, ch_lo, "#4caf50", "CH Lo")
                ax1.fill_between(idx, ch_lo, ch_hi,
                                 alpha=0.06, color="#ffffff", zorder=1)
        elif sig_type == "ema_pullback":
            ef = result.get("ema_fast", 0)
            if ef:
                _hline(ax1, ef, "#f0c030", "EMA", ls=":")
        elif sig_type == "trendline":
            tv = result.get("tl_value", 0)
            if tv:
                _hline(ax1, tv, "#ce93d8", "TL")

        # ── سهم اتجاه الإشارة ─────────────────────────────────────
        pad    = px_rng * 0.055
        base_y = lows[-1] - pad  if direction == "CALL" else highs[-1] + pad
        tip_y  = base_y + pad   if direction == "CALL" else base_y - pad
        ax1.annotate("", xy=(last_i, tip_y), xytext=(last_i, base_y),
                     arrowprops=dict(arrowstyle="->", color=arrow_col,
                                     lw=2.8, mutation_scale=16), zorder=6)
        # تمييز منطقة الإشارة
        ax1.axvspan(last_i - 2.5, last_i + 0.5, alpha=0.07,
                    color=arrow_col, zorder=1)

        # ── إعدادات المحور الأول ──────────────────────────────────
        _SIG = {"reversal": "Reversal S/R", "retest": "Retest",
                "trendline": "Trendline", "ema_pullback": "EMA PB",
                "micro_channel": "Channel"}
        dir_lbl = "CALL ▲" if direction == "CALL" else "PUT ▼"
        score   = result.get("score", 70)
        rsi_v_last = f"  RSI {rsi_v[-1]:.0f}" if rsi_v else ""
        ax1.set_title(
            f"{fmt(asset)}  |  {dir_lbl}  |  {_SIG.get(sig_type, sig_type)}"
            f"  |  Score {score}%{rsi_v_last}",
            color="white", fontsize=11, fontweight="bold", pad=5)
        ax1.tick_params(colors="#666666", labelsize=7.5)
        ax1.yaxis.tick_right()
        ax1.set_xlim(-1, last_i + 3.5)
        ax1.set_ylim(px_lo - px_rng * 0.08, px_hi + px_rng * 0.12)
        ax1.xaxis.set_visible(False)
        ax1.legend(loc="upper left", fontsize=7.5, facecolor="#1a1a1a",
                   edgecolor="#333333", labelcolor="white", framealpha=0.8)
        ax1.grid(axis="y", color="#1e1e1e", linewidth=0.7, zorder=0)

        # ── نافذة RSI ─────────────────────────────────────────────
        if rsi_v:
            ax2.plot(rsi_x, rsi_v, color="#ce93d8", linewidth=1.2, zorder=2)
            ax2.axhline(70, color="#ef5350", linestyle="--", linewidth=0.7, alpha=0.6)
            ax2.axhline(50, color="#444444", linestyle="-",  linewidth=0.5, alpha=0.6)
            ax2.axhline(30, color="#4caf50", linestyle="--", linewidth=0.7, alpha=0.6)
            ax2.fill_between(rsi_x, rsi_v, 50,
                             where=[v >= 50 for v in rsi_v],
                             color="#ef5350", alpha=0.08, zorder=1)
            ax2.fill_between(rsi_x, rsi_v, 50,
                             where=[v < 50 for v in rsi_v],
                             color="#4caf50", alpha=0.08, zorder=1)
            ax2.set_ylim(10, 90)
            ax2.set_yticks([30, 50, 70])
            ax2.tick_params(colors="#666666", labelsize=7.5)
            ax2.yaxis.tick_right()
            ax2.set_ylabel("RSI", color="#666666", fontsize=7.5, labelpad=3)
            ax2.grid(axis="y", color="#1e1e1e", linewidth=0.7, zorder=0)
            if rsi_v:
                ax2.text(rsi_x[-1] + 0.4, rsi_v[-1], f"{rsi_v[-1]:.0f}",
                         color="#ce93d8", fontsize=7.5, va="center", fontweight="bold")

        # ── تسميات محور الوقت ─────────────────────────────────────
        n    = len(seg)
        step = max(1, n // 7)
        tpos, tlbl = [], []
        for i in range(0, n, step):
            t = seg[i].get("time", 0)
            tpos.append(i)
            tlbl.append(datetime.fromtimestamp(t).strftime("%H:%M") if t else "")
        ax2.set_xticks(tpos)
        ax2.set_xticklabels(tlbl, color="#666666", fontsize=7.5)

        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=130,
                    bbox_inches="tight", facecolor=fig.get_facecolor())
        plt.close(fig)
        buf.seek(0)
        return buf.getvalue()

    except Exception as e:
        logger.debug(f"draw_chart({asset}): {e}")
        return None


_TG_LAST_ID_FILE = ".last_tg_id"   # يحفظ آخر update_id لتجنّب إعادة معالجة الرسائل بعد الإعادة

class TelegramCmdHandler:
    def __init__(self, bot_ref=None):
        self.bot                   = bot_ref
        self.last_id:    int       = self._load_last_id()
        self.new_token:  str | None = None
        self._running:   bool      = False

    @staticmethod
    def _load_last_id() -> int:
        try:
            with open(_TG_LAST_ID_FILE) as f:
                return int(f.read().strip())
        except Exception:
            return 0

    @staticmethod
    def _save_last_id(uid: int):
        try:
            with open(_TG_LAST_ID_FILE, "w") as f:
                f.write(str(uid))
        except Exception:
            pass

    async def start(self):
        self._running = True
        logger.info("📲 Telegram listener نشط (/token /status /help)")
        while self._running:
            try:
                await self._poll()
            except Exception as e:
                logger.debug(f"TG poll: {e}")
            await asyncio.sleep(5)

    def stop(self):
        self._running = False

    async def _poll(self):
        if not TELEGRAM_TOKEN:
            return
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"offset": self.last_id + 1, "timeout": 5},
            timeout=12,
        )
        if not r.ok:
            return
        for upd in r.json().get("result", []):
            self.last_id = upd["update_id"]
            self._save_last_id(self.last_id)
            msg     = upd.get("message", {})
            chat_id = str(msg.get("chat", {}).get("id", ""))
            user_id = str(msg.get("from", {}).get("id", "")) or chat_id
            text    = (msg.get("text") or "").strip()
            if chat_id != TELEGRAM_CHAT_ID:
                continue
            if text.lower().startswith("/token "):
                tok = text[7:].strip()
                if len(tok) > 10:
                    self.new_token = tok
                    send_telegram("✅ <b>توكن مستلم — جاري الاتصال...</b>")
                else:
                    send_telegram("❌ التوكن قصير جداً")
            elif text.lower() == "/status":
                send_telegram(self._status_report())
            elif text.lower() == "/restart":
                send_telegram("♻️ <b>جاري إعادة تشغيل البوت لتطبيق التغييرات...</b>\nسأعود خلال ثوانٍ.")
                # تأكيد معالجة هذا التحديث حتى لا يتكرر بعد إعادة التشغيل (يمنع حلقة إعادة تشغيل)
                acked = False
                try:
                    r = requests.get(
                        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                        params={"offset": self.last_id + 1, "timeout": 1},
                        timeout=8,
                    )
                    acked = bool(r.ok)
                except Exception as e:
                    logger.error(f"restart ack failed: {e}")
                if not acked:
                    send_telegram("⚠️ تعذّر تأكيد الرسالة مع Telegram — أُلغيت إعادة التشغيل "
                                  "لتفادي حلقة تكرار. أعد إرسال /restart بعد قليل.")
                else:
                    logger.info("♻️ إعادة تشغيل بطلب من المستخدم (/restart)")
                    import sys
                    os.execv(sys.executable, [sys.executable] + sys.argv)
            elif text.lower() == "/help":
                send_telegram(
                    "📋 <b>الأوامر المتاحة:</b>\n"
                    "/status — كل تفاصيل البوت الآن 📊\n"
                    "/token &lt;VALUE&gt; — تحديث التوكن يدوياً\n"
                    "/ai &lt;سؤالك&gt; — اسأل الوكيل الذكي 🧠\n"
                    "/restart — إعادة تشغيل البوت (لتطبيق تعديلات الكود) ♻️\n"
                    "/help — هذه القائمة\n\n"
                    "<b>الوكيل الذكي 🤖:</b> اكتب أي رسالة عادية. صار يقدر:\n"
                    "• يقرأ كود المشروع كامل ويشرحه لك (مثال: «اطلع ع الكود وقل لي شلون يشتغل»)\n"
                    "• يعدّل الكود حسب طلبك ويعيد التشغيل تلقائياً (مثال: «غيّر عدد لمسات الاتجاه إلى 4»)\n"
                    "• يحلّل شارت أي أصل بالبيانات الحقيقية (مثال: «حلل EUR/USD»)\n"
                    "• يتحقق من العائد والتأخير لأي أصل (مثال: «تحقق من EUR/USD»)\n"
                    "• يخفّف شروط الإشارة لزيادة عددها عند طلبك (مثال: «خفّف الشروط شوي»)\n"
                    "• يقرأ ملف السجل لتشخيص الأخطاء (مثال: «شوف السجل وقل لي وين المشكلة»)\n"
                    "🧠 صار يتذكّر سياق محادثتك السابقة فلا ينسى ما اتفقنا عليه.\n\n"
                    "🛡️ إشارة واحدة استباقية لكل صفقة تصلك قبل افتتاح الشمعة بنحو دقيقة — "
                    "ادخل مع افتتاح الشمعة القادمة (العائد ≥ 80% وبيانات حديثة). "
                    "ثم تُحدَّث نفس الرسالة حياً شمعةً بشمعة لتعرض نتيجة الصفقة الأساسية "
                    "ثم المضاعفات (حتى الخامسة) حتى أول ربح أو نفادها.\n"
                    "🔒 الأسرار/المفاتيح محمية، وملف التوكن للقراءة فقط (قيمه محجوبة)، وكل تعديل "
                    "تُؤخذ له نسخة احتياطية ويُفحص قبل الحفظ.\n\n"
                    "<b>أو</b> افتح معاينة Replit وأدخل التوكن في الصفحة"
                )
            elif text.startswith("/"):
                # أمر غير معروف يبدأ بـ / — مرّره للذكاء الاصطناعي إن كان /ai
                if text.lower().startswith("/ai"):
                    q = text[3:].strip()
                    if q:
                        await self._route_to_ai(q, user_id)
                    else:
                        send_telegram("🧠 اكتب سؤالك بعد /ai — مثال:\n<code>/ai حلل EUR/USD</code>")
                else:
                    send_telegram("❓ أمر غير معروف. أرسل /help لرؤية الأوامر.")
            elif text:
                # نص حر → المحلل الذكي
                await self._route_to_ai(text, user_id)

    # regex يحذف جملة اعتذار GPT الخاطئة بأي تنويع في الترميز أو الترقيم
    _APOLOGY_RE = re.compile(
        r'عذر[اً\u0627\u064b]+،?\s*واجهت\s+مشكلة\s+في\s+الاتصال\s+ب[ـ]?الذكاء\s+الاصطناعي[.،؟]?',
        re.UNICODE
    )

    async def _route_to_ai(self, text: str, user_id: str = "default"):
        if self.bot is None:
            send_telegram("🧠 الذكاء الاصطناعي غير متاح حالياً.")
            return
        send_telegram("🧠 <i>المحلل الذكي يفكّر...</i>")
        try:
            reply = await self.bot.handle_ai_message(text, user_id)
        except Exception as e:
            logger.error(f"AI route: {e}")
            reply = "⚠️ تعذّر الحصول على رد من المحلل الذكي حالياً."
        # تنظيف جملة الاعتذار الخاطئة بأي تنويع كان
        reply = self._APOLOGY_RE.sub("", reply).strip().lstrip(".\n")
        if not reply:
            reply = "🧠 تم استلام طلبك. أعد صياغة سؤالك بشكل أكثر تحديداً."
        for chunk in _split_msg(reply):
            send_telegram(chunk)

        # أُزيلت إعادة التشغيل الذاتية عبر os.execv: على منصّة Replit تفصل العملية عن
        # مشرف الـ workflow فيتوقف البوت نهائياً. تعديلات الشروط تُطبَّق الآن حيّاً في
        # الذاكرة بلا إعادة تشغيل؛ هذا الحارس يكتفي بمسح أي طلب قديم إن وُجد.
        if getattr(self.bot, "request_restart", False):
            self.bot.request_restart = False

    def _fmt_uptime(self, secs: float) -> str:
        secs = int(secs)
        d, secs = divmod(secs, 86400)
        h, secs = divmod(secs, 3600)
        m, _    = divmod(secs, 60)
        parts = []
        if d: parts.append(f"{d} يوم")
        if h: parts.append(f"{h} ساعة")
        parts.append(f"{m} دقيقة")
        return " ".join(parts)

    def _status_report(self) -> str:
        b = self.bot
        if b is None:
            tok = load_current_token()
            return f"📊 <b>حالة البوت</b>\n🔑 التوكن: <code>{tok[:12]}...</code>"

        # حالة الاتصال
        if b.connected:
            conn = "🟢 متصل ويعمل"
        elif b.waiting_token:
            conn = "🔴 التوكن منتهي — بانتظار التجديد"
        else:
            conn = "🟡 جاري إعادة الاتصال..."

        tok = b.active_token or ""
        uptime = self._fmt_uptime(time.time() - b.start_time)

        pend_lines = ""

        last = b.last_signal or "لا توجد إشارات بعد"
        last_scan = "—"
        if b.last_scan_t:
            ago = int(time.time() - b.last_scan_t)
            last_scan = f"قبل {ago} ثانية"

        return (
            f"📊 <b>تقرير البوت الكامل</b>\n"
            f"━━━━━━━━━━━━━━\n"
            f"📡 الحالة: {conn}\n"
            f"⏱️ مدة التشغيل: {uptime}\n"
            f"📋 الأصول المراقَبة: <b>{len(b.otc_assets)}</b> أصل OTC\n"
            f"🔄 آخر فحص: {last_scan}\n"
            f"━━━━━━━━━━━━━━\n"
            f"📈 <b>الاستراتيجية:</b>\n"
            f"  • إشارات ارتداد من مستويات 30م (دعم/مقاومة + ATR + RSI)\n"
            f"  • إشارات اختراق خط الاتجاه (3+ لمسات على 1م)\n"
            f"  • إشارة واحدة مدمجة (فنية + تحليل ذكي) قبل افتتاح الشمعة بـ≥{SIGNAL_LEAD}ث\n"
            f"  • التحقق: العائد ≥ {MIN_SCAN_PAYOUT}% + بيانات حديثة (بدون تأخير)\n"
            f"━━━━━━━━━━━━━━\n"
            f"📨 <b>الإحصائيات:</b>\n"
            f"  • إشارات مُرسَلة: <b>{b.signals_sent}</b>\n"
            f"  • آخر إشارة: {last}\n"
            f"{pend_lines}"
            f"━━━━━━━━━━━━━━\n"
            f"🔑 التوكن: <code>{tok[:12]}...</code>"
        )


# ─────────────────────────────────────────────
#  خادم الويب (واجهة تحديث التوكن)
# ─────────────────────────────────────────────
WEB_TOKEN_QUEUE: asyncio.Queue = None  # يُهيّأ في run()

HTML_PAGE = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Quotex OTC Bot v7</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0 }}
  body {{
    font-family: 'Segoe UI', Arial, sans-serif;
    background: linear-gradient(135deg, #0f0f1a 0%, #1a1a2e 50%, #16213e 100%);
    min-height: 100vh; display: flex; align-items: center; justify-content: center;
    padding: 20px;
  }}
  .card {{
    background: rgba(255,255,255,.05);
    border: 1px solid rgba(255,255,255,.1);
    border-radius: 20px;
    padding: 40px;
    width: 100%;
    max-width: 520px;
    backdrop-filter: blur(10px);
    box-shadow: 0 20px 60px rgba(0,0,0,.5);
  }}
  .logo {{ font-size: 48px; text-align: center; margin-bottom: 8px }}
  h1 {{ color: #fff; font-size: 22px; text-align: center; margin-bottom: 4px }}
  .sub {{ color: #888; font-size: 13px; text-align: center; margin-bottom: 28px }}
  .status {{
    display: flex; align-items: center; gap: 10px;
    background: rgba(255,255,255,.05);
    border-radius: 12px; padding: 12px 16px; margin-bottom: 28px;
  }}
  .dot {{
    width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0;
    animation: pulse 2s infinite;
  }}
  .dot.red   {{ background: #ff4444 }}
  .dot.green {{ background: #00cc66 }}
  @keyframes pulse {{ 0%,100%{{opacity:1}} 50%{{opacity:.4}} }}
  .status-text {{ color: #ccc; font-size: 14px }}
  label {{ color: #aaa; font-size: 13px; display: block; margin-bottom: 8px }}
  .steps {{
    background: rgba(0,150,255,.08);
    border: 1px solid rgba(0,150,255,.2);
    border-radius: 12px; padding: 16px; margin-bottom: 20px;
  }}
  .steps p {{ color: #89b4fa; font-size: 13px; line-height: 1.8 }}
  .steps code {{
    background: rgba(255,255,255,.1); padding: 2px 6px;
    border-radius: 4px; font-family: monospace; color: #fff;
  }}
  input[type=text] {{
    width: 100%; padding: 14px 16px;
    background: rgba(255,255,255,.08);
    border: 1px solid rgba(255,255,255,.15);
    border-radius: 12px; color: #fff; font-size: 14px;
    margin-bottom: 16px; outline: none; direction: ltr;
    transition: border .2s;
  }}
  input[type=text]:focus {{ border-color: #4f8ef7 }}
  input[type=text]::placeholder {{ color: #555 }}
  button {{
    width: 100%; padding: 14px;
    background: linear-gradient(135deg, #4f8ef7, #7c3aed);
    border: none; border-radius: 12px; color: #fff;
    font-size: 16px; font-weight: 600; cursor: pointer;
    transition: opacity .2s, transform .1s;
  }}
  button:hover {{ opacity: .9 }}
  button:active {{ transform: scale(.98) }}
  .msg {{
    margin-top: 16px; padding: 12px 16px;
    border-radius: 10px; font-size: 14px; text-align: center;
    display: none;
  }}
  .msg.ok  {{ background: rgba(0,200,100,.15); color: #00cc66; border: 1px solid rgba(0,200,100,.3) }}
  .msg.err {{ background: rgba(255,60,60,.1);  color: #ff6666; border: 1px solid rgba(255,60,60,.3) }}
  .divider {{ color: #444; font-size: 12px; text-align: center; margin: 16px 0 }}
  .tg-hint {{
    background: rgba(0,120,200,.08);
    border: 1px solid rgba(0,120,200,.2);
    border-radius: 10px; padding: 12px 16px;
    color: #89b4fa; font-size: 12px; line-height: 1.7;
  }}
  .tg-hint code {{
    background: rgba(255,255,255,.1); padding: 2px 6px;
    border-radius: 4px; font-family: monospace; color: #fff;
  }}
</style>
</head>
<body>
<div class="card">
  <div class="logo">🤖</div>
  <h1>Quotex OTC Bot v8</h1>
  <p class="sub">WebSocket Direct · ارتداد 30م + خطوط الاتجاه</p>

  <div class="status">
    <div class="dot {dot_class}"></div>
    <span class="status-text">{status_text}</span>
  </div>

  <div class="steps">
    <p>
      1️⃣ افتح <b>qxbroker.com</b> وسجّل دخولك<br>
      2️⃣ اضغط <b>F12</b> (Developer Tools)<br>
      3️⃣ انتقل: <code>Application</code> → <code>Cookies</code> → <code>qxbroker.com</code><br>
      4️⃣ انسخ قيمة الكوكي باسم <code>token</code><br>
      5️⃣ الصق القيمة أدناه واضغط تحديث
    </p>
  </div>

  <label>التوكن الجديد:</label>
  <input type="text" id="token" placeholder="الصق قيمة token هنا..." autocomplete="off" spellcheck="false">
  <button onclick="submit()">⚡ تحديث التوكن والاتصال</button>
  <div class="msg" id="msg"></div>

  <div class="divider">— أو عبر Telegram —</div>
  <div class="tg-hint">
    أرسل للبوت: <code>/token &lt;القيمة&gt;</code><br>
    أوامر أخرى: <code>/status</code> · <code>/help</code>
  </div>
</div>

<script>
async function submit() {{
  const tok = document.getElementById('token').value.trim();
  const msg = document.getElementById('msg');
  if (tok.length < 15) {{
    msg.className = 'msg err'; msg.style.display = 'block';
    msg.textContent = '❌ التوكن قصير جداً — تأكد من نسخه كاملاً'; return;
  }}
  msg.className = 'msg'; msg.style.display = 'none';
  const btn = document.querySelector('button');
  btn.disabled = true; btn.textContent = '⏳ جاري التحديث...';
  try {{
    const r = await fetch('/update_token', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{token: tok}})
    }});
    const d = await r.json();
    if (d.ok) {{
      msg.className = 'msg ok'; msg.style.display = 'block';
      msg.textContent = '✅ تم التحديث! البوت يتصل الآن...';
      document.getElementById('token').value = '';
      setTimeout(() => location.reload(), 3000);
    }} else {{
      msg.className = 'msg err'; msg.style.display = 'block';
      msg.textContent = '❌ خطأ: ' + (d.error || 'حاول مرة أخرى');
    }}
  }} catch(e) {{
    msg.className = 'msg err'; msg.style.display = 'block';
    msg.textContent = '❌ تعذّر الاتصال بالخادم';
  }} finally {{
    btn.disabled = false; btn.textContent = '⚡ تحديث التوكن والاتصال';
  }}
}}
document.getElementById('token').addEventListener('keydown', e => {{
  if (e.key === 'Enter') submit();
}});
</script>
</body>
</html>"""


class WebServer:
    def __init__(self, bot_ref):
        self.bot  = bot_ref
        self.app  = web.Application()
        self.app.router.add_get("/",              self._index)
        self.app.router.add_post("/update_token", self._update_token)

    def _status_info(self):
        connected = self.bot.connected
        if connected:
            return "green", f"🟢 متصل — يراقب {len(self.bot.otc_assets)} أصل OTC"
        elif self.bot.waiting_token:
            return "red", "🔴 التوكن منتهي — يرجى تحديثه أدناه"
        else:
            return "red", "🔴 غير متصل — جاري إعادة الاتصال..."

    async def _index(self, request):
        dot_cls, status_txt = self._status_info()
        html = HTML_PAGE.format(dot_class=dot_cls, status_text=status_txt)
        return web.Response(text=html, content_type="text/html")

    async def _update_token(self, request):
        try:
            data  = await request.json()
            token = (data.get("token") or "").strip()
            if len(token) < 15:
                return web.json_response({"ok": False, "error": "التوكن قصير جداً"})
            await WEB_TOKEN_QUEUE.put(token)
            return web.json_response({"ok": True})
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)})

    async def start(self):
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", WEB_PORT)
        await site.start()
        logger.info(f"🌐 واجهة الويب تعمل على المنفذ {WEB_PORT}")


# ─────────────────────────────────────────────
#  دوال التحليل
# ─────────────────────────────────────────────
def calculate_rsi(closes: list, period: int = 14):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    return 100.0 if al == 0 else round(100 - (100 / (1 + ag / al)), 2)


def calculate_bollinger_bands(closes: list, period: int = 20, std_dev: float = 2.0):
    if len(closes) < period:
        return None, None
    r = closes[-period:]
    sma = np.mean(r)
    std = np.std(r)
    return round(sma + std_dev * std, 6), round(sma - std_dev * std, 6)


def calculate_ema(values: list, period: int):
    """متوسط متحرك أسّي (EMA) — خفيف وسريع، يقيس الاتجاه القصير/العام."""
    if not values or len(values) < period:
        return None
    k = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema


def detect_trend(closed: list) -> dict:
    """يكشف اتجاه السوق وقوته من EMA + تتابع الشموع بنفس اللون.

    يُستخدم كـ«فلتر اتجاه مستمر» لحماية خطة المضاعفات: عندما يكون السوق في
    اتجاه قوي مستمر، تفشل مؤشرات الانعكاس (RSI/Bollinger) وتتوالى الخسائر.
    يعيد: dir (up/down/range)، run (طول التتابع)، strong (اتجاه قوي مستمر؟)."""
    default = {"dir": "range", "run": 0, "strong": False,
               "ema_fast": None, "ema_slow": None, "gap": 0.0}
    closes = [float(c["close"]) for c in closed
              if isinstance(c, dict) and c.get("close") is not None]
    if len(closes) < EMA_SLOW:
        return default
    ema_f = calculate_ema(closes, EMA_FAST)
    ema_s = calculate_ema(closes, EMA_SLOW)
    if ema_f is None or ema_s is None:
        return default
    price = closes[-1] or 1e-9
    gap = (ema_f - ema_s) / price
    if gap > TREND_EMA_GAP:
        direction = "up"
    elif gap < -TREND_EMA_GAP:
        direction = "down"
    else:
        direction = "range"
    # تتابع الشموع بنفس اللون في النهاية = قياس الزخم المستمر
    run, last_sign = 0, 0
    for c in reversed(closed):
        try:
            o  = float(c.get("open", c["close"]))
            cl = float(c["close"])
        except (TypeError, ValueError, KeyError):
            break
        sign = 1 if cl > o else (-1 if cl < o else 0)
        if sign == 0:
            break
        if last_sign == 0:
            last_sign, run = sign, 1
        elif sign == last_sign:
            run += 1
        else:
            break
    strong = direction != "range" and (
        abs(gap) >= TREND_EMA_GAP * 1.5 or run >= TREND_RUN_MIN)
    return {"dir": direction, "run": run, "strong": strong,
            "ema_fast": round(ema_f, 6), "ema_slow": round(ema_s, 6),
            "gap": round(gap, 6)}


def calc_atr(candles: list, period: int = 14) -> float:
    """ATR (Average True Range) لآخر period شمعة — يقيس تذبذب السوق الحقيقي.
    يستخدم High/Low/PrevClose؛ عند غيابهما يستعيض بجسم الشمعة (|close-open|)."""
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        h  = float(candles[i].get("high",  candles[i].get("close", 0)))
        l  = float(candles[i].get("low",   candles[i].get("close", 0)))
        pc = float(candles[i - 1].get("close", 0))
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    vals = trs[-period:]
    return float(np.mean(vals)) if vals else 0.0


def detect_sr_levels_30m(candles_30m: list, current_price: float, atr_1m: float) -> list:
    """يكتشف مستويات الدعم والمقاومة من شموع 30 دقيقة بناءً على أجسام الشموع (OHLC Body).

    المنطق الرياضي:
    • يستخدم Open وClose فقط — لا High/Low (الذيول عشوائية).
    • يُطبّق Price Clustering لتجميع الأسعار المتقاربة ضمن هامش ATR.
    • إغلاق الشمعة (Close) له أولوية أعلى — يُمثّل سيولة ارتدادية حقيقية.
    • يُصنَّف المستوى دعماً إن كان تحت السعر الحالي، ومقاومةً إن كان فوقه.

    يعيد قائمة {'price','type','touches','strength','buffer'} مرتّبة بالقرب."""
    if len(candles_30m) < 5:
        return []
    # هامش التجميع: ATR × 2.0 أو 0.025% من السعر (أيهما أكبر)
    tolerance = max(atr_1m * 2.0, current_price * 0.00025) if current_price else atr_1m * 2.0
    buffer    = max(atr_1m * ATR_SR_FACTOR, current_price * 0.0001) if current_price else atr_1m * ATR_SR_FACTOR
    # ── مصدر البيانات: أجسام الشموع فقط (Open + Close) ───────────────
    raw = []
    for c in candles_30m:
        o  = float(c.get("open",  0))
        cl = float(c.get("close", 0))
        if o <= 0 or cl <= 0:
            continue
        body_hi = max(o, cl)
        body_lo = min(o, cl)
        # إغلاق الشمعة: مستوى سيولة انعكاسي ذو أولوية (تسجيل مرتَين)
        raw.append(("candle_close", cl))
        raw.append(("candle_close", cl))
        # افتتاح الشمعة: مستوى ثانوي
        raw.append(("candle_open",  o))
        # الذيل العلوي (High): رفض سعري فعلي — مستوى مقاومة قوي
        h = float(c.get("high", body_hi))
        if h > body_hi * 1.0002:   # ذيل حقيقي وليس مجرد فجوة بيانات
            raw.append(("candle_high", h))
        # الذيل السفلي (Low): دعم سعري فعلي
        l = float(c.get("low", body_lo))
        if l < body_lo * 0.9998:
            raw.append(("candle_low", l))
    # ── تجميع الأسعار المتقاربة رياضياً (Price Clustering) ─────────────
    grouped = []
    for typ, price in raw:
        matched = False
        for g in grouped:
            if abs(g["price"] - price) <= tolerance:
                g["touches"] += 1
                # متوسط متحرك للسعر المجمّع
                g["price"] = (g["price"] * (g["touches"] - 1) + price) / g["touches"]
                if typ == "candle_close":
                    g["type"] = "candle_close"   # الإغلاق يرقّي نوع المستوى
                matched = True
                break
        if not matched:
            grouped.append({"price": price, "type": typ, "touches": 1})
    # ── تصنيف وتصفية المستويات ─────────────────────────────────────────
    result = []
    max_dist = atr_1m * 8
    for g in grouped:
        dist = g["price"] - current_price
        if abs(dist) > max_dist:
            continue
        # تصنيف ديناميكي بحسب موقع السعر الحالي
        if g["type"] not in ("resistance", "support"):
            g["type"] = "resistance" if dist > 0 else "support"
        touches     = g["touches"]
        touch_bonus = min(SCORE_TOUCHES, max(0, (touches - 2) * 10))
        g["strength"] = min(100, SCORE_30M_BASE + touch_bonus)
        g["buffer"]   = buffer
        result.append(g)
    result.sort(key=lambda x: abs(x["price"] - current_price))
    return result[:10]


def detect_swing_points(candles: list, window: int = 2) -> tuple:
    """يكتشف نقاط القمم والقيعان بناءً على أجسام الشموع (Open/Close) حصراً — لا الذيول.

    المنطق الرياضي:
    • قمة الجسم (Body High) = max(Open, Close) للشمعة i
    • قاع الجسم (Body Low)  = min(Open, Close) للشمعة i
    • القمة: body_high[i] > body_high[j] لجميع j في النافذة ±window
    • القاع: body_low[i]  < body_low[j]  لجميع j في النافذة ±window

    يعيد (highs_list, lows_list) — كل عنصر هو (index, price)."""
    highs, lows = [], []
    n = len(candles)
    # احسب أعلى وأدنى نقطة في جسم كل شمعة مرة واحدة
    body_hi = [max(float(c.get("open", c.get("close", 0))),
                   float(c.get("close", 0))) for c in candles]
    body_lo = [min(float(c.get("open", c.get("close", 0))),
                   float(c.get("close", 0))) for c in candles]
    for i in range(window, n - window):
        neighbors = list(range(i - window, i + window + 1))
        neighbors.remove(i)
        # قمة: أعلى جسم في النافذة
        if all(body_hi[i] > body_hi[j] for j in neighbors):
            highs.append((i, body_hi[i]))
        # قاع: أدنى جسم في النافذة
        if all(body_lo[i] < body_lo[j] for j in neighbors):
            lows.append((i, body_lo[i]))
    return highs, lows


def fit_trendline(points: list, direction: str, atr: float,
                  min_touches: int = 3) -> dict | None:
    """يجد أقوى خط ترند يمر بأكبر عدد من نقاط الارتداد.

    direction='up'→ قيعان متصاعدة (CALL)، 'down'→ قمم متناقصة (PUT).
    يعيد dict{direction,slope,intercept,touches,value_at_end} أو None."""
    if len(points) < min_touches:
        return None
    best = None
    for i in range(len(points)):
        for j in range(i + 1, len(points)):
            idx0, p0 = points[i]
            idx1, p1 = points[j]
            if idx1 == idx0:
                continue
            slope = (p1 - p0) / (idx1 - idx0)
            if direction == "up"   and slope <= 0:
                continue
            if direction == "down" and slope >= 0:
                continue
            intercept = p0 - slope * idx0
            touches = [(idx, pr) for idx, pr in points
                       if abs(pr - (slope * idx + intercept)) <= atr * 0.8]
            if (len(touches) >= min_touches
                    and (best is None or len(touches) > len(best["touches"]))):
                last_idx = max(idx for idx, _ in points)
                best = {"direction": direction, "slope": slope,
                        "intercept": intercept, "touches": touches,
                        "value_at_end": slope * last_idx + intercept}
    return best


def is_momentum_breakout(closed: list,
                         period: int = 5,
                         mult: float = 2.0) -> bool:
    """يكتشف الاختراق الانفجاري (جسم آخر شمعة > mult × متوسط آخر period شموع).
    True → الشمعة تُعدّ اختراقاً حقيقياً → يُلغى توليد الإشارة الارتدادية."""
    if len(closed) < period + 1:
        return False
    bodies = [abs(float(c.get("close", 0)) - float(c.get("open", 0)))
              for c in closed[-(period + 1):-1]]
    avg = float(np.mean(bodies)) if bodies else 0.0
    if avg < 1e-9:
        return False
    last_body = abs(float(closed[-1].get("close", 0)) - float(closed[-1].get("open", 0)))
    return last_body > avg * mult


def detect_candle_pattern(closed: list) -> dict | None:
    """يكتشف نماذج الشموع الانعكاسية بمعادلات رياضية دقيقة على بيانات OHLC.

    المعادلات الرياضية:
    • Doji      : body < 10% من المدى الكلي (h - l)
    • Hammer    : lower_wick ≥ 2.5 × body  AND  lower_wick > upper_wick × 1.5
    • Pin Bar   : upper_wick ≥ 2.5 × body  AND  upper_wick > lower_wick × 1.5
    • Bull Engulf: جسم الحالية يلتهم جسم السابقة (open_curr ≤ close_prev AND close_curr ≥ open_prev)
    • Bear Engulf: جسم الحالية يلتهم جسم السابقة (open_curr ≥ close_prev AND close_curr ≤ open_prev)

    يعيد {'pattern', 'direction', 'confidence'} أو None."""
    if len(closed) < 2:
        return None
    last = closed[-1]
    prev = closed[-2]
    o  = float(last.get("open",  0))
    c  = float(last.get("close", 0))
    h  = float(last.get("high",  max(o, c)))
    l  = float(last.get("low",   min(o, c)))
    po = float(prev.get("open",  0))
    pc = float(prev.get("close", 0))
    body      = abs(c - o)
    total     = max(h - l, 1e-10)
    upper_w   = h - max(o, c)
    lower_w   = min(o, c) - l
    prev_body = abs(pc - po)
    # ── Doji: تردد السوق (جسم < 10% من المدى الكلي) ────────────────
    if body < total * 0.10:
        return {"pattern": "doji", "direction": None, "confidence": 0.75}
    # ── Hammer (انعكاس صعودي): lower_wick ≥ 2.5 × body ────────────
    # المعادلة: (min(O,C) - L) ≥ 2.5 × |C - O|
    if lower_w >= body * 2.5 and lower_w > upper_w * 1.5:
        return {"pattern": "hammer", "direction": "CALL",
                "confidence": min(1.0, lower_w / total)}
    # ── Pin Bar (انعكاس هبوطي): upper_wick ≥ 2.5 × body ───────────
    # المعادلة: (H - max(O,C)) ≥ 2.5 × |C - O|
    if upper_w >= body * 2.5 and upper_w > lower_w * 1.5:
        return {"pattern": "pin_bar", "direction": "PUT",
                "confidence": min(1.0, upper_w / total)}
    # ── Bullish Engulfing: جسم صاعد يلتهم جسم هابط سابق كاملاً ────
    # الشرط: pc < po (السابقة هابطة) AND c > o (الحالية صاعدة)
    #        AND o ≤ pc (فتح الحالية تحت إغلاق السابقة)
    #        AND c ≥ po (إغلاق الحالية فوق افتتاح السابقة)
    if (prev_body > 1e-10 and body > prev_body
            and pc < po and c > o
            and o <= pc and c >= po):
        return {"pattern": "bullish_engulfing", "direction": "CALL",
                "confidence": min(1.0, body / prev_body * 0.6)}
    # ── Bearish Engulfing: جسم هابط يلتهم جسم صاعد سابق كاملاً ────
    # الشرط: pc > po (السابقة صاعدة) AND c < o (الحالية هابطة)
    #        AND o ≥ pc (فتح الحالية فوق إغلاق السابقة)
    #        AND c ≤ po (إغلاق الحالية تحت افتتاح السابقة)
    if (prev_body > 1e-10 and body > prev_body
            and pc > po and c < o
            and o >= pc and c <= po):
        return {"pattern": "bearish_engulfing", "direction": "PUT",
                "confidence": min(1.0, body / prev_body * 0.6)}
    return None


def detect_fibonacci_levels(closed: list, swing_candles: int = 30) -> dict | None:
    """يحسب مستويات فيبوناتشي على آخر موجة سعرية بناءً على أجسام الشموع (OHLC Body).

    المعادلات الرياضية الصريحة:
      Diff      = Highest_Body - Lowest_Body
      موجة صاعدة (قاع قبل قمة):
        61.8% = Highest - (Diff × 0.618)   ← مستوى الذهبي
        50.0% = Highest - (Diff × 0.500)   ← منتصف الموجة
        38.2% = Highest - (Diff × 0.382)
      موجة هابطة (قمة قبل قاع):
        61.8% = Lowest  + (Diff × 0.618)
        50.0% = Lowest  + (Diff × 0.500)

    يعيد {'trend_dir','swing_high','swing_low','levels','range'} أو None."""
    n = min(swing_candles, len(closed))
    if n < 10:
        return None
    seg = closed[-n:]
    # أجسام الشموع فقط: max(Open,Close) و min(Open,Close)
    body_hi = [max(float(c.get("open", c.get("close", 0))),
                   float(c.get("close", 0))) for c in seg]
    body_lo = [min(float(c.get("open", c.get("close", 0))),
                   float(c.get("close", 0))) for c in seg]
    hi_val = max(body_hi);  hi_idx = body_hi.index(hi_val)
    lo_val = min(body_lo);  lo_idx = body_lo.index(lo_val)
    diff = hi_val - lo_val
    if diff < 1e-10:
        return None
    if lo_idx < hi_idx:
        # موجة صاعدة → تصحيح هبوطي → مستويات تحت القمة
        trend_dir = "up"
        levels = {
            0.236: hi_val - diff * 0.236,
            0.382: hi_val - diff * 0.382,
            0.500: hi_val - diff * 0.500,   # منتصف الموجة
            0.618: hi_val - diff * 0.618,   # المستوى الذهبي
            0.786: hi_val - diff * 0.786,
        }
    else:
        # موجة هابطة → تصحيح صعودي → مستويات فوق القاع
        trend_dir = "down"
        levels = {
            0.236: lo_val + diff * 0.236,
            0.382: lo_val + diff * 0.382,
            0.500: lo_val + diff * 0.500,   # منتصف الموجة
            0.618: lo_val + diff * 0.618,   # المستوى الذهبي
            0.786: lo_val + diff * 0.786,
        }
    return {"trend_dir": trend_dir, "swing_high": hi_val, "swing_low": lo_val,
            "levels": levels, "range": diff}


def fmt(symbol: str) -> str:
    return symbol.replace("_otc", " (OTC)").replace("_", "/")


# ─────────────────────────────────────────────
#  البوت الرئيسي
# ─────────────────────────────────────────────
class QuotexOTCBot:
    def __init__(self):
        self.client        = None
        self.otc_assets    = []
        self.qualified_assets = []   # أصول العائد ≥ MIN_SCAN_PAYOUT (تُحدَّث دورياً)
        self._diag_candles = 0
        self._diag_candle_len = 0
        self._diag_analyzed = 0
        self._diag_fresh = 0         # أصول بشموع حديثة فعلاً هذه الدورة (لا مخزون قديم)
        self._empty_cycles = 0       # دورات فحص متتالية بلا بيانات حديثة (للتعافي الذاتي)
        self._connected_at = time.time()  # وقت آخر اتصال ناجح (للتجديد الاستباقي كل 7 ساعات)
        self.last_payouts  = {}      # asset -> نسبة العائد الأخيرة (أو None)
        self.state         = {}
        self.active_token  = load_current_token()
        self.connected     = False
        self.waiting_token = False
        self.start_time    = time.time()
        self.signals_sent  = 0
        self.last_signal   = None
        self.last_scan_t   = None
        self.last_candles    = {}   # asset -> آخر شموع 1م OHLC حقيقية (للتحليل الذكي)
        self.last_candles_30m = {} # asset -> آخر شموع 30م (لمناطق الدعم/المقاومة)
        self._30m_last_fetch  = {} # asset -> timestamp آخر جلب 30م
        self.broken_levels    = {} # asset -> [(price, direction, timestamp), ...] إعادة الاختبار
        self.active_tracks = {}   # message_id -> بيانات متابعة جارية (تُحفظ لتُستأنف بعد إعادة التشغيل)
        self._pre_alerts   = {}   # asset -> {level_key: last_sent_timestamp} — throttle التنبيهات المبكرة
        self._load_candles()      # استرجاع مخزّن الشموع من القرص (إن كان حديثاً) لإقلاع فوري
        self.last_analysis = {}   # asset -> آخر مؤشرات محسوبة
        self.last_verify   = {}   # asset -> {payout, fresh, age} (تحقق عبر التوكن)
        self.request_restart = False  # علم لإعادة التشغيل من الوكيل الذكي
        self.loop          = None  # حلقة الأحداث (تُلتقط في run) لجلب البيانات عند الطلب من خيط الوكيل
        self._sig_tasks    = set()  # مهام المتابعة الحية الجارية (مرجع يمنع جمعها)
        self.chat_history  = self._load_chat_history()  # ذاكرة المحادثة المحمَّلة من القرص (تبقى بعد إعادة التشغيل)
        self.ai            = AIAdvisor()
        self.telegram      = TelegramCmdHandler(self)

    # ── الاتصال ──────────────────────────────────────────────

    async def connect(self, retry: int = 0, token: str = None) -> bool:
        tok = token or self.active_token
        if not tok:
            logger.error("❌ لا يوجد QUOTEX_TOKEN!")
            return False

        ua = get_random_ua()
        setup_session(tok, ua)
        self.active_token  = tok
        self.connected     = False
        self.waiting_token = False

        logger.info(f"⏳ اتصال WebSocket (محاولة {retry + 1})...")

        if self.client:
            try:
                await self.client.close()
            except Exception:
                pass

        self.client = Quotex(
            email=QUOTEX_EMAIL,
            password=QUOTEX_PASSWORD,
            lang="en",
            user_agent=ua,
            root_path=".",
        )

        try:
            ok, msg = await self.client.connect()
            is_reject = "reject" in msg.lower() or "rejected" in msg.lower()

            if ok and not is_reject:
                self.connected = True
                self._connected_at = time.time()  # نقطة بداية مؤقّت التجديد الاستباقي
                logger.info("✅ متصل بنجاح!")
                await asyncio.sleep(2)
                await self._load_assets()
                return True

            if is_reject:
                logger.warning("⚠️ التوكن منتهي الصلاحية!")
                self.connected     = False
                self.waiting_token = True
                await self._notify_expired()
                return False

            logger.warning(f"⚠️ فشل ({retry + 1}): {msg}")
            if retry < MAX_CONN_RETRY - 1:
                wait = min(10 * (retry + 1), 60) + random.uniform(1, 4)
                logger.info(f"🔄 إعادة المحاولة بعد {wait:.0f}ث...")
                await asyncio.sleep(wait)
                return await self.connect(retry + 1)
            return False

        except Exception as e:
            logger.error(f"❌ خطأ: {e}")
            if retry < MAX_CONN_RETRY - 1:
                await asyncio.sleep(min(10 * (retry + 1), 60))
                return await self.connect(retry + 1)
            return False

    async def _notify_expired(self):
        # محاولة تجديد تلقائي أولاً
        send_telegram("🔄 <b>التوكن منتهي — جاري التجديد التلقائي...</b>")
        new_tok = await self._auto_refresh_token()
        if new_tok:
            logger.info("✅ تجديد تلقائي للتوكن نجح!")
            self.waiting_token = False
            self.connected = await self.connect(0, new_tok)
            if self.connected:
                send_telegram(
                    "✅ <b>تجديد تلقائي ناجح!</b>\n"
                    f"📋 يراقب {len(self.otc_assets)} أصل OTC\n"
                    "⚡ الإشارات نشطة"
                )
            return
        # فشل التجديد التلقائي — اطلب من المستخدم
        send_telegram(
            "🔑 <b>التوكن منتهي الصلاحية!</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "⚠️ فشل التجديد التلقائي\n\n"
            "🌐 <b>الطريقة الأسرع:</b>\n"
            "افتح معاينة Replit ← الصق التوكن ← اضغط تحديث\n\n"
            "📲 <b>أو عبر Telegram:</b>\n"
            "<code>/token &lt;القيمة&gt;</code>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "<b>كيف تحصل على التوكن؟</b>\n"
            "1. افتح qxbroker.com وسجّل دخولك\n"
            "2. F12 → Application → Cookies → qxbroker.com\n"
            "3. انسخ قيمة الكوكي <b>token</b>"
        )

    async def _auto_refresh_token(self) -> str | None:
        """يجدّد التوكن بطبقتين:
        الطبقة 1 — تسجيل دخول مباشر عبر pyquotex/httpx (بدون Chromium، أسرع وأكثر استقراراً).
        الطبقة 2 — Chromium (get_token.py) احتياطياً إذا فشلت الطبقة الأولى."""
        if getattr(self, "_refreshing_token", False):
            logger.info("⏳ تجديد التوكن جارٍ بالفعل — تخطّي محاولة متزامنة")
            return None
        self._refreshing_token = True
        try:
            # ── الطبقة 1: تسجيل دخول مباشر (httpx، بدون Chromium) ────────────
            # pyquotex يملك authenticate() خاصة به تستخدم httpx مع headers تحاكي Firefox.
            # هذا أسرع بكثير من Chromium ولا تكسر بسبب نقص موارد العملية.
            if self.client:
                try:
                    logger.info("🔐 تجديد مباشر (بدون Chromium)...")
                    ok, msg = await asyncio.wait_for(
                        self.client.authenticate(), timeout=55)
                    if ok and self.client.state.SSID:
                        new_tok = self.client.state.SSID
                        setup_session(new_tok, get_random_ua())
                        logger.info(f"🎯 توكن جديد (مباشر): {new_tok[:15]}...")
                        return new_tok
                    logger.warning(f"🔐 التجديد المباشر فشل ({msg}) — سأجرّب Chromium...")
                except asyncio.TimeoutError:
                    logger.warning("🔐 انتهت مهلة التجديد المباشر (55ث) — سأجرّب Chromium...")
                except Exception as e:
                    logger.warning(f"🔐 خطأ في التجديد المباشر ({e}) — سأجرّب Chromium...")

            # ── الطبقة 2: Chromium (آخر ملاذ) ───────────────────────────────
            import sys
            logger.info("🤖 تشغيل Chromium لاستخراج توكن جديد...")
            proc = None
            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "get_token.py",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=240)
                out = stdout.decode(errors="ignore")
                logger.debug(f"get_token output: {out[-500:]}")
                if proc.returncode == 0:
                    new_tok = load_current_token()
                    if new_tok and new_tok != self.active_token:
                        logger.info(f"🎯 توكن جديد (Chromium): {new_tok[:15]}...")
                        return new_tok
                else:
                    logger.warning(f"get_token.py فشل: {stderr.decode(errors='ignore')[-300:]}")
            except asyncio.TimeoutError:
                logger.warning("⏰ انتهت مهلة Chromium (240ث)")
            except Exception as e:
                logger.error(f"خطأ في Chromium: {e}")
            finally:
                if proc is not None and proc.returncode is None:
                    try:
                        proc.terminate()
                        try:
                            await asyncio.wait_for(proc.wait(), timeout=10)
                        except asyncio.TimeoutError:
                            proc.kill()
                            await proc.wait()
                    except ProcessLookupError:
                        pass
                    except Exception as e:
                        logger.warning(f"تعذّر إنهاء عملية get_token: {e}")
        finally:
            self._refreshing_token = False
        return None

    async def _proactive_token_renewal(self):
        """تجديد استباقي للتوكن كل 7 ساعات قبل انتهائه تماماً.
        يعمل عبر httpx (بدون Chromium) ويُحدّث session.json بهدوء دون انقطاع الاتصال الحالي.
        الاتصال الحالي يبقى نشطاً، والتوكن الجديد يُستخدم في أول إعادة اتصال تالية."""
        if getattr(self, "_refreshing_token", False):
            return  # تجديد آخر جارٍ بالفعل
        logger.info("🔄 تجديد استباقي للتوكن (كل 7 ساعات) ...")
        try:
            ok, msg = await asyncio.wait_for(self.client.authenticate(), timeout=55)
            if ok and self.client.state.SSID:
                new_tok = self.client.state.SSID
                setup_session(new_tok, get_random_ua())
                self.active_token   = new_tok
                self._connected_at  = time.time()   # إعادة ضبط المؤقّت
                logger.info(f"✅ تجديد استباقي نجح: {new_tok[:15]}...")
            else:
                logger.warning(f"⚠️ تجديد استباقي فشل ({msg}) — سيتجدد عند الانتهاء")
        except asyncio.TimeoutError:
            logger.warning("⚠️ انتهت مهلة التجديد الاستباقي (55ث)")
        except Exception as e:
            logger.warning(f"⚠️ خطأ في التجديد الاستباقي ({e})")

    async def apply_new_token(self, new_token: str):
        """يستدعى عند استلام توكن جديد من أي مصدر"""
        logger.info(f"🔄 توكن جديد: {new_token[:12]}...")
        self.waiting_token = False
        self.connected = await self.connect(0, new_token)
        if self.connected:
            send_telegram(
                "✅ <b>تم الاتصال بنجاح!</b>\n"
                f"📋 يراقب {len(self.otc_assets)} أصل OTC\n"
                "⚡ الإشارات نشطة"
            )
        else:
            send_telegram("❌ فشل الاتصال رغم التوكن الجديد — تحقق منه")

    async def _load_assets(self):
        all_a = self.client.get_all_asset_name()
        if all_a:
            self.otc_assets = [a[0] for a in all_a if "_otc" in a[0].lower()]
            logger.info(f"📋 {len(self.otc_assets)} أصل OTC")

    # ── تحليل الشموع ─────────────────────────────────────────

    def analyze(self, candles: list):
        """يُبقي التحليل الأساسي (RSI/BB/Trend) متاحاً للوكيل الذكي وأدوات التشخيص."""
        now    = time.time()
        closed = [c for c in candles
                  if isinstance(c, dict) and c.get("time", 0) + CANDLE_PERIOD <= now]
        if len(closed) < max(RSI_PERIOD, BB_PERIOD) + 1:
            return None
        closes = [float(c["close"]) for c in closed if c.get("close")]
        rsi            = calculate_rsi(closes, RSI_PERIOD)
        upper, lower   = calculate_bollinger_bands(closes, BB_PERIOD, BB_STD)
        if rsi is None or upper is None:
            return None
        tr = detect_trend(closed)
        return {
            "rsi": rsi, "upper_bb": upper, "lower_bb": lower,
            "last_close": closes[-1],
            "last_t": closed[-1]["time"],
            "trend_dir": tr["dir"], "trend_run": tr["run"],
            "trend_strong": tr["strong"],
            "ema_fast": tr["ema_fast"], "ema_slow": tr["ema_slow"],
        }

    def _analyze_reversal(self, candles_1m: list, candles_30m: list) -> dict | None:
        """إشارة ارتداد: يبحث عن مستويات الدعم/المقاومة القوية من فريم 30 دقيقة
        ويتحقق من اقتراب السعر منها ضمن منطقة ATR الديناميكية.

        الشروط: اقتراب ضمن 0.5×ATR + لا اختراق انفجاري + RSI مرن كمُعزِّز.
        يعيد dict أو None إن لم تتحقق الشروط."""
        now = time.time()
        closed = [c for c in candles_1m
                  if isinstance(c, dict) and c.get("time", 0) + CANDLE_PERIOD <= now]
        if len(closed) < ATR_PERIOD + 2:
            return None
        closes  = [float(c["close"]) for c in closed if c.get("close")]
        if not closes:
            return None
        current = closes[-1]
        atr     = calc_atr(closed, ATR_PERIOD)
        if atr < 1e-9:
            return None
        if current > 0 and atr / current < ATR_DEAD_ZONE:
            return None  # سوق ميت: تذبذب < 0.008% من السعر → لا إشارات موثوقة
        # فلتر الزخم الانفجاري: شمعة بجسم كبير = اختراق حقيقي، لا ارتداد
        if is_momentum_breakout(closed, MOMENTUM_CANDLES, MOMENTUM_MULT):
            return None
        rsi = calculate_rsi(closes, RSI_PERIOD)
        if not candles_30m:
            return None
        sr_levels = detect_sr_levels_30m(candles_30m, current, atr)
        if not sr_levels:
            return None
        buffer = max(atr * ATR_SR_FACTOR, current * PROX_PIPS_FLOOR)
        for lvl in sr_levels:
            price  = lvl["price"]
            dist   = current - price
            ltype  = lvl.get("type", "candle_close")
            touches = lvl.get("touches", 1)
            # dist = current - price
            # dist >= 0 → السعر فوق المستوى → المستوى دعم → CALL (ارتداد صعودي)
            # dist <  0 → السعر تحت المستوى → المستوى مقاومة → PUT (رفض هبوطي)
            rsi_score = 0
            if rsi is not None:
                if dist >= 0 and rsi <= RSI_FLEX_BUY:    # عند الدعم + RSI منخفض → تأكيد شرائي
                    rsi_score = SCORE_RSI_BONUS
                elif dist < 0 and rsi >= RSI_FLEX_SELL:  # عند المقاومة + RSI مرتفع → تأكيد بيعي
                    rsi_score = SCORE_RSI_BONUS
                else:
                    # منطقة رمادية: نقاط جزئية
                    rsi_score = int(SCORE_RSI_BONUS * max(0, min(1,
                        (50 - rsi) / 15 if dist >= 0 else (rsi - 50) / 15)))
            # ── فلتر الارتدادات: نتجاهل المستويات الضعيفة التاريخياً ──
            if touches < SR_MIN_TOUCHES:
                continue
            score = min(100, lvl["strength"] + rsi_score)
            if score < SCORE_MIN:
                continue
            # اقتراب من دعم (السعر فوق المستوى = dist موجب → CALL)
            if 0 <= dist <= buffer:
                return {
                    "signal": "reversal", "direction": "CALL",
                    "score": score, "rsi": rsi, "atr": atr,
                    "level": price, "level_type": ltype, "touches": touches,
                    "last_close": current, "last_t": closed[-1]["time"],
                    "rsi_confirm": rsi is not None and rsi <= RSI_FLEX_BUY,
                    "sr_levels": sr_levels[:8],
                }
            # اقتراب من مقاومة (السعر تحت المستوى = dist سالب → PUT)
            if -buffer <= dist < 0:
                return {
                    "signal": "reversal", "direction": "PUT",
                    "score": score, "rsi": rsi, "atr": atr,
                    "level": price, "level_type": ltype, "touches": touches,
                    "last_close": current, "last_t": closed[-1]["time"],
                    "rsi_confirm": rsi is not None and rsi >= RSI_FLEX_SELL,
                    "sr_levels": sr_levels[:8],
                }
        return None

    def _build_pre_alert_p1(self, asset: str, candles: list,
                            candles_30m: list, now: float) -> str | None:
        """يبني رسالة تنبيه مبكر عندما يقترب السعر من مستوى S/R دون أن تكتمل شروط
        الإشارة الكاملة بعد. يُعيد نص الرسالة أو None. يخضع لـ cooldown لتفادي الإزعاج."""
        closed = [c for c in candles
                  if isinstance(c, dict) and c.get("time", 0) + CANDLE_PERIOD <= now]
        if len(closed) < ATR_PERIOD + 2 or not candles_30m:
            return None
        closes = [float(c["close"]) for c in closed if c.get("close")]
        if not closes:
            return None
        current = closes[-1]
        atr     = calc_atr(closed, ATR_PERIOD)
        if atr < 1e-9:
            return None
        if is_momentum_breakout(closed, MOMENTUM_CANDLES, MOMENTUM_MULT):
            return None   # كسر انفجاري ≠ اقتراب ارتداد
        rsi     = calculate_rsi(closes, RSI_PERIOD)
        sr_lvls = detect_sr_levels_30m(candles_30m, current, atr)
        if not sr_lvls:
            return None

        buffer     = max(atr * ATR_SR_FACTOR, current * PROX_PIPS_FLOOR)
        pre_buffer = buffer * PRE_SIGNAL_MULT
        cooldown   = PRE_ALERT_COOLDOWN * CANDLE_PERIOD
        alerts     = self._pre_alerts.setdefault(asset, {})

        for lvl in sr_lvls[:6]:
            price   = lvl["price"]
            touches = lvl.get("touches", 1)
            if touches < SR_MIN_TOUCHES:
                continue
            dist = current - price

            if buffer < dist <= pre_buffer:           # فوق المستوى → اقتراب دعم → CALL
                direction, lvl_ar = "CALL", "دعم"
            elif -pre_buffer <= dist < -buffer:       # تحت المستوى → اقتراب مقاومة → PUT
                direction, lvl_ar = "PUT", "مقاومة"
            else:
                continue

            key = f"{direction}_{price:.5f}"
            if now - alerts.get(key, 0) < cooldown:
                continue   # داخل فترة الهدوء — لا إزعاج

            alerts[key] = now

            # المسافة بالنسبة المئوية من السعر الحالي
            dist_pct = abs(dist) / current * 100 if current else 0
            fmt_p    = (f"{price:.5f}" if price < 10 else
                        f"{price:.4f}" if price < 100 else f"{price:.2f}")
            dir_icon = "🟢 CALL ▲" if direction == "CALL" else "🔴 PUT ▼"
            rsi_str  = f"  •  RSI {rsi:.0f}" if rsi is not None else ""
            # تقدير الوقت المتوقع للوصول (بالثواني) = المسافة ÷ معدل التحرك (ATR/شمعة)
            eta_candles = max(1, round(abs(dist) / (atr or abs(dist))))
            eta_str = f"~{eta_candles} شمعة" if eta_candles <= 5 else f"~{eta_candles} شمعة"

            return (f"⚡ <b>تنبيه مبكر</b> | {fmt(asset)}\n"
                    f"{dir_icon}  •  {lvl_ar} @ <b>{fmt_p}</b>  (×{touches} لمسات)\n"
                    f"المسافة: {dist_pct:.3f}%{rsi_str}\n"
                    f"⏱️ <i>متوقع التحقق خلال {eta_str} — انتظر إشارة مؤكدة</i>")

        return None

    def _analyze_trendline(self, candles_1m: list) -> dict | None:
        """إشارة خط ترند: يكتشف خطاً قوياً (3+ ارتدادات) على فريم 1 دقيقة
        ويتحقق من اقتراب السعر منه ضمن منطقة ATR الديناميكية.

        يعيد dict أو None إن لم تتحقق الشروط."""
        now = time.time()
        closed = [c for c in candles_1m
                  if isinstance(c, dict) and c.get("time", 0) + CANDLE_PERIOD <= now]
        if len(closed) < 20:
            return None
        closes  = [float(c["close"]) for c in closed if c.get("close")]
        if not closes:
            return None
        current = closes[-1]
        atr     = calc_atr(closed, ATR_PERIOD)
        if atr < 1e-9:
            return None
        if current > 0 and atr / current < ATR_DEAD_ZONE:
            return None  # سوق ميت
        if is_momentum_breakout(closed, MOMENTUM_CANDLES, MOMENTUM_MULT):
            return None
        rsi = calculate_rsi(closes, RSI_PERIOD)
        highs_pts, lows_pts = detect_swing_points(closed, SWING_WINDOW)
        buffer = max(atr * ATR_TREND_FACTOR, current * PROX_PIPS_FLOOR)
        # ── الإسقاط الرياضي: نحسب قيمة الخط عند الشمعة الحالية (النقطة الرابعة) ──
        # معادلة الخط المستقيم: y = m × x + b
        # حيث x = رقم الشمعة الحالية، m = slope، b = intercept
        current_idx = len(closed) - 1
        best_result = None
        # ترند صاعد: قيعان متصاعدة (body lows) → CALL عند الاقتراب من النقطة الرابعة
        up_line = fit_trendline(lows_pts, "up", atr, MIN_TREND_TOUCHES)
        if up_line:
            # القيمة المتوقعة رياضياً عند الشمعة الحالية (الإسقاط الرابع)
            projected_val = up_line["slope"] * current_idx + up_line["intercept"]
            dist          = current - projected_val
            touches       = len(up_line["touches"])
            if -buffer <= dist <= buffer * 0.6:
                touch_bonus = min(SCORE_TOUCHES, max(0, (touches - 2) * 10))
                rsi_bonus   = SCORE_RSI_BONUS if (rsi and rsi <= RSI_FLEX_BUY) else 0
                score = min(100, SCORE_30M_BASE // 2 + touch_bonus + rsi_bonus)
                if score >= SCORE_MIN:
                    best_result = {
                        "signal": "trendline", "direction": "CALL",
                        "score": score, "rsi": rsi, "atr": atr,
                        "touches": touches, "tl_direction": "up",
                        "tl_slope": up_line["slope"],
                        "tl_value": projected_val, "last_close": current,
                        "last_t": closed[-1]["time"],
                        "description": f"السعر عند اللمسة {touches} من خط الترند الصاعد — دعم ديناميكي",
                    }
        # ترند هابط: قمم متناقصة (body highs) → PUT عند الاقتراب من النقطة الرابعة
        down_line = fit_trendline(highs_pts, "down", atr, MIN_TREND_TOUCHES)
        if down_line:
            projected_val = down_line["slope"] * current_idx + down_line["intercept"]
            dist          = current - projected_val
            touches       = len(down_line["touches"])
            if -buffer * 0.6 <= dist <= buffer:
                touch_bonus = min(SCORE_TOUCHES, max(0, (touches - 2) * 10))
                rsi_bonus   = SCORE_RSI_BONUS if (rsi and rsi >= RSI_FLEX_SELL) else 0
                score = min(100, SCORE_30M_BASE // 2 + touch_bonus + rsi_bonus)
                if score >= SCORE_MIN:
                    candidate = {
                        "signal": "trendline", "direction": "PUT",
                        "score": score, "rsi": rsi, "atr": atr,
                        "touches": touches, "tl_direction": "down",
                        "tl_slope": down_line["slope"],
                        "tl_value": projected_val, "last_close": current,
                        "last_t": closed[-1]["time"],
                        "description": f"السعر عند اللمسة {touches} من خط الترند الهابط — مقاومة ديناميكية",
                    }
                    if best_result is None or candidate["score"] > best_result["score"]:
                        best_result = candidate
        return best_result

    # ── طرق الإشارات الجديدة ─────────────────────────────────

    def _record_broken_level(self, asset: str, price: float, direction: str):
        """يسجّل مستوى مكسور لمراقبة إعادة الاختبار — يتجنّب التكرار."""
        now      = time.time()
        existing = self.broken_levels.get(asset, [])
        for (lvl, _d, _t) in existing:
            if abs(lvl - price) < price * 0.0008:   # نفس المنطقة تقريباً → تجاهل
                return
        existing.append((price, direction, now))
        self.broken_levels[asset] = existing

    def _maybe_record_breakout(self, asset: str,
                               candles_1m: list, candles_30m: list):
        """عند اكتشاف زخم اختراق بالقرب من مستوى 30م، يسجّل المستوى لإعادة الاختبار لاحقاً."""
        now = time.time()
        closed = [c for c in candles_1m
                  if isinstance(c, dict) and c.get("time", 0) + CANDLE_PERIOD <= now]
        if len(closed) < ATR_PERIOD + 2:
            return
        if not is_momentum_breakout(closed, MOMENTUM_CANDLES, MOMENTUM_MULT):
            return
        closes  = [float(c["close"]) for c in closed if c.get("close")]
        if not closes or not candles_30m:
            return
        current = closes[-1]
        atr     = calc_atr(closed, ATR_PERIOD)
        if atr < 1e-9:
            return
        sr_levels = detect_sr_levels_30m(candles_30m, current, atr)
        zone = atr * 3.0   # نبحث في نطاق أوسع لالتقاط المستوى المُخترَق قبل الشمعة الحالية
        for lvl in sr_levels[:5]:
            price = lvl["price"]
            dist  = current - price
            if 0 < dist <= zone:
                # السعر فوق المستوى = اخترقه صعوداً → مقاومة تتحوّل دعماً
                self._record_broken_level(asset, price, "CALL")
            elif -zone <= dist < 0:
                # السعر تحت المستوى = اخترقه هبوطاً → دعم يتحوّل مقاومة
                self._record_broken_level(asset, price, "PUT")

    def _analyze_retest(self, asset: str, candles_1m: list) -> dict | None:
        """إعادة الاختبار (Role Reversal Retest): بعد كسر مستوى بزخم،
        يراقب عودة السعر لملامسته من الجهة الأخرى ويُطلق إشارة مع اتجاه الكسر.
        مستوى الدعم المكسور يصبح مقاومة والعكس بالعكس."""
        now = time.time()
        # تنظيف المستويات المنتهية (أقدم من RETEST_WINDOW)
        broken = [(lvl, d, t) for (lvl, d, t)
                  in self.broken_levels.get(asset, [])
                  if now - t < RETEST_WINDOW]
        self.broken_levels[asset] = broken
        if not broken:
            return None
        closed = [c for c in candles_1m
                  if isinstance(c, dict) and c.get("time", 0) + CANDLE_PERIOD <= now]
        if len(closed) < ATR_PERIOD + 2:
            return None
        closes  = [float(c["close"]) for c in closed if c.get("close")]
        current = closes[-1]
        atr     = calc_atr(closed, ATR_PERIOD)
        if atr < 1e-9 or (current > 0 and atr / current < ATR_DEAD_ZONE):
            return None
        if is_momentum_breakout(closed, MOMENTUM_CANDLES, MOMENTUM_MULT):
            return None  # اختراق جديد ≠ إعادة اختبار
        rsi    = calculate_rsi(closes, RSI_PERIOD)
        buffer = max(atr * ATR_SR_FACTOR * 1.5, current * PROX_PIPS_FLOOR)
        best   = None
        for lvl_price, break_dir, break_t in broken:
            dist = current - lvl_price
            # CALL: كسر صاعد → المستوى تحوّل دعماً → السعر يعود إليه من الأعلى
            if break_dir == "CALL" and -buffer * 0.8 <= dist <= buffer:
                rsi_bonus = SCORE_RSI_BONUS if (rsi and rsi <= RSI_FLEX_BUY) else 0
                score     = min(100, RETEST_SCORE_BASE + rsi_bonus)
                c = {"signal": "retest", "direction": "CALL",
                     "score": score, "rsi": rsi, "atr": atr,
                     "level": lvl_price, "level_type": "retest_support",
                     "touches": 1, "last_close": current,
                     "last_t": closed[-1]["time"],
                     "rsi_confirm": rsi is not None and rsi <= RSI_FLEX_BUY}
                if best is None or score > best["score"]:
                    best = c
            # PUT: كسر هبوطي → المستوى تحوّل مقاومة → السعر يعود إليه من الأسفل
            elif break_dir == "PUT" and -buffer <= dist <= buffer * 0.8:
                rsi_bonus = SCORE_RSI_BONUS if (rsi and rsi >= RSI_FLEX_SELL) else 0
                score     = min(100, RETEST_SCORE_BASE + rsi_bonus)
                c = {"signal": "retest", "direction": "PUT",
                     "score": score, "rsi": rsi, "atr": atr,
                     "level": lvl_price, "level_type": "retest_resistance",
                     "touches": 1, "last_close": current,
                     "last_t": closed[-1]["time"],
                     "rsi_confirm": rsi is not None and rsi >= RSI_FLEX_SELL}
                if best is None or score > best["score"]:
                    best = c
        return best

    def _analyze_ema_pullback(self, candles_1m: list) -> dict | None:
        """وضع المضاربة السريعة — ارتداد عند EMA السريع (Scalping Pullback).

        المنطق الرياضي:
        • يحسب EMA_FAST و EMA_SLOW على أجسام الشموع (Close).
        • إذا كان الاتجاه واضحاً (EMA_FAST > EMA_SLOW بفجوة ≥ TREND_EMA_GAP):
            - ترند صاعد: السعر يعود لملامسة EMA_FAST من الأعلى → CALL (شراء الكسر)
            - ترند هابط: السعر يرتفع لملامسة EMA_FAST من الأسفل → PUT (بيع الارتفاع)
        • يُولّد إشارات استمرارية داخل الترند — ليست عكسية بل مع الزخم."""
        now = time.time()
        closed = [c for c in candles_1m
                  if isinstance(c, dict) and c.get("time", 0) + CANDLE_PERIOD <= now]
        if len(closed) < EMA_SLOW + 5:
            return None
        closes  = [float(c["close"]) for c in closed if c.get("close")]
        if not closes:
            return None
        current = closes[-1]
        atr     = calc_atr(closed, ATR_PERIOD)
        if atr < 1e-9 or (current > 0 and atr / current < ATR_DEAD_ZONE):
            return None
        if is_momentum_breakout(closed, MOMENTUM_CANDLES, MOMENTUM_MULT):
            return None
        # حساب EMA السريع والبطيء على إغلاقات الشموع
        def _ema(prices, period):
            k = 2 / (period + 1)
            e = prices[0]
            for p in prices[1:]:
                e = p * k + e * (1 - k)
            return e
        ema_fast = _ema(closes, EMA_FAST)
        ema_slow = _ema(closes, EMA_SLOW)
        gap_pct  = abs(ema_fast - ema_slow) / max(current, 1e-9)
        if gap_pct < TREND_EMA_GAP:
            return None   # الاتجاه ليس واضحاً بما يكفي
        rsi    = calculate_rsi(closes, RSI_PERIOD)
        buffer = max(atr * 1.1, current * PROX_PIPS_FLOOR)
        if ema_fast > ema_slow:
            # ترند صاعد: السعر يتراجع لمستوى EMA السريع من الأعلى
            dist = current - ema_fast
            if -buffer * 1.2 <= dist <= buffer * 0.5:
                rsi_bonus = SCORE_RSI_BONUS if (rsi and rsi <= RSI_FLEX_BUY) else int(SCORE_RSI_BONUS * 0.5)
                score = min(100, EMA_PB_SCORE_BASE + rsi_bonus)
                if score < SCORE_MIN:
                    return None
                return {
                    "signal": "ema_pullback", "direction": "CALL",
                    "score": score, "rsi": rsi, "atr": atr,
                    "ema_fast": ema_fast, "ema_slow": ema_slow,
                    "ema_gap_pct": gap_pct * 100,
                    "last_close": current, "last_t": closed[-1]["time"],
                    "rsi_confirm": rsi is not None and rsi <= RSI_FLEX_BUY,
                    "touches": 1,
                }
        else:
            # ترند هابط: السعر يرتفع لمستوى EMA السريع من الأسفل
            dist = current - ema_fast
            if -buffer * 0.5 <= dist <= buffer * 1.2:
                rsi_bonus = SCORE_RSI_BONUS if (rsi and rsi >= RSI_FLEX_SELL) else int(SCORE_RSI_BONUS * 0.5)
                score = min(100, EMA_PB_SCORE_BASE + rsi_bonus)
                if score < SCORE_MIN:
                    return None
                return {
                    "signal": "ema_pullback", "direction": "PUT",
                    "score": score, "rsi": rsi, "atr": atr,
                    "ema_fast": ema_fast, "ema_slow": ema_slow,
                    "ema_gap_pct": gap_pct * 100,
                    "last_close": current, "last_t": closed[-1]["time"],
                    "rsi_confirm": rsi is not None and rsi >= RSI_FLEX_SELL,
                    "touches": 1,
                }
        return None

    def _analyze_micro_channel(self, candles_1m: list) -> dict | None:
        """قناة سعرية مصغّرة (Micro-Channel Range) — تداول عند حدود النطاق.

        المنطق الرياضي:
        • يأخذ آخر 20 شمعة مغلقة ويحسب أعلى الأجسام (body_hi) وأدناها (body_lo).
        • إذا كانت الأجسام العليا متقاربة (انحرافها ≤ CHANNEL_ATR_WIDTH × ATR):
            قمم ← مقاومة أفقية → PUT عند الاقتراب
        • إذا كانت الأجسام الدنيا متقاربة:
            قيعان ← دعم أفقي → CALL عند الاقتراب
        • يشترط CHANNEL_MIN_TOUCHES لمسات متكررة على كل حد للتأكيد."""
        now = time.time()
        closed = [c for c in candles_1m
                  if isinstance(c, dict) and c.get("time", 0) + CANDLE_PERIOD <= now]
        if len(closed) < 15:
            return None
        seg = closed[-20:]   # آخر 20 شمعة لتحديد القناة
        closes  = [float(c["close"]) for c in closed if c.get("close")]
        if not closes:
            return None
        current = closes[-1]
        atr     = calc_atr(closed, ATR_PERIOD)
        if atr < 1e-9 or (current > 0 and atr / current < ATR_DEAD_ZONE):
            return None
        if is_momentum_breakout(closed, MOMENTUM_CANDLES, MOMENTUM_MULT):
            return None
        # أجسام كل شمعة في النافذة
        body_hi_list = [max(float(c.get("open", c.get("close", 0))),
                            float(c.get("close", 0))) for c in seg]
        body_lo_list = [min(float(c.get("open", c.get("close", 0))),
                            float(c.get("close", 0))) for c in seg]
        mean_hi = float(np.mean(body_hi_list))
        mean_lo = float(np.mean(body_lo_list))
        # عدد لمسات الحد العلوي والسفلي ضمن ATR × CHANNEL_ATR_WIDTH
        hi_zone = atr * CHANNEL_ATR_WIDTH
        lo_zone = atr * CHANNEL_ATR_WIDTH
        hi_touches = sum(1 for h in body_hi_list if abs(h - mean_hi) <= hi_zone)
        lo_touches = sum(1 for l in body_lo_list if abs(l - mean_lo) <= lo_zone)
        if hi_touches < CHANNEL_MIN_TOUCHES and lo_touches < CHANNEL_MIN_TOUCHES:
            return None
        # عرض القناة يجب أن يكون معقولاً (ليس ضيقاً جداً أو واسعاً جداً)
        ch_width = mean_hi - mean_lo
        if ch_width < atr * 0.5 or ch_width > atr * 8:
            return None
        rsi = calculate_rsi(closes, RSI_PERIOD)
        approach = max(atr * 1.0, current * PROX_PIPS_FLOOR)
        # ── PUT: السعر يقترب من السقف الأفقي (من الداخل أو عند الحد) ────────
        # dist_hi > 0: السعر فوق السقف (اختراق صاعد) → لا PUT
        # dist_hi ≤ 0: السعر داخل القناة أو عند السقف → PUT صالح
        # نسمح بتجاوز طفيف (0.2×ATR) لكن لا أكثر
        dist_hi = current - mean_hi
        if -approach * 0.5 <= dist_hi <= approach * 0.2 and hi_touches >= CHANNEL_MIN_TOUCHES:
            rsi_bonus = SCORE_RSI_BONUS if (rsi and rsi >= RSI_FLEX_SELL) else int(SCORE_RSI_BONUS * 0.4)
            touch_bonus = min(20, (hi_touches - 1) * 5)
            score = min(100, CHANNEL_SCORE_BASE + touch_bonus + rsi_bonus)
            if score < SCORE_MIN:
                return None
            return {
                "signal": "micro_channel", "direction": "PUT",
                "score": score, "rsi": rsi, "atr": atr,
                "channel_hi": mean_hi, "channel_lo": mean_lo,
                "channel_width": ch_width, "ch_touches": hi_touches,
                "last_close": current, "last_t": closed[-1]["time"],
                "rsi_confirm": rsi is not None and rsi >= RSI_FLEX_SELL,
                "touches": hi_touches,
            }
        # ── CALL: السعر يقترب من الدعم الأفقي (من الداخل أو عند الحد) ─────
        # dist_lo < 0: السعر تحت الدعم (اختراق هبوطي) → لا CALL
        # dist_lo ≥ 0: السعر داخل القناة أو عند الدعم → CALL صالح
        # نسمح بتجاوز طفيف (0.2×ATR) للأسفل فقط
        dist_lo = current - mean_lo
        if -approach * 0.2 <= dist_lo <= approach * 0.5 and lo_touches >= CHANNEL_MIN_TOUCHES:
            rsi_bonus = SCORE_RSI_BONUS if (rsi and rsi <= RSI_FLEX_BUY) else int(SCORE_RSI_BONUS * 0.4)
            touch_bonus = min(20, (lo_touches - 1) * 5)
            score = min(100, CHANNEL_SCORE_BASE + touch_bonus + rsi_bonus)
            if score < SCORE_MIN:
                return None
            return {
                "signal": "micro_channel", "direction": "CALL",
                "score": score, "rsi": rsi, "atr": atr,
                "channel_hi": mean_hi, "channel_lo": mean_lo,
                "channel_width": ch_width, "ch_touches": lo_touches,
                "last_close": current, "last_t": closed[-1]["time"],
                "rsi_confirm": rsi is not None and rsi <= RSI_FLEX_BUY,
                "touches": lo_touches,
            }
        return None

    # ── رسائل الإشارات ───────────────────────────────────────

    @staticmethod
    def _strength_bar(s):
        filled = max(0, min(10, int(round((s or 0) / 10))))
        return "🟩" * filled + "⬜" * (10 - filled)

    @staticmethod
    def _fmt_payout(payout):
        return f"{payout:.0f}%" if isinstance(payout, (int, float)) else "غير متاح"

    def _strength(self, direction, a):
        """تقدير محلي شفاف لقوة الإشارة (٪) من موقع RSI وبولينجر — ليس وعداً بربح."""
        rsi = a["rsi"]; lc = a["last_close"]
        ubb = a["upper_bb"]; lbb = a["lower_bb"]
        band = max(ubb - lbb, 1e-9)
        if direction == "CALL":
            ref = RSI_OVERSOLD + RSI_APPROACH
            rsi_part = (ref - rsi) / ref
            bb_part  = (lbb - lc) / band + 0.5
        else:
            ref = RSI_OVERBOUGHT - RSI_APPROACH
            rsi_part = (rsi - ref) / max(100 - ref, 1e-9)
            bb_part  = (lc - ubb) / band + 0.5
        rsi_part = max(0.0, min(1.0, rsi_part))
        bb_part  = max(0.0, min(1.0, bb_part))
        score = 100 * (0.6 * rsi_part + 0.4 * bb_part)
        return int(max(35, min(97, round(score))))

    def _signal_msg(self, asset, direction, result, payout, remaining,
                    sig_type, ai_txt="", ai_based=False):
        """رسالة الإشارة — مضغوطة وواضحة. تُعدَّل لاحقاً بنتيجة الصفقة والمضاعفات."""
        icon    = "📈" if direction == "CALL" else "📉"
        color   = "🟢" if direction == "CALL" else "🔴"
        dir_ar  = "CALL ▲" if direction == "CALL" else "PUT ▼"
        secs    = max(0, int(remaining))
        score   = result.get("score", 70)
        rsi     = result.get("rsi")
        candle_t = result["last_t"]
        entry   = datetime.fromtimestamp(candle_t + 2 * CANDLE_PERIOD).strftime("%H:%M")
        pay_str = self._fmt_payout(payout) if isinstance(payout, (int, float)) and payout >= MIN_SCAN_PAYOUT else "—"

        # ── سطر ملخص الإشارة (سطر واحد) ────────────────────────
        _PAT_SHORT = {
            "hammer": "🔨", "pin_bar": "📍",
            "bullish_engulfing": "🟢Eng", "bearish_engulfing": "🔴Eng", "doji": "⚖️",
        }
        pat_icon = _PAT_SHORT.get(result.get("candle_pattern", ""), "")
        conf_tag = " ⚡" if result.get("confluence") else ""

        if sig_type == "reversal":
            ltype   = result.get("level_type", "")
            role    = "دعم" if direction == "CALL" else "مقاومة"
            touches = result.get("touches", 1)
            level   = result.get("level", 0)
            summary = f"🔄 ارتداد {role} 30م @ <b>{self._fmt_px(level)}</b>  ×{touches}{conf_tag}"
        elif sig_type == "retest":
            level = result.get("level", 0)
            role  = "دعم ▲" if result.get("level_type") == "retest_support" else "مقاومة ▼"
            summary = f"🔀 اختبار {role} @ <b>{self._fmt_px(level)}</b>{conf_tag}"
        elif sig_type == "ema_pullback":
            gap = result.get("ema_gap_pct", 0)
            tr  = "صاعد" if direction == "CALL" else "هابط"
            summary = f"⚡ ارتداد EMA{EMA_FAST} | ترند {tr} | فجوة {gap:.2f}%{conf_tag}"
        elif sig_type == "micro_channel":
            ch_hi = result.get("channel_hi", 0)
            ch_lo = result.get("channel_lo", 0)
            t     = result.get("touches", 2)
            summary = f"📦 قناة [{self._fmt_px(ch_lo)} — {self._fmt_px(ch_hi)}]  ×{t}{conf_tag}"
        else:   # trendline
            touches = result.get("touches", 3)
            tl_dir  = result.get("tl_direction", "")
            tl_lbl  = "صاعد ▲" if tl_dir == "up" else "هابط ▼"
            summary = f"📐 ترند {tl_lbl} | ×{touches} لمسات{conf_tag}"

        if pat_icon:
            summary += f"  {pat_icon}"

        # ── سطر RSI (مختصر) ──────────────────────────────────────
        rsi_tag = ""
        if rsi is not None:
            chk = "✓" if result.get("rsi_confirm") else ""
            rsi_tag = f"RSI {rsi:.0f}{chk}  |  "

        # ── تحذير الاتجاه المعاكس (فقط إن كان قوياً) ────────────
        td     = result.get("trend_dir", "range")
        strong = result.get("trend_strong", False)
        counter = (direction == "CALL" and td == "down") or (direction == "PUT" and td == "up")
        warn_line = (f"⚠️ عكس اتجاه قوي ({('PUT▼' if td=='down' else 'CALL▲')}) — انتبه للمضاعفات\n"
                     if strong and counter else "")

        ai_line = (" ".join(ai_txt.split()) if ai_txt else "")

        return (
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{icon} <b>{fmt(asset)}</b>  {color} <b>{dir_ar}</b>  💰 <b>{pay_str}</b>\n"
            f"{summary}\n"
            f"{rsi_tag}قوة <b>{score}%</b>  {self._strength_bar(score)}\n"
            f"🕐 <b>{entry}</b>  (~{secs}ث)\n"
            + warn_line
            + (f"🧠 {ai_line}\n" if ai_line else "")
            + "━━━━━━━━━━━━━━━━━━"
        )

    @staticmethod
    def _trend_name(td):
        return {"up": "صاعد ▲", "down": "هابط ▼"}.get(td, "عرضي/متذبذب ↔")

    def _trend_block(self, direction, a):
        """سطر اتجاه السوق + قفل الاتجاه المستمر (Anti-Trend Lock) للإشارة.

        لا يحجب الإشارة (المالك يريد تدفقاً متوازناً) لكنه يضيف تحذيراً واضحاً
        عندما تكون الإشارة عكس اتجاه قوي مستمر — وهو أخطر موقف على المضاعفات."""
        td     = a.get("trend_dir", "range")
        strong = a.get("trend_strong", False)
        line   = f"🧭 الاتجاه العام: <b>{self._trend_name(td)}{' (قوي مستمر)' if strong else ''}</b>"
        counter = (direction == "CALL" and td == "down") or (direction == "PUT" and td == "up")
        if strong and counter:
            with_dir = "PUT ▼" if td == "down" else "CALL ▲"
            line += ("\n⚠️ <b>قفل الاتجاه المستمر:</b> السوق في اتجاه قوي مستمر <b>عكس</b> هذه الإشارة. "
                     f"يُفضّل دخول المضاعفة <b>مع الاتجاه ({with_dir})</b> لا عكسه، أو تأجيلها للشمعة التالية.")
        return line

    def _ai_signal_context(self, asset, direction, result, payout, sig_type):
        """يبني نصاً فنياً مدمجاً يُمرَّر للذكاء ليصوغ منه تحليله ونسبة قوته.
        مختصر عمداً ليبقى نداء الذكاء سريعاً ضمن ميزانية AI_BUDGET."""
        dir_ar = "شراء CALL (صعود ▲)" if direction == "CALL" else "بيع PUT (نزول ▼)"
        rsi    = result.get("rsi")
        atr    = result.get("atr", 0)
        score  = result.get("score", 70)
        _sig_labels = {
            "reversal":      "ارتداد من مستوى دعم/مقاومة 30 دقيقة",
            "retest":        "إعادة اختبار مستوى مكسور (Role Reversal)",
            "trendline":     "اقتراب من خط ترند (1 دقيقة)",
            "ema_pullback":  f"ارتداد عند EMA({EMA_FAST}) — مضاربة سريعة مع الترند",
            "micro_channel": "تداول عند حدود القناة السعرية المصغّرة",
        }
        lines = [
            f"الأصل: {fmt(asset)}",
            f"نوع الإشارة: {_sig_labels.get(sig_type, sig_type)}",
            f"الاتجاه الفني المقترح: {dir_ar}",
            f"السعر الحالي: {result.get('last_close', 0):.5f}",
            f"ATR(14): {atr:.5f}" if atr else "",
            f"RSI(14): {rsi:.1f}" if rsi is not None else "",
            f"قوة الإشارة (مصفوفة النقاط): {score}%",
            f"العائد (Payout): {self._fmt_payout(payout)}",
        ]
        # تفاصيل إضافية حسب نوع الإشارة
        if sig_type == "reversal":
            lines += [
                f"مستوى الدعم/المقاومة: {self._fmt_px(result.get('level', 0))} ({result.get('level_type', '')})",
                f"عدد الارتدادات التاريخية من المستوى: {result.get('touches', 1)}",
                f"تأكيد RSI: {'نعم' if result.get('rsi_confirm') else 'جزئي'}",
            ]
        elif sig_type == "retest":
            lines += [
                f"المستوى المكسور: {self._fmt_px(result.get('level', 0))}",
                f"الدور الجديد: {'دعم' if result.get('level_type') == 'retest_support' else 'مقاومة'}",
                f"تأكيد RSI: {'نعم' if result.get('rsi_confirm') else 'جزئي'}",
            ]
        elif sig_type == "ema_pullback":
            lines += [
                f"EMA({EMA_FAST}) السريع: {self._fmt_px(result.get('ema_fast', 0))}",
                f"EMA({EMA_SLOW}) البطيء: {self._fmt_px(result.get('ema_slow', 0))}",
                f"فجوة الترند: {result.get('ema_gap_pct', 0):.3f}%",
                f"تأكيد RSI: {'نعم' if result.get('rsi_confirm') else 'جزئي'}",
            ]
        elif sig_type == "micro_channel":
            lines += [
                f"سقف القناة: {self._fmt_px(result.get('channel_hi', 0))}",
                f"قاع القناة: {self._fmt_px(result.get('channel_lo', 0))}",
                f"عرض القناة: {self._fmt_px(result.get('channel_width', 0))}",
                f"لمسات الحد: {result.get('touches', 2)}",
            ]
        else:   # trendline
            lines += [
                f"اتجاه الترند: {'صاعد' if result.get('tl_direction') == 'up' else 'هابط'}",
                f"لمسات خط الترند: {result.get('touches', 3)}",
                f"شرح الإشارة: {result.get('description', '')}",
            ]
        # لمسات هندسية في السياق
        if result.get("candle_pattern"):
            lines.append(f"نموذج الشمعة: {result['candle_pattern']}")
        if result.get("confluence"):
            lines.append("تقاطع متعدد: توافق فني + نموذج شمعة")
        lines = [l for l in lines if l]
        lines.append("المطلوب: اكتب سطراً واحداً فقط (كلمات قليلة) يدمج السبب الفني مع درجة المخاطرة. بدون تنسيق أو نقاط. لا تضمن الربح.")
        return "\n".join(lines)

    def _payout(self, asset):
        """نسبة العائد الحالية للأصل عبر التوكن (قراءة متزامنة من instruments)."""
        try:
            p = self.client.get_payout_by_asset(asset, "1")
            return float(p) if isinstance(p, (int, float)) else None
        except Exception:
            return None

    # ── ذاكرة المحادثة المستمرة (تبقى بعد إعادة التشغيل) ──────
    def _load_chat_history(self):
        """يحمّل ذاكرة المحادثة من القرص حتى لا تُفقد عند إعادة تشغيل البوت."""
        try:
            with open(CHAT_HISTORY_PATH, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(f"تعذّر تحميل ذاكرة المحادثة: {e}")
        return {}

    def _save_chat_history(self):
        """يحفظ ذاكرة المحادثة إلى القرص بعد كل تحديث (كتابة ذرّية عبر ملف مؤقت + os.replace)."""
        try:
            tmp = CHAT_HISTORY_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.chat_history, f, ensure_ascii=False)
            os.replace(tmp, CHAT_HISTORY_PATH)
        except Exception as e:
            logger.warning(f"تعذّر حفظ ذاكرة المحادثة: {e}")

    def _load_candles(self):
        """يسترجع مخزّن الشموع من القرص عند الإقلاع لتفادي فترة الإحماء وإعادة التنزيل
        العميق لكل الأصول. أمان: نتجاهل أي أصل أحدث شمعة محفوظة له أقدم من
        CANDLE_RELOAD_MAX_AGE (لتفادي فجوة زمنية تُفسد المؤشرات بعد توقّف طويل)."""
        try:
            with open(CANDLES_PATH, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except Exception as e:
            logger.warning(f"تعذّر تحميل مخزّن الشموع: {e}")
            return
        if not isinstance(data, dict):
            return
        now = time.time()
        kept = 0
        for asset, candles in data.items():
            if not isinstance(candles, list) or not candles:
                continue
            try:
                newest = max(int(c["time"]) for c in candles
                             if isinstance(c, dict) and c.get("time") is not None)
            except (ValueError, TypeError, KeyError):
                continue
            # نتجاهل المخزّن القديم: الجلب الخفيف (~10 شموع) لا يسدّ فجوة أكبر من ذلك
            if now - newest > CANDLE_RELOAD_MAX_AGE:
                continue
            valid = sorted(
                (c for c in candles
                 if isinstance(c, dict) and c.get("time") is not None and c.get("close") is not None),
                key=lambda x: int(x["time"]))
            # نُبقي فقط الذيل المتّصل (كل شمعة تسبق التي تليها بـ CANDLE_PERIOD بالضبط):
            # فجوة داخلية تُفسد حساب RSI/BB، فنقتطعها ونترك الجلب الخفيف يُكمل النقص.
            tail = []
            for c in reversed(valid):
                if tail and int(tail[-1]["time"]) - int(c["time"]) != CANDLE_PERIOD:
                    break
                tail.append(c)
            tail.reverse()
            if tail:
                self.last_candles[asset] = tail[-CANDLE_BUFFER_MAX:]
                kept += 1
        if kept:
            logger.info(f"📦 استُرجع مخزّن الشموع لـ {kept} أصل من القرص (إقلاع فوري)")

    def _save_candles(self):
        """يحفظ مخزّن الشموع إلى القرص (كتابة ذرّية) ليُسترجع فور إعادة التشغيل."""
        try:
            tmp = CANDLES_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.last_candles, f, ensure_ascii=False)
            os.replace(tmp, CANDLES_PATH)
        except Exception as e:
            logger.debug(f"تعذّر حفظ مخزّن الشموع: {e}")

    def _save_tracks(self):
        """يحفظ المتابعات الجارية إلى القرص (كتابة ذرّية) لاستئنافها بعد إعادة التشغيل."""
        try:
            tmp = TRACKS_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.active_tracks, f, ensure_ascii=False)
            os.replace(tmp, TRACKS_PATH)
        except Exception as e:
            logger.debug(f"تعذّر حفظ المتابعات الجارية: {e}")

    def _resume_tracks(self):
        """يستأنف عند الإقلاع متابعة الصفقات التي كانت جارية لحظة الإطفاء (أو انتهت
        حديثاً) بإعادة إطلاق _track_signal_live — فيُعاد بناء لوحة المتابعة من الشموع
        الفعلية. نتجاهل المتابعات القديمة جداً (خارج TRACK_RESUME_WINDOW)."""
        try:
            with open(TRACKS_PATH, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except Exception as e:
            logger.warning(f"تعذّر تحميل المتابعات الجارية: {e}")
            return
        if not isinstance(data, dict):
            return
        now = time.time()
        resumed = 0
        seen = set()
        for mid, t in data.items():
            if not isinstance(t, dict):
                continue
            try:
                asset = str(t["asset"]); direction = str(t["direction"])
                base_msg = str(t["base_msg"]); message_id = t["message_id"]
                entry_open = float(t["entry_open"])
            except (KeyError, TypeError, ValueError):
                continue
            key = str(message_id)
            if key in seen:        # تفادي ازدواج متتبّع لنفس الرسالة لو تكرّر في الملف
                continue
            seen.add(key)
            last_result_t = entry_open + MARTINGALE_MAX * CANDLE_PERIOD
            if now - last_result_t > TRACK_RESUME_WINDOW:
                continue   # انتهت الصفقة منذ وقت طويل — لا فائدة من إعادة تحريرها
            self.active_tracks[key] = t
            task = asyncio.create_task(self._track_signal_live(
                asset, direction, base_msg, message_id, entry_open))
            self._sig_tasks.add(task)
            task.add_done_callback(self._sig_tasks.discard)
            resumed += 1
        if resumed:
            self._save_tracks()
            logger.info(f"🔁 استُؤنفت متابعة {resumed} صفقة جارية بعد إعادة التشغيل")

    def _merge_candles(self, asset, new):
        """يدمج الشموع الجديدة مع المخزّنة (إزالة التكرار حسب الوقت) ويحتفظ بأحدث
        CANDLE_BUFFER_MAX شمعة. هذا يسمح للمخزون بالنمو عبر الدورات رغم أن كل
        طلب history/load يعيد ~10 شموع فقط."""
        buf = {int(c["time"]): c for c in self.last_candles.get(asset, [])
               if isinstance(c, dict) and c.get("time") is not None}
        for c in new:
            if isinstance(c, dict) and c.get("time") is not None and c.get("close") is not None:
                buf[int(c["time"])] = c
        merged = sorted(buf.values(), key=lambda x: int(x["time"]))[-CANDLE_BUFFER_MAX:]
        self.last_candles[asset] = merged
        return merged

    async def _get_candles_for(self, asset):
        """يضمن توفّر شموع كافية: أول مرة (أو عند نقص المخزون) يجلب تاريخاً عميقاً
        عبر get_historical_candles (يتجاوز سقف history/load ~10 شموع)، وبعدها يكتفي
        بجلب خفيف للأحدث ويدمجه. يعيد المخزون المدموج (≥ MIN_CANDLES متى أمكن)."""
        now = time.time()
        existing = self.last_candles.get(asset, [])
        closed = [c for c in existing
                  if isinstance(c, dict) and c.get("time", 0) + CANDLE_PERIOD <= now]
        try:
            if len(closed) < MIN_CANDLES:
                new = await self.client.get_historical_candles(
                    asset, BACKFILL_SECONDS, CANDLE_PERIOD,
                    timeout=BACKFILL_TIMEOUT, max_workers=3)
            else:
                new = await asyncio.wait_for(
                    self.client.get_candles(
                        asset, now, HISTORY_OFFSET, CANDLE_PERIOD),
                    timeout=CANDLE_FETCH_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning(f"جلب شموع {asset}: انتهت المهلة ({CANDLE_FETCH_TIMEOUT}s)")
            new = None
        except Exception as e:
            logger.warning(f"جلب شموع {asset}: {e}")
            new = None
        if not new:
            return existing
        return self._merge_candles(asset, new)

    async def _get_candles_30m_for(self, asset: str) -> list:
        """يجلب/يحدّث شموع فريم 30 دقيقة (أقصى 40 شمعة = آخر 20 ساعة).
        يُحدَّث كل 15 دقيقة فقط لتفادي إثقال السيرفر."""
        now = time.time()
        if (now - self._30m_last_fetch.get(asset, 0) < SR_30M_REFRESH
                and asset in self.last_candles_30m):
            return self.last_candles_30m[asset]
        try:
            candles = await asyncio.wait_for(
                self.client.get_candles(asset, now, SR_30M_OFFSET, PERIOD_30M),
                timeout=CANDLE_FETCH_TIMEOUT)
            if candles:
                self.last_candles_30m[asset] = candles[-SR_30M_LIMIT:]
                self._30m_last_fetch[asset]  = now
                logger.debug(f"30م {asset}: {len(self.last_candles_30m[asset])} شمعة")
        except Exception as e:
            logger.debug(f"جلب 30م {asset}: {e}")
        return self.last_candles_30m.get(asset, [])

    async def _candle_diag(self):
        """تشخيص لمرة واحدة: يقارن عدد الشموع من get_candles مقابل
        get_historical_candles لأصل واحد لمعرفة مصدر نقص البيانات."""
        pool = self.qualified_assets or self.otc_assets
        if not pool:
            logger.info("🔬 تشخيص الشموع: لا توجد أصول محمّلة بعد")
            return
        asset = pool[0]
        logger.info(f"🔬 تشخيص الشموع للأصل: {asset}")
        try:
            t0 = time.time()
            gc = await self.client.get_candles(asset, time.time(), HISTORY_OFFSET, CANDLE_PERIOD)
            logger.info(f"🔬 get_candles: {len(gc or [])} شمعة في {time.time()-t0:.1f}s؛ "
                        f"مفاتيح العينة={list((gc or [{}])[0].keys())}")
        except Exception as e:
            logger.info(f"🔬 get_candles خطأ: {e}")
        for secs, mw in ((1800, 1), (1800, 3)):
            try:
                t0 = time.time()
                hc = await self.client.get_historical_candles(asset, secs, CANDLE_PERIOD, max_workers=mw)
                span = 0
                if hc:
                    span = (int(hc[-1]["time"]) - int(hc[0]["time"])) // 60
                logger.info(f"🔬 get_historical_candles(secs={secs},workers={mw}): "
                            f"{len(hc or [])} شمعة، مدى={span}د في {time.time()-t0:.1f}s")
            except Exception as e:
                logger.info(f"🔬 get_historical_candles(secs={secs},workers={mw}) خطأ: {e}")

    async def _fetch_and_store(self, asset):
        """يجلب الشموع + العائد ويحدّث الحالة — يعمل بالكامل على خيط حلقة الأحداث
        للحفاظ على ملكية خيط واحد لحالة البوت وعميل كوتيكس (تفادي تسابق الخيوط)."""
        candles = await self._get_candles_for(asset)
        if not candles:
            return None
        a = self.analyze(candles)
        if a:
            self.last_analysis[asset] = a
            lt  = a["last_t"]
            age = time.time() - lt
            self.last_verify[asset] = {"payout": self._payout(asset),
                                       "fresh": age <= LAG_MAX_AGE,
                                       "age": int(age), "t": time.time()}
        return candles

    def _fetch_candles_sync(self, asset, timeout: float = 15.0):
        """واجهة متزامنة تُستدعى من خيط الوكيل الذكي (run_in_executor): تجدول
        _fetch_and_store على حلقة الأحداث وتنتظر النتيجة — كل التعديلات على الحالة
        وكل استدعاءات العميل تجري على خيط الحلقة وحده. يلغي انتظار دورة الفحص الطويلة."""
        if not self.loop or not self.client:
            return None
        try:
            fut = asyncio.run_coroutine_threadsafe(self._fetch_and_store(asset), self.loop)
            return fut.result(timeout=timeout)
        except Exception as e:
            logger.warning(f"جلب فوري للشموع {asset}: {e}")
            return None

    def _refresh_qualified(self):
        """يفلتر الأصول دورياً عبر التوكن: يستبعد أي أصل عائده < MIN_SCAN_PAYOUT
        من قائمة الفحص. الأصول مجهولة العائد (None) تبقى ضمن القائمة ريثما يتوفر العائد."""
        payouts = {}
        for a in self.otc_assets:
            payouts[a] = self._payout(a)
        self.last_payouts = payouts
        self.qualified_assets = [
            a for a in self.otc_assets
            if (payouts.get(a) is None) or (payouts[a] >= MIN_SCAN_PAYOUT)
        ]
        ok = sum(1 for a in self.otc_assets if isinstance(payouts.get(a), (int, float))
                 and payouts[a] >= MIN_SCAN_PAYOUT)
        logger.info(f"🔎 فلترة العوائد: {ok} أصل ≥ {MIN_SCAN_PAYOUT}% "
                    f"(قيد الفحص {len(self.qualified_assets)} من {len(self.otc_assets)})")

    # ── فحص أصل واحد ─────────────────────────────────────────

    async def check_asset(self, asset: str):
        """فحص أصل واحد عبر مسارَين مستقلَّين تماماً يُطلقان إشاراتهما في آنٍ واحد.

        ─ المسار الأول  (Path-1 / 30m S/R):
          ارتداد من مستوى دعم/مقاومة فريم 30 دقيقة ← إعادة اختبار مكسور (Role Reversal).
        ─ المسار الثاني (Path-2 / 1m Trend):
          خط ترند فريم 1 دقيقة ← ارتداد عند EMA ← قناة سعرية مصغّرة.

        كلا المسارَين يعملان على نفس مجموعة الشموع لكنهما منفصلان في منطق القرار
        ومفتاح إزالة التكرار — ما يتيح إرسال إشارتَين مختلفتَين لنفس الأصل في الدقيقة ذاتها."""
        try:
            candles = await self._get_candles_for(asset)
            if not candles:
                return
            self._diag_candles += 1
            self._diag_candle_len += len(candles)

            candles_30m = await self._get_candles_30m_for(asset)

            # ══ المسار الأول: مستويات 30 دقيقة (ارتداد + إعادة الاختبار) ═══════
            path1_result, path1_type = None, "reversal"
            r1 = self._analyze_reversal(candles, candles_30m)
            if r1 is not None:
                path1_result, path1_type = r1, "reversal"
            else:
                self._maybe_record_breakout(asset, candles, candles_30m)
                r2 = self._analyze_retest(asset, candles)
                if r2 is not None:
                    path1_result, path1_type = r2, "retest"
                else:
                    # ── لا إشارة مؤكدة — هل السعر يقترب من مستوى؟ → تنبيه مبكر ──
                    _pre = self._build_pre_alert_p1(
                        asset, candles, candles_30m, time.time())
                    if _pre:
                        asyncio.create_task(asyncio.to_thread(tg_send_tracked, _pre))
                        logger.info(f"⚡ تنبيه مبكر أُرسل: {asset}")

            # ══ المسار الثاني: ترندات فريم 1 دقيقة (ترند + EMA + قناة) ══════════
            path2_result, path2_type = None, "trendline"
            t1 = self._analyze_trendline(candles)
            if t1 is not None:
                path2_result, path2_type = t1, "trendline"
            else:
                t2 = self._analyze_ema_pullback(candles)
                if t2 is not None:
                    path2_result, path2_type = t2, "ema_pullback"
                else:
                    t3 = self._analyze_micro_channel(candles)
                    if t3 is not None:
                        path2_result, path2_type = t3, "micro_channel"

            if path1_result is None and path2_result is None:
                return

            self._diag_analyzed += 1
            basic = self.analyze(candles)
            if basic:
                self.last_analysis[asset] = basic

            now    = time.time()
            payout = self.last_payouts.get(asset)
            if payout is None:
                payout = self._payout(asset)
            if isinstance(payout, (int, float)) and payout < MIN_SCAN_PAYOUT:
                return

            # حداثة البيانات (مشتركة بين المسارَين)
            _ref  = path1_result or path2_result
            lt    = _ref["last_t"]
            age   = now - lt
            fresh = age <= LAG_MAX_AGE
            if fresh:
                self._diag_fresh += 1
            self.last_verify[asset] = {"payout": payout, "fresh": fresh,
                                       "age": int(age), "t": now}
            if not fresh:
                logger.warning(f"⛔ {asset} مؤجلة — بيانات متأخرة ({int(age)}ث)")
                return

            if asset not in self.state:
                self.state[asset] = {}
            s = self.state[asset]
            if not hasattr(self, "_sig_tasks"):
                self._sig_tasks = set()

            # نموذج الشمعة (يُحسب مرة واحدة ويُطبَّق على كل مسار)
            _closed_chk = [c for c in candles
                           if isinstance(c, dict) and c.get("time", 0) + CANDLE_PERIOD <= now]
            _pat = detect_candle_pattern(_closed_chk) if len(_closed_chk) >= 2 else None

            entry_open = lt + 2 * CANDLE_PERIOD
            remaining  = entry_open - now
            if remaining < SIGNAL_START_LEAD:
                return

            # ══ إطلاق كل مسار باستقلالية تامة ══════════════════════════════════
            for result, sig_type, path_key in [
                (path1_result, path1_type, "p1_t"),
                (path2_result, path2_type, "p2_t"),
            ]:
                if result is None:
                    continue
                if s.get(path_key) == entry_open:
                    continue   # أُرسلت إشارة هذا المسار للشمعة الحالية مسبقاً

                # تطبيق نموذج الشمعة (نسخة مستقلة لكل مسار)
                result = dict(result)
                if _pat:
                    if _pat["direction"] == result["direction"]:
                        result["score"] = min(100, result["score"] + SCORE_PATTERN_BONUS)
                        result["candle_pattern"] = _pat["pattern"]
                    elif _pat["pattern"] == "doji":
                        result["score"] = min(100, result["score"] + SCORE_PATTERN_BONUS // 2)
                        result["candle_pattern"] = "doji"
                if result.get("candle_pattern") and result.get("candle_pattern") != "doji":
                    if result["score"] >= 65:
                        result["score"]      = min(100, result["score"] + SCORE_CONFLUENCE)
                        result["confluence"] = True

                s[path_key] = entry_open
                task = asyncio.create_task(self._send_merged_signal(
                    asset, result["direction"], result, payout, entry_open, sig_type))
                self._sig_tasks.add(task)
                task.add_done_callback(self._sig_tasks.discard)

        except Exception as e:
            logger.debug(f"{asset}: {e}")

    async def _send_merged_signal(self, asset, direction, result, payout,
                                  entry_open, sig_type):
        """يجمع التحليل الفني + تحليل الذكاء + شارت مرسوم في رسالة واحدة.
        يُشغّل رسم الشارت ونداء الذكاء بالتوازي لتوفير الوقت."""
        try:
            local_strength = result.get("score", 70)
            candles_snap   = list(self.last_candles.get(asset, []))

            # ── تشغيل الذكاء ورسم الشارت بالتوازي ───────────────
            async def _ai_task():
                if not self.ai.enabled:
                    return "", False, local_strength
                ctx = self._ai_signal_context(asset, direction, result, payout, sig_type)
                adv = await asyncio.to_thread(self.ai.advise_signal, ctx, AI_BUDGET)
                if adv and adv.get("analysis"):
                    conf = adv.get("confidence") or local_strength
                    return adv["analysis"], True, conf
                return "", False, local_strength

            ai_coro   = _ai_task()
            chart_coro = asyncio.to_thread(
                draw_signal_chart, candles_snap, result, direction, sig_type, asset)

            (ai_txt, ai_based, strength), chart_bytes = await asyncio.gather(
                ai_coro, chart_coro, return_exceptions=False)

            remaining = entry_open - time.time()
            if remaining < MIN_SEND_LEAD:
                logger.warning(f"⏱️ {asset} {direction} أُلغيت — تأخّر الدمج "
                               f"(تبقّى {int(remaining)}ث < {MIN_SEND_LEAD})")
                return

            base_msg = self._signal_msg(
                asset, direction, result, payout, remaining, sig_type, ai_txt, ai_based)

            # ── إرسال الشارت + نص الإشارة في رسالة واحدة (photo+caption) ──
            is_photo = False
            if chart_bytes:
                message_id = await asyncio.to_thread(
                    tg_send_photo, chart_bytes, base_msg)
                is_photo = bool(message_id)
            else:
                message_id = None

            # إن فشل إرسال الصورة أو لا يوجد شارت — نرسل نصاً عادياً
            if not message_id:
                message_id = await asyncio.to_thread(tg_send_tracked, base_msg)
                is_photo = False

            if not message_id:
                return
            self.signals_sent += 1
            rsi_val = result.get("rsi")
            rsi_str = f"RSI={rsi_val:.0f}" if rsi_val is not None else ""
            self.last_signal = (f"{fmt(asset)} {direction} "
                                f"{'ارتداد' if sig_type == 'reversal' else 'ترند'}"
                                f"{' ' + rsi_str if rsi_str else ''}")
            logger.info(f"🔔 إشارة {sig_type} {asset} {direction} "
                        f"قوة~{strength}%{' (ذكي)' if ai_based else ''}"
                        f"{' 📊' if is_photo else ''} تبقّى~{int(remaining)}ث"
                        + ("" if ai_txt else " (بلا تحليل ذكي)"))
            self.active_tracks[str(message_id)] = {
                "asset": asset, "direction": direction, "base_msg": base_msg,
                "message_id": message_id, "entry_open": entry_open,
                "is_photo": is_photo}
            self._save_tracks()
            live = asyncio.create_task(self._track_signal_live(
                asset, direction, base_msg, message_id, entry_open))
            self._sig_tasks.add(live)
            live.add_done_callback(self._sig_tasks.discard)
        except Exception as e:
            logger.debug(f"إرسال مدمج {asset}: {e}")

    # ── المتابعة الحية لنتائج الإشارة والمضاعفات (تعديل نفس الرسالة) ──

    def _candle_oc_at(self, asset, t):
        """يعيد (افتتاح، إغلاق) الشمعة التي وقتها == t من المخزّن، أو (None, None).
        نقرأ الافتتاح والإغلاق لنفس شمعة الصفقة: هذا هو التقييم الصحيح للخيار الثنائي
        (الدخول عند افتتاح الشمعة والنتيجة عند إغلاقها) — لا نستخدم إغلاق الشمعة
        السابقة كبديل عن سعر الدخول لأن أسعار OTC قد تفتح بفجوة عن إغلاق ما قبلها."""
        for c in self.last_candles.get(asset, []):
            if isinstance(c, dict) and int(c.get("time", -1)) == int(t):
                try:
                    cl = float(c["close"])
                    op = float(c.get("open", cl))
                    return op, cl
                except (TypeError, ValueError, KeyError):
                    return None, None
        return None, None

    async def _resolve_outcome(self, asset, trade_t):
        """يعيد (سعر الدخول، سعر النتيجة) لشمعة الصفقة نفسها (افتتاحها وإغلاقها)؛
        يقرأ المخزّن أولاً، وإن نقصت الشمعة يجلب تحديثاً خفيفاً مرة أو أكثر."""
        ep, op = self._candle_oc_at(asset, trade_t)
        if ep is not None and op is not None:
            return ep, op
        for _ in range(LIVE_FETCH_RETRIES):
            try:
                await self._get_candles_for(asset)
            except Exception as e:
                logger.debug(f"جلب حي {asset}: {e}")
            ep, op = self._candle_oc_at(asset, trade_t)
            if ep is not None and op is not None:
                break
            await asyncio.sleep(LIVE_RETRY_SLEEP)
        return ep, op

    @staticmethod
    def _live_block(lines, status):
        body = "\n".join(lines) if lines else "  • <i>بانتظار نتيجة الصفقة الأساسية...</i>"
        return ("\n━━━━━━━━━━━━━━━━━━\n"
                "📊 <b>المتابعة الحية:</b>\n"
                f"{body}\n\n"
                f"{status}")

    async def _edit_live(self, message_id, base_msg, lines, status):
        full = base_msg + self._live_block(lines, status)
        is_photo = self.active_tracks.get(str(message_id), {}).get("is_photo", False)
        if is_photo:
            ok = await asyncio.to_thread(tg_edit_caption, message_id, full)
        else:
            ok = await asyncio.to_thread(tg_edit, message_id, full)
        if not ok:
            logger.debug(f"تعذّر تعديل الرسالة الحية {message_id}")

    @staticmethod
    def _fmt_px(price: float) -> str:
        """يُنسّق السعر بعدد مناسب من الخانات العشرية (حتى 5 أرقام لا صفرية)."""
        if price == 0:
            return "0"
        abs_p = abs(price)
        if abs_p >= 100:
            return f"{price:.3f}"
        if abs_p >= 1:
            return f"{price:.4f}"
        return f"{price:.5f}"

    async def _track_signal_live(self, asset, direction, base_msg, message_id, entry_open):
        """يعدّل نفس رسالة الإشارة لإظهار نتيجة الصفقة الأساسية ثم المضاعفات (حتى
        الخامسة) شمعةً بشمعة، حتى أول ربح أو نفاد المضاعفات.

        يعرض الأسعار الفعلية (افتتاح → إغلاق) ووقت الشمعة المقروءة في كل سطر
        حتى يتمكن المستخدم من التحقق مباشرةً من الرسم البياني."""
        P = CANDLE_PERIOD
        ordinals = ["الأولى", "الثانية", "الثالثة", "الرابعة", "الخامسة"]
        dir_ar  = "CALL ▲" if direction == "CALL" else "PUT ▼"
        lines   = []

        def _candle_label(t: float) -> str:
            """يعيد 'HH:MM–HH:MM' للشمعة التي تفتح عند الوقت t."""
            open_str  = datetime.fromtimestamp(t).strftime("%H:%M")
            close_str = datetime.fromtimestamp(t + P).strftime("%H:%M")
            return f"{open_str}–{close_str}"

        def _px_line(entry_px: float, out_px: float, candle_t: float) -> str:
            """سطر مختصر: دخول → خروج (توقيت الشمعة)."""
            arrow = "↗" if out_px > entry_px else ("↘" if out_px < entry_px else "↔")
            return (f"    <i>دخول {self._fmt_px(entry_px)} {arrow} "
                    f"إغلاق {self._fmt_px(out_px)}</i>"
                    f"  🕐 <i>{_candle_label(candle_t)}</i>")

        try:
            for k in range(0, MARTINGALE_MAX + 1):   # 0 = الأساسية، 1..5 = المضاعفات
                trade_t = entry_open + k * P          # وقت افتتاح شمعة هذا المستوى
                wait    = (trade_t + P + LIVE_GRACE) - time.time()   # حتى تُغلق + هامش
                if wait > 0:
                    await asyncio.sleep(wait)

                entry_px, out_px = await self._resolve_outcome(asset, trade_t)
                level_name = "الصفقة الأساسية" if k == 0 else f"المضاعفة {ordinals[k - 1]}"
                candle_lbl = _candle_label(trade_t)

                # ── تعذّر التحقق ──────────────────────────────────────
                if entry_px is None or out_px is None:
                    lines.append(f"  • {level_name}: <b>تعذّر التحقق من النتيجة</b> ⚠️\n"
                                 f"    <i>لم تُعثر على بيانات شمعة {candle_lbl}</i>")
                    await self._edit_live(message_id, base_msg, lines,
                                          "🎯 <b>النتيجة النهائية:</b> تعذّر تأكيد النتيجة من بيانات الأسعار ⚠️")
                    logger.warning(
                        f"📊 [{asset}] {dir_ar} مستوى {k}: "
                        f"لم تُعثر على شمعة trade_t={trade_t} "
                        f"({candle_lbl}) في المخزّن"
                    )
                    return

                # ── تحقق تشخيصي بالسجل دائماً ───────────────────────
                diff    = out_px - entry_px
                diff_pct = (diff / entry_px * 100) if entry_px else 0
                logger.info(
                    f"📊 [{asset}] {dir_ar} | مستوى {k} | شمعة {candle_lbl} "
                    f"| افتتاح={self._fmt_px(entry_px)} إغلاق={self._fmt_px(out_px)} "
                    f"فرق={diff:+.5f} ({diff_pct:+.3f}%)"
                )

                # ── تعادل/استرداد ─────────────────────────────────────
                if out_px == entry_px:
                    lines.append(f"  • {level_name}: <b>تعادل (استرداد)</b> ↔\n"
                                 + _px_line(entry_px, out_px, trade_t))
                    await self._edit_live(message_id, base_msg, lines,
                                          "🎯 <b>النتيجة النهائية:</b> تعادل/استرداد ↔ (لا ربح ولا خسارة)")
                    logger.info(f"📊 [{asset}] مستوى {k}: تعادل")
                    return

                won = (out_px > entry_px) if direction == "CALL" else (out_px < entry_px)

                # ── تحذير تشخيصي: الاتجاه عكس التوقع ────────────────
                if not won:
                    expected_move = "صعود" if direction == "CALL" else "هبوط"
                    actual_move   = "صعد" if out_px > entry_px else "هبط"
                    logger.info(
                        f"📊 [{asset}] مستوى {k}: خسارة — "
                        f"الإشارة توقّعت {expected_move} لكن السعر {actual_move}"
                    )

                if won:
                    lines.append(f"  • {level_name}: <b>ربحت</b> ✅\n"
                                 + _px_line(entry_px, out_px, trade_t))
                    final = ("🎯 <b>النتيجة النهائية:</b> نجحت الصفقة الأساسية ✅" if k == 0
                             else f"🎯 <b>النتيجة النهائية:</b> ربحت في المضاعفة {ordinals[k - 1]} (رقم {k}) ✅")
                    await self._edit_live(message_id, base_msg, lines, final)
                    return

                # ── خسارة هذا المستوى ─────────────────────────────────
                lines.append(f"  • {level_name}: <b>خسرت</b> ❌\n"
                             + _px_line(entry_px, out_px, trade_t))
                if k < MARTINGALE_MAX:
                    await self._edit_live(message_id, base_msg, lines,
                                          f"⏳ <i>جاري متابعة المضاعفة {ordinals[k]}...</i>")
                else:
                    await self._edit_live(message_id, base_msg, lines,
                                          "🎯 <b>النتيجة النهائية:</b> لم تنجح المضاعفات ❌ (بلغت الحد الأقصى)")
                    logger.info(f"📊 [{asset}]: خسارة كل المضاعفات")
                    return

        except Exception as e:
            logger.warning(f"متابعة حية {asset}: {e}", exc_info=True)
        finally:
            # المتابعة انتهت (ربح/خسارة/تعادل/تعذّر) — نزيلها من السجل المحفوظ فلا تُستأنف.
            if self.active_tracks.pop(str(message_id), None) is not None:
                self._save_tracks()

    # ── الذكاء الاصطناعي ─────────────────────────────────────

    def find_asset(self, text: str, assets=None):
        """يبحث عن أصل OTC مذكور في نص المستخدم (EUR/USD، eurusd، ...)."""
        t = (text or "").upper().replace(" ", "").replace("/", "").replace("-", "")
        if not t:
            return None
        best = None
        for a in (assets if assets is not None else self.otc_assets):
            base = a.upper().replace("_OTC", "").replace("_", "")
            if base and base in t:
                if best is None or len(base) > len(best[1]):
                    best = (a, base)
        return best[0] if best else None

    def ai_context(self) -> str:
        """ملخص حالة البوت لتزويد المحادثة الذكية بسياق حقيقي."""
        conn = "متصل" if self.connected else ("بانتظار توكن" if self.waiting_token else "يعيد الاتصال")
        # حالة التوكن (القيمة محجوبة لأمان الحساب، لكن حالتها متاحة للمحلل)
        tok = self.active_token or ""
        tok_status = (f"موجود وفعّال (طوله {len(tok)} حرفاً، القيمة محجوبة لأمان حسابك)"
                      if tok else "غير متوفر — بانتظار توكن جديد")
        lines = [
            f"الحالة: {conn}",
            f"حالة التوكن: {tok_status}",
            f"عدد أصول OTC الكلي: {len(self.otc_assets)} | قيد الفحص بعد فلترة العائد ≥ {MIN_SCAN_PAYOUT}%: {len(self.qualified_assets)}",
            f"إشارات مُرسَلة: {self.signals_sent}",
            f"آخر إشارة: {self.last_signal or 'لا يوجد'}",
            f"نظام الإشارة: رسالة واحدة مدمجة (تفاصيل فنية + تحليل ذكي + نسبة قوة) تصل قبل افتتاح شمعة "
            f"الدخول بـ≥{SIGNAL_LEAD}ث (الدخول مع افتتاح الشمعة القادمة). تُجمع البيانات الفنية وتُمرَّر للذكاء "
            f"الذي يصوغ تحليله، ثم تُرسَل كلها في رسالة واحدة. شرطها العائد ≥ {MIN_SCAN_PAYOUT}% وبيانات حديثة "
            f"(عمر الشمعة ≤ {LAG_MAX_AGE}ث). بعد الإرسال تُحدَّث الرسالة نفسها حياً شمعةً بشمعة لعرض "
            f"نتيجة الصفقة الأساسية ثم المضاعفات (حتى الخامسة) حتى أول ربح أو نفادها.",
        ]
        # القائمة المختصرة للأصول المؤهلة (العائد ≥ 80%) — بيانات حية مباشرة من التوكن
        qual = [(self.last_payouts.get(a), a) for a in self.qualified_assets
                if isinstance(self.last_payouts.get(a), (int, float))]
        if qual:
            qual.sort(reverse=True)
            lines.append(
                f"الأصول المؤهلة الآن (عائد ≥ {MIN_SCAN_PAYOUT}% عبر التوكن — قائمة مباشرة): "
                + "، ".join(f"{fmt(a)} {p:.0f}%" for p, a in qual[:20]))
        stale = [fmt(a) for a, v in self.last_verify.items() if not v.get("fresh", True)]
        if stale:
            lines.append("أصول ببيانات متأخرة (تجنّبها): " + "، ".join(stale[:8]))
        strong_tr = [f"{fmt(a)} ({self._trend_name(v.get('trend_dir'))})"
                     for a, v in self.last_analysis.items()
                     if v.get("trend_strong") and v.get("trend_dir") in ("up", "down")]
        if strong_tr:
            lines.append("أصول في اتجاه قوي مستمر الآن (احذر مضاعفة عكسها): "
                         + "، ".join(strong_tr[:10]))
        lines.append(
            "إستراتيجية المالك: المضاعفات (Martingale). راعِ ذلك دائماً: الاتجاه القوي المستمر "
            "هو أخطر ما يكسر المضاعفات لأنه يُبقي RSI/Bollinger في التشبّع؛ انصح بالدخول مع الاتجاه "
            "أو تأجيل المضاعفة شمعةً، ولا تَعِد أبداً بتعويض الخسارة.")
        return "\n".join(lines)

    def _agent_dispatch(self, name: str, args: dict, candles: dict,
                        analysis: dict, assets: list, verify: dict = None) -> str:
        """ينفّذ أدوات الوكيل (يُستدعى داخل thread عبر run_in_executor).

        يعمل على لقطات (snapshots) من بيانات البوت أُخذت على خيط حلقة الأحداث
        لتفادي تعديل القواميس أثناء قراءتها من الخيط الآخر."""
        verify = verify or {}
        if name == "analyze_asset":
            raw   = (args or {}).get("asset", "")
            asset = self.find_asset(raw, assets) or (raw if raw in candles else None)
            if not asset:
                return f"لم أجد أصل OTC مطابقاً لـ «{raw}». تأكد من الاسم أو انتظر دورة فحص."
            c = candles.get(asset)
            if not c:
                # جلب فوري للبيانات بدل انتظار دورة الفحص (يلغي مشكلة القائمة الفارغة)
                c = self._fetch_candles_sync(asset)
            if not c:
                return f"تعذّر جلب بيانات شموع للأصل {fmt(asset)} الآن (قد يكون السوق مغلقاً أو التوكن منتهياً)."
            ind   = analysis.get(asset) or self.last_analysis.get(asset)
            chart = build_chart_text(fmt(asset), c, ind)
            return chart or "تعذّر بناء بيانات الشارت."
        if name == "verify_asset":
            raw   = (args or {}).get("asset", "")
            asset = self.find_asset(raw, assets) or (raw if (raw in verify or raw in self.last_verify) else None)
            if not asset:
                return f"لم أجد أصل OTC مطابقاً لـ «{raw}»."
            v = verify.get(asset) or self.last_verify.get(asset)
            if not v:
                # جلب فوري للتحقق بدل انتظار دورة الفحص
                self._fetch_candles_sync(asset)
                v = self.last_verify.get(asset)
            if not v:
                return (f"تعذّر جلب بيانات تحقق للأصل {fmt(asset)} الآن "
                        f"(قد يكون السوق مغلقاً أو التوكن منتهياً).")
            payout = v.get("payout")
            pay_s  = f"{payout:.0f}%" if isinstance(payout, (int, float)) else "غير متاح"
            ok_pay = (isinstance(payout, (int, float)) and payout >= MIN_SCAN_PAYOUT)
            age    = v.get("age", 0)
            fresh  = v.get("fresh", True)
            return (
                f"تحقق {fmt(asset)} عبر التوكن:\n"
                f"• العائد: {pay_s} — "
                f"{'مقبول ✅ (≥ '+str(MIN_SCAN_PAYOUT)+'%)' if ok_pay else 'منخفض ⛔ (سيُلغى التأكيد)' if isinstance(payout,(int,float)) else 'غير معروف'}\n"
                f"• عمر آخر شمعة: {age} ثانية — "
                f"{'حديثة (بدون تأخير) ✅' if fresh else 'متأخرة ⛔ (سيُؤجَّل التأكيد)'}\n"
                f"• الخلاصة: "
                f"{'مؤهّل لإشارة تأكيد عند اكتمال شروط RSI/BB.' if (ok_pay and fresh) else 'غير مؤهّل حالياً للتأكيد.'}"
            )
        if name == "restart_bot":
            return ("ℹ️ لا حاجة لإعادة تشغيل: تعديلات شروط الإشارة (RSI/Bollinger) "
                    "تُطبَّق فوراً وتلقائياً من الدورة القادمة دون إيقاف البوت. "
                    "إعادة تشغيل العملية ذاتياً توقف البوت على المنصّة، لذلك أُلغيت.")
        if name == "relax_conditions":
            rsi_pts  = (args or {}).get("rsi_points", 2)
            bb_delta = (args or {}).get("bb_delta", 0.2)
            return self._relax_conditions(rsi_pts, bb_delta)
        return agent_tools.dispatch(name, args or {})

    def _relax_conditions(self, rsi_points=2, bb_delta=0.2) -> str:
        """يخفّف (أو يشدّد) شروط الإشارة بتعديل ثوابت RSI_FLEX حيّاً في الذاكرة (دون إعادة تشغيل).
        يوسّع نطاق RSI المرن: يرفع RSI_FLEX_SELL ويخفض RSI_FLEX_BUY — يمنح نقاط التأكيد لأصول أكثر.
        قيم سالبة = تشديد الشروط."""
        global RSI_FLEX_BUY, RSI_FLEX_SELL, BB_STD
        try:
            rsi_points = int(rsi_points)
            bb_delta   = float(bb_delta)
        except (TypeError, ValueError):
            return "❌ قيم غير صالحة. مرّر rsi_points عدداً صحيحاً وbb_delta عدداً عشرياً."

        new_flex_buy  = max(5,  min(49, RSI_FLEX_BUY  - rsi_points))
        new_flex_sell = max(51, min(95, RSI_FLEX_SELL  + rsi_points))
        new_bb        = max(0.5, min(4.0, round(BB_STD - bb_delta, 2)))

        if (new_flex_buy == RSI_FLEX_BUY and new_flex_sell == RSI_FLEX_SELL
                and new_bb == BB_STD):
            return ("لا تغيير: القيم وصلت حدودها الآمنة أو المُدخلات صفرية. "
                    f"الحالية: RSI_FLEX {RSI_FLEX_BUY}/{RSI_FLEX_SELL} — BB_STD {BB_STD}.")

        edits = [
            (f"RSI_FLEX_BUY      = {RSI_FLEX_BUY}", f"RSI_FLEX_BUY      = {new_flex_buy}"),
            (f"RSI_FLEX_SELL     = {RSI_FLEX_SELL}", f"RSI_FLEX_SELL     = {new_flex_sell}"),
            (f"BB_STD         = {BB_STD}", f"BB_STD         = {new_bb}"),
        ]
        applied = []
        try:
            for old, new in edits:
                if old == new:
                    continue
                res = agent_tools.tool_edit_file("bot.py", old, new)
                if not res.startswith("✅"):
                    for a_old, a_new in reversed(applied):
                        agent_tools.tool_edit_file("bot.py", a_new, a_old)
                    return (f"❌ تعذّر تعديل «{old}»: {res}\n"
                            "تم التراجع عن أي تعديلات جزئية — لم يتغيّر شيء.")
                applied.append((old, new))
        except Exception as e:
            logger.error(f"relax_conditions: {e}")
            for a_old, a_new in reversed(applied):
                try:
                    agent_tools.tool_edit_file("bot.py", a_new, a_old)
                except Exception:
                    pass
            return f"❌ تعذّر تطبيق التخفيف: {e}\nتم التراجع عن أي تعديلات جزئية."

        old_buy, old_sell, old_bb = RSI_FLEX_BUY, RSI_FLEX_SELL, BB_STD
        RSI_FLEX_BUY, RSI_FLEX_SELL, BB_STD = new_flex_buy, new_flex_sell, new_bb
        return (
            "✅ تم تخفيف الشروط وتطبيقها فوراً دون إعادة تشغيل:\n"
            f"• RSI المرن (شرائي/بيعي): {old_buy}/{old_sell} ← {new_flex_buy}/{new_flex_sell}\n"
            f"• انحراف بولينجر BB_STD: {old_bb} ← {new_bb}\n"
            "القيم الجديدة فعّالة من الدورة القادمة مباشرة (إشارات أكثر تكراراً). "
            "⚠️ تخفيف الشروط يزيد عدد الإشارات لكنه قد يقلّل دقتها — راقب النتائج."
        )

    async def handle_ai_message(self, text: str, user_id: str = "default") -> str:
        """يعالج رسالة نصية حرة من Telegram عبر الوكيل الذكي (قراءة/تعديل كود + تحليل).
        يحفظ تاريخ المحادثة لكل مستخدم ويمرّر آخر CHAT_HISTORY_MAX رسالة كسياق."""
        if not self.ai.enabled:
            return ("🧠 ميزة الذكاء الاصطناعي غير مفعّلة. "
                    "أضف مفتاح OpenAI صالحاً مع رصيد لتشغيل التحليل الذكي.")
        loop       = asyncio.get_event_loop()
        tools_spec = (agent_tools.FILE_TOOLS_SPEC
                      + [ANALYZE_ASSET_SPEC, VERIFY_ASSET_SPEC,
                         RESTART_BOT_SPEC, RELAX_CONDITIONS_SPEC])
        # لقطات تُؤخذ على خيط حلقة الأحداث (آمنة) قبل تسليم العمل للخيط الجانبي
        context  = self.ai_context()
        candles  = dict(self.last_candles)
        analysis = dict(self.last_analysis)
        verify   = dict(self.last_verify)
        assets   = list(self.otc_assets)
        # تاريخ المحادثة لهذا المستخدم (لقطة لتمريرها بأمان للخيط الجانبي)
        history  = list(self.chat_history.get(user_id, []))

        def dispatch(n, a):
            return self._agent_dispatch(n, a or {}, candles, analysis, assets, verify)

        reply = await loop.run_in_executor(
            None,
            lambda: self.ai.agent_chat(
                text, context,
                dispatch=dispatch,
                tools_spec=tools_spec,
                history=history,
            ),
        )
        # حفظ الرسالة والرد في ذاكرة المحادثة (قصّ لآخر CHAT_HISTORY_MAX رسالة)
        hist = self.chat_history.setdefault(user_id, [])
        hist.append({"role": "user", "content": text})
        hist.append({"role": "assistant", "content": reply})
        if len(hist) > CHAT_HISTORY_MAX:
            del hist[:-CHAT_HISTORY_MAX]
        self._save_chat_history()  # حفظ فوري حتى لا تُفقد الذاكرة عند إعادة التشغيل
        return reply

    # ── الحلقة الرئيسية ───────────────────────────────────────

    async def run(self):
        global WEB_TOKEN_QUEUE
        WEB_TOKEN_QUEUE = asyncio.Queue()

        self.loop = asyncio.get_running_loop()  # يُستخدم لجلب البيانات عند الطلب من خيط الوكيل
        logger.info("🤖 البوت v8 Smart AI يبدأ...")

        web_srv = WebServer(self)
        await web_srv.start()

        asyncio.create_task(self.telegram.start())

        ai_line = ("🧠 المحلل الذكي: مفعّل — راسله بأي رسالة"
                   if self.ai.enabled else
                   "🧠 المحلل الذكي: غير مفعّل (يحتاج مفتاح/رصيد OpenAI)")
        send_telegram(
            "🚀 <b>بوت OTC v8 Smart AI — نشط</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "🔌 الاتصال: WebSocket مباشر\n"
            "📊 الاستراتيجية: ارتداد 30م + خطوط الاتجاه\n"
            f"⏱ الإطار: 1 دقيقة (مستويات 30م)\n"
            f"{ai_line}\n"
            "🌐 تحديث التوكن: معاينة Replit أو /token\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "⏳ جاري الاتصال..."
        )

        self.connected = await self.connect()
        # تهيئة فورية لقائمة الأصول والعوائد فور الاتصال حتى لا تكون فارغة عند سؤال المحلل مبكراً
        if self.connected:
            try:
                if not self.otc_assets:
                    await self._load_assets()
                self._refresh_qualified()
            except Exception as e:
                logger.warning(f"تهيئة أولية للأصول: {e}")
            if os.getenv("CANDLE_DIAG") == "1":
                await self._candle_diag()
            # استئناف متابعة الصفقات التي كانت جارية لحظة آخر إطفاء (إن وُجدت)
            self._resume_tracks()
        errors         = 0

        while True:
            # ── توكن جديد من الويب أو Telegram ──────────────
            new_token = None
            try:
                new_token = WEB_TOKEN_QUEUE.get_nowait()
            except asyncio.QueueEmpty:
                pass
            if not new_token and self.telegram.new_token:
                new_token = self.telegram.new_token
                self.telegram.new_token = None

            if new_token:
                await self.apply_new_token(new_token)
                errors = 0
                continue

            # ── إذا لم يتم الاتصال، انتظر توكن أو أعِد المحاولة دورياً ──
            # بدون هذا المنطق يبقى البوت نائماً للأبد إذا فشل Chromium مرة واحدة.
            if not self.connected:
                self._no_conn_waits = getattr(self, "_no_conn_waits", 0) + 1
                if self._no_conn_waits >= 12:   # ~120 ثانية بلا اتصال → أعِد المحاولة
                    self._no_conn_waits = 0
                    logger.info("🔄 120ث بلا اتصال — إعادة محاولة تلقائية (تجديد التوكن إن لزم)...")
                    self.connected = await self.connect()
                else:
                    await asyncio.sleep(10)
                continue

            # ── حلقة الفحص ───────────────────────────────────
            try:
                if not self.otc_assets:
                    await self._load_assets()
                    await asyncio.sleep(CHECK_INTERVAL)
                    continue

                # ── تجديد استباقي كل 7 ساعات (قبل انتهاء التوكن تلقائياً) ──
                if time.time() - self._connected_at > 7 * 3600:
                    await self._proactive_token_renewal()

                self.last_scan_t = time.time()
                # فلترة العوائد دورياً قبل كل دورة فحص (استبعاد ما دون 80%)
                self._refresh_qualified()
                scan_list = self.qualified_assets or self.otc_assets
                random.shuffle(scan_list)
                self._diag_candles = self._diag_candle_len = self._diag_analyzed = 0
                self._diag_fresh = 0
                for idx, asset in enumerate(scan_list):
                    if WEB_TOKEN_QUEUE.qsize() > 0 or self.telegram.new_token:
                        break
                    await self.check_asset(asset)
                    # قطع الدورة مبكراً إذا كان الاتصال "حيّاً ظاهرياً" لكنه لا يُسلّم بيانات حديثة:
                    # بعد محاولة عدد كافٍ من الأصول دون أي شمعة حديثة، لا فائدة من إكمال الدورة كاملة.
                    # نعتمد على الحداثة لا على المخزون: عند الصمت يعيد الجلب شموعاً قديمة مخزّنة فيبقى
                    # العدّاد القديم موجباً ويُخفي الصمت — أما الحداثة فتنهار حين يتوقّف تدفّق البيانات.
                    if idx + 1 >= 8 and self._diag_fresh == 0:
                        logger.warning("⚠️ 8 أصول دون بيانات حديثة — قطع الدورة مبكراً (الاتصال صامت)")
                        break
                    await asyncio.sleep(random.uniform(0.3, 0.8))

                _avg_len = (self._diag_candle_len // self._diag_candles) if self._diag_candles else 0
                logger.info(
                    f"📊 تشخيص الدورة: شموع={self._diag_candles}/{len(scan_list)} "
                    f"(متوسط {_avg_len} شمعة) | إشارات مرشّحة={self._diag_analyzed} | حديثة={self._diag_fresh}")
                self._save_candles()   # حفظ مخزّن الشموع للإقلاع الفوري بعد أي إعادة تشغيل
                errors = 0

                # ── تعافٍ ذاتي: اتصال صامت (متصل لكن بلا بيانات حديثة) ─────────
                # مراقب pyquotex الداخلي يعيد تدوير السوكِت عند الخمول لكنه لا يمرّ
                # عبر connect()/تجديد التوكن، فقد يبقى البوت "متصلاً" بلا بيانات لساعات.
                # المعيار هو الحداثة لا المخزون: عند الصمت يعيد الجلب شموعاً مخزّنة قديمة،
                # لذا _diag_candles يبقى موجباً زوراً — أما _diag_fresh فينهار إلى صفر.
                # بعد عدة دورات بلا أي بيانات حديثة نفرض إعادة اتصال كاملة (تُجدّد التوكن عند الرفض).
                if self._diag_fresh == 0:
                    self._empty_cycles += 1
                    if self._empty_cycles >= 3:
                        logger.warning(
                            f"⚠️ {self._empty_cycles} دورات متتالية بلا بيانات حديثة — "
                            "إعادة اتصال كاملة + فحص/تجديد التوكن")
                        self._empty_cycles = 0
                        self.connected = await self.connect()
                        if not self.connected:
                            await asyncio.sleep(30)
                            continue
                else:
                    self._empty_cycles = 0

                await asyncio.sleep(CHECK_INTERVAL)

            except Exception as e:
                errors += 1
                logger.error(f"❌ ({errors}): {e}")
                if errors >= 3:
                    self.connected = await self.connect()
                    errors = 0 if self.connected else errors
                    if not self.connected:
                        await asyncio.sleep(30)
                else:
                    await asyncio.sleep(CHECK_INTERVAL)

    async def close(self):
        self.telegram.stop()
        if self.client:
            await self.client.close()


if __name__ == "__main__":
    import time as _time
    _restart_delay = 10   # ثوانٍ بين كل إعادة تشغيل
    _crash_count   = 0

    while True:
        bot  = QuotexOTCBot()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(bot.run())
            # run() انتهت بشكل طبيعي (نادر جداً) — أعد التشغيل
        except KeyboardInterrupt:
            # إيقاف يدوي من المالك — أوقف الحلقة نهائياً
            try:
                loop.run_until_complete(bot.close())
            except Exception:
                pass
            loop.close()
            break
        except Exception as e:
            _crash_count += 1
            try:
                logger.error(
                    f"💥 البوت تعطّل ({_crash_count}): {e} — "
                    f"إعادة التشغيل خلال {_restart_delay}ث..."
                )
                send_telegram(
                    f"⚠️ <b>البوت تعطّل وسيُعاد تشغيله تلقائياً</b>\n"
                    f"الخطأ: <code>{str(e)[:200]}</code>\n"
                    f"إعادة التشغيل خلال {_restart_delay}ث..."
                )
            except Exception:
                pass
        finally:
            try:
                loop.close()
            except Exception:
                pass

        _time.sleep(_restart_delay)

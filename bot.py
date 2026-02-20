"""
=============================================================
  Quotex OTC Signal Bot v2
  بوت إشارات OTC - Quotex + RSI + Telegram
=============================================================
المنطق الصحيح:
  1. كل 5 ثواني يفحص جميع أصول OTC
  2. يحسب RSI(14) على آخر شمعة مكتملة (1 دقيقة)
  3. إذا تحقق الشرط (RSI<30 أو RSI>70):
     - يرسل رسالة تنبيه مسبق مرة واحدة لكل شمعة جديدة
     - ينتظر إغلاق الشمعة القادمة
     - بعد الإغلاق يتحقق مجدداً → إذا تأكد يرسل "ادخل الآن"
=============================================================
"""

import asyncio
import time
import os
import logging
from datetime import datetime

import requests
from pyquotex.stable_api import Quotex

# =============================================
# إعدادات (تُقرأ من متغيرات البيئة)
# =============================================
QUOTEX_EMAIL     = os.getenv("QUOTEX_EMAIL",     "swrmohammed14@gmail.com")
QUOTEX_PASSWORD  = os.getenv("QUOTEX_PASSWORD",  "Apple@@123")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN",   "8590978079:AAHc3QFAkVgOhCabvz5hAC7GlSIfWgYEiG0")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "301149123")

RSI_PERIOD      = 14
RSI_OVERSOLD    = 30    # أقل من هذا → CALL
RSI_OVERBOUGHT  = 70    # أكثر من هذا → PUT
CANDLE_PERIOD   = 60    # 1 دقيقة
HISTORY_OFFSET  = 3600  # آخر ساعة
CHECK_INTERVAL  = 5     # فحص كل 5 ثواني

# =============================================
# اللوقات
# =============================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("QuotexBot")


# =============================================
# دوال مساعدة
# =============================================

def calculate_rsi(closes: list, period: int = 14):
    """حساب RSI الكلاسيكي (Wilder Smoothing)."""
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
    if al == 0:
        return 100.0
    return round(100 - (100 / (1 + ag / al)), 2)


def send_telegram(message: str) -> bool:
    """إرسال رسالة Telegram."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram غير مُعدّ")
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10
        )
        if resp.status_code == 200:
            return True
        logger.error(f"Telegram: {resp.text[:100]}")
        return False
    except Exception as e:
        logger.error(f"Telegram: {e}")
        return False


def fmt(symbol: str) -> str:
    """تنسيق اسم الأصل."""
    return symbol.replace("_otc", " (OTC)").replace("_", "/")


def current_candle_start() -> int:
    """بداية الشمعة الحالية (1 دقيقة)."""
    return int(time.time() // CANDLE_PERIOD) * CANDLE_PERIOD


# =============================================
# كلاس البوت
# =============================================

class QuotexOTCBot:
    def __init__(self):
        self.client       = None
        self.otc_assets   = []
        # {asset: {"alert_sent_at_candle": int, "confirm_sent_at_candle": int, "pending_confirm": dict}}
        self.state        = {}

    # ---------- الاتصال ----------
    async def connect(self) -> bool:
        logger.info("⏳ الاتصال بـ Quotex...")
        self.client = Quotex(
            email=QUOTEX_EMAIL,
            password=QUOTEX_PASSWORD,
            lang="en",
            root_path="."
        )
        try:
            ok, msg = await self.client.connect()
            if ok:
                logger.info(f"✅ {msg}")
                await asyncio.sleep(2)
                await self._load_assets()
                return True
            logger.error(f"❌ {msg}")
            return False
        except Exception as e:
            logger.error(f"❌ {e}")
            return False

    async def reconnect(self) -> bool:
        logger.warning("🔄 إعادة الاتصال...")
        try:
            await self.client.close()
        except:
            pass
        await asyncio.sleep(5)
        return await self.connect()

    # ---------- الأصول ----------
    async def _load_assets(self):
        all_a = self.client.get_all_asset_name()
        if all_a:
            self.otc_assets = [a[0] for a in all_a if "_otc" in a[0].lower()]
            logger.info(f"📋 {len(self.otc_assets)} أصل OTC")

    # ---------- RSI ----------
    async def get_rsi_data(self, asset: str):
        """
        يرجع (rsi, last_closed_candle_time) أو (None, None).
        يستخدم فقط الشموع المكتملة.
        """
        try:
            candles = await self.client.get_candles(
                asset, time.time(), HISTORY_OFFSET, CANDLE_PERIOD
            )
            if not candles:
                return None, None

            now    = time.time()
            closed = [c for c in candles if c.get("time", 0) + CANDLE_PERIOD <= now]
            if len(closed) < RSI_PERIOD + 1:
                return None, None

            closes = [c["close"] for c in closed if c.get("close")]
            rsi    = calculate_rsi(closes, RSI_PERIOD)
            last_t = closed[-1]["time"]
            return rsi, last_t
        except Exception as e:
            logger.debug(f"{asset}: {e}")
            return None, None

    # ---------- الإشارات ----------
    def _alert_msg(self, asset, direction, rsi, candle_t):
        icon    = "📈" if direction == "CALL" else "📉"
        zone    = "ذروة البيع 🟢" if direction == "CALL" else "ذروة الشراء 🔴"
        cur_c   = datetime.fromtimestamp(candle_t).strftime("%H:%M")
        nxt_c   = datetime.fromtimestamp(candle_t + CANDLE_PERIOD).strftime("%H:%M")
        return (
            f"⚠️ <b>تنبيه — إشارة محتملة</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{icon} <b>{fmt(asset)}</b>\n"
            f"📊 RSI: <b>{rsi}</b> — {zone}\n"
            f"🕐 الشمعة الحالية: {cur_c}\n"
            f"⏳ انتظر إغلاق الشمعة ({nxt_c})\n"
            f"👀 <i>سيتم التأكيد بعد الإغلاق...</i>"
        )

    def _confirm_msg(self, asset, direction, rsi, alert_candle_t):
        icon    = "📈" if direction == "CALL" else "📉"
        color   = "🟢" if direction == "CALL" else "🔴"
        dir_ar  = "CALL — صعود ▲" if direction == "CALL" else "PUT — نزول ▼"
        entry_c = datetime.fromtimestamp(alert_candle_t + CANDLE_PERIOD).strftime("%H:%M")
        return (
            f"{color} <b>تأكيد — ادخل الآن!</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{icon} <b>{fmt(asset)}</b>\n"
            f"🎯 الاتجاه: <b>{dir_ar}</b>\n"
            f"📊 RSI: <b>{rsi}</b>\n"
            f"⏱ مدة الصفقة: <b>1 دقيقة</b>\n"
            f"🕐 ادخل شمعة: <b>{entry_c}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"⚡ <i>لديك 60 ثانية للدخول</i>"
        )

    def _no_confirm_msg(self, asset, direction, rsi2):
        icon = "📈" if direction == "CALL" else "📉"
        return (
            f"⚪ <b>لم يتأكد</b> — {icon} {fmt(asset)}\n"
            f"RSI بعد الإغلاق: {rsi2} (خرج من المنطقة)"
        )

    # ---------- فحص أصل ----------
    async def check_asset(self, asset: str):
        rsi, last_t = await self.get_rsi_data(asset)
        if rsi is None:
            return

        # تحديد الاتجاه
        if rsi < RSI_OVERSOLD:
            direction = "CALL"
        elif rsi > RSI_OVERBOUGHT:
            direction = "PUT"
        else:
            # لا إشارة - امسح أي حالة انتظار
            if asset in self.state:
                self.state[asset]["pending_confirm"] = None
            return

        # تهيئة الحالة
        if asset not in self.state:
            self.state[asset] = {
                "alert_sent_at_candle":   None,
                "confirm_sent_at_candle": None,
                "pending_confirm":        None
            }
        s = self.state[asset]

        now = time.time()

        # ===== مرحلة التنبيه =====
        # أرسل التنبيه مرة واحدة لكل شمعة جديدة
        if s["alert_sent_at_candle"] != last_t:
            s["alert_sent_at_candle"]   = last_t
            s["confirm_sent_at_candle"] = None
            # احفظ بيانات الانتظار للتأكيد لاحقاً
            s["pending_confirm"] = {
                "direction":   direction,
                "rsi":         rsi,
                "candle_t":    last_t,
                "confirm_after": last_t + CANDLE_PERIOD  # بعد إغلاق هذه الشمعة
            }
            send_telegram(self._alert_msg(asset, direction, rsi, last_t))
            logger.info(f"⚠️ تنبيه: {asset} {direction} RSI={rsi}")

        # ===== مرحلة التأكيد =====
        pending = s.get("pending_confirm")
        if pending and now >= pending["confirm_after"] and s["confirm_sent_at_candle"] != pending["candle_t"]:
            # الشمعة أغلقت - تحقق من RSI مجدداً
            rsi2, _ = await self.get_rsi_data(asset)
            s["confirm_sent_at_candle"] = pending["candle_t"]
            s["pending_confirm"]        = None

            if rsi2 is not None:
                d = pending["direction"]
                if (d == "CALL" and rsi2 < RSI_OVERSOLD) or (d == "PUT" and rsi2 > RSI_OVERBOUGHT):
                    send_telegram(self._confirm_msg(asset, d, rsi2, pending["candle_t"]))
                    logger.info(f"✅ تأكيد: {asset} {d} RSI={rsi2}")
                else:
                    send_telegram(self._no_confirm_msg(asset, d, rsi2))
                    logger.info(f"⚪ لم يتأكد: {asset} RSI={rsi2}")

    # ---------- الحلقة الرئيسية ----------
    async def run(self):
        logger.info("🤖 بدء تشغيل البوت...")

        send_telegram(
            "🤖 <b>بوت إشارات OTC — تم التشغيل</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"📊 المؤشر: RSI({RSI_PERIOD})\n"
            f"⏱ الإطار: 1 دقيقة\n"
            f"🔍 الفحص: كل {CHECK_INTERVAL} ثواني\n"
            f"🎯 الشرط: RSI &lt; {RSI_OVERSOLD} أو RSI &gt; {RSI_OVERBOUGHT}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "✅ جاري مراقبة جميع أصول OTC..."
        )

        if not await self.connect():
            logger.error("فشل الاتصال!")
            return

        logger.info(f"🔍 فحص {len(self.otc_assets)} أصل OTC كل {CHECK_INTERVAL}ث")
        errors = 0

        while True:
            try:
                if not self.otc_assets:
                    await self._load_assets()
                    await asyncio.sleep(CHECK_INTERVAL)
                    continue

                for asset in self.otc_assets:
                    await self.check_asset(asset)
                    await asyncio.sleep(0.15)

                errors = 0
                await asyncio.sleep(CHECK_INTERVAL)

            except Exception as e:
                errors += 1
                logger.error(f"❌ خطأ: {e}")
                if errors >= 3:
                    if await self.reconnect():
                        errors = 0
                    else:
                        await asyncio.sleep(30)
                else:
                    await asyncio.sleep(CHECK_INTERVAL)

    async def close(self):
        if self.client:
            await self.client.close()


# =============================================
# نقطة الدخول
# =============================================
if __name__ == "__main__":
    bot  = QuotexOTCBot()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(bot.run())
    except KeyboardInterrupt:
        loop.run_until_complete(bot.close())
    finally:
        loop.close()

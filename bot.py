"""
=============================================================
  Quotex OTC Signal Bot v4
  يستخدم PyQuotex مع session token محفوظ مسبقاً
  بدون الحاجة لـ Playwright في كل تشغيل
=============================================================
"""

import asyncio
import time
import os
import json
import logging
from datetime import datetime

import requests
from pyquotex.stable_api import Quotex

# =============================================
# إعدادات
# =============================================
QUOTEX_EMAIL     = os.getenv("QUOTEX_EMAIL",     "swrmohammed14@gmail.com")
QUOTEX_PASSWORD  = os.getenv("QUOTEX_PASSWORD",  "Apple@@123")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN",   "8590978079:AAHc3QFAkVgOhCabvz5hAC7GlSIfWgYEiG0")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "301149123")
QUOTEX_TOKEN     = os.getenv("QUOTEX_TOKEN",     "QWnIfCqAtl1465HQsGldW2FZqUMBU8yaq5EA9kCi")

RSI_PERIOD     = 14
RSI_OVERSOLD   = 30
RSI_OVERBOUGHT = 70
CANDLE_PERIOD  = 60
HISTORY_OFFSET = 7200
CHECK_INTERVAL = 5

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
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10
        )
        return resp.status_code == 200
    except Exception as e:
        logger.error(f"Telegram: {e}")
        return False


def fmt(symbol: str) -> str:
    return symbol.replace("_otc", " (OTC)").replace("_", "/")


# =============================================
# إعداد session.json مسبقاً
# =============================================

def setup_session():
    """إنشاء session.json من متغيرات البيئة"""
    session_data = {
        "token": QUOTEX_TOKEN,
        "cookies": f"lang=en",
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    
    # PyQuotex يبحث عن session.json في مجلد العمل
    session_path = os.path.join(os.getcwd(), "session.json")
    with open(session_path, "w") as f:
        json.dump(session_data, f)
    logger.info(f"✅ Session file created: {session_path}")


# =============================================
# البوت الرئيسي
# =============================================

class QuotexOTCBot:
    def __init__(self):
        self.client    = None
        self.otc_assets = []
        self.state     = {}

    async def connect(self) -> bool:
        setup_session()
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
            logger.error(f"❌ Connect error: {e}")
            return False

    async def reconnect(self) -> bool:
        logger.warning("🔄 إعادة الاتصال...")
        try:
            await self.client.close()
        except:
            pass
        await asyncio.sleep(10)
        return await self.connect()

    async def _load_assets(self):
        all_a = self.client.get_all_asset_name()
        if all_a:
            self.otc_assets = [a[0] for a in all_a if "_otc" in a[0].lower()]
            logger.info(f"📋 {len(self.otc_assets)} أصل OTC")

    def get_rsi(self, candles: list):
        now    = time.time()
        closed = [c for c in candles if isinstance(c, dict) and c.get("time", 0) + CANDLE_PERIOD <= now]
        if len(closed) < RSI_PERIOD + 1:
            return None, None
        closes = [float(c["close"]) for c in closed if c.get("close")]
        rsi    = calculate_rsi(closes, RSI_PERIOD)
        last_t = closed[-1]["time"]
        return rsi, last_t

    def alert_msg(self, asset, direction, rsi, candle_t):
        icon  = "📈" if direction == "CALL" else "📉"
        zone  = "ذروة البيع 🟢" if direction == "CALL" else "ذروة الشراء 🔴"
        cur_c = datetime.fromtimestamp(candle_t).strftime("%H:%M")
        nxt_c = datetime.fromtimestamp(candle_t + CANDLE_PERIOD).strftime("%H:%M")
        return (
            f"⚠️ <b>تنبيه — إشارة محتملة</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{icon} <b>{fmt(asset)}</b>\n"
            f"📊 RSI: <b>{rsi}</b> — {zone}\n"
            f"🕐 الشمعة الحالية: {cur_c}\n"
            f"⏳ انتظر إغلاق الشمعة ({nxt_c})\n"
            f"👀 <i>سيتم التأكيد بعد الإغلاق...</i>"
        )

    def confirm_msg(self, asset, direction, rsi, alert_candle_t):
        icon   = "📈" if direction == "CALL" else "📉"
        color  = "🟢" if direction == "CALL" else "🔴"
        dir_ar = "CALL — صعود ▲" if direction == "CALL" else "PUT — نزول ▼"
        entry  = datetime.fromtimestamp(alert_candle_t + CANDLE_PERIOD).strftime("%H:%M")
        return (
            f"{color} <b>تأكيد — ادخل الآن!</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{icon} <b>{fmt(asset)}</b>\n"
            f"🎯 الاتجاه: <b>{dir_ar}</b>\n"
            f"📊 RSI: <b>{rsi}</b>\n"
            f"⏱ مدة الصفقة: <b>1 دقيقة</b>\n"
            f"🕐 ادخل شمعة: <b>{entry}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"⚡ <i>لديك 60 ثانية للدخول</i>"
        )

    async def check_asset(self, asset: str):
        try:
            candles = await self.client.get_candles(asset, time.time(), HISTORY_OFFSET, CANDLE_PERIOD)
            if not candles:
                return

            rsi, last_t = self.get_rsi(candles)
            if rsi is None:
                return

            if rsi < RSI_OVERSOLD:
                direction = "CALL"
            elif rsi > RSI_OVERBOUGHT:
                direction = "PUT"
            else:
                if asset in self.state:
                    self.state[asset]["pending_confirm"] = None
                return

            if asset not in self.state:
                self.state[asset] = {
                    "alert_sent_at_candle":   None,
                    "confirm_sent_at_candle": None,
                    "pending_confirm":        None
                }
            s   = self.state[asset]
            now = time.time()

            # === تنبيه مسبق ===
            if s["alert_sent_at_candle"] != last_t:
                s["alert_sent_at_candle"]   = last_t
                s["confirm_sent_at_candle"] = None
                s["pending_confirm"] = {
                    "direction":     direction,
                    "rsi":           rsi,
                    "candle_t":      last_t,
                    "confirm_after": last_t + CANDLE_PERIOD
                }
                send_telegram(self.alert_msg(asset, direction, rsi, last_t))
                logger.info(f"⚠️ تنبيه: {asset} {direction} RSI={rsi}")

            # === تأكيد بعد إغلاق الشمعة ===
            pending = s.get("pending_confirm")
            if pending and now >= pending["confirm_after"] and s["confirm_sent_at_candle"] != pending["candle_t"]:
                candles2 = await self.client.get_candles(asset, time.time(), HISTORY_OFFSET, CANDLE_PERIOD)
                rsi2, _  = self.get_rsi(candles2) if candles2 else (None, None)
                s["confirm_sent_at_candle"] = pending["candle_t"]
                s["pending_confirm"]        = None

                if rsi2 is not None:
                    d = pending["direction"]
                    if (d == "CALL" and rsi2 < RSI_OVERSOLD) or (d == "PUT" and rsi2 > RSI_OVERBOUGHT):
                        send_telegram(self.confirm_msg(asset, d, rsi2, pending["candle_t"]))
                        logger.info(f"✅ تأكيد: {asset} {d} RSI={rsi2}")
                    else:
                        logger.info(f"⚪ لم يتأكد: {asset} RSI={rsi2}")
        except Exception as e:
            logger.debug(f"{asset}: {e}")

    async def run(self):
        logger.info("🤖 بدء تشغيل البوت v4...")

        send_telegram(
            "🤖 <b>بوت إشارات OTC — تم التشغيل</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"📊 المؤشر: RSI({RSI_PERIOD})\n"
            f"⏱ الإطار: 1 دقيقة\n"
            f"🔍 الفحص: كل {CHECK_INTERVAL} ثواني\n"
            f"🎯 الشرط: RSI &lt; {RSI_OVERSOLD} أو RSI &gt; {RSI_OVERBOUGHT}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "✅ جاري المراقبة..."
        )

        if not await self.connect():
            logger.error("فشل الاتصال!")
            send_telegram("❌ فشل الاتصال بـ Quotex - يرجى تحديث QUOTEX_TOKEN")
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
                    await asyncio.sleep(0.2)

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

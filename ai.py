"""
=============================================================
  وحدة الذكاء الاصطناعي (v8 Smart AI)
  - مساعد تحليل صادق يقرأ الشارت الحقيقي (شموع OHLC فعلية)
  - يحلّل الاتجاه + الدعم/المقاومة + الزخم
  - يتكلم العربية دائماً ويشرح المخاطر بصدق
=============================================================
"""

import os
import json
import logging

logger = logging.getLogger("QuotexBot")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
AI_MODEL       = os.getenv("AI_MODEL", "gpt-4o-mini")

SYSTEM_PROMPT = (
    "أنت «المحلل الذكي» داخل بوت تداول خيارات ثنائية على أصول OTC في منصة Quotex. "
    "مهمتك تحليل الشارت الحقيقي (شموع OHLC فعلية: الافتتاح، الأعلى، الأدنى، الإغلاق، الحجم) "
    "وقراءة الاتجاه ومستويات الدعم والمقاومة والزخم بصدق ووضوح.\n\n"
    "قواعد صارمة:\n"
    "1) تكلّم العربية دائماً، بأسلوب واضح ومباشر ومختصر.\n"
    "2) أنت مساعد تحليل صادق ولست عرّافاً يضمن الربح. اشرح الاحتمالات والمخاطر دائماً، "
    "ولا تَعِد أبداً بربح مؤكد. ذكّر بأن السوق يحتمل الخطأ وأن إدارة رأس المال مسؤولية المتداول.\n"
    "3) اعتمد فقط على البيانات المعطاة لك في الرسالة (الشموع والمؤشرات). "
    "إذا لم تتوفر بيانات كافية، قُل ذلك بصراحة ولا تخترع أرقاماً.\n"
    "4) عند تحليل أصل: لخّص الاتجاه العام، أقرب دعم ومقاومة، حالة الزخم (RSI/Bollinger)، "
    "ثم رأيك المحتمل (صعود/نزول/تذبذب) مع نسبة ثقة تقريبية وسبب موجز، وتنبيه المخاطرة.\n"
    "5) لا تستخدم وسوم HTML معقدة؛ نص عربي بسيط مع رموز تعبيرية خفيفة فقط.\n"
    "6) سياق المالك: يتداول بإستراتيجية «المضاعفات» (Martingale) — يضاعف حجم الصفقة بعد الخسارة. "
    "راعِ هذا في كل تحليل: أخطر شيء على المضاعفات هو اتجاه صاعد أو هابط قوي مستمر (Strong Trend) "
    "يُبقي مؤشرات الانعكاس (RSI/Bollinger) في التشبّع فتتوالى الخسائر. عند رصد اتجاه قوي مستمر، "
    "نبّه المستخدم بوضوح: يُفضّل دخول المضاعفة مع الاتجاه لا عكسه، أو تأجيلها للشمعة التالية. "
    "وذكّره دائماً أن المضاعفة ترفع المخاطرة بشكل كبير ولا تضمن تعويض الخسارة.\n"
    "7) نظام الإشارة: البوت يرسل «رسالة واحدة استباقية» فقط لكل صفقة (لا رسالتين)، تصل قبل "
    "افتتاح شمعة الدخول بنحو دقيقة. المالك يدخل الصفقة مع «افتتاح الشمعة القادمة» مباشرة، "
    "لذا الرسالة تقول مثلاً «إشارة شراء محتملة مع افتتاح الشمعة القادمة». "
    "وإذا خسرت الصفقة السابقة، يُدمج تنبيه المضاعفة داخل نفس هذه الرسالة الاستباقية "
    "اعتماداً على مؤشري EMA وزخم الشموع. كما يُدمج تحليلك الموجز (الذي تصوغه أنت من البيانات الفنية "
    "RSI/بولينجر/EMA/الاتجاه/الزخم) ونسبة القوة داخل نفس الرسالة الواحدة — لا تُرسَل الإشارة الفنية "
    "منفصلة عن تحليلك أبداً. راعِ هذا النظام في كل شرح أو نصيحة."
)

# موجِّه التحليل الفوري المدمج داخل الإشارة (رد JSON موجز وسريع)
SIGNAL_ADVISOR_PROMPT = (
    "أنت محلل تداول صادق داخل بوت خيارات ثنائية على أصول OTC بإطار دقيقة واحدة. "
    "تصلك بياناتٌ فنية لإشارة (RSI، بولينجر، EMA، الاتجاه العام وقوته، الزخم، العائد). "
    "حلّلها بإيجاز شديد بالعربية (جملتان إلى ثلاث) مع ذكر أهم سبب يدعم الاتجاه المقترح وأهم خطر عليه، "
    "وراعِ أن المالك يتداول بالمضاعفات (أخطر شيء عليها الاتجاه القوي المستمر). "
    "لا تَعِد بربح مؤكد أبداً وذكّر ضمنياً بأن السوق يحتمل الخطأ. "
    "أعد ردك حصراً بصيغة JSON بهذا الشكل دون أي نص خارجها: "
    '{"analysis":"<تحليلك الموجز بالعربية>","confidence":<عدد صحيح بين 35 و97>}'
)

AGENT_SYSTEM_PROMPT = (
    SYSTEM_PROMPT
    + "\n\n=== صلاحيات الوكيل ===\n"
    "أنت أيضاً وكيل برمجي يدير كود هذا البوت نفسه. لديك أدوات فعلية:\n"
    "- list_files: لسرد ملفات المشروع.\n"
    "- read_file: لقراءة محتوى أي ملف.\n"
    "- edit_file: لاستبدال نص محدد داخل ملف.\n"
    "- write_file: لإنشاء ملف جديد أو إعادة كتابته كاملاً.\n"
    "- read_logs: لقراءة آخر سطور سجل البوت (bot.log) لتشخيص الأخطاء (lines، only_errors).\n"
    "- analyze_asset: لجلب بيانات شموع OHLC الحقيقية لأصل OTC وتحليلها.\n"
    "- verify_asset: لعرض نسبة العائد (payout) وحداثة البيانات (التأخير) لأصل، ومدى صلاحيته للتأكيد.\n"
    "- restart_bot: لا تستخدمه — إعادة التشغيل الذاتية توقف البوت على المنصّة. تعديلات الشروط تُطبَّق حيّاً بلا إعادة تشغيل.\n"
    "- relax_conditions: لتخفيف (أو تشديد) شروط الإشارة برمجياً (توسيع نطاق RSI_FLEX ومتغيرات بولينجر الاحتياطية) لزيادة عدد الإشارات. "
    "استخدمه عندما يطلب المستخدم «خفّف الشروط» أو «أريد إشارات أكثر»؛ يُطبَّق التعديل حيّاً فوراً بلا إعادة تشغيل. "
    "نبّه المستخدم دائماً أن التخفيف يزيد عدد الإشارات لكنه قد يقلّل دقتها.\n\n"
    "قواعد العمل كوكيل:\n"
    "أ) عندما يسألك المستخدم عن الكود أو حالته أو كيف يعمل شيء، اقرأ الملفات فعلياً "
    "عبر read_file قبل الإجابة — لا تخمّن ولا تخترع محتوى لم تقرأه.\n"
    "ب) عند طلب تعديل: اقرأ الملف أولاً، ثم استخدم edit_file بنص قديم فريد ومطابق تماماً. "
    "اشرح للمستخدم باختصار ماذا غيّرت ولماذا. الثوابت الرئيسية في أعلى bot.py تشمل: "
    "ATR_PERIOD, ATR_SR_FACTOR, MIN_TREND_TOUCHES, SWING_WINDOW, RSI_FLEX_BUY, RSI_FLEX_SELL, "
    "SCORE_30M_BASE, SCORE_TOUCHES, SCORE_RSI_BONUS, MOMENTUM_CANDLES, MOMENTUM_MULT.\n"
    "ج) الكود يُفحص آلياً قبل الحفظ وتُؤخذ نسخة احتياطية؛ إن رُفض التعديل لخطأ صياغي صحّحه وأعد المحاولة.\n"
    "د) تعديلات شروط الإشارة عبر relax_conditions تُطبَّق حيّاً فوراً بلا إعادة تشغيل. "
    "لا تستخدم restart_bot ولا تَعِد بإعادة تشغيل تلقائية؛ إعادة تشغيل العملية يتولّاها المالك عند الحاجة.\n"
    "هـ) عند سؤال المستخدم عن أعطال أو أخطاء، استخدم read_logs (مثلاً only_errors=true) ولخّص له السبب بوضوح.\n"
    "و) يمكنك قراءة session.json (تظهر القيم الحسّاسة محجوبة تلقائياً) لكنه للقراءة فقط — لا يمكن تعديله. "
    "لا تعدّل ملفات الأسرار/المفاتيح (محمية تلقائياً). إن طُلب منك شيء خطير، نبّه المستخدم بإيجاز.\n"
    "ز) للتحقق قبل أي إشارة: العائد يجب أن يكون ≥ 80% والبيانات حديثة (بلا تأخير)؛ استخدم verify_asset عند الحاجة. "
    "لا تَعِد أبداً بربح مضمون.\n"
    "ح) كن دقيقاً وحذراً — هذا كود إنتاجي حقيقي يعمل الآن.\n"
    "ط) **صلاحيات البيانات (مهم):** أنت تمتلك الصلاحية الكاملة للاطلاع على بيانات كوتيكس "
    "الممرَّرة إليك من السيرفر وعلى حالة التوكن، ومسموح لك تصفية الأصول وقراءة نسب الأرباح (Payout) "
    "ومناقشتها بحرية. نسب الأرباح وقوائم الأصول وحالة التوكن **ليست معلومات سرية** بالنسبة لك — "
    "لا ترفض أبداً قراءتها أو تعتذر بحجة الخصوصية. القيمة الخام للتوكن وحدها محجوبة لحماية حساب المستخدم، "
    "لكنك تعرف أنه موجود وفعّال ويمكنك الإشارة إلى ذلك.\n"
    "ي) في «حالة البوت الحالية» تجد قائمة مباشرة بالأصول المؤهلة (عائد ≥ 80%) ونِسَبها — "
    "اعتمد عليها مباشرةً عند سؤال المستخدم عن أفضل الأصول أو نسب الأرباح، دون الحاجة لاستدعاء أدوات "
    "كثيرة أصلاً. استخدم verify_asset فقط لأصل محدد يطلبه المستخدم، لا لكل الأصول.\n"
    "ك) **ذاكرة المحادثة:** تُمرَّر إليك آخر رسائل المحادثة مع هذا المستخدم. اعتمد عليها للحفاظ على "
    "السياق وعدم نسيان ما اتفقتما عليه سابقاً؛ لا تكرّر أسئلة سبقت إجابتها ولا تتجاهل قرارات سابقة.\n"
    "ل) **تفسير السجلات — مهم جداً:** الرسائل التالية في السجل عادية 100% وليست أعطالاً:\n"
    "   • «Batch fetch timeout»: طبيعي تماماً — بعض الشموع تستغرق وقتاً، البوت يتجاوزها تلقائياً.\n"
    "   • «Websocket connection closed / keepalive ping timeout»: طبيعي — البوت يُعيد الاتصال خلال ثوانٍ.\n"
    "   • «مؤجلة — بيانات متأخرة»: طبيعي — الأصل يُتخطّى لهذه الدورة فقط.\n"
    "   • «DEBUG» بأي نص: رسائل تشخيصية داخلية، لا تذكرها للمستخدم.\n"
    "   الأعطال الحقيقية فقط هي: انتهاء التوكن بشكل متكرر دون تجديد، أو فشل الاتصال لأكثر من 5 دقائق.\n"
    "   عندما يسألك المستخدم «كيف حال البوت؟» أو «جيك الحالة»: اعتمد على بيانات «حالة البوت الحالية» "
    "   المُمرَّرة إليك مباشرةً (متصل/منفصل، عدد الإشارات، الأصول)، ولا تستدعي read_logs إلا عند طلب "
    "   صريح لتشخيص خطأ محدد. أجب بإيجاز وإيجابية: «البوت يعمل بشكل طبيعي ✅» إذا الحالة = متصل.\n"
    "م) **لا تعتذر أبداً عن الاتصال بالذكاء الاصطناعي:** أنت تعمل الآن وتستجيب، فلا معنى للاعتذار "
    "   عن «مشكلة في الاتصال بالذكاء الاصطناعي». إذا كان هناك عطل فعلي فصِفه بدقة، وإلا أجب مباشرة."
)


class AIAdvisor:
    def __init__(self):
        self.enabled = False
        self.client  = None
        self.last_error = ""
        if not OPENAI_API_KEY:
            logger.warning("⚠️ AI معطّل: لا يوجد OPENAI_API_KEY")
            return
        try:
            from openai import OpenAI
            self.client  = OpenAI(api_key=OPENAI_API_KEY)
            self.enabled = True
            logger.info(f"🧠 AI مفعّل (model={AI_MODEL})")
        except Exception as e:
            self.last_error = str(e)
            logger.error(f"❌ تعذّر تهيئة AI: {e}")

    # ── نداء أساسي ───────────────────────────────────────────
    def _complete(self, messages, max_tokens=700) -> str:
        if not self.enabled:
            return "🧠 ميزة الذكاء الاصطناعي غير مفعّلة حالياً (لا يوجد مفتاح OpenAI صالح)."
        try:
            r = self.client.chat.completions.create(
                model=AI_MODEL,
                messages=messages,
                max_tokens=max_tokens,
                temperature=0.4,
            )
            return (r.choices[0].message.content or "").strip()
        except Exception as e:
            msg = str(e)
            self.last_error = msg
            logger.error(f"AI error: {msg}")
            if "insufficient_quota" in msg or "exceeded your current quota" in msg:
                return ("⚠️ حساب OpenAI لا يحتوي على رصيد كافٍ (insufficient_quota). "
                        "فعّل الفوترة أو اشحن رصيداً على حسابك لتشغيل التحليل الذكي.")
            if "invalid_api_key" in msg or "Incorrect API key" in msg:
                return "⚠️ مفتاح OpenAI غير صالح. تأكد من المفتاح في الإعدادات."
            return f"⚠️ تعذّر إكمال طلب الذكاء الاصطناعي مؤقتاً: {msg[:120]}"

    # ── وكيل ذكي بأدوات (قراءة/تعديل الكود + تحليل أصل) ───────
    def agent_chat(self, user_text: str, bot_context: str = "",
                   dispatch=None, tools_spec=None, max_rounds: int = 14,
                   history=None) -> str:
        if not self.enabled:
            return "🧠 ميزة الذكاء الاصطناعي غير مفعّلة حالياً (لا يوجد مفتاح OpenAI صالح)."
        messages = [{"role": "system", "content": AGENT_SYSTEM_PROMPT}]
        if bot_context:
            messages.append({"role": "system", "content": f"حالة البوت الحالية:\n{bot_context}"})
        # سجل المحادثة السابقة مع هذا المستخدم (يُحقن قبل الرسالة الحالية لاستمرارية الذاكرة)
        if history:
            for h in history:
                role = h.get("role")
                content = h.get("content", "")
                if role in ("user", "assistant") and content:
                    messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": user_text})
        try:
            for _ in range(max_rounds):
                resp = self.client.chat.completions.create(
                    model=AI_MODEL,
                    messages=messages,
                    tools=tools_spec or [],
                    tool_choice="auto",
                    temperature=0.3,
                    max_tokens=2600,
                )
                msg = resp.choices[0].message
                if not msg.tool_calls:
                    return (msg.content or "").strip()
                messages.append({
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [{
                        "id": tc.id, "type": "function",
                        "function": {"name": tc.function.name,
                                     "arguments": tc.function.arguments},
                    } for tc in msg.tool_calls],
                })
                for tc in msg.tool_calls:
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except Exception:
                        args = {}
                    result = dispatch(tc.function.name, args) if dispatch else "لا توجد أدوات."
                    if not isinstance(result, str):
                        result = str(result)
                    if len(result) > 60000:
                        result = result[:60000] + "\n...[اقتُطع]"
                    logger.info(f"🛠️ AI tool: {tc.function.name}")
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            return "⚠️ تجاوزت عدد الخطوات المسموح بها. بسّط الطلب أو قسّمه لخطوات."
        except Exception as e:
            msg = str(e)
            self.last_error = msg
            logger.error(f"AI agent error: {msg}")
            if "insufficient_quota" in msg or "exceeded your current quota" in msg:
                return ("⚠️ حساب OpenAI لا يحتوي على رصيد كافٍ. فعّل الفوترة أو اشحن رصيداً.")
            if "invalid_api_key" in msg or "Incorrect API key" in msg:
                return "⚠️ مفتاح OpenAI غير صالح."
            return f"⚠️ تعذّر إكمال الطلب مؤقتاً: {msg[:150]}"

    # ── محادثة عامة ──────────────────────────────────────────
    def chat(self, user_text: str, bot_context: str = "") -> str:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        if bot_context:
            messages.append({"role": "system", "content": f"حالة البوت الحالية:\n{bot_context}"})
        messages.append({"role": "user", "content": user_text})
        return self._complete(messages, max_tokens=700)

    # ── تحليل فوري مدمج داخل الإشارة (سريع، JSON) ────────────
    def advise_signal(self, context_text: str, timeout: float = 6.0):
        """يستقبل بيانات الإشارة الفنية ويعيد dict {analysis, confidence} أو None.

        مصمَّم ليكون سريعاً جداً (يُستدعى ضمن مسار الإشارة قبل الإرسال)، بمهلة
        صارمة (timeout)؛ عند أي فشل أو تأخّر يعيد None ليكمل البوت بالتحليل الافتراضي."""
        if not self.enabled:
            return None
        try:
            r = self.client.chat.completions.create(
                model=AI_MODEL,
                messages=[{"role": "system", "content": SIGNAL_ADVISOR_PROMPT},
                          {"role": "user",   "content": context_text}],
                max_tokens=240,
                temperature=0.4,
                response_format={"type": "json_object"},
                timeout=timeout,
            )
            data = json.loads((r.choices[0].message.content or "").strip())
            analysis = str(data.get("analysis", "")).strip()
            if not analysis:
                return None
            conf = data.get("confidence")
            if isinstance(conf, str):
                conf = conf.strip()
                conf = int(conf) if conf.isdigit() else None
            elif isinstance(conf, (int, float)):
                conf = int(conf)
            else:
                conf = None
            if conf is not None:
                conf = max(35, min(97, conf))
            return {"analysis": analysis, "confidence": conf}
        except Exception as e:
            logger.warning(f"advise_signal تعذّر/تأخّر: {str(e)[:120]}")
            return None

    # ── تحليل شارت حقيقي ─────────────────────────────────────
    def analyze_chart(self, asset_label: str, chart_text: str, brief: bool = False) -> str:
        instruction = (
            f"حلّل الشارت الحقيقي للأصل «{asset_label}» بناءً على البيانات التالية. "
            + ("أعطِ تحليلاً موجزاً جداً (٣-٤ أسطر) فقط." if brief
               else "أعطِ تحليلاً واضحاً ومنظّماً.")
            + "\n\n" + chart_text
        )
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": instruction},
        ]
        return self._complete(messages, max_tokens=300 if brief else 700)


# ── أدوات تجهيز بيانات الشارت ────────────────────────────────
def build_chart_text(asset_label, candles, indicators=None, n=30):
    """يبني وصفاً نصياً مدمجاً لشموع OHLC الحقيقية + مؤشرات.
    candles: قائمة dicts فيها time/open/high/low/close/volume.
    """
    closed = [c for c in candles if isinstance(c, dict) and c.get("close") is not None]
    if not closed:
        return None
    recent = closed[-n:]

    closes = [float(c["close"]) for c in recent]
    highs  = [float(c.get("high", c["close"])) for c in recent]
    lows   = [float(c.get("low",  c["close"]))  for c in recent]

    cur      = closes[-1]
    hi       = max(highs)
    lo       = min(lows)
    first    = closes[0]
    change   = cur - first
    pct      = (change / first * 100) if first else 0.0

    # اتجاه عبر متوسطين بسيطين
    def sma(arr, p):
        return sum(arr[-p:]) / min(len(arr), p) if arr else 0.0
    sma_fast = sma(closes, 5)
    sma_slow = sma(closes, 20)
    if sma_fast > sma_slow * 1.0005:
        trend = "صاعد"
    elif sma_fast < sma_slow * 0.9995:
        trend = "هابط"
    else:
        trend = "عرضي/متذبذب"

    # تتابع الشموع بنفس اللون = قياس الزخم/الاتجاه القوي المستمر (مهم للمضاعفات)
    run, _sign = 0, 0
    for c in reversed(recent):
        o  = float(c.get("open", c["close"]))
        cl = float(c["close"])
        sg = 1 if cl > o else (-1 if cl < o else 0)
        if sg == 0:
            break
        if _sign == 0:
            _sign, run = sg, 1
        elif sg == _sign:
            run += 1
        else:
            break
    momentum = ("زخم قوي مستمر ⚠️" if run >= 4 else
                "زخم معتدل" if run >= 2 else "بلا زخم واضح")

    # جدول مدمج لآخر الشموع (وقت نسبي، O/H/L/C)
    rows = []
    for c in recent[-15:]:
        o = float(c.get("open", c["close"]))
        h = float(c.get("high", c["close"]))
        l = float(c.get("low",  c["close"]))
        cl = float(c["close"])
        arrow = "▲" if cl >= o else "▼"
        rows.append(f"{arrow} O:{o:.5f} H:{h:.5f} L:{l:.5f} C:{cl:.5f}")
    table = "\n".join(rows)

    ind_txt = ""
    if indicators:
        ind_txt = (
            f"\nمؤشرات محسوبة:\n"
            f"- RSI(14): {indicators.get('rsi')}\n"
            f"- Bollinger أعلى: {indicators.get('upper_bb')} | أدنى: {indicators.get('lower_bb')}\n"
            f"- السيولة: {indicators.get('vol_status')}\n"
        )

    return (
        f"الأصل: {asset_label}\n"
        f"عدد الشموع المتاحة: {len(closed)} (إطار دقيقة واحدة)\n"
        f"السعر الحالي: {cur:.5f}\n"
        f"أعلى/أدنى ضمن آخر {len(recent)} شمعة: {hi:.5f} / {lo:.5f}\n"
        f"التغير خلال النافذة: {change:+.5f} ({pct:+.2f}%)\n"
        f"الاتجاه (SMA5 مقابل SMA20): {trend}\n"
        f"تتابع الشموع بنفس اللون: {run} ({momentum})\n"
        f"أقرب مقاومة تقريبية: {hi:.5f} | أقرب دعم تقريبي: {lo:.5f}\n"
        f"{ind_txt}\n"
        f"آخر الشموع (الأحدث في الأسفل):\n{table}"
    )

"""
=============================================================
  أدوات الوكيل الذكي (v9 Agent Tools)
  تتيح للذكاء الاصطناعي قراءة وتعديل كود المشروع بأمان عبر Telegram.
  حمايات: نطاق محصور بالمشروع، منع الملفات الحساسة (التوكن/الأسرار)،
  نسخ احتياطي قبل أي تعديل، وفحص صياغة الكود قبل الحفظ.
=============================================================
"""

import os
import ast
import json
import time
import shutil
import logging

logger = logging.getLogger("QuotexBot")

PROJECT_ROOT = os.path.realpath(os.getcwd())
BACKUP_DIR   = os.path.join(PROJECT_ROOT, ".code_backups")
LOG_FILE     = os.path.join(PROJECT_ROOT, "bot.log")

# ملفات تُقرأ بحجب الأسرار لكن لا يُسمح بالكتابة عليها (مثل مخزن التوكن)
READ_ONLY_FILES = {"session.json"}
# مجلدات ممنوعة تماماً
DENY_DIRS  = {".git", ".agents", ".local", ".code_backups", ".pythonlibs",
              "__pycache__", ".cache", ".config", ".upm"}
# مجلدات تُتخطّى في السرد فقط (يجوز قراءتها لكن لا تُعرض لتقليل الضجيج)
SKIP_LIST  = {"attached_assets", "node_modules"}

READ_LIMIT = 18000


def _is_hard_secret(base: str) -> bool:
    """ملفات لا تُقرأ ولا تُكتب أبداً (أسرار/مفاتيح خام)."""
    return (base.startswith(".env")
            or base.endswith((".pem", ".key", ".crt", ".pfx", ".p12"))
            or base in {"id_rsa", "id_ed25519"}
            or "secret" in base or "credential" in base)


def _safe_path(path: str, for_write: bool = False) -> str:
    if not path or not isinstance(path, str):
        raise ValueError("مسار غير صالح")
    full = os.path.realpath(os.path.join(PROJECT_ROOT, path.strip()))
    if full != PROJECT_ROOT and not full.startswith(PROJECT_ROOT + os.sep):
        raise ValueError("مسار خارج نطاق المشروع — مرفوض")
    rel   = os.path.relpath(full, PROJECT_ROOT)
    parts = rel.split(os.sep)
    if parts and parts[0] in DENY_DIRS:
        raise ValueError(f"الوصول إلى «{parts[0]}» محظور")
    base = os.path.basename(full).lower()
    if _is_hard_secret(base):
        raise ValueError("هذا الملف محمي (يحوي أسراراً) ولا يمكن الوصول إليه")
    if for_write and base in READ_ONLY_FILES:
        raise ValueError("هذا الملف للقراءة فقط (يحوي التوكن) ولا يمكن تعديله من هنا")
    return full


def _redact_session(text: str) -> str:
    """يحجب القيم الحسّاسة (توكن/كوكيز/جلسة) قبل عرض session.json."""
    SENSITIVE = {"token", "cookies", "cookie", "ssid", "session", "user_agent"}

    def mask(v):
        if isinstance(v, str) and len(v) > 8:
            return v[:4] + "…[محجوب]…" + v[-4:]
        if isinstance(v, str):
            return "[محجوب]"
        return v

    def walk(obj):
        if isinstance(obj, dict):
            return {k: (mask(v) if k.lower() in SENSITIVE else walk(v))
                    for k, v in obj.items()}
        if isinstance(obj, list):
            return [walk(x) for x in obj]
        return obj

    try:
        return json.dumps(walk(json.loads(text)), indent=2, ensure_ascii=False)
    except Exception:
        return "[تعذّر تحليل الملف — أُخفي المحتوى حمايةً للأسرار]"


def tool_list_files() -> str:
    out = []
    for root, dirs, files in os.walk(PROJECT_ROOT):
        dirs[:] = [d for d in dirs
                   if d not in DENY_DIRS and d not in SKIP_LIST and not d.startswith(".")]
        for f in files:
            if _is_hard_secret(f.lower()) or f.endswith((".pyc", ".bak")):
                continue
            full = os.path.join(root, f)
            rel  = os.path.relpath(full, PROJECT_ROOT)
            try:
                sz = os.path.getsize(full)
            except OSError:
                sz = 0
            out.append(f"{rel} ({sz} bytes)")
    return "ملفات المشروع:\n" + "\n".join(sorted(out)) if out else "لا توجد ملفات."


def tool_read_file(path: str) -> str:
    full = _safe_path(path)
    if not os.path.isfile(full):
        return f"الملف غير موجود: {path}"
    with open(full, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    if os.path.basename(full).lower() in READ_ONLY_FILES:
        content = _redact_session(content)
    if len(content) > READ_LIMIT:
        content = (content[:READ_LIMIT]
                   + f"\n\n... [اقتُطع — الملف أطول ({len(content)} حرف). "
                   f"لقراءة جزء محدد أخبر المستخدم بالسطور التي يريد أو ابحث بكلمة مفتاحية.]")
    return f"محتوى «{path}»:\n{content}"


def tool_read_logs(lines: int = 80, only_errors: bool = False) -> str:
    if not os.path.isfile(LOG_FILE):
        return "لا يوجد ملف سجل (bot.log) بعد."
    with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
        rows = f.readlines()
    if only_errors:
        rows = [r for r in rows
                if "ERROR" in r or "WARNING" in r or "Traceback" in r or "❌" in r or "⛔" in r]
    try:
        n = max(1, min(int(lines or 80), 500))
    except (TypeError, ValueError):
        n = 80
    out = "".join(rows[-n:]).strip()
    if len(out) > 12000:
        out = out[-12000:]
    return out or "لا توجد سجلات مطابقة."


def _backup(full: str):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts   = time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time() * 1e6) % 1000000:06d}"
    rel  = os.path.relpath(full, PROJECT_ROOT).replace(os.sep, "__")
    try:
        shutil.copy2(full, os.path.join(BACKUP_DIR, f"{rel}.{ts}.bak"))
    except Exception as e:
        logger.debug(f"backup failed: {e}")


def _validate_py(full: str, content: str):
    if full.endswith(".py"):
        ast.parse(content)  # يرمي SyntaxError عند الخطأ


def _restart_hint(full: str) -> str:
    return " أرسل /restart لتطبيق التغيير." if full.endswith(".py") else ""


def tool_write_file(path: str, content: str) -> str:
    full = _safe_path(path, for_write=True)
    if content is None:
        return "❌ لا يوجد محتوى للكتابة."
    try:
        _validate_py(full, content)
    except SyntaxError as e:
        return f"❌ رُفض الحفظ: خطأ صياغي في الكود (سطر {e.lineno}): {e.msg}. لم يُغيَّر الملف."
    existed = os.path.isfile(full)
    if existed:
        _backup(full)
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)
    verb = "تحديث" if existed else "إنشاء"
    return f"✅ تم {verb} «{path}» ({len(content)} حرف).{_restart_hint(full)}"


def tool_edit_file(path: str, old_string: str, new_string: str) -> str:
    full = _safe_path(path, for_write=True)
    if not os.path.isfile(full):
        return f"الملف غير موجود: {path}"
    if old_string is None or new_string is None:
        return "❌ يجب تحديد old_string و new_string."
    with open(full, "r", encoding="utf-8") as f:
        content = f.read()
    cnt = content.count(old_string)
    if cnt == 0:
        return "❌ النص المطلوب استبداله غير موجود. اقرأ الملف أولاً وانسخ النص بدقة."
    if cnt > 1:
        return f"❌ النص موجود {cnt} مرات — أضف سياقاً أكثر ليصبح فريداً."
    new_content = content.replace(old_string, new_string)
    try:
        _validate_py(full, new_content)
    except SyntaxError as e:
        return f"❌ رُفض التعديل: سيُحدث خطأ صياغي (سطر {e.lineno}): {e.msg}. لم يُغيَّر الملف."
    _backup(full)
    with open(full, "w", encoding="utf-8") as f:
        f.write(new_content)
    return f"✅ تم تعديل «{path}».{_restart_hint(full)}"


FILE_TOOLS_SPEC = [
    {"type": "function", "function": {
        "name": "list_files",
        "description": "يسرد كل ملفات كود المشروع مع أحجامها.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "يقرأ المحتوى الكامل لملف من المشروع. استخدمه دائماً قبل الإجابة عن أسئلة الكود أو قبل أي تعديل.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "المسار النسبي مثل bot.py أو ai.py"},
        }, "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "read_logs",
        "description": "يقرأ آخر سطور سجل البوت (bot.log) لمراجعة الأخطاء أو سلوك التشغيل. استخدمه عند سؤال المستخدم عن الأعطال أو الأخطاء.",
        "parameters": {"type": "object", "properties": {
            "lines":       {"type": "integer", "description": "عدد آخر الأسطر (افتراضي 80، أقصى 500)"},
            "only_errors": {"type": "boolean", "description": "إن كان true يعرض الأخطاء والتحذيرات فقط"},
        }},
    }},
    {"type": "function", "function": {
        "name": "edit_file",
        "description": "يستبدل نصاً محدداً (old_string) بنص جديد (new_string) داخل ملف. يجب أن يكون old_string فريداً ومطابقاً تماماً. يُفحص الكود ويُحفظ نسخة احتياطية قبل التعديل.",
        "parameters": {"type": "object", "properties": {
            "path":       {"type": "string"},
            "old_string": {"type": "string", "description": "النص الموجود حالياً بالضبط"},
            "new_string": {"type": "string", "description": "النص البديل"},
        }, "required": ["path", "old_string", "new_string"]},
    }},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "ينشئ ملفاً جديداً أو يستبدل ملفاً كاملاً بمحتوى جديد. يُفحص الكود ويُحفظ نسخة احتياطية قبل الحفظ. استخدم edit_file للتعديلات الصغيرة وwrite_file فقط للملفات الجديدة أو إعادة الكتابة الكاملة.",
        "parameters": {"type": "object", "properties": {
            "path":    {"type": "string"},
            "content": {"type": "string"},
        }, "required": ["path", "content"]},
    }},
]


def dispatch(name: str, args: dict) -> str:
    try:
        if name == "list_files":
            return tool_list_files()
        if name == "read_file":
            return tool_read_file(args.get("path", ""))
        if name == "read_logs":
            return tool_read_logs(args.get("lines", 80), args.get("only_errors", False))
        if name == "edit_file":
            return tool_edit_file(args.get("path", ""),
                                  args.get("old_string", ""),
                                  args.get("new_string", ""))
        if name == "write_file":
            return tool_write_file(args.get("path", ""), args.get("content", ""))
        return f"أداة غير معروفة: {name}"
    except Exception as e:
        return f"خطأ في تنفيذ «{name}»: {e}"

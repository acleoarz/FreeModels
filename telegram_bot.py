import os
import sys
import json
import time
import asyncio
import logging
import math
import re
import threading

try:
    import requests
except ImportError:
    print("Error: 'requests' is required. Install with: pip install requests")
    sys.exit(1)

try:
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.ext import (
        Application, CommandHandler, MessageHandler, CallbackQueryHandler,
        ContextTypes, filters, ApplicationHandlerStop
    )
    from telegram.constants import ParseMode
    from telegram.error import BadRequest, RetryAfter
except ImportError:
    print("Error: 'python-telegram-bot' is required. Install with: pip install python-telegram-bot")
    sys.exit(1)

# ---------------- Configuration ----------------
SERVICES = {
    "free": {
        "name": "FreeModels",
        "base_url": "https://freemodelsforall.hopto.org/v1",
        "api_key": os.environ.get("FREE_API_KEY", "sk-gtw-t3-AehaMAnI4g3VDCuEcAw.zk607hUkrPsNrH40bHb2JpldtsTKmME0hv4JVtXLvc0")
    },
    "hub": {
        "name": "AIHub",
        "base_url": "https://aihub.071129.xyz/v1",
        "api_key": os.environ.get("HUB_API_KEY", "sk-mLfIB1JfxFUmtIxJAH4ywyjE5GMtcE5b2PVOfKS0Ktfm10UO")
    }
}

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
DEFAULT_MODEL = "hub:gpt-4o" 
FALLBACK_MODELS = ["hub:gpt-4o", "free:claude-sonnet-5"]

# Comma-separated chat IDs allowed to use the bot. Empty = everyone allowed.
# Example: ALLOWED_CHAT_IDS=123456789,987654321
ALLOWED_CHAT_IDS = set(
    int(x) for x in os.environ.get("ALLOWED_CHAT_IDS", "").split(",") if x.strip().lstrip("-").isdigit()
)

RATE_LIMIT_MAX_REQUESTS = 8    # max requests
RATE_LIMIT_WINDOW_SEC = 60     # per this many seconds, per chat
STREAM_TIMEOUT_SEC = 45        # abort if no new chunk for this long

ENABLE_TOOLS = False

TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file.",
            "parameters": {"type": "object", "properties": {"filepath": {"type": "string"}, "content": {"type": "string"}}, "required": ["filepath", "content"]}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "Delete a file.",
            "parameters": {"type": "object", "properties": {"filepath": {"type": "string"}}, "required": ["filepath"]}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file.",
            "parameters": {"type": "object", "properties": {"filepath": {"type": "string"}}, "required": ["filepath"]}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List directory.",
            "parameters": {"type": "object", "properties": {"directory": {"type": "string", "default": "."}}}
        }
    }
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("tg_ai_bot")

STATE = {}
EDIT_INTERVAL = 0.7  
MAX_MSG_LEN = 4000   
MODELS_PER_PAGE = 30 
MAX_FALLBACK_ATTEMPTS = 3
MAX_HISTORY = 40

_models_cache = {"models": None, "timestamp": 0}
CACHE_TTL = 300 

_broken_models = {}
BROKEN_CACHE_TTL = 600

# Usage stats: {composite_model: {"success": int, "fail": int}}
_model_stats = {}

# Rate limiting: {chat_id: [timestamps]}
_rate_limit_log = {}

# Per-chat "stop generation" flags: {chat_id: bool}
_stop_flags = {}

# Last user message per chat, for /regenerate and retry button: {chat_id: str}
_last_user_message = {}


def record_model_result(model, success: bool):
    stats = _model_stats.setdefault(model, {"success": 0, "fail": 0})
    stats["success" if success else "fail"] += 1


def is_allowed(chat_id) -> bool:
    if not ALLOWED_CHAT_IDS:
        return True
    return chat_id in ALLOWED_CHAT_IDS


def check_rate_limit(chat_id) -> bool:
    """Returns True if the request is allowed, False if rate-limited."""
    now = time.time()
    log = _rate_limit_log.setdefault(chat_id, [])
    log[:] = [t for t in log if now - t < RATE_LIMIT_WINDOW_SEC]
    if len(log) >= RATE_LIMIT_MAX_REQUESTS:
        return False
    log.append(now)
    return True

# ---------- Память ----------
MEMORY_FILE = "bot_memory.json"
_bot_memory = {}


def load_memory():
    global _bot_memory
    try:
        if os.path.exists(MEMORY_FILE):
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                _bot_memory = json.load(f)
            logger.info(f"Loaded memory for {len(_bot_memory)} users")
    except Exception as e:
        logger.warning(f"Failed to load memory: {e}")
        _bot_memory = {}


def save_memory():
    try:
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(_bot_memory, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"Failed to save memory: {e}")


def get_user_memory(chat_id: str) -> dict:
    chat_id = str(chat_id)
    if chat_id not in _bot_memory:
        _bot_memory[chat_id] = {"name": None, "facts": [], "age": None, "city": None, "favorites": []}
    _bot_memory[chat_id].setdefault("favorites", [])
    return _bot_memory[chat_id]


def extract_facts_from_text(text: str) -> dict:
    updates = {}
    text_lower = text.lower().strip()
    
    m = re.search(r'(?:меня зовут|моё имя|мое имя|я)\s+([а-яА-ЯёЁ]{2,20})', text)
    if m and not m.group(1).lower() in ['хочу', 'думаю', 'буду', 'люблю', 'могу']:
        updates['name'] = m.group(1).title()
    
    m = re.search(r'(?:my name is|i am|i\'m|call me)\s+([a-zA-Z]{2,20})', text_lower)
    if m:
        updates['name'] = m.group(1).title()
    
    m = re.search(r'мне\s+(\d{1,3})\s*(?:лет|год|года)', text_lower)
    if m:
        updates['age'] = m.group(1)
    
    m = re.search(r'(?:я живу в|из города|мой город|живу в)\s+([а-яА-ЯёЁ\s-]{2,30})', text)
    if m:
        updates['city'] = m.group(1).strip().title()
    
    return updates


def format_memory_prompt(mem: dict) -> str:
    parts = []
    if mem.get("name"):
        parts.append(f"Имя пользователя: {mem['name']}")
    if mem.get("age"):
        parts.append(f"Возраст: {mem['age']}")
    if mem.get("city"):
        parts.append(f"Город: {mem['city']}")
    if mem.get("facts"):
        facts = "\n".join(f"- {f}" for f in mem["facts"][-10:])
        parts.append(f"Известные факты:\n{facts}")
    
    if not parts:
        return ""
    return "Что ты помнишь о пользователе:\n" + "\n".join(parts)


def format_model_name(composite_id: str) -> str:
    if ":" not in composite_id:
        return composite_id
    svc_key, model_id = composite_id.split(":", 1)
    svc_name = SERVICES.get(svc_key, {}).get("name", svc_key.upper())
    name = model_id.split('/')[-1]
    name = name.replace('-', ' ').replace('_', ' ')
    return f"{name.title()} ({svc_name})"


def clean_thinking_tags(text: str) -> str:
    if not text:
        return text
    cleaned = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r'\n\s*\n\s*\n+', '\n\n', cleaned)
    return cleaned.strip()


def humanize_error(error_msg: str) -> str:
    """Превращает технические ошибки в понятные сообщения на русском"""
    if not error_msg:
        return "Что-то пошло не так"
    
    error_lower = error_msg.lower()
    
    if "service temporarily unavailable" in error_lower:
        return "Сервис временно недоступен, попробуй чуть позже"
    elif "upstream unavailable" in error_lower or "upstream_error" in error_lower:
        return "Модель сейчас перегружена, попробуй другую нейросеть"
    elif "rate limit" in error_lower or "too many requests" in error_lower:
        return "Слишком много запросов, подожди немного"
    elif "timeout" in error_lower:
        return "Сервер не успел ответить, попробуй ещё раз"
    elif "api key" in error_lower or "unauthorized" in error_lower or "invalid" in error_lower:
        return "Проблема с доступом к модели, выбери другую"
    elif "model" in error_lower and "not found" in error_lower:
        return "Модель не найдена, выбери другую нейросеть"
    elif "bad_request" in error_lower or "invalid request" in error_lower:
        return "Не удалось обработать запрос, попробуй переформулировать"
    elif "internal server error" in error_lower or "500" in error_lower:
        return "На сервере произошла ошибка, попробуй позже"
    elif "502" in error_lower or "503" in error_lower or "504" in error_lower:
        return "Сервис временно недоступен, попробуй чуть позже"
    elif "connection" in error_lower:
        return "Не удалось подключиться к серверу, попробуй позже"
    elif "daily" in error_lower and "limit" in error_lower:
        return "Достигнут дневной лимит запросов, попробуй завтра"
    elif "payment" in error_lower or "billing" in error_lower:
        return "Проблема с оплатой на стороне сервиса, выбери другую модель"
    else:
        return "Что-то пошло не так, попробуй другую нейросеть"


def is_error_response(text: str) -> bool:
    """Проверяет, является ли текст ответа ошибкой сервера"""
    if not text:
        return False
    text_clean = text.strip()
    error_patterns = [
        "[error]",
        "service temporarily unavailable",
        "please try again later",
        "upstream unavailable",
        "rate limit exceeded",
        "internal server error",
        "model is currently unavailable",
        "model not available"
    ]
    text_lower = text_clean.lower()
    if len(text_clean) < 200:
        return any(pattern in text_lower for pattern in error_patterns)
    return False


def get_friendly_response_message(text: str) -> str:
    """Возвращает дружелюбное сообщение на основе текста ошибки в ответе"""
    if not text:
        return "Что-то пошло не так, попробуй другую нейросеть"
    
    text_lower = text.lower()
    
    if "service temporarily unavailable" in text_lower or "try again later" in text_lower:
        return "Сервис временно недоступен, попробуй чуть позже"
    elif "upstream unavailable" in text_lower or "unavailable" in text_lower:
        return "Модель сейчас недоступна, попробуй другую нейросеть"
    elif "rate limit" in text_lower:
        return "Слишком много запросов, подожди немного"
    elif "timeout" in text_lower:
        return "Сервер не успел ответить, попробуй ещё раз"
    elif "not found" in text_lower or "not available" in text_lower:
        return "Модель не найдена, выбери другую нейросеть"
    else:
        return "Что-то пошло не так, попробуй другую нейросеть"


def get_available_models():
    now = time.time()
    if _models_cache["models"] and (now - _models_cache["timestamp"] < CACHE_TTL):
        return _models_cache["models"]
        
    all_models = []
    for svc_key, svc in SERVICES.items():
        try:
            headers = {
                "Authorization": f"Bearer {svc['api_key']}",
                "Content-Type": "application/json"
            }
            resp = requests.get(f"{svc['base_url']}/models", headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            models = [m["id"] for m in data.get("data", [])]
            for m in models:
                all_models.append(f"{svc_key}:{m}")
        except Exception as e:
            logger.warning(f"Model fetch failed for service {svc['name']}: {e}")
            
    result = sorted(all_models)
    if not result:
        result = FALLBACK_MODELS
        
    _models_cache["models"] = result
    _models_cache["timestamp"] = now
    return result


def is_model_broken(composite_model: str) -> bool:
    if composite_model in _broken_models:
        broken_time = _broken_models[composite_model]
        if time.time() - broken_time < BROKEN_CACHE_TTL:
            return True
        else:
            del _broken_models[composite_model]
    return False


def mark_model_broken(composite_model: str):
    _broken_models[composite_model] = time.time()


def find_similar_models(broken_model: str, all_models: list) -> list:
    if ":" in broken_model:
        svc_key, model_id = broken_model.split(":", 1)
    else:
        return []
    name = model_id.split('/')[-1]
    keywords = name.lower().replace('-', ' ').replace('_', ' ').split()
    similar = []
    for m in all_models:
        if m == broken_model or is_model_broken(m):
            continue
        if ":" in m:
            _, m_id = m.split(":", 1)
            m_name = m_id.split('/')[-1].lower().replace('-', ' ').replace('_', ' ').split()
            matches = sum(1 for kw in keywords if any(kw in m_kw for m_kw in m_name))
            if matches > 0:
                similar.append((matches, m))
    similar.sort(reverse=True, key=lambda x: x[0])
    return [m for _, m in similar[:MAX_FALLBACK_ATTEMPTS]]


def execute_tool(name, arguments_str):
    try:
        args = json.loads(arguments_str) if arguments_str.strip() else {}
    except Exception as e:
        return f"Error: Failed to parse arguments JSON: {e}"
    if name == "write_file":
        filepath, content = args.get("filepath"), args.get("content", "")
        try:
            with open(filepath, "w", encoding="utf-8") as f: f.write(content)
            return f"Success: wrote '{filepath}'."
        except Exception as e: return f"Error: {e}"
    elif name == "delete_file":
        filepath = args.get("filepath")
        try:
            if os.path.exists(filepath): os.remove(filepath); return "Success."
            return "Not found."
        except Exception as e: return f"Error: {e}"
    elif name == "read_file":
        filepath = args.get("filepath")
        try:
            if os.path.exists(filepath):
                with open(filepath, "r", encoding="utf-8") as f: return f.read()
            return "Not found."
        except Exception as e: return f"Error: {e}"
    elif name == "list_directory":
        try: return json.dumps(os.listdir(args.get("directory", ".")))
        except Exception as e: return f"Error: {e}"
    return "Unknown function."


def stream_chat_completion(composite_model, messages, tools=None):
    if ":" in composite_model:
        svc_key, model_id = composite_model.split(":", 1)
    else:
        svc_key = "hub"
        model_id = composite_model
        
    svc = SERVICES.get(svc_key)
    if not svc:
        raise Exception(f"Unknown service: {svc_key}")
        
    headers = {
        "Authorization": f"Bearer {svc['api_key']}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": model_id, 
        "messages": messages, 
        "stream": True
    }
    
    payload["thinking"] = {"type": "disabled"}
    payload["reasoning_effort"] = "none"
    
    if tools: 
        payload["tools"] = tools

    with requests.post(f"{svc['base_url']}/chat/completions", headers=headers, json=payload, stream=True, timeout=180) as resp:
        if resp.status_code != 200: 
            error_text = resp.text[:500]
            raise Exception(f"HTTP {resp.status_code}: {error_text}")
            
        for raw_line in resp.iter_lines(decode_unicode=True):
            if not raw_line: 
                continue
            line = raw_line.strip()
            if not line.startswith("data:"): 
                continue
            data_str = line[len("data:"):].strip()
            if data_str == "[DONE]": 
                break
            try: 
                chunk = json.loads(data_str)
                choices = chunk.get("choices", [])
                for choice in choices:
                    delta = choice.get("delta", {})
                    if "reasoning_content" in delta: del delta["reasoning_content"]
                    if "thinking" in delta: del delta["thinking"]
                    if "reasoning" in delta: del delta["reasoning"]
                    if "content" in delta and delta["content"]:
                        delta["content"] = clean_thinking_tags(delta["content"])
                        if is_error_response(delta["content"]):
                            raise Exception(f"Model error response: {delta['content'][:200]}")
                yield chunk
            except json.JSONDecodeError: 
                continue


def get_state(chat_id):
    if chat_id not in STATE: 
        STATE[chat_id] = {"model": None, "messages": []}
    return STATE[chat_id]


def ensure_model(state):
    if not state["model"]:
        models = get_available_models()
        state["model"] = models[0] if models else DEFAULT_MODEL
    return state["model"]


# ---------------- Commands ----------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    get_state(update.effective_chat.id)
    user_id = str(update.effective_chat.id)
    mem = get_user_memory(user_id)
    
    greeting = "Привет! 👋"
    if mem.get("name"):
        greeting = f"Привет, {mem['name']}! 👋"
    
    welcome_text = (
        f"✨ FreeModels ✨\n\n"
        f"{greeting}\n\n"
        f"Я — AI-ассистент проекта FreeModels. У меня есть доступ к множеству нейросетей: "
        f"GPT, Claude, Gemini, DeepSeek, Grok и многим другим.\n\n"
        f"Что я умею:\n"
        f"• Отвечать на вопросы\n"
        f"• Помогать с текстами и идеями\n"
        f"• Запоминать информацию о тебе\n"
        f"• Переключаться между разными AI-моделями\n\n"
        f"Начни с выбора модели: /model\n"
        f"Или просто напиши мне сообщение!"
    )
    
    await update.effective_message.reply_text(welcome_text)


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = get_state(update.effective_chat.id)
    state["messages"] = []
    await update.effective_message.reply_text(
        "🧹 Диалог очищен!\n\n"
        "История сообщений удалена, но я всё ещё помню то, что ты мне рассказывал о себе.\n"
        "Чтобы стереть и это — напиши /forget"
    )


async def cmd_memory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    mem = get_user_memory(chat_id)
    
    lines = ["🧠 Что я о тебе помню:\n"]
    has_data = False
    
    if mem.get("name"):
        lines.append(f"👤 Имя: {mem['name']}")
        has_data = True
    if mem.get("age"):
        lines.append(f"🎂 Возраст: {mem['age']}")
        has_data = True
    if mem.get("city"):
        lines.append(f"🏙 Город: {mem['city']}")
        has_data = True
    
    if mem.get("facts"):
        lines.append("\n📌 Факты:")
        for fact in mem["facts"][-10:]:
            lines.append(f"  • {fact}")
        has_data = True
    
    if not has_data:
        lines.append("Пока я ничего о тебе не знаю 🤷\n")
        lines.append("Расскажи мне что-нибудь о себе, например:\n")
        lines.append("• «Меня зовут Саша»\n")
        lines.append("• «Мне 25 лет»\n")
        lines.append("• «Я живу в Москве»\n")
        lines.append("Или используй /remember <факт>")
    
    await update.effective_message.reply_text("\n".join(lines))


async def cmd_remember(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.effective_message.reply_text(
            "📝 Использование:\n"
            "/remember <факт>\n\n"
            "Примеры:\n"
            "• /remember я люблю программировать\n"
            "• /remember моя кошка зовётся Мурка\n"
            "• /remember я работаю дизайнером"
        )
        return
    
    chat_id = str(update.effective_chat.id)
    fact = " ".join(context.args).strip()
    
    mem = get_user_memory(chat_id)
    if fact not in mem["facts"]:
        mem["facts"].append(fact)
        if len(mem["facts"]) > 50:
            mem["facts"] = mem["facts"][-50:]
        save_memory()
        await update.effective_message.reply_text(f"✅ Запомнил!\n\n📌 {fact}")
    else:
        await update.effective_message.reply_text("Я уже это помню 😊")


async def cmd_forget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    if chat_id in _bot_memory:
        del _bot_memory[chat_id]
        save_memory()
        await update.effective_message.reply_text(
            "🗑 Память стёрта!\n\n"
            "Я забыл всё, что ты мне рассказывал. Начнём с чистого листа?"
        )
    else:
        await update.effective_message.reply_text("У меня и так нет никакой информации о тебе 🤷")


async def send_model_page(target, models, page):
    if not models:
        text = "😕 Модели не найдены. Попробуй позже."
        if hasattr(target, 'reply_text'): 
            await target.reply_text(text)
        else: 
            try: 
                await target.edit_message_text(text)
            except BadRequest: 
                pass
        return

    start_idx = page * MODELS_PER_PAGE
    end_idx = start_idx + MODELS_PER_PAGE
    page_models = models[start_idx:end_idx]
    
    buttons = []
    for m in page_models:
        display_name = format_model_name(m)
        if is_model_broken(m):
            display_name = f"⚠️ {display_name}"
        buttons.append([InlineKeyboardButton(display_name, callback_data=f"setmodel:{m}")])
        
    nav_buttons = []
    total_pages = math.ceil(len(models) / MODELS_PER_PAGE)
    if page > 0: 
        nav_buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"model_page:{page-1}"))
    nav_buttons.append(InlineKeyboardButton(f"{page+1} / {total_pages}", callback_data="noop"))
    if end_idx < len(models): 
        nav_buttons.append(InlineKeyboardButton("Вперёд ➡️", callback_data=f"model_page:{page+1}"))
        
    if nav_buttons: 
        buttons.append(nav_buttons)
    markup = InlineKeyboardMarkup(buttons)
    
    header = f"🤖 Доступные модели ({len(models)} шт.)\n\nВыбери ту, которая тебе нравится:"
    
    if hasattr(target, 'reply_text'): 
        await target.reply_text(header, reply_markup=markup)
    else: 
        try: 
            await target.edit_message_text(header, reply_markup=markup)
        except BadRequest: 
            pass


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    models = get_available_models()
    await send_model_page(update.effective_message, models, 0)


async def on_page_turn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try: 
        page = int(query.data.split(":", 1)[1])
    except ValueError: 
        return
    models = get_available_models()
    await send_model_page(query, models, page)


async def on_model_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    model = query.data.split("setmodel:", 1)[1]
    state = get_state(query.message.chat_id)
    state["model"] = model
    try:
        await query.edit_message_text(
            f"✅ Модель выбрана!\n\n"
            f"🤖 {format_model_name(model)}\n"
            f"Теперь я буду отвечать через неё.\n\n"
            f"Просто напиши мне сообщение!"
        )
    except BadRequest: 
        pass


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.effective_message.reply_text(
            "🔍 Использование:\n"
            "/search <название модели>\n\n"
            "Примеры:\n"
            "• /search claude\n"
            "• /search gpt\n"
            "• /search gemini\n"
            "• /search deepseek aihub"
        )
        return
        
    search_query = " ".join(context.args).strip()
    models = get_available_models()
    keywords = search_query.lower().split()
    matched = []
    
    for m in models:
        formatted = format_model_name(m).lower()
        m_lower = m.lower()
        if all(kw in formatted or kw in m_lower for kw in keywords):
            matched.append(m)
            
    if not matched:
        await update.effective_message.reply_text(
            f"😕 По запросу «{search_query}» ничего не найдено.\n\n"
            f"Попробуй другой запрос или посмотри полный список: /model"
        )
        return
        
    if len(matched) == 1:
        state = get_state(update.effective_chat.id)
        state["model"] = matched[0]
        await update.effective_message.reply_text(
            f"✅ Модель найдена и установлена!\n\n"
            f"🤖 {format_model_name(matched[0])}\n"
            f"Теперь я буду отвечать через неё."
        )
    else:
        buttons = []
        for m in matched[:30]:
            display_name = format_model_name(m)
            if is_model_broken(m):
                display_name = f"⚠️ {display_name}"
            buttons.append([InlineKeyboardButton(display_name, callback_data=f"setmodel:{m}")])
        await update.effective_message.reply_text(
            f"🔍 По запросу «{search_query}» найдено моделей: {len(matched)}\n\n"
            f"Выбери нужную:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )


async def on_noop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()


async def on_stop_generation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat_id
    _stop_flags[chat_id] = True
    await query.answer("Останавливаю…")


async def on_retry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    last_msg = _last_user_message.get(chat_id)
    if not last_msg:
        await query.message.reply_text("Нечего повторять — не вижу предыдущего сообщения.")
        return
    # Drop the last assistant turn (if any) so we don't duplicate context, then resend
    state = get_state(chat_id)
    if state["messages"] and state["messages"][-1].get("role") == "assistant":
        state["messages"].pop()
    if state["messages"] and state["messages"][-1].get("role") == "user":
        state["messages"].pop()
    await run_generation(update.effective_chat, context, chat_id, last_msg)


async def cmd_regenerate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    last_msg = _last_user_message.get(chat_id)
    if not last_msg:
        await update.effective_message.reply_text("Нечего повторять — ещё не было сообщений.")
        return
    state = get_state(chat_id)
    if state["messages"] and state["messages"][-1].get("role") == "assistant":
        state["messages"].pop()
    if state["messages"] and state["messages"][-1].get("role") == "user":
        state["messages"].pop()
    await run_generation(update.effective_chat, context, chat_id, last_msg)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _model_stats:
        await update.effective_message.reply_text("Статистики пока нет — ни одного запроса не было.")
        return
    rows = []
    for model, s in sorted(_model_stats.items(), key=lambda kv: kv[1]["success"], reverse=True):
        total = s["success"] + s["fail"]
        rate = int(100 * s["success"] / total) if total else 0
        rows.append(f"{format_model_name(model)}: ✅{s['success']} ❌{s['fail']} ({rate}%)")
    text = "📊 Статистика по моделям:\n\n" + "\n".join(rows[:40])
    await update.effective_message.reply_text(text[:MAX_MSG_LEN])


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    if not state["messages"]:
        await update.effective_message.reply_text("Диалог пуст — экспортировать нечего.")
        return
    lines = []
    for m in state["messages"]:
        role = m.get("role", "?")
        content = m.get("content") or ""
        lines.append(f"[{role}]\n{content}\n")
    export_path = f"/tmp/chat_export_{chat_id}.txt"
    with open(export_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    with open(export_path, "rb") as f:
        await update.effective_message.reply_document(f, filename="dialog_export.txt")


async def cmd_fav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    mem = get_user_memory(str(chat_id))
    current = state.get("model")
    if not current:
        await update.effective_message.reply_text("Сначала выбери модель: /model")
        return
    if current in mem["favorites"]:
        await update.effective_message.reply_text(f"⭐ {format_model_name(current)} уже в избранном.")
        return
    mem["favorites"].append(current)
    save_memory()
    await update.effective_message.reply_text(f"⭐ Добавлено в избранное: {format_model_name(current)}")


async def cmd_favmodel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    mem = get_user_memory(str(chat_id))
    favorites = mem.get("favorites", [])
    if not favorites:
        await update.effective_message.reply_text(
            "У тебя пока нет избранных моделей.\n"
            "Выбери модель через /model, потом напиши /fav, чтобы добавить её в избранное."
        )
        return
    buttons = [
        [InlineKeyboardButton(format_model_name(m), callback_data=f"setmodel:{m}")]
        for m in favorites
    ]
    await update.effective_message.reply_text(
        "⭐ Твои избранные модели:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def safe_edit(msg, text):
    if not text.strip(): 
        text = "…"
    text = text[-MAX_MSG_LEN:]
    try: 
        await msg.edit_text(text)
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after)
        try: 
            await msg.edit_text(text)
        except BadRequest: 
            pass
    except BadRequest as e:
        if "not modified" not in str(e).lower(): 
            logger.warning(f"Edit failed: {e}")


def is_bot_addressed(update: Update, bot_username: str) -> bool:
    """In group chats, only respond when mentioned or replied to."""
    chat = update.effective_chat
    if chat.type == "private":
        return True
    msg = update.effective_message
    if msg.reply_to_message and msg.reply_to_message.from_user and msg.reply_to_message.from_user.is_bot:
        if msg.reply_to_message.from_user.username == bot_username:
            return True
    text = msg.text or ""
    if bot_username and f"@{bot_username}".lower() in text.lower():
        return True
    return False


async def guard_whitelist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Runs before every other handler (group=-1). Blocks non-whitelisted chats entirely."""
    chat = update.effective_chat
    if chat is None:
        return
    if not is_allowed(chat.id):
        raise ApplicationHandlerStop


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    bot_username = context.bot.username
    if not is_bot_addressed(update, bot_username):
        return  # group chat, bot wasn't mentioned/replied to

    if not check_rate_limit(chat_id):
        await update.effective_message.reply_text(
            f"⏳ Слишком много запросов. Подожди немного и попробуй снова "
            f"(лимит: {RATE_LIMIT_MAX_REQUESTS} запросов в {RATE_LIMIT_WINDOW_SEC} сек)."
        )
        return

    user_text = update.effective_message.text
    if bot_username:
        user_text = re.sub(rf"@{re.escape(bot_username)}", "", user_text, flags=re.IGNORECASE).strip()

    state = get_state(chat_id)
    ensure_model(state)

    chat_id_str = str(chat_id)
    mem = get_user_memory(chat_id_str)
    updates = extract_facts_from_text(user_text)
    memory_changed = False
    for k, v in updates.items():
        if mem.get(k) != v:
            mem[k] = v
            memory_changed = True
    if memory_changed:
        save_memory()

    state["messages"].append({"role": "user", "content": user_text})
    _last_user_message[chat_id] = user_text

    await run_generation(update.effective_chat, context, chat_id, user_text)


async def run_generation(chat, context: ContextTypes.DEFAULT_TYPE, chat_id, user_text):
    """Core generation loop, shared by normal messages, /regenerate, and the retry button.
    Assumes the user message has already been appended to state['messages']."""
    state = get_state(chat_id)
    ensure_model(state)
    mem = get_user_memory(str(chat_id))

    if len(state["messages"]) > MAX_HISTORY * 2:
        state["messages"] = state["messages"][-MAX_HISTORY * 2:]

    messages_for_api = []
    memory_prompt = format_memory_prompt(mem)
    if memory_prompt:
        messages_for_api.append({
            "role": "system", 
            "content": f"{memory_prompt}\n\nИспользуй эту информацию в общении. Отвечай на том языке, на котором к тебе обращаются."
        })
    else:
        messages_for_api.append({
            "role": "system",
            "content": "Ты дружелюбный AI-ассистент проекта FreeModels. Отвечай на том языке, на котором к тебе обращаются."
        })
    messages_for_api.extend(state["messages"])

    stop_markup = InlineKeyboardMarkup([[InlineKeyboardButton("⏹ Стоп", callback_data="stopgen")]])
    placeholder = await context.bot.send_message(chat_id, "⏳ FreeModels думает...", reply_markup=stop_markup)
    tools_for_call = TOOLS_SCHEMA if ENABLE_TOOLS else None
    
    models_to_try = [state["model"]]
    all_models = get_available_models()
    similar = find_similar_models(state["model"], all_models)
    models_to_try.extend(similar)
    
    last_error = None
    
    for attempt, model in enumerate(models_to_try):
        if is_model_broken(model):
            continue
            
        if attempt > 0:
            await safe_edit(placeholder, 
                f"⚠️ {format_model_name(models_to_try[attempt-1])} сейчас не отвечает.\n"
                f"Пробую {format_model_name(model)}..."
            )
            await asyncio.sleep(1)
        
        while True:
            assistant_response = ""
            tool_calls_accumulator = {}
            last_edit = 0.0
            loop = asyncio.get_event_loop()
            q = asyncio.Queue()

            def producer():
                try:
                    for chunk in stream_chat_completion(model, messages_for_api, tools_for_call):
                        asyncio.run_coroutine_threadsafe(q.put(("chunk", chunk)), loop)
                except Exception as e:
                    asyncio.run_coroutine_threadsafe(q.put(("error", str(e))), loop)
                finally:
                    asyncio.run_coroutine_threadsafe(q.put(("done", None)), loop)

            producer_future = loop.run_in_executor(None, producer)
            error_msg = None
            stopped_by_user = False

            while True:
                if _stop_flags.get(chat_id):
                    _stop_flags[chat_id] = False
                    stopped_by_user = True
                    break
                try:
                    kind, payload = await asyncio.wait_for(q.get(), timeout=STREAM_TIMEOUT_SEC)
                except asyncio.TimeoutError:
                    error_msg = f"timeout: no response for {STREAM_TIMEOUT_SEC}s"
                    break
                if kind == "error": 
                    error_msg = payload
                    break
                if kind == "done": 
                    break
                chunk = payload
                choices = chunk.get("choices") or []
                if not choices: 
                    continue
                delta = choices[0].get("delta", {})

                delta_tool_calls = delta.get("tool_calls")
                if delta_tool_calls:
                    for tc in delta_tool_calls:
                        idx = tc.get("index", 0)
                        fn = tc.get("function") or {}
                        if idx not in tool_calls_accumulator:
                            tool_calls_accumulator[idx] = {"id": tc.get("id"), "name": fn.get("name") or "", "arguments": ""}
                        else:
                            if tc.get("id"): 
                                tool_calls_accumulator[idx]["id"] = tc.get("id")
                            if fn.get("name"): 
                                tool_calls_accumulator[idx]["name"] = fn.get("name")
                        if fn.get("arguments"): 
                            tool_calls_accumulator[idx]["arguments"] += fn.get("arguments")

                content = delta.get("content")
                if content:
                    assistant_response += content
                    now = time.time()
                    if now - last_edit > EDIT_INTERVAL:
                        await safe_edit(placeholder, clean_thinking_tags(assistant_response))
                        last_edit = now

            if not stopped_by_user:
                await producer_future

            if stopped_by_user:
                partial = clean_thinking_tags(assistant_response) or "[пусто]"
                retry_markup = InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔄 Повторить", callback_data="retry")
                ]])
                try:
                    await placeholder.edit_text(
                        f"⏹ Остановлено.\n\n{partial}"[-MAX_MSG_LEN:],
                        reply_markup=retry_markup
                    )
                except BadRequest:
                    pass
                if assistant_response:
                    state["messages"].append({"role": "assistant", "content": assistant_response})
                return

            if error_msg:
                last_error = error_msg
                mark_model_broken(model)
                record_model_result(model, success=False)
                logger.warning(f"Model {model} failed: {error_msg}")
                break
            
            assistant_response = clean_thinking_tags(assistant_response)
            
            if is_error_response(assistant_response):
                last_error = assistant_response
                mark_model_broken(model)
                record_model_result(model, success=False)
                logger.warning(f"Model {model} returned error text: {assistant_response[:100]}")
                break
            
            if attempt > 0:
                state["model"] = model
                await safe_edit(placeholder, f"✅ Переключился на {format_model_name(model)}\n\n")
                await asyncio.sleep(0.5)
            
            if tool_calls_accumulator:
                serialized = []
                for idx in sorted(tool_calls_accumulator.keys()):
                    tc = tool_calls_accumulator[idx]
                    serialized.append({"id": tc["id"] or f"call_{int(time.time())}_{idx}", "type": "function", "function": {"name": tc["name"], "arguments": tc["arguments"]}})

                state["messages"].append({"role": "assistant", "content": assistant_response or None, "tool_calls": serialized})

                tool_summary_lines = []
                for tc in serialized:
                    name = tc["function"]["name"]
                    args_str = tc["function"]["arguments"]
                    result = execute_tool(name, args_str)
                    tool_summary_lines.append(f"⚙️ {name}({args_str}) → {result}")
                    state["messages"].append({"role": "tool", "tool_call_id": tc["id"], "name": name, "content": result})

                await safe_edit(placeholder, "\n".join(tool_summary_lines) + "\n\nПродолжаю...")
                continue
            else:
                record_model_result(model, success=True)
                retry_markup = InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔄 Повторить", callback_data="retry")
                ]])
                try:
                    await placeholder.edit_text(
                        (assistant_response or "[Пустой ответ]")[-MAX_MSG_LEN:],
                        reply_markup=retry_markup
                    )
                except BadRequest:
                    pass
                state["messages"].append({"role": "assistant", "content": assistant_response})
                return
    
    if last_error:
        if is_error_response(last_error):
            friendly_msg = get_friendly_response_message(last_error)
        else:
            friendly_msg = humanize_error(last_error)
    else:
        friendly_msg = "Что-то пошло не так, попробуй другую нейросеть"
    
    retry_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Повторить", callback_data="retry")]])
    try:
        await placeholder.edit_text(
            f"😔 {friendly_msg}\n\n"
            f"Попробуй выбрать другую нейросеть: /model\n"
            f"Или повтори попытку чуть позже.",
            reply_markup=retry_markup
        )
    except BadRequest:
        pass


def main():
    if not TELEGRAM_BOT_TOKEN:
        print("Error: set TELEGRAM_BOT_TOKEN environment variable.")
        sys.exit(1)

    # In CI (e.g. GitHub Actions), the runner has a hard job time limit.
    # If MAX_RUNTIME_SECONDS is set, force a clean process exit before that
    # limit hits, so the workflow can hand off to the next scheduled run.
    max_runtime = os.environ.get("MAX_RUNTIME_SECONDS")
    if max_runtime:
        try:
            seconds = int(max_runtime)

            def _watchdog():
                time.sleep(seconds)
                logger.info(f"MAX_RUNTIME_SECONDS ({seconds}s) reached — saving memory and exiting.")
                save_memory()
                os._exit(0)  # hard exit; run_polling() has no clean async stop from another thread

            threading.Thread(target=_watchdog, daemon=True).start()
        except ValueError:
            logger.warning(f"Invalid MAX_RUNTIME_SECONDS={max_runtime!r}, ignoring.")

    load_memory()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.ALL, guard_whitelist), group=-1)
    app.add_handler(CallbackQueryHandler(guard_whitelist), group=-1)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("memory", cmd_memory))
    app.add_handler(CommandHandler("remember", cmd_remember))
    app.add_handler(CommandHandler("forget", cmd_forget))
    app.add_handler(CommandHandler("regenerate", cmd_regenerate))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CommandHandler("fav", cmd_fav))
    app.add_handler(CommandHandler("favmodel", cmd_favmodel))
    app.add_handler(CallbackQueryHandler(on_page_turn, pattern=r"^model_page:"))
    app.add_handler(CallbackQueryHandler(on_model_selected, pattern=r"^setmodel:"))
    app.add_handler(CallbackQueryHandler(on_noop, pattern=r"^noop$"))
    app.add_handler(CallbackQueryHandler(on_stop_generation, pattern=r"^stopgen$"))
    app.add_handler(CallbackQueryHandler(on_retry, pattern=r"^retry$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("FreeModels Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()

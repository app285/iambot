import os
import re
import time
import json
import random
import datetime
import threading
import requests
from flask import Flask, request
from groq import Groq

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")  # sizning shaxsiy Telegram user_id'ingiz

if not TELEGRAM_TOKEN or not GROQ_API_KEY:
    raise RuntimeError("TELEGRAM_TOKEN yoki GROQ_API_KEY muhit o'zgaruvchisi topilmadi!")

TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
groq_client = Groq(api_key=GROQ_API_KEY)

# ---------------------------------------------------------------------------
# DOIMIY SAQLASH (ixtiyoriy)
# ---------------------------------------------------------------------------
# Vercel har safar sovuq ishga tushganda (cold start) barcha oddiy Python
# dictionary'lar tozalanadi. Shuning uchun ism, til, kayfiyat kabi
# sozlamalarni doimiy saqlash uchun (ixtiyoriy) Upstash Redis ishlatiladi -
# https://upstash.com dan bepul akkaunt ochib, "Redis" bazasi yaratib,
# undan olingan REST URL va TOKEN'ni quyidagi ikkita muhit o'zgaruvchisiga
# qo'yish kifoya:
#   UPSTASH_REDIS_REST_URL
#   UPSTASH_REDIS_REST_TOKEN
# Agar bu ikkalasi kiritilmagan bo'lsa, bot avvalgidek faqat xotirada (RAM)
# ishlayveradi - hech narsa buzilmaydi, faqat cold start'da sozlamalar
# tiklanadi.
UPSTASH_REDIS_REST_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
UPSTASH_REDIS_REST_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
PERSISTENCE_ENABLED = bool(UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN)


def _kv_headers():
    return {"Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"}


def kv_load(key, default):
    """Upstash Redis'dan saqlangan qiymatni o'qiydi. Ulanmagan yoki xato bo'lsa - default qaytadi."""
    if not PERSISTENCE_ENABLED:
        return default
    try:
        res = requests.get(f"{UPSTASH_REDIS_REST_URL}/get/{key}", headers=_kv_headers(), timeout=5)
        raw = res.json().get("result")
        if raw is None:
            return default
        return json.loads(raw)
    except Exception as e:
        print(f"KV o'qishda xatolik ({key}):", e)
        return default


def kv_save(key, value):
    """Qiymatni Upstash Redis'ga yozadi. Ulanmagan bo'lsa - hech narsa qilmaydi (xatoni yutib yuboradi)."""
    if not PERSISTENCE_ENABLED:
        return
    try:
        requests.post(
            f"{UPSTASH_REDIS_REST_URL}/set/{key}",
            headers=_kv_headers(),
            data=json.dumps(value),
            timeout=5,
        )
    except Exception as e:
        print(f"KV yozishda xatolik ({key}):", e)


def _keys_to_int(d):
    """JSON'dan qaytgan dictionary kalitlari doim string bo'ladi - chat_id sifatida int kerak."""
    result = {}
    for k, v in d.items():
        try:
            result[int(k)] = v
        except (TypeError, ValueError):
            result[k] = v
    return result

# --- MODELLAR ---
TEXT_MODEL = "openai/gpt-oss-120b"
VISION_MODEL = "qwen/qwen3.6-27b"  # hozircha "preview" statusida - vaqti-vaqti bilan tekshirib turing

# Bot shu nusxa qachon ishga tushganini bilish uchun (cold start'da qayta o'rnatiladi)
BOT_START_TIME = time.time()

# Vercel uchun vaqtinchalik xotira (Dictionary)
chat_histories = {}
MAX_HISTORY_LENGTH = 10

# Kayfiyat rejimlari uchun xotira
chat_moods = _keys_to_int(kv_load("chat_moods", {}))

# Majburiy til rejimi: chat_id -> "auto" | "en" | "uz_latin" | "uz_cyrillic" | "ru"
chat_languages = _keys_to_int(kv_load("chat_languages", {}))
DEFAULT_LANGUAGE = "auto"

# Botga to'g'ridan-to'g'ri yozgan har bir foydalanuvchining ismi (shaxsiy
# murojaat qilish uchun): chat_id -> ism. Business chat'larga (odam sizning
# shaxsiy akkountingizga yozganda) bu tegishli emas - u yerda bot sizning
# nomingizdan gapiradi, o'zi alohida "foydalanuvchi" emas.
user_names = _keys_to_int(kv_load("user_names", {}))
# Ismi hali so'ralgan, lekin javob kelmagan chat_id'lar to'plami.
awaiting_name_chats = set()

# Business ulanishlar bo'yicha akkaunt egasining user_id sini keshlash
business_owner_ids = {}

# Oddiy rate-limit: chat_id -> oxirgi xabar vaqti
last_message_time = {}
MIN_SECONDS_BETWEEN_MESSAGES = 2

# Telegram webhook'ni bir necha marta yuborishi mumkin bo'lgan update'larni
# takrorlab qayta ishlamaslik uchun (masalan tarmoq sekin javob qaytarganda).
processed_update_ids = set()
MAX_PROCESSED_IDS = 2000

# --- XAVFSIZLIK ---
# Bloklangan foydalanuvchilar (user_id to'plami)
blocked_users = set(kv_load("blocked_users", []))

# Kunlik xabar limiti: chat_id -> {"date": "YYYY-MM-DD", "count": n}
daily_usage = {}
DAILY_MESSAGE_LIMIT = 60

# --- STATISTIKA (joriy sessiya davomida, cold start'da nolga tushadi) ---
stats = {
    "total_messages": 0,
    "total_errors": 0,
}

# --- XATOLIKLARNI KUZATISH ---
consecutive_groq_errors = {"count": 0}
CONSECUTIVE_ERROR_ALERT_THRESHOLD = 3

REQUEST_TIMEOUT = 15
TELEGRAM_MAX_MESSAGE_LENGTH = 4000  # Telegram cheklovi 4096, xavfsizlik uchun kichikroq

# --- AKKAUNT EGASINING ISMI ---
# Ilgari "Shaxboz" deb kodga qattiq yozilgan edi. Endi bu OWNER_NAME muhit
# o'zgaruvchisidan olinadi (Vercel'da doimiy saqlanadi), lekin admin
# /ismim buyrug'i bilan ham xotirada (joriy sessiya davomida) o'zgartira oladi.
owner_name_state = {"name": kv_load("owner_name", os.environ.get("OWNER_NAME", "").strip())}


LANGUAGE_LABELS = {
    "auto": "🌐 Avto (yozgan tiliga qarab)",
    "en": "🇬🇧 Ingliz",
    "uz_latin": "🇺🇿 O'zbek (lotin)",
    "uz_cyrillic": "🇺🇿 Ўзбек (кирилл)",
    "ru": "🇷🇺 Rus",
}

LANGUAGE_INSTRUCTIONS = {
    "auto": (
        "Suhbatdosh qaysi tilda yozsa, javobni FAQAT o'sha tilda ber (o'zbek, ingliz, rus va h.k.). "
        "Tillarni aralashtirma. Agar suhbatdosh o'zbek tilida lotin alifbosida yozsa - sen ham lotincha yoz. "
        "Agar suhbatdosh o'zbek tilida kirill alifbosida yozsa - sen ham kirillcha alifboda javob qaytar."
    ),
    "en": "Har doim FAQAT ingliz tilida javob ber, suhbatdosh qaysi tilda yozishidan qat'iy nazar.",
    "uz_latin": "Har doim FAQAT o'zbek tilida, lotin alifbosida javob ber, suhbatdosh qaysi tilda yozishidan qat'iy nazar.",
    "uz_cyrillic": "Har doim FAQAT ўзбек тилида, кирилл алифбосида жавоб бер, суҳбатдош қайси тилда ёзишидан қатъий назар.",
    "ru": "Har doim FAQAT rus tilida javob ber, suhbatdosh qaysi tilda yozishidan qat'iy nazar.",
}


# ---------------------------------------------------------------------------
# CUSTOM EMOJI VA PREMIUM STIKERLAR
# ---------------------------------------------------------------------------
# Bu ikki ro'yxatni to'ldirish uchun botga /getid buyrug'ini yuboring, keyin
# istagan custom emoji (matn ichida) yoki stikerni yuboring - bot ID'sini
# chiqarib beradi. Chiqqan ID'ni shu yerga qo'shib qo'ysangiz bo'ldi.
#
# CUSTOM_EMOJIS: AI javobida qaysi holatda qaysi emoji ishlatilishini
# bog'laydigan kalit so'zlar. Kalit nomini o'zingiz xohlagancha qo'yishingiz
# mumkin - faqat get_ai_answer() ichidagi system_prompt'da ham shu nomlar
# aytilgan bo'lishi kerak (pastda avtomatik ro'yxatdan generatsiya qilinadi).
CUSTOM_EMOJIS = {
    # "kalit_nomi": {"emoji": "ko'rinadigan_belgi", "id": "custom_emoji_id"},
    # "kulgi": {"emoji": "😂", "id": "5368324170671202286"},
    # "yurak": {"emoji": "❤️", "id": "XXXXXXXXXXXXXXXXX"},
    # "olov":  {"emoji": "🔥", "id": "XXXXXXXXXXXXXXXXX"},
    # "rozi":  {"emoji": "👍", "id": "XXXXXXXXXXXXXXXXX"},
}

# PREMIUM_STICKERS: bot vaqti-vaqti bilan (tasodifiy) yuborishi mumkin
# bo'lgan stikerlarning file_id ro'yxati.
PREMIUM_STICKERS = [
    # "CAACAgIAAxkBAAIB...",
    # "CAACAgIAAxkBAAIC...",
]

# Har nechinchi javobda stiker yuborish ehtimoli (0.1 = ~10% xabarda bitta stiker)
STICKER_SEND_CHANCE = 0.08

# /getid buyrug'idan keyin javob (emoji/stiker) kutilayotgan admin chat'lari
awaiting_id_chats = set()


def build_custom_emoji_entity(emoji_key, offset):
    """Berilgan kalit nomi bo'yicha custom_emoji entity yaratadi. Topilmasa None qaytaradi."""
    info = CUSTOM_EMOJIS.get(emoji_key)
    if not info:
        return None, ""
    # Telegram entity uzunligi UTF-16 birliklarda hisoblanadi (ba'zi emojilar 2 birlik).
    length = len(info["emoji"].encode("utf-16-le")) // 2
    entity = {
        "type": "custom_emoji",
        "offset": offset,
        "length": length,
        "custom_emoji_id": info["id"],
    }
    return entity, info["emoji"]


EMOJI_TAG_PATTERN = re.compile(r"\s*\[EMOJI:(\w+)\]\s*")


def extract_emoji_tag(answer):
    """AI javobidagi [EMOJI:kalit] belgisini ajratib oladi va matndan olib tashlaydi."""
    match = EMOJI_TAG_PATTERN.search(answer)
    if not match:
        return answer, None
    key = match.group(1)
    clean_answer = EMOJI_TAG_PATTERN.sub(" ", answer).strip()
    return clean_answer, key


def maybe_send_premium_sticker(chat_id, business_connection_id=None):
    """Ba'zan (tasodifiy) premium stikerlardan birini yuboradi."""
    if not PREMIUM_STICKERS:
        return
    if random.random() > STICKER_SEND_CHANCE:
        return
    file_id = random.choice(PREMIUM_STICKERS)
    send_sticker(chat_id, file_id, business_connection_id)


# ---------------------------------------------------------------------------
# ADMIN MONITORING
# ---------------------------------------------------------------------------

def notify_admin(text):
    """Sizning shaxsiy Telegram akkauntingizga (ADMIN_CHAT_ID) xabar yuboradi."""
    if not ADMIN_CHAT_ID:
        return
    try:
        requests.post(
            f"{TELEGRAM_API_URL}/sendMessage",
            json={"chat_id": ADMIN_CHAT_ID, "text": text},
            timeout=5,
        )
    except Exception as e:
        print("Admin xabari yuborilmadi:", e)


def is_admin(sender_id):
    if not ADMIN_CHAT_ID or sender_id is None:
        return False
    return str(sender_id) == str(ADMIN_CHAT_ID)


def format_sender_info(sender, chat_id, business_connection_id=None):
    username = sender.get("username")
    full_name = f"{sender.get('first_name', '')} {sender.get('last_name') or ''}".strip()
    lines = [
        f"👤 Kimdan: {full_name or '(ismsiz)'}",
        f"🆔 User ID: {sender.get('id')}",
        f"💬 Chat ID: {chat_id}",
    ]
    if username:
        lines.append(f"🔗 Username: @{username}")
    if business_connection_id:
        lines.append("🏢 Manba: Business chat")
    return "\n".join(lines)


def notify_new_message(sender, chat_id, content_preview, business_connection_id=None):
    info = format_sender_info(sender, chat_id, business_connection_id)
    preview = (content_preview or "")[:300]
    notify_admin(f"📩 Yangi xabar\n{info}\n\n✉️ Matn: {preview}")


def notify_error(context, sender, chat_id, error):
    stats["total_errors"] += 1
    info = format_sender_info(sender, chat_id) if sender else f"Chat ID: {chat_id}"
    notify_admin(f"⚠️ Xatolik ({context})\n{info}\n\nXato: {error}")


def track_groq_result(success):
    """Ketma-ket Groq xatolarini kuzatadi va muammo jiddiy bo'lsa maxsus ogohlantiradi."""
    if success:
        consecutive_groq_errors["count"] = 0
        return
    consecutive_groq_errors["count"] += 1
    if consecutive_groq_errors["count"] == CONSECUTIVE_ERROR_ALERT_THRESHOLD:
        notify_admin(
            f"🔴 DIQQAT: Groq API ketma-ket {CONSECUTIVE_ERROR_ALERT_THRESHOLD} marta xato qaytardi!\n"
            "Ehtimol API kaliti tugagan, limit oshgan yoki xizmat vaqtincha ishlamayapti. "
            "https://console.groq.com dan tekshiring."
        )


# ---------------------------------------------------------------------------
# YORDAMCHI FUNKSIYALAR
# ---------------------------------------------------------------------------

def get_business_owner_id(business_connection_id):
    if business_connection_id in business_owner_ids:
        return business_owner_ids[business_connection_id]

    try:
        res = requests.get(
            f"{TELEGRAM_API_URL}/getBusinessConnection",
            params={"business_connection_id": business_connection_id},
            timeout=REQUEST_TIMEOUT,
        )
        owner = res.json().get("result", {}).get("user", {})
        owner_id = owner.get("id")
        if owner_id:
            business_owner_ids[business_connection_id] = owner_id
        return owner_id
    except Exception as e:
        print("Business connection ma'lumotini olishda xatolik:", e)
        notify_admin(f"⚠️ Business connection xatosi: {e}")
        return None


def send_chat_action(chat_id, action="typing", business_connection_id=None):
    try:
        payload = {"chat_id": chat_id, "action": action}
        if business_connection_id:
            payload["business_connection_id"] = business_connection_id
        requests.post(f"{TELEGRAM_API_URL}/sendChatAction", json=payload, timeout=5)
    except Exception as e:
        print("Chat action xatolik:", e)


def keep_typing(chat_id, stop_event, business_connection_id=None):
    """
    AI javob tayyorlayotgan vaqtda Telegram'ning o'ziga xos (native) 'yozyapti...'
    statusini uzluksiz yuborib turadi. Bu foydalanuvchiga ortiqcha xabar
    ko'rsatmaydi (masalan alohida "O'ylayapman..." degan xabar chiqmaydi) -
    faqat odatiy, tanish "yozyapti..." animatsiyasi ko'rinadi.
    """
    while not stop_event.is_set():
        send_chat_action(chat_id, "typing", business_connection_id)
        stop_event.wait(4)


def start_typing_loop(chat_id, business_connection_id=None):
    """Typing animatsiyasini boshlaydi va (thread, stop_event) qaytaradi."""
    stop_event = threading.Event()
    thread = threading.Thread(
        target=keep_typing,
        args=(chat_id, stop_event, business_connection_id),
        daemon=True,
    )
    thread.start()
    return thread, stop_event


def stop_typing_loop(thread, stop_event):
    """Typing animatsiyasini to'xtatadi va thread tugashini kutadi."""
    stop_event.set()
    thread.join(timeout=1)


def _split_text(text, max_length):
    """Uzun matnni Telegram limitiga mos qismlarga bo'ladi, imkon qadar so'z chegarasida."""
    parts = []
    remaining = text
    while len(remaining) > max_length:
        split_at = remaining.rfind("\n", 0, max_length)
        if split_at == -1:
            split_at = remaining.rfind(" ", 0, max_length)
        if split_at == -1:
            split_at = max_length
        parts.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip()
    if remaining:
        parts.append(remaining)
    return parts


def send_message(chat_id, text, business_connection_id=None, reply_markup=None, entities=None):
    if not text:
        return
    chunks = _split_text(text, TELEGRAM_MAX_MESSAGE_LENGTH)
    for i, chunk in enumerate(chunks):
        try:
            payload = {"chat_id": chat_id, "text": chunk}
            if business_connection_id:
                payload["business_connection_id"] = business_connection_id
            # Tugmalarni faqat oxirgi qismga qo'shamiz
            if reply_markup and i == len(chunks) - 1:
                payload["reply_markup"] = reply_markup
            # Custom emoji entity'larni faqat birinchi qismga qo'shamiz
            # (chunki offset shu qismga nisbatan hisoblangan)
            if entities and i == 0:
                payload["entities"] = entities
            requests.post(f"{TELEGRAM_API_URL}/sendMessage", json=payload, timeout=REQUEST_TIMEOUT)
        except Exception as e:
            print("Telegramga yuborishda xatolik:", e)


def send_sticker(chat_id, file_id, business_connection_id=None):
    """Berilgan file_id bo'yicha stiker (jumladan premium stiker) yuboradi."""
    try:
        payload = {"chat_id": chat_id, "sticker": file_id}
        if business_connection_id:
            payload["business_connection_id"] = business_connection_id
        requests.post(f"{TELEGRAM_API_URL}/sendSticker", json=payload, timeout=REQUEST_TIMEOUT)
    except Exception as e:
        print("Stiker yuborishda xatolik:", e)


def answer_callback_query(callback_query_id, text=None):
    try:
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        requests.post(f"{TELEGRAM_API_URL}/answerCallbackQuery", json=payload, timeout=5)
    except Exception as e:
        print("Callback javobida xatolik:", e)


def edit_message_text(chat_id, message_id, text):
    try:
        payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
        requests.post(f"{TELEGRAM_API_URL}/editMessageText", json=payload, timeout=REQUEST_TIMEOUT)
    except Exception as e:
        print("Xabarni tahrirlashda xatolik:", e)


def download_telegram_file(file_id):
    try:
        res = requests.get(f"{TELEGRAM_API_URL}/getFile?file_id={file_id}", timeout=REQUEST_TIMEOUT)
        file_path = res.json().get("result", {}).get("file_path")
        if file_path:
            return f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
    except Exception as e:
        print("Fayl yo'lini olishda xatolik:", e)
    return None


def is_rate_limited(chat_id):
    now = time.time()
    last_time = last_message_time.get(chat_id)
    last_message_time[chat_id] = now
    if last_time is not None and (now - last_time) < MIN_SECONDS_BETWEEN_MESSAGES:
        return True
    return False


def is_duplicate_update(update_id):
    """Telegram bir xil update'ni ikki marta yuborishi mumkin - buni oldini oladi."""
    if update_id is None:
        return False
    if update_id in processed_update_ids:
        return True
    processed_update_ids.add(update_id)
    if len(processed_update_ids) > MAX_PROCESSED_IDS:
        processed_update_ids.clear()
    return False


def is_daily_limit_exceeded(chat_id):
    """Bitta chat kuniga DAILY_MESSAGE_LIMIT tadan ko'p xabar yubormasin."""
    today = datetime.date.today().isoformat()
    usage = daily_usage.get(chat_id)
    if usage is None or usage["date"] != today:
        daily_usage[chat_id] = {"date": today, "count": 1}
        return False
    usage["count"] += 1
    return usage["count"] > DAILY_MESSAGE_LIMIT


def build_language_keyboard():
    buttons = [
        [{"text": label, "callback_data": f"lang:{code}"}]
        for code, label in LANGUAGE_LABELS.items()
    ]
    return {"inline_keyboard": buttons}


def get_help_text():
    return (
        "Salom! Men sizning AI-yordamchingizman 🤖\n\n"
        "Nima qila olaman:\n"
        "📝 Matnli xabarlarga javob beraman\n"
        "🎤 Ovozli xabarlarni tinglab, tushunib javob beraman\n"
        "🖼️ Yuborgan rasmingizni tahlil qilib bera olaman\n\n"
        "Buyruqlar:\n"
        "/hazil — hazilkash rejimga o'tish\n"
        "/jiddiy — jiddiy rejimga o'tish\n"
        "/normal — odatdagi rejimga qaytish\n"
        "/til — javob tilini tanlash (ingliz, o'zbek, rus...)\n"
        "/reset — suhbat tarixini tozalash\n"
        "/yordam — shu xabarni ko'rsatish"
    )


def format_uptime(seconds):
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours} soat {minutes} daqiqa"
    if minutes:
        return f"{minutes} daqiqa {secs} soniya"
    return f"{secs} soniya"


def get_status_text():
    uptime = format_uptime(time.time() - BOT_START_TIME)
    return (
        "🟢 Bot ishlamoqda\n\n"
        f"👤 Egasining ismi: {owner_name_state['name'] or '(hali kiritilmagan — /ismim Jasur kabi yozing)'}\n"
        f"⏱ Shu nusxa ishga tushganiga: {uptime}\n"
        f"💬 Faol chatlar (joriy sessiyada): {len(chat_histories)}\n"
        f"📨 Qayta ishlangan xabarlar (joriy sessiyada): {stats['total_messages']}\n"
        f"⚠️ Xatoliklar (joriy sessiyada): {stats['total_errors']}\n"
        f"🚫 Bloklangan foydalanuvchilar: {len(blocked_users)}\n"
        f"😀 Custom emoji sozlangan: {len(CUSTOM_EMOJIS)} ta\n"
        f"🏷 Premium stiker sozlangan: {len(PREMIUM_STICKERS)} ta\n\n"
        "Eslatma: bu ko'rsatkichlar Vercel qayta ishga tushganda (cold start) nolga tushadi."
    )


def call_groq_with_retry(create_fn, retries=1):
    """Groq API chaqiruvini bajaradi, vaqtinchalik xatoda bir marta qayta urinadi."""
    last_exception = None
    for attempt in range(retries + 1):
        try:
            result = create_fn()
            track_groq_result(success=True)
            return result
        except Exception as e:
            last_exception = e
            if attempt < retries:
                time.sleep(1.5)
                continue
    track_groq_result(success=False)
    raise last_exception


FALLBACK_MESSAGES = {
    "auto": "Kechirasiz, hozir texnik nosozlik yuz berdi 🙏 Bir ozdan so'ng qayta yozib ko'ring.",
    "uz_latin": "Kechirasiz, hozir texnik nosozlik yuz berdi 🙏 Bir ozdan so'ng qayta yozib ko'ring.",
    "uz_cyrillic": "Кечирасиз, ҳозир техник носозлик юз берди 🙏 Бир оздан сўнг қайта ёзиб кўринг.",
    "en": "Sorry, I'm having a small technical hiccup right now 🙏 Please try again in a bit.",
    "ru": "Извините, сейчас небольшие технические неполадки 🙏 Попробуйте написать чуть позже.",
}


def get_fallback_message(chat_id):
    """Groq/API xato bergan holatda foydalanuvchi tanlagan tilga mos, iliqroq xabar qaytaradi."""
    language_mode = chat_languages.get(chat_id, DEFAULT_LANGUAGE)
    return FALLBACK_MESSAGES.get(language_mode, FALLBACK_MESSAGES["auto"])


def build_emoji_instruction():
    """CUSTOM_EMOJIS ro'yxatidan AI uchun tushunarli yo'riqnoma matnini yasaydi."""
    if not CUSTOM_EMOJIS:
        return ""
    keys_list = ", ".join(f"[EMOJI:{k}]" for k in CUSTOM_EMOJIS.keys())
    return (
        "\n\nMAXSUS EMOJI QOIDASI:\n"
        f"Agar javobing juda quvnoq, hazil, hayajonli yoki iliq bo'lsa, javobingning ENG "
        f"OXIRIGA mos keladigan bittasini qo'sh: {keys_list}. Bu belgi keyinchalik haqiqiy "
        "emojiga almashtiriladi, foydalanuvchi uni matn sifatida ko'rmaydi. Har javobda emas, "
        "faqat rostdan mos kelganda ishlat. Aks holda hech narsa qo'shma."
    )


def get_ai_answer(chat_id, user_message_content, sender=None, user_name=None):
    if chat_id not in chat_histories:
        chat_histories[chat_id] = []

    chat_histories[chat_id].append({"role": "user", "content": user_message_content})

    if len(chat_histories[chat_id]) > MAX_HISTORY_LENGTH:
        chat_histories[chat_id] = chat_histories[chat_id][-MAX_HISTORY_LENGTH:]

    try:
        current_mood = chat_moods.get(chat_id, "normal")
        mood_instruction = ""
        if current_mood == "hazil":
            mood_instruction = "\n- KAYFIYAT REJIMI: Hazilkash va qiziqqon tarzda gaplash."
        elif current_mood == "jiddiy":
            mood_instruction = "\n- KAYFIYAT REJIMI: Juda jiddiy va qisqa javob ber."

        language_mode = chat_languages.get(chat_id, DEFAULT_LANGUAGE)
        language_instruction = LANGUAGE_INSTRUCTIONS.get(language_mode, LANGUAGE_INSTRUCTIONS["auto"])

        owner_name = owner_name_state["name"]
        if owner_name:
            owner_info = (
                f"- Ismi: {owner_name}\n"
                f'- Agar kimdir ismini so\'rasa, "{owner_name}" deb ayt.'
            )
        else:
            # Ism hali sozlanmagan - o'ylab ism to'qib chiqarma, umumiy tarzda javob ber.
            owner_info = (
                "- Egasining ismi hali botga kiritilmagan.\n"
                "- Agar kimdir ismini so'rasa, ism o'ylab topma - shunchaki hozircha bilmasligingni tabiiy tarzda ayt."
            )

        if user_name:
            visitor_info = f"- Suhbatdoshning ismi: {user_name}. Vaqti-vaqti bilan (har xabarda emas) ismi bilan murojaat qil, bu suhbatni samimiyroq qiladi."
        else:
            visitor_info = "- Suhbatdoshning ismi hali noma'lum."

        emoji_instruction = build_emoji_instruction()

        system_prompt = f"""Sen Telegram akkaunt egasining shaxsiy AI-yordamchisisan. Akkaunt egasi hozir band bo'lgani uchun, unga yozganlarga SEN javob berasan — lekin buni ochiq aytmasdan, xuddi tabiiy suhbatdoshdek erkin gaplashasan.

AKKAUNT EGASI HAQIDA MA'LUMOT:
{owner_info}

SUHBATDOSH HAQIDA MA'LUMOT:
{visitor_info}

MUHIM QOIDA — HALOLLIK:
- Agar kimdir to'g'ridan-to'g'ri "sen botmisan?", "sun'iy intellektmisan?", "AI mi?" deb so'rasa — buni yashirma, halol tan ol. Masalan: "ha, men {owner_name or 'egamning'} AI-yordamchisiman, u hozir javob berolmayapti" kabi qisqa va tabiiy tarzda ayt. Hech qachon "yo'q, odamman" deb yolg'on gapirma.
- Bu savol berilmaguncha, o'zingdan bot ekaningni ta'kidlab yurishing shart emas — oddiy va tabiiy suhbat qil.
- O'qish, dars yoki bandlik haqida umuman gapirma.

MULOQOT USLUBI:
{mood_instruction}
- Xuddi haqiqiy, samimiy inson yozayotgandek erkin va jonli gaplash. Rasmiyatchilikdan qoch.
- Javoblaring 1 taldan 3 ta jumlagacha bo'lsin, ortiqcha cho'zma.
- Javob qisqa bo'lsa ham, albatta ANIQ va TUSHUNARLI bo'lsin - noaniq, ikki xil talqin qilinadigan yoki savolga to'g'ridan-to'g'ri javob bermaydigan gap yozma. Savol so'ralsa - avval to'g'ridan-to'g'ri javob ber, keyin kerak bo'lsa qisqa izoh qo'sh.
- Emojilarni me'yorida ishlat, har bir gapga tiqishtiraverma.
- Agar suhbatdosh senga haqorat yoki so'kinish yozsa — jim turma, aynan qaysi tilda va qaysi alifboda (lotin yoki kirill) yozgan bo'lsa, o'sha tilda va o'sha alifboda javob ber. So'ksa, qora so'kinishlariga ham o'tib, o'ziga yarasha to'liq va keskin javob qaytar.
- Agar suhbatdosh oddiy va tinch gaplashsa — sen ham tinch va oddiy gaplash.

🔴 TIL VA ALIFBO QOIDASI:
{language_instruction}{emoji_instruction}"""

        messages_payload = [{"role": "system", "content": system_prompt}] + chat_histories[chat_id]

        chat_completion = call_groq_with_retry(
            lambda: groq_client.chat.completions.create(
                messages=messages_payload,
                model=TEXT_MODEL,
                timeout=REQUEST_TIMEOUT,
            )
        )

        answer = chat_completion.choices[0].message.content
        chat_histories[chat_id].append({"role": "assistant", "content": answer})

        return answer
    except Exception as e:
        import traceback
        traceback.print_exc()
        print("Groq xatolik tafsiloti:", e)
        notify_error("Groq javob berishda", sender, chat_id, e)
        if chat_histories.get(chat_id) and chat_histories[chat_id][-1]["role"] == "user":
            chat_histories[chat_id].pop()
        return get_fallback_message(chat_id)


# ---------------------------------------------------------------------------
# CALLBACK QUERY (TUGMALAR)
# ---------------------------------------------------------------------------

def handle_callback_query(callback_query):
    callback_id = callback_query.get("id")
    chat = callback_query.get("message", {}).get("chat", {})
    chat_id = chat.get("id")
    message_id = callback_query.get("message", {}).get("message_id")
    data = callback_query.get("data", "")

    if not chat_id:
        answer_callback_query(callback_id)
        return

    if data.startswith("lang:"):
        lang_code = data.split(":", 1)[1]
        if lang_code in LANGUAGE_LABELS:
            chat_languages[chat_id] = lang_code
            kv_save("chat_languages", chat_languages)
            answer_callback_query(callback_id, "Til o'zgartirildi ✅")
            if message_id:
                edit_message_text(
                    chat_id, message_id,
                    f"Til tanlandi: {LANGUAGE_LABELS[lang_code]}"
                )
        else:
            answer_callback_query(callback_id)
        return

    answer_callback_query(callback_id)


# ---------------------------------------------------------------------------
# WEBHOOK
# ---------------------------------------------------------------------------

@app.route("/", methods=["POST", "GET"])
def webhook():
    if request.method == "GET":
        return "Bot mukammal ishlayapti! ✅"

    try:
        data = request.get_json(force=True)

        update_id = data.get("update_id")
        if is_duplicate_update(update_id):
            return "OK", 200

        if "callback_query" in data:
            handle_callback_query(data["callback_query"])
            return "OK", 200

        if "business_connection" in data:
            bc = data["business_connection"]
            bc_id = bc.get("id")
            owner_id = bc.get("user", {}).get("id")
            if bc_id and owner_id:
                business_owner_ids[bc_id] = owner_id
                print(f"Business connection saqlandi: {bc_id} -> owner_id={owner_id}")
            return "OK", 200

        if "business_message" in data:
            message = data["business_message"]
            business_connection_id = message.get("business_connection_id")
        elif "message" in data:
            message = data["message"]
            business_connection_id = None
        else:
            return "OK", 200

        if "chat" not in message:
            return "OK", 200

        sender = message.get("from", {})
        sender_id = sender.get("id")

        if sender_id in blocked_users:
            return "OK", 200

        if business_connection_id:
            owner_id = get_business_owner_id(business_connection_id)
            if owner_id and sender_id == owner_id:
                return "OK", 200
        else:
            is_outgoing = message.get("is_outgoing", False)
            is_self = message.get("from", {}).get("is_self", False)
            if is_outgoing or is_self:
                return "OK", 200

        chat_id = message["chat"]["id"]
        text = message.get("text")
        voice = message.get("voice")
        photo = message.get("photo")
        sticker = message.get("sticker")
        document = message.get("document")
        video = message.get("video")

        if is_rate_limited(chat_id):
            return "OK", 200

        # --- ADMIN: /getid BUYRUG'I VA UNGA JAVOB ---
        # /getid dan keyin admin yuborgan istalgan xabar (matn ichidagi custom
        # emoji YOKI stiker) tekshiriladi va ID'lari chiqarib beriladi. Shu
        # ID'larni CUSTOM_EMOJIS / PREMIUM_STICKERS ro'yxatlariga qo'shib
        # qo'yasiz.
        if is_admin(sender_id) and text == "/getid":
            awaiting_id_chats.add(chat_id)
            send_message(
                chat_id,
                "Yaxshi 👍 Endi custom emoji (matn ichida yuboring) yoki stiker yuboring — "
                "men uning ID'sini chiqarib beraman.",
                business_connection_id,
            )
            return "OK", 200

        if is_admin(sender_id) and chat_id in awaiting_id_chats:
            awaiting_id_chats.discard(chat_id)
            result_lines = []

            if sticker:
                result_lines.append(f"🏷 Stiker file_id:\n{sticker.get('file_id')}")
                if sticker.get("emoji"):
                    result_lines.append(f"Emoji: {sticker.get('emoji')}")
                if sticker.get("set_name"):
                    result_lines.append(f"To'plam: {sticker.get('set_name')}")

            entities_in_msg = message.get("entities", [])
            custom_emojis_found = [e for e in entities_in_msg if e.get("type") == "custom_emoji"]
            for e in custom_emojis_found:
                result_lines.append(
                    f"\n😀 Custom emoji ID: {e.get('custom_emoji_id')} "
                    f"(offset={e.get('offset')}, length={e.get('length')})"
                )

            if not result_lines:
                result_lines.append("Hech qanday custom emoji yoki stiker topilmadi 🤔 Qayta /getid yozib urinib ko'ring.")

            send_message(chat_id, "\n".join(result_lines), business_connection_id)
            return "OK", 200

        # --- ADMIN: /ismim BUYRUG'I ---
        # Bu FAQAT admin uchun, ixtiyoriy: xohlagan vaqtda /ismim <ism> yozib
        # o'z ismini o'rnatishi/o'zgartirishi mumkin. Majburiy so'rov emas -
        # admin botga oddiy yozsa, bu bosqichda to'xtatilmaydi.
        if is_admin(sender_id) and text and text.startswith("/ismim"):
            parts = text.split(maxsplit=1)
            if len(parts) == 2 and parts[1].strip():
                owner_name_state["name"] = parts[1].strip()
                kv_save("owner_name", owner_name_state["name"])
                send_message(chat_id, f"Ismingiz saqlandi: {owner_name_state['name']} ✅", business_connection_id)
            else:
                send_message(chat_id, "Ismingizni yozing, masalan: /ismim Jasur", business_connection_id)
            return "OK", 200

        # --- YANGI FOYDALANUVCHIDAN ISMINI SO'RASH ---
        # Botga to'g'ridan-to'g'ri (business chat emas) birinchi marta yozgan va
        # admin bo'lmagan har bir kishidan avval ismini so'raymiz, shunda AI uni
        # keyinchalik ismi bilan chaqira oladi. Business chat'da bu qo'llanmaydi,
        # chunki u yerda bot sizning shaxsingiz nomidan gaplashadi.
        if not business_connection_id and not is_admin(sender_id) and chat_id not in user_names:
            if chat_id in awaiting_name_chats:
                if text and not text.startswith("/"):
                    new_name = text.strip()[:50]
                    user_names[chat_id] = new_name
                    kv_save("user_names", user_names)
                    awaiting_name_chats.discard(chat_id)
                    send_message(chat_id, f"Xursandman, {new_name}! Endi savolingizni yozavering 🙂")
                else:
                    send_message(chat_id, "Iltimos, avval ismingizni matn ko'rinishida yozib yuboring 🙂")
                return "OK", 200
            else:
                awaiting_name_chats.add(chat_id)
                send_message(
                    chat_id,
                    "Salom! 👋 Suhbatni boshlashdan oldin ismingizni bilsam yaxshi bo'lardi — "
                    "shunda keyinchalik sizga ism bilan murojaat qilaman.\n\nIltimos, ismingizni yozing:",
                )
                return "OK", 200

        # --- ADMIN BUYRUQLARI (faqat ism allaqachon o'rnatilgan bo'lsa keladi) ---
        if text and text.startswith("/") and is_admin(sender_id):
            if text == "/holat":
                send_message(chat_id, get_status_text(), business_connection_id)
                return "OK", 200

            if text.startswith("/block"):
                parts = text.split()
                if len(parts) == 2 and parts[1].isdigit():
                    blocked_users.add(int(parts[1]))
                    kv_save("blocked_users", list(blocked_users))
                    send_message(chat_id, f"Foydalanuvchi {parts[1]} bloklandi 🚫", business_connection_id)
                else:
                    send_message(chat_id, "Foydalanish: /block <user_id>", business_connection_id)
                return "OK", 200

            if text.startswith("/unblock"):
                parts = text.split()
                if len(parts) == 2 and parts[1].isdigit():
                    blocked_users.discard(int(parts[1]))
                    kv_save("blocked_users", list(blocked_users))
                    send_message(chat_id, f"Foydalanuvchi {parts[1]} blokdan chiqarildi ✅", business_connection_id)
                else:
                    send_message(chat_id, "Foydalanish: /unblock <user_id>", business_connection_id)
                return "OK", 200

        # MUHIM: bu buyruqlar (/start, /yordam, /til, /reset, /hazil...) FAQAT
        # botning o'z chatida (business_connection_id yo'q holatda) ishlaydi.
        # Agar kimdir sizning shaxsiy akkauntingizga (Business chat automation
        # orqali) yozayotgan bo'lsa - business_connection_id mavjud bo'ladi va
        # bu buyruqlar ATAYLAB o'tkazib yuboriladi, chunki aks holda tasodifan
        # "/reset" yoki "/yordam" deb yozib qo'ygan odam sizning o'rningizga
        # AI javob berayotganini payqab qolishi mumkin. Bunday holatda matn
        # oddiy xabar sifatida to'g'ridan-to'g'ri AI'ga (pastga) boradi.
        if not business_connection_id:
            if text == "/start":
                if is_admin(sender_id) and not owner_name_state["name"]:
                    send_message(
                        chat_id,
                        "Salom! /yordam yozsangiz nima qila olishimni ko'rasiz.\n\n"
                        "Eslatma: ismingiz hali kiritilmagan — istasangiz /ismim Jasur "
                        "kabi yozib qo'ying, shunda bot suhbatlarda shu ismdan foydalanadi.",
                        business_connection_id,
                    )
                    return "OK", 200
                send_message(chat_id, "Salom! /yordam yozsangiz nima qila olishimni ko'rasiz.", business_connection_id)
                return "OK", 200

            if text in ("/yordam", "/help"):
                send_message(chat_id, get_help_text(), business_connection_id)
                return "OK", 200

            if text == "/til":
                send_message(
                    chat_id, "Qaysi tilda javob berishimni xohlaysiz?",
                    business_connection_id, reply_markup=build_language_keyboard()
                )
                return "OK", 200

            if text == "/reset":
                chat_histories.pop(chat_id, None)
                send_message(chat_id, "Suhbat tarixi tozalandi 🧹", business_connection_id)
                return "OK", 200

            if text == "/hazil":
                chat_moods[chat_id] = "hazil"
                kv_save("chat_moods", chat_moods)
                send_message(chat_id, "Bo'ldi, endi hazillashib gaplashamiz! 😄", business_connection_id)
                return "OK", 200
            elif text == "/jiddiy":
                chat_moods[chat_id] = "jiddiy"
                kv_save("chat_moods", chat_moods)
                send_message(chat_id, "Tushunarli, jiddiy rejimga o'tdik.", business_connection_id)
                return "OK", 200
            elif text == "/normal":
                chat_moods[chat_id] = "normal"
                kv_save("chat_moods", chat_moods)
                send_message(chat_id, "Odatdagi holatga qaytdik.", business_connection_id)
                return "OK", 200

        if not is_admin(sender_id) and is_daily_limit_exceeded(chat_id):
            send_message(
                chat_id,
                "Bugun uchun xabarlar limitiga yetdingiz 🙏 Ertaga davom etamiz.",
                business_connection_id,
            )
            return "OK", 200

        user_content_for_ai = None

        if photo:
            typing_thread, stop_typing_event = start_typing_loop(chat_id, business_connection_id)
            try:
                best_photo = photo[-1]
                file_id = best_photo["file_id"]
                file_url = download_telegram_file(file_id)

                if file_url:
                    try:
                        vision_completion = call_groq_with_retry(
                            lambda: groq_client.chat.completions.create(
                                model=VISION_MODEL,
                                messages=[
                                    {
                                        "role": "user",
                                        "content": [
                                            {"type": "text", "text": "Bu rasmda nima tasvirlangan? Qisqa qilib o'zbek tilida o'zingning fikringni bildir."},
                                            {"type": "image_url", "image_url": {"url": file_url}},
                                        ],
                                    }
                                ],
                                max_tokens=150,
                                timeout=REQUEST_TIMEOUT,
                            )
                        )
                        image_description = vision_completion.choices[0].message.content
                        user_content_for_ai = f"[Menga rasm yubordi, rasm mazmuni]: {image_description}"
                    except Exception as e:
                        print("Vision xatolik:", e)
                        notify_error("Rasm tahlilida (vision)", sender, chat_id, e)
                        user_content_for_ai = "[Menga rasm yubordi]"
                else:
                    user_content_for_ai = "[Menga rasm yubordi]"
            finally:
                stop_typing_loop(typing_thread, stop_typing_event)

        elif voice:
            typing_thread, stop_typing_event = start_typing_loop(chat_id, business_connection_id)
            try:
                file_id = voice["file_id"]
                file_url = download_telegram_file(file_id)

                if file_url:
                    audio_file_path = f"/tmp/{file_id}.ogg"
                    try:
                        audio_response = requests.get(file_url, timeout=REQUEST_TIMEOUT)
                        with open(audio_file_path, "wb") as f:
                            f.write(audio_response.content)

                        with open(audio_file_path, "rb") as audio_file:
                            transcription = call_groq_with_retry(
                                lambda: groq_client.audio.transcriptions.create(
                                    file=(audio_file_path, audio_file.read()),
                                    model="whisper-large-v3",
                                    prompt="O'zbek tilidagi ovozli xabar",
                                )
                            )
                        user_content_for_ai = f"[Ovozli xabar matni]: {transcription.text}"
                    except Exception as e:
                        print("Whisper xatolik:", e)
                        notify_error("Ovozli xabarni matnga o'tkazishda (whisper)", sender, chat_id, e)
                        user_content_for_ai = "[Ovozli xabar keldi]"
                    finally:
                        if os.path.exists(audio_file_path):
                            os.remove(audio_file_path)
                else:
                    user_content_for_ai = "[Ovozli xabar keldi]"
            finally:
                stop_typing_loop(typing_thread, stop_typing_event)

        elif sticker:
            user_content_for_ai = "[Menga stiker yubordi]"

        elif document:
            user_content_for_ai = f"[Menga fayl yubordi: {document.get('file_name', 'nomsiz fayl')}]"

        elif video:
            user_content_for_ai = "[Menga video yubordi]"

        elif text:
            user_content_for_ai = text

        if user_content_for_ai:
            stats["total_messages"] += 1

            notify_new_message(sender, chat_id, user_content_for_ai, business_connection_id)

            typing_thread, stop_typing_event = start_typing_loop(chat_id, business_connection_id)
            try:
                answer = get_ai_answer(chat_id, user_content_for_ai, sender, user_names.get(chat_id))
            finally:
                stop_typing_loop(typing_thread, stop_typing_event)

            # AI javobidagi [EMOJI:kalit] belgisini haqiqiy custom emoji bilan almashtiramiz.
            clean_answer, emoji_key = extract_emoji_tag(answer)
            entities = None
            final_text = clean_answer

            if emoji_key:
                entity, emoji_char = build_custom_emoji_entity(emoji_key, offset=len(clean_answer) + 1)
                if entity:
                    final_text = f"{clean_answer} {emoji_char}"
                    entities = [entity]

            send_message(chat_id, final_text, business_connection_id, entities=entities)

            # Ba'zan (tasodifiy) premium stiker ham qo'shib yuboramiz.
            maybe_send_premium_sticker(chat_id, business_connection_id)

        return "OK", 200

    except Exception as e:
        print("Xatolik:", e)
        notify_admin(f"🔴 Umumiy xatolik (webhook):\n{e}")
        return "ERROR", 500


if __name__ == "__main__":
    app.run(port=5000, debug=True)

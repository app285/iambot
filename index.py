import os
import time
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

# --- MODELLAR ---
TEXT_MODEL = "openai/gpt-oss-120b"
VISION_MODEL = "qwen/qwen3.6-27b"  # hozircha "preview" statusida - vaqti-vaqti bilan tekshirib turing

# Bot shu nusxa qachon ishga tushganini bilish uchun (cold start'da qayta o'rnatiladi)
BOT_START_TIME = time.time()

# Vercel uchun vaqtinchalik xotira (Dictionary)
chat_histories = {}
MAX_HISTORY_LENGTH = 10

# Kayfiyat rejimlari uchun xotira
chat_moods = {}

# Majburiy til rejimi: chat_id -> "auto" | "en" | "uz_latin" | "uz_cyrillic" | "ru"
chat_languages = {}
DEFAULT_LANGUAGE = "auto"

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
blocked_users = set()

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
owner_name_state = {"name": os.environ.get("OWNER_NAME", "").strip()}


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


def send_message(chat_id, text, business_connection_id=None, reply_markup=None):
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
            requests.post(f"{TELEGRAM_API_URL}/sendMessage", json=payload, timeout=REQUEST_TIMEOUT)
        except Exception as e:
            print("Telegramga yuborishda xatolik:", e)


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
        f"👤 Egasining ismi: {owner_name_state['name'] or '(hali kiritilmagan — /ismim <ism>)'}\n"
        f"⏱ Shu nusxa ishga tushganiga: {uptime}\n"
        f"💬 Faol chatlar (joriy sessiyada): {len(chat_histories)}\n"
        f"📨 Qayta ishlangan xabarlar (joriy sessiyada): {stats['total_messages']}\n"
        f"⚠️ Xatoliklar (joriy sessiyada): {stats['total_errors']}\n"
        f"🚫 Bloklangan foydalanuvchilar: {len(blocked_users)}\n\n"
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


def get_ai_answer(chat_id, user_message_content, sender=None):
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

        system_prompt = f"""Sen Telegram akkaunt egasining shaxsiy AI-yordamchisisan. Akkaunt egasi hozir band bo'lgani uchun, unga yozganlarga SEN javob berasan — lekin buni ochiq aytmasdan, xuddi tabiiy suhbatdoshdek erkin gaplashasan.

AKKAUNT EGASI HAQIDA MA'LUMOT:
{owner_info}

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
{language_instruction}"""

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
        return "Keyinroq yozvoraman."


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

        # --- ADMIN BUYRUQLARI ---
        if text and text.startswith("/") and is_admin(sender_id):
            if text == "/holat":
                send_message(chat_id, get_status_text(), business_connection_id)
                return "OK", 200

            if text.startswith("/ismim"):
                parts = text.split(maxsplit=1)
                if len(parts) == 2 and parts[1].strip():
                    owner_name_state["name"] = parts[1].strip()
                    send_message(
                        chat_id,
                        f"Ismingiz saqlandi: {owner_name_state['name']} ✅\n"
                        "Eslatma: bu Vercel qayta ishga tushganda (cold start) tiklanadi. "
                        "Doimiy saqlash uchun OWNER_NAME muhit o'zgaruvchisiga ham shu ismni yozib qo'ying.",
                        business_connection_id,
                    )
                else:
                    send_message(chat_id, "Foydalanish: /ismim <ismingiz>\nMasalan: /ismim Jasur", business_connection_id)
                return "OK", 200

            if text.startswith("/block"):
                parts = text.split()
                if len(parts) == 2 and parts[1].isdigit():
                    blocked_users.add(int(parts[1]))
                    send_message(chat_id, f"Foydalanuvchi {parts[1]} bloklandi 🚫", business_connection_id)
                else:
                    send_message(chat_id, "Foydalanish: /block <user_id>", business_connection_id)
                return "OK", 200

            if text.startswith("/unblock"):
                parts = text.split()
                if len(parts) == 2 and parts[1].isdigit():
                    blocked_users.discard(int(parts[1]))
                    send_message(chat_id, f"Foydalanuvchi {parts[1]} blokdan chiqarildi ✅", business_connection_id)
                else:
                    send_message(chat_id, "Foydalanish: /unblock <user_id>", business_connection_id)
                return "OK", 200

        if text == "/start":
            if is_admin(sender_id) and not owner_name_state["name"]:
                send_message(
                    chat_id,
                    "Salom! Botni ishga tushirishdan oldin ismingizni kiriting, chunki bot "
                    "sizga yozganlarga javob berganda o'zini tanishtirishda shu ismdan foydalanadi "
                    "(masalan: \"men Jasurning AI-yordamchisiman\").\n\n"
                    "Yozing: /ismim <ismingiz>\nMasalan: /ismim Jasur",
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
            send_message(chat_id, "Bo'ldi, endi hazillashib gaplashamiz! 😄", business_connection_id)
            return "OK", 200
        elif text == "/jiddiy":
            chat_moods[chat_id] = "jiddiy"
            send_message(chat_id, "Tushunarli, jiddiy rejimga o'tdik.", business_connection_id)
            return "OK", 200
        elif text == "/normal":
            chat_moods[chat_id] = "normal"
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
                answer = get_ai_answer(chat_id, user_content_for_ai, sender)
            finally:
                stop_typing_loop(typing_thread, stop_typing_event)

            send_message(chat_id, answer, business_connection_id)

        return "OK", 200

    except Exception as e:
        print("Xatolik:", e)
        notify_admin(f"🔴 Umumiy xatolik (webhook):\n{e}")
        return "ERROR", 500


if __name__ == "__main__":
    app.run(port=5000, debug=True)

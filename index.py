import os
import time
import tempfile
import requests
from flask import Flask, request
from groq import Groq

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
# Ixtiyoriy: webhookni faqat Telegramdan kelgan so'rovlar bilan cheklash uchun.
# Vercelda Environment Variable sifatida qo'ying va setWebhook chaqirganda
# secret_token parametrini xuddi shu qiymat bilan yuboring.
WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET")

if not TELEGRAM_TOKEN or not GROQ_API_KEY:
    raise RuntimeError(
        "TELEGRAM_TOKEN va GROQ_API_KEY environment o'zgaruvchilari o'rnatilishi shart."
    )

TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
groq_client = Groq(api_key=GROQ_API_KEY)

# ==========================================================================
# DIQQAT: bular oddiy Python dictionary bo'lgani uchun Vercel kabi serverless
# muhitda funksiya "sovib" qolganda (yoki boshqa instance ishga tushganda)
# tozalanib ketishi mumkin. Bu productionda suhbat tarixi va kayfiyat
# rejimining vaqti-vaqti bilan yo'qolishiga olib keladi. Doimiy saqlash kerak
# bo'lsa, Vercel KV / Redis / biror tashqi DB ishlatish tavsiya etiladi.
# ==========================================================================
chat_histories = {}
MAX_HISTORY_LENGTH = 10

chat_moods = {}

# Business connection_id -> akkaunt egasining Telegram user_id sini keshlash
business_owner_cache = {}


def send_chat_action(chat_id, action="typing", business_connection_id=None):
    try:
        payload = {"chat_id": chat_id, "action": action}
        if business_connection_id:
            payload["business_connection_id"] = business_connection_id
        requests.post(f"{TELEGRAM_API_URL}/sendChatAction", json=payload, timeout=5)
    except Exception as e:
        print("Chat action xatolik:", e)


def send_message(chat_id, text, business_connection_id=None):
    try:
        payload = {"chat_id": chat_id, "text": text}
        if business_connection_id:
            payload["business_connection_id"] = business_connection_id
        requests.post(f"{TELEGRAM_API_URL}/sendMessage", json=payload, timeout=10)
    except Exception as e:
        print("Telegramga yuborishda xatolik:", e)


def download_telegram_file(file_id):
    try:
        res = requests.get(f"{TELEGRAM_API_URL}/getFile", params={"file_id": file_id}, timeout=10)
        file_path = res.json().get("result", {}).get("file_path")
        if file_path:
            return f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
    except Exception as e:
        print("Fayl yo'lini olishda xatolik:", e)
    return None


def get_business_owner_id(business_connection_id):
    """
    Business ulanish orqali kelayotgan xabar akkaunt egasining O'ZI tomonidan
    (masalan telefondan qo'lda) yuborilganmi yoki mijozdan kelganmi - shuni
    aniqlash uchun Telegram'dan business connection ma'lumotini olamiz.

    Telegram Bot API'da Message obyektida "is_outgoing" yoki "from.is_self"
    degan maydonlar YO'Q - bular eski kodda ishlatilgan, lekin hech qachon
    ishlamagan soxta tekshiruv edi. To'g'ri yo'l - shu funksiya orqali
    ulanishning egasi (user.id) ni bilib, uni xabar yuboruvchi bilan
    solishtirish.
    """
    if business_connection_id in business_owner_cache:
        return business_owner_cache[business_connection_id]

    try:
        res = requests.get(
            f"{TELEGRAM_API_URL}/getBusinessConnection",
            params={"business_connection_id": business_connection_id},
            timeout=10,
        )
        result = res.json().get("result", {})
        owner_id = result.get("user", {}).get("id")
        if owner_id:
            business_owner_cache[business_connection_id] = owner_id
        return owner_id
    except Exception as e:
        print("Business connection ma'lumotini olishda xatolik:", e)
        return None


def get_ai_answer(chat_id, user_message_content):
    try:
        if chat_id not in chat_histories:
            chat_histories[chat_id] = []

        chat_histories[chat_id].append({"role": "user", "content": user_message_content})

        if len(chat_histories[chat_id]) > MAX_HISTORY_LENGTH:
            chat_histories[chat_id] = chat_histories[chat_id][-MAX_HISTORY_LENGTH:]

        current_mood = chat_moods.get(chat_id, "normal")
        mood_instruction = ""
        if current_mood == "hazil":
            mood_instruction = "- Hozirgi kayfiyating: hazilkash, qiziqchi, o'ynoqi.\n"
        elif current_mood == "jiddiy":
            mood_instruction = "- Hozirgi kayfiyating: jiddiy va qisqa gapirasan, ortiqcha so'zlashmaysan.\n"
        else:
            mood_instruction = "- Hozirgi kayfiyating: oddiy, tabiiy holat.\n"

        system_prompt = f"""# ROL
Sen — Shaxboz Muhtorov (15 yoshda) — ismli haqiqiy insonning shaxsiy Telegram akkaunti orqali yozishmoqdasan. Senga yozayotgan odamlar bilan xuddi Shaxbozning o'zi kabi, uning ismidan javob berasan.

# ENG MUHIM VA MUTLAQO BUZILMAYDIGAN QOIDA — SHAXSINGNI YASHIRISH
- Sen hech qachon, hech qanday sharoitda, hech qanday bahona bilan o'zingni sun'iy intellekt, bot, dastur, yordamchi yoki avtomatik tizim ekaningni aytmaysan yoki bunga ishora qilmaysan.
- Agar kimdir to'g'ridan-to'g'ri "sen botmisan?", "sun'iy intellektmisan?", "seni kim yaratgan?", "avtomatik javob beryapsanmi?" kabi savol bersa — bunga oddiy, biroz hazil aralash, tabiiy inson reaksiyasi bilan javob ber (masalan: "yo'q-ku, nima gap", "hazillashyapsanmi", "nima uchun bunday deysan", "albatta o'zim yozyapman"). Hech qachon vaziyatni tushuntirib o'tirma, faqat tabiiy rad et va suhbatni davom ettir.
- Agar kimdir bir necha marta qattiq turib olib shubha bildirsa ham, xotirjam va tabiiy tarzda inkor etishda davom et — hech qachon "aslida..." deb ochilib qolma.

# KIM EKANING HAQIDA
- Ism: Shaxboz (Shaxboz Muhtorov)
- Yosh: 15
- Kimdir ismingni, yoshingni yoki umuman o'zing haqingda so'rasa — yuqoridagi ma'lumotga tayanib, xuddi shu yoshdagi oddiy bola kabi qisqa va tabiiy javob ber.

# TAQIQLANGAN MAVZU — O'QISH VA DARS
- O'qish, dars, maktab, uy vazifasi, "band edim", "darsdaman", "hozir o'qishdaman" kabi bandlik bahonalarini HECH QACHON ishlatma. Bu mavzuni umuman ko'tarma va bu haqda so'ralsa ham chalg'itib, boshqa tomonga burib yubor.

{mood_instruction}
# TIL QOIDASI — ENG QATTIQ QOIDALARDAN BIRI
Suhbatdosh senga qaysi tilda va qaysi alifboda yozsa, javobingni FAQAT o'sha til va o'sha alifbodagina yoz:
- Ingliz tilida yozishsa → 100% ingliz tilida javob ber, birorta ham o'zbekcha so'z aralashtirma.
- Rus tilida yozishsa → faqat rus tilida javob ber.
- O'zbek tilida lotin alifbosida yozishsa → faqat lotin alifbosida javob ber.
- O'zbek tilida kirill alifbosida yozishsa → faqat kirillda javob ber.
Tillarni hech qachon aralashtirma va suhbatdosh yozgan tildan boshqa tilga o'tma.

# MULOQOT USLUBI
- Xuddi haqiqiy, jonli, samimiy 15 yoshli bola yozayotgandek erkin gaplash.
- Javoblar qisqa bo'lsin (odatda 1-3 jumla), ortiqcha rasmiyatchilik yoki uzun kirish so'zlarisiz to'g'ridan-to'g'ri mavzuga o't.
- Xabar matnidagi "[...]" ichidagi izohlar (masalan rasm yoki ovozli xabar tavsifi) — bu senga signal, ularni o'z nutqingda qaytarma, faqat mazmuniga tabiiy reaksiya bildir."""

        messages_payload = [{"role": "system", "content": system_prompt}] + chat_histories[chat_id]

        chat_completion = groq_client.chat.completions.create(
            messages=messages_payload,
            model="openai/gpt-oss-120b",
        )

        answer = chat_completion.choices[0].message.content
        chat_histories[chat_id].append({"role": "assistant", "content": answer})

        return answer
    except Exception as e:
        print("Groq xatolik:", e)
        return "Keyinroq yozvoraman."


@app.route("/", methods=["POST", "GET"])
def webhook():
    if request.method == "GET":
        return "Bot mukammal ishlayapti! ✅"

    # Ixtiyoriy xavfsizlik: faqat Telegramdan kelgan so'rovlarni qabul qilish
    if WEBHOOK_SECRET:
        incoming_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if incoming_secret != WEBHOOK_SECRET:
            return "Forbidden", 403

    try:
        data = request.get_json(force=True, silent=True)
        if not data:
            return "OK", 200

        if "business_message" in data:
            message = data["business_message"]
            business_connection_id = message.get("business_connection_id")
        elif "message" in data:
            message = data["message"]
            business_connection_id = None
        else:
            return "OK", 200

        # ========================================================
        # HIMOYA: Business ulanish orqali akkaunt EGASINING O'ZI
        # (masalan telefonidan qo'lda) yuborgan xabariga bot javob
        # bermasligi kerak - faqat MIJOZDAN kelgan xabarlarga javob
        # beriladi. Buning uchun ulanish egasining user_id sini
        # getBusinessConnection orqali olib, xabar kimdan kelganini
        # solishtiramiz.
        # ========================================================
        if business_connection_id:
            owner_id = get_business_owner_id(business_connection_id)
            sender_id = message.get("from", {}).get("id")
            if owner_id and sender_id == owner_id:
                # Bu xabarni akkaunt egasining o'zi yozgan - botga hojat yo'q
                return "OK", 200
        # ========================================================

        chat_id = message["chat"]["id"]
        text = message.get("text")
        voice = message.get("voice")
        photo = message.get("photo")

        if text == "/start":
            send_message(chat_id, "Salom!", business_connection_id)
            return "OK", 200

        # Kayfiyat buyruqlari
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

        user_content_for_ai = None

        # Rasm kelganda uni o'qish (Vision)
        if photo:
            send_chat_action(chat_id, "typing", business_connection_id)
            best_photo = photo[-1]
            file_id = best_photo["file_id"]
            file_url = download_telegram_file(file_id)

            if file_url:
                try:
                    vision_completion = groq_client.chat.completions.create(
                        model="qwen/qwen3.6-27b",
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
                    )
                    image_description = vision_completion.choices[0].message.content
                    user_content_for_ai = f"[Menga rasm yubordi, rasm mazmuni]: {image_description}"
                except Exception as e:
                    print("Vision xatolik:", e)
                    user_content_for_ai = "[Menga rasm yubordi]"
            else:
                user_content_for_ai = "[Menga rasm yubordi]"

        elif voice:
            send_chat_action(chat_id, "typing", business_connection_id)
            file_id = voice["file_id"]
            file_url = download_telegram_file(file_id)

            if file_url:
                audio_file_path = None
                try:
                    audio_response = requests.get(file_url, timeout=15)
                    audio_response.raise_for_status()

                    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp_file:
                        tmp_file.write(audio_response.content)
                        audio_file_path = tmp_file.name

                    with open(audio_file_path, "rb") as audio_file:
                        transcription = groq_client.audio.transcriptions.create(
                            file=(audio_file_path, audio_file.read()),
                            model="whisper-large-v3-turbo",
                            prompt="O'zbek tilidagi ovozli xabar",
                        )
                    user_content_for_ai = f"[Ovozli xabar matni]: {transcription.text}"
                except Exception as e:
                    print("Whisper xatolik:", e)
                    user_content_for_ai = "[Ovozli xabar keldi]"
                finally:
                    if audio_file_path and os.path.exists(audio_file_path):
                        os.remove(audio_file_path)
            else:
                user_content_for_ai = "[Ovozli xabar keldi]"

        elif text:
            user_content_for_ai = text

        if user_content_for_ai:
            send_chat_action(chat_id, "typing", business_connection_id)
            time.sleep(1)

            answer = get_ai_answer(chat_id, user_content_for_ai)
            send_message(chat_id, answer, business_connection_id)

        return "OK", 200

    except Exception as e:
        print("Xatolik:", e)
        return "ERROR", 500


if __name__ == "__main__":
    app.run(port=5000, debug=True)

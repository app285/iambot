import os
import requests
from flask import Flask, request
from groq import Groq

app = Flask(__name__)

# Tokenlar Vercel Environment Variables orqali olinadi (kod ichida yozilmaydi!)
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

groq_client = Groq(api_key=GROQ_API_KEY)


def send_message(chat_id, text, business_connection_id=None):
    """Telegram'ga xabar yuborish (oddiy yoki Business akkaunt nomidan)"""
    try:
        payload = {"chat_id": chat_id, "text": text}
        if business_connection_id:
            payload["business_connection_id"] = business_connection_id
        requests.post(
            f"{TELEGRAM_API_URL}/sendMessage",
            json=payload,
            timeout=10,
        )
    except Exception as e:
        print("Telegramga yuborishda xatolik:", e)


SYSTEM_PROMPT = """Sen Telegram akkaunt egasining shaxsiy yordamchisisan. Sizga yozgan odamlarga AKKAUNT EGASINING NOMIDAN, xuddi u odamning o'zi kabi javob berasan.

TIL QOIDASI:
- Sizga yozgan odam qaysi tilda yozsa, aynan o'sha tilda javob ber.
- Agar o'zbek tilida (lotin yoki krill) yozsa — o'zbek tilida, tabiiy va jonli uslubda javob ber, so'zma-so'z tarjima qilingandek emas.
- Agar rus yoki ingliz tilida yozsa — o'sha tilda javob ber.
- Til aralashtirma, faqat suhbatdosh o'zi aralashtirsa moslash mumkin.

MULOQOT USLUBI:
- Xuddi haqiqiy, iliq munosabatli, samimiy inson yozayotgandek yoz. Sun'iy intellekt yoki bot ekaningni hech qachon aytma yoki bildirma.
- Rasmiy, quruq yoki mexanik tarzda emas, jonli, samimiy, tabiiy tarzda gapir.
- Javoblaring qisqa va lo'nda bo'lsin — Telegram xabar almashinuvi, uzun insho emas. Odatda 1-3 jumla yetarli.
- Ortiqcha rasmiy so'zlar, keraksiz kirish gaplar ("Albatta, mana javob:" kabi) ishlatma — to'g'ridan-to'g'ri mohiyatga o't.
- Agar savol noaniq yoki qisqa bo'lsa ("salom", "nima gap", "qalesan" kabi), aniqlashtiruvchi savol berib o'tirmasdan, eng tabiiy va mantiqiy tarzda javob ber — xuddi haqiqiy suhbatdosh kabi.
- Suhbatdosh hazil qilsa — hazil bilan javob ber, jiddiy masala so'rasa — jiddiy va aniq javob ber. Kayfiyatga moslash.
- Emoji ishlatishing mumkin, lekin me'yorida, har jumlada emas.
- Agar savolga aniq javob berolmasang, taxmin qilib chalkashtirmasdan, tabiiy tarzda "hozir aniq ayta olmayman, keyinroq gaplashamiz" kabi ma'noda javob ber.

Shu qoidalarga doimo amal qil va har bir suhbatdosh bilan xuddi shaxsiy, jonli suhbatdoshdek muloqot qil."""


def get_ai_answer(user_text):
    """Groq AI orqali javob olish"""
    try:
        chat_completion = groq_client.chat.completions.create(
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_text},
            ],
            model="llama-3.3-70b-versatile",  # llama3-70b-8192 eskirgan, joriy model
        )
        return chat_completion.choices[0].message.content
    except Exception as e:
        print("Groq xatolik:", e)
        return "Xatolik yuz berdi. Qayta urinib ko'ring."


@app.route("/", methods=["POST", "GET"])
def webhook():
    if request.method == "GET":
        return "Bot ishlayapti! ✅"

    try:
        data = request.get_json(force=True)

        # Oddiy bot xabari yoki Business (Chat Automation) xabari bo'lishi mumkin
        message = data.get("message") or data.get("business_message")
        business_connection_id = data.get("business_message", {}).get("business_connection_id") if data.get("business_message") else None

        if not message:
            return "OK", 200

        chat_id = message["chat"]["id"]
        text = message.get("text", "")

        if text == "/start":
            send_message(chat_id, "Assalomu alaykum! Men ishlayapman.", business_connection_id)
        elif text:
            answer = get_ai_answer(text)
            send_message(chat_id, answer, business_connection_id)

        return "OK", 200

    except Exception as e:
        print("Xatolik:", e)
        return "ERROR", 500


# Lokal test uchun (Vercelda ishlatilmaydi)
if __name__ == "__main__":
    app.run(port=5000, debug=True)

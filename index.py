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


def send_message(chat_id, text):
    """Telegram'ga xabar yuborish (oddiy HTTP so'rov orqali)"""
    try:
        requests.post(
            f"{TELEGRAM_API_URL}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
    except Exception as e:
        print("Telegramga yuborishda xatolik:", e)


def get_ai_answer(user_text):
    """Groq AI orqali javob olish"""
    try:
        chat_completion = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": user_text}],
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
        message = data.get("message")

        if not message:
            return "OK", 200

        chat_id = message["chat"]["id"]
        text = message.get("text", "")

        if text == "/start":
            send_message(chat_id, "Assalomu alaykum! Men ishlayapman.")
        elif text:
            answer = get_ai_answer(text)
            send_message(chat_id, answer)

        return "OK", 200

    except Exception as e:
        print("Xatolik:", e)
        return "ERROR", 500


# Lokal test uchun (Vercelda ishlatilmaydi)
if __name__ == "__main__":
    app.run(port=5000, debug=True)

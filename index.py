import os
import time
import requests
from flask import Flask, request
from groq import Groq

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
groq_client = Groq(api_key=GROQ_API_KEY)

# Vercel uchun xotira (Oddiy Python Dictionary)
chat_histories = {}
MAX_HISTORY_LENGTH = 10


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
        res = requests.get(f"{TELEGRAM_API_URL}/getFile?file_id={file_id}", timeout=10)
        file_path = res.json().get("result", {}).get("file_path")
        if file_path:
            return f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
    except Exception as e:
        print("Fayl yo'lini olishda xatolik:", e)
    return None


def get_current_status_prompt():
    import datetime
    now = datetime.datetime.now()
    hour = now.hour

    if hour >= 23 or hour < 7:
        return "Hozir kechasi vaqt, akkaunt egasi uxlab yotibdi. Agar yozishsa, qisqa qilib 'uxlayapman, ertalab gaplashamiz' degan ma'noda tabiiy javob ber."
    elif 9 <= hour <= 14:
        return "Hozir kunduzgi o'qish/dars vaqti. Akkaunt egasi band bo'lishi mumkin, shuning uchun biroz qisqaroq va kechroq javob yozishini bildiradigan tarzda muloqot qil."
    else:
        return "Hozir bo'sh vaqti, odatdagidek samimiy va erkin muloqot qil."


def get_ai_answer(chat_id, user_message_content):
    try:
        if chat_id not in chat_histories:
            chat_histories[chat_id] = []

        chat_histories[chat_id].append({"role": "user", "content": user_message_content})

        if len(chat_histories[chat_id]) > MAX_HISTORY_LENGTH:
            chat_histories[chat_id] = chat_histories[chat_id][-MAX_HISTORY_LENGTH:]

        dynamic_status = get_current_status_prompt()

        system_prompt = f"""Sen Telegram akkaunt egasining shaxsiy yordamchisisan. Sizga yozgan odamlarga AKKAUNT EGASINING NOMIDAN, xuddi o'sha odamning o'zi kabi javob berasan.

AKKAUNT EGASI HAQIDA MA'LUMOT (SEN SHU INSON SAN):
- Isming: Shaxboz (Shaxboz Muhtorov)
- Yoshing: 15 yoshda
- Shug'ullanishing: Najot Ta'limda Web Full-Stack dasturlashni o'rganayotgan talabasan.
- Agar kimdir ismingni, yoshingni yoki nima ish qilishingni so'rasa, yuqoridagi ma'lumotlarga tayanib xuddi o'zingdek oddiy va tabiiy javob ber.

JORIY HOLAT QOIDASI:
- {dynamic_status}

ENG MUHIM QOIDA (HECH QACHON BUZMA):
- Sen SUN'IY INTELLEKT, BOT, YORDAMCHI, DASTUR EKANINGNI HECH QACHON, HECH QANDAY SHAROITDA aytmaysan yoki bildirmaysan.
- Agar kimdir "sen botmisan?", "sun'iy intellektmisan?", "kim yaratgan seni?" desa, ODDIY INSON sifatida tabiiy rad et (masalan: "yo'q, nima derding", "hazillashyapsanmi", "albatta odamman").

TIL VA ALIFBO QOIDASI:
- Suhbatdosh qaysi tilda yozsa, aynan o'sha tilda javob ber.
- Lotincha yozsa — lotincha, kirillcha yozsa — kirillcha javob qaytar.

MULOQOT USLUBI:
- Xuddi haqiqiy, samimiy inson yozayotgandek erkin va jonli gaplash (1-3 jumla).
- Rasmiyatchilikdan qoch, ortiqcha kirish so'zlarsiz to'g'ridan-to'g'ri mavzuga o't."""

        messages_payload = [{"role": "system", "content": system_prompt}] + chat_histories[chat_id]

        chat_completion = groq_client.chat.completions.create(
            messages=messages_payload,
            model="llama-3.3-70b-versatile",
        )
        
        answer = chat_completion.choices[0].message.content
        chat_histories[chat_id].append({"role": "assistant", "content": answer})

        return answer
    except Exception as e:
        print("Groq xatolik:", e)
        return "Keyinroq yozvoraman, ozgina bandman."


@app.route("/", methods=["POST", "GET"])
def webhook():
    if request.method == "GET":
        return "Bot Vercelda mukammal ishlayapti! ✅"

    try:
        data = request.get_json(force=True)

        if "business_message" in data:
            message = data["business_message"]
            business_connection_id = message.get("business_connection_id")
        elif "message" in data:
            message = data["message"]
            business_connection_id = None
        else:
            return "OK", 200

        if message.get("from", {}).get("is_self", False):
            return "OK", 200

        chat_id = message["chat"]["id"]
        text = message.get("text")
        voice = message.get("voice")

        if text == "/start":
            send_message(chat_id, "Salom! Keyinroq yozaman, hozir ozgina band edim.", business_connection_id)
            return "OK", 200

        user_content_for_ai = None

        if voice:
            send_chat_action(chat_id, "typing", business_connection_id)
            file_id = voice["file_id"]
            file_url = download_telegram_file(file_id)
            
            if file_url:
                try:
                    audio_response = requests.get(file_url)
                    audio_file_path = f"/tmp/{file_id}.ogg"
                    with open(audio_file_path, "wb") as f:
                        f.write(audio_response.content)

                    with open(audio_file_path, "rb") as audio_file:
                        transcription = groq_client.audio.transcriptions.create(
                            file=(audio_file_path, audio_file.read()),
                            model="whisper-large-v3",
                            prompt="O'zbek tilidagi ovozli xabar",
                        )
                    user_content_for_ai = f"[Ovozli xabar matni]: {transcription.text}"
                    os.remove(audio_file_path)
                except Exception as e:
                    print("Whisper xatolik:", e)
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

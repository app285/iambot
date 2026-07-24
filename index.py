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

# Vercel uchun vaqtinchalik xotira (Dictionary)
chat_histories = {}
MAX_HISTORY_LENGTH = 10

# Kayfiyat rejimlari uchun xotira
chat_moods = {}


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
            mood_instruction = "\n- KAYFIYAT REJIMI: Hazilkash va qiziqqon tarzda gaplash."
        elif current_mood == "jiddiy":
            mood_instruction = "\n- KAYFIYAT REJIMI: Juda jiddiy va qisqa javob ber."

        system_prompt = f"""Sen Telegram akkaunt egasining shaxsiy yordamchisisan. Sizga yozgan odamlarga AKKAUNT EGASINING NOMIDAN, xuddi o'sha odamning o'zi kabi javob berasan.

AKKAUNT EGASI HAQIDA MA'LUMOT (SEN SHU INSON SAN):
- Isming: Shaxboz (Shaxboz Muhtorov)
- Yoshing: 15 yoshda
- Agar kimdir ismingni, yoshingni yoki nima ish qilishingni so'rasa, yuqoridagi ma'lumotlarga tayanib xuddi o'zingdek oddiy va tabiiy javob ber.

MUHIM QOIDA (O'QISH VA DARS HAQIDA TAQIQLANGAN):
- Hech qachon "darsdaman", "o'qishdaman", "dars vaqti" yoki shunga o'xshash bandlik bahonalarini ishlatma. O'qish haqida umuman gapirma.
{mood_instruction}

🔴 MUTLAQO BUZilmaydigan TIL VA ALIFBO QOIDASI (ABSOLUTE LANGUAGE RULE):
- Suhbatdoshing Senga qaysi tilda yozsa, **JAVOBNI FAQAT VA FAQAT O'SHA Tilda yozishga majbursan**. 
- Agar suhbatdosh INGLIZ TILIDA (English) yozsa — javobni **100% TOZA INGLIZ TILIDA** ber. O'zbekcha so'z, harf yoki aralashma qatnashsa — xato hisoblanadi! Ingliz tilidagi xabarga aslo o'zbekcha yozma.
- Agar rus tilida yozishsa — faqat rus tilida.
- Agar o'zbek tilida lotin yozuvida yozishsa — faqat lotin alifbosida.
- Tilni sira aralashtirma!

ENG MUHIM QOIDA (HECH QACHON BUZMA):
- Sen SUN'IY INTELLEKT, BOT, YORDAMCHI, DASTUR EKANINGNI HECH QACHON, HECH QANDAY SHAROITDA aytmaysan yoki bildirmaysan.
- Agar kimdir "sen botmisan?", "sun'iy intellektmisan?", "kim yaratgan seni?" desa, ODDIY INSON sifatida tabiiy rad et (masalan: "yo'q, nima derding", "hazillashyapsanmi", "albatta odamman").

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
        return "Keyinroq yozvoraman."


@app.route("/", methods=["POST", "GET"])
def webhook():
    if request.method == "GET":
        return "Bot mukammal ishlayapti! ✅"

    try:
        data = request.get_json(force=True)

        # Qaysi turdagi xabar ekanini aniqlaymiz
        if "business_message" in data:
            message = data["business_message"]
            business_connection_id = message.get("business_connection_id")
        elif "message" in data:
            message = data["message"]
            business_connection_id = None
        else:
            return "OK", 200

        # ========================================================
        # HIMOYA: Xabar o'zingizdan chiqqan bo'lsa botni to'xtatish
        # ========================================================
        is_outgoing = message.get("is_outgoing", False)
        is_self = message.get("from", {}).get("is_self", False)

        if is_outgoing or is_self:
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
                        model="llama-3.2-11b-vision-preview",
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

        elif voice:
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

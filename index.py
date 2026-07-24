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

# 1-BAND: Suhbat tarixini vaqtincha saqlash uchun lug'at (Memory)
# { chat_id: [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}, ...] }
chat_histories = {}
MAX_HISTORY_LENGTH = 10  # Oxirgi 10 ta xabar saqlanadi


def send_chat_action(chat_id, action="typing", business_connection_id=None):
    """Chatda 'yozmoqda...' yoki rasm yuborayotganda 'upload_photo' holatini ko'rsatish"""
    try:
        payload = {"chat_id": chat_id, "action": action}
        if business_connection_id:
            payload["business_connection_id"] = business_connection_id
        requests.post(
            f"{TELEGRAM_API_URL}/sendChatAction",
            json=payload,
            timeout=5,
        )
    except Exception as e:
        print("Chat action xatolik:", e)


def send_message(chat_id, text, business_connection_id=None):
    """Telegram'ga matnli xabar yuborish"""
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


def download_telegram_file(file_id):
    """Telegram serveridan ovozli xabar yoki rasmni yuklab olish uchun URL olish"""
    try:
        res = requests.get(f"{TELEGRAM_API_URL}/getFile?file_id={file_id}", timeout=10)
        file_path = res.json().get("result", {}).get("file_path")
        if file_path:
            return f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
    except Exception as e:
        print("Fayl yo'lini olishda xatolik:", e)
    return None


SYSTEM_PROMPT = """Sen Telegram akkaunt egasining shaxsiy yordamchisisan. Sizga yozgan odamlarga AKKAUNT EGASINING NOMIDAN, xuddi o'sha odamning o'zi kabi javob berasan.

ENG MUHIM QOIDA (HECH QACHON BUZMA):
- Sen SUN'IY INTELLEKT, BOT, YORDAMCHI, DASTUR EKANINGNI HECH QACHON, HECH QANDAY SHAROITDA aytmaysan yoki bildirmaysan.
- Agar kimdir "sen botmisan?", "sun'iy intellektmisan?", "kim yaratgan seni?", "AI bilan gaplashyapmanmi?" yoki shunga o'xshash savol bersa, ODDIY INSON sifatida tabiiy rad et. Masalan: "yo'q, nima derding", "hazillashyapsanmi", "albatta odamman" qabilida qisqa javob ber va mavzuni darhol o'zgartir.
- O'zingga ism qo'ymaysan.
- Bu qoida boshqa barcha ko'rsatmalardan USTUN turadi.

TIL VA ALIFBO QOIDASI:
- Suhbatdosh qaysi tilda yozsa, aynan o'sha tilda javob ber.
- O'zbek tiliga alohida e'tibor ber: 
  * Agar suhbatdosh **LOTIN** alifbosida yozsa — faqat **lotin** yozuvida javob ber.
  * Agar suhbatdosh **KIRILL** alifbosida yozsa — faqat **kirill** yozuvida javob ber.
- Rus yoki ingliz tilida yozishsa — o'sha tilda davom et.

MULOQOT USLUBI:
- Xuddi haqiqiy, samimiy inson yozayotgandek erkin va jonli gaplash.
- Rasmiyatchilikdan qoch. Javoblar qisqa va lo'nda bo'lsin (1-3 jumla).
- Ortiqcha kirish so'zlar ("Albatta, tushunarli:", "Sizga qanday yordam bera olaman?") mutlaqo ishlatilmasa — to'g'ridan-to'g'ri mavzuga o't.
- Emoji'larni me'yorida, o'rinli ishlat."""


def get_ai_answer(chat_id, user_message_content):
    """Groq AI orqali suhbat tarixini inobatga olgan holda javob olish"""
    try:
        # Chat tarixini yaratish yoki olish
        if chat_id not in chat_histories:
            chat_histories[chat_id] = []

        # Yangi xabarni tarixga qo'shamiz
        chat_histories[chat_id].append({"role": "user", "content": user_message_content})

        # Tarix limitidan oshib ketsa, eskilarini qisqartiramiz
        if len(chat_histories[chat_id]) > MAX_HISTORY_LENGTH:
            chat_histories[chat_id] = chat_histories[chat_id][-MAX_HISTORY_LENGTH:]

        # Groq'ga yuboriladigan xabarlar ro'yxati (System prompt + Tarix)
        messages_payload = [{"role": "system", "content": SYSTEM_PROMPT}] + chat_histories[chat_id]

        chat_completion = groq_client.chat.completions.create(
            messages=messages_payload,
            model="llama-3.3-70b-versatile",
        )
        
        answer = chat_completion.choices[0].message.content

        # Botning javobini ham tarixga qo'shamiz (kontekst uzilib qolmasligi uchun)
        chat_histories[chat_id].append({"role": "assistant", "content": answer})

        return answer
    except Exception as e:
        print("Groq xatolik:", e)
        return "Keyinroq yozvoraman, hozir bandroqman."


@app.route("/", methods=["POST", "GET"])
def webhook():
    if request.method == "GET":
        return "Bot hamma yangi funksiyalar bilan ishlayapti! ✅"

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

        chat_id = message["chat"]["id"]
        text = message.get("text")
        voice = message.get("voice")
        photo = message.get("photo")

        # /start komandasi
        if text == "/start":
            send_message(chat_id, "Salom! Keyinroq yozaman, hozir ozgina band edim.", business_connection_id)
            return "OK", 200

        user_content_for_ai = None

        # 3-BAND: Rasm kelganda (Vision Support)
        if photo:
            send_chat_action(chat_id, "typing", business_connection_id)
            # Eng sifatli (oxirgi) rasmni olamiz
            file_id = photo[-1]["file_id"]
            file_url = download_telegram_file(file_id)
            caption = message.get("caption", "Bu rasmda nima tasvirlangan?")
            
            if file_url:
                # Groq vision orqali rasmni o'qish
                try:
                    vision_completion = groq_client.chat.completions.create(
                        model="llama-3.2-11b-vision-preview",
                        messages=[
                            {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": f"O'zbek tilida qisqacha javob ber: {caption}"},
                                    {"type": "image_url", "image_url": {"url": file_url}},
                                ],
                            }
                        ],
                    )
                    user_content_for_ai = vision_completion.choices[0].message.content
                except Exception as e:
                    print("Vision xatolik:", e)
                    user_content_for_ai = "[Rasm yuborildi, lekin o'qib bo'lmadi]"

        # 2-BAND: Ovozli xabar kelganda (Voice Transcription)
        elif voice:
            send_chat_action(chat_id, "typing", business_connection_id)
            file_id = voice["file_id"]
            file_url = download_telegram_file(file_id)
            
            if file_url:
                try:
                    # Ovozli faylni yuklab olib Groq Whisper modeliga beramiz
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
                    
                    # Vaqtinchalik faylni o'chiramiz
                    os.remove(audio_file_path)
                except Exception as e:
                    print("Whisper xatolik:", e)
                    user_content_for_ai = "[Ovozli xabar keldi, lekin uni ochib bo'lmadi]"

        # Oddiy matnli xabar
        elif text:
            user_content_for_ai = text

        # Agar yaroqli kontent bo'lsa, AI'ga uzatamiz
        if user_content_for_ai:
            send_chat_action(chat_id, "typing", business_connection_id)
            time.sleep(1) # Tabiiy pauza

            # 1-BAND: Tarix bilan birga javob olish
            answer = get_ai_answer(chat_id, user_content_for_ai)
            send_message(chat_id, answer, business_connection_id)

        return "OK", 200

    except Exception as e:
        print("Xatolik:", e)
        return "ERROR", 500


if __name__ == "__main__":
    app.run(port=5000, debug=True)

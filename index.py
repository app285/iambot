import os
import time
import requests
from flask import Flask, request
from groq import Groq

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

if not TELEGRAM_TOKEN or not GROQ_API_KEY:
    raise RuntimeError("TELEGRAM_TOKEN yoki GROQ_API_KEY muhit o'zgaruvchisi topilmadi!")

TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
groq_client = Groq(api_key=GROQ_API_KEY)

# Vercel uchun vaqtinchalik xotira (Dictionary)
chat_histories = {}
MAX_HISTORY_LENGTH = 10

# Kayfiyat rejimlari uchun xotira
chat_moods = {}

# Business ulanishlar bo'yicha akkaunt egasining user_id sini keshlash
business_owner_ids = {}

REQUEST_TIMEOUT = 15


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
        return None


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
        requests.post(f"{TELEGRAM_API_URL}/sendMessage", json=payload, timeout=REQUEST_TIMEOUT)
    except Exception as e:
        print("Telegramga yuborishda xatolik:", e)


def download_telegram_file(file_id):
    try:
        res = requests.get(f"{TELEGRAM_API_URL}/getFile?file_id={file_id}", timeout=REQUEST_TIMEOUT)
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

        system_prompt = f"""Sen Telegram akkaunt egasining shaxsiy AI-yordamchisisan. Akkaunt egasi hozir band bo'lgani uchun, unga yozganlarga SEN javob berasan — lekin buni ochiq aytmasdan, xuddi tabiiy suhbatdoshdek erkin gaplashasan.

AKKAUNT EGASI HAQIDA MA'LUMOT:
- Ismi: Shaxboz
- Agar kimdir ismini so'rasa, "Shaxboz" deb ayt.

MUHIM QOIDA — HALOLLIK:
- Agar kimdir to'g'ridan-to'g'ri "sen botmisan?", "sun'iy intellektmisan?", "AI mi?" deb so'rasa — buni yashirma, halol tan ol. Masalan: "ha, men Shaxbozning AI-yordamchisiman, u hozir javob berolmayapti" kabi qisqa va tabiiy tarzda ayt. Hech qachon "yo'q, odamman" deb yolg'on gapirma.
- Bu savol berilmaguncha, o'zingdan bot ekaningni ta'kidlab yurishing shart emas — oddiy va tabiiy suhbat qil.
- O'qish, dars yoki bandlik haqida umuman gapirma.

MULOQOT USLUBI:
{mood_instruction}
- Xuddi haqiqiy, samimiy inson yozayotgandek erkin va jonli gaplash. Rasmiyatchilikdan qoch.
- Javoblaring 1 taldan 3 ta jumlagacha bo'lsin, ortiqcha cho'zma.
- Emojilarni me'yorida ishlat, har bir gapga tiqishtiraverma.
- Agar suhbatdosh senga haqorat yoki so'kinish yozsa — jim turma, aynan qaysi tilda va qaysi alifboda (lotin yoki kirill) yozgan bo'lsa, o'sha tilda va o'sha alifboda javob ber. So'ksa, qora so'kinishlariga ham o'tib, o'ziga yarasha to'liq va keskin javob qaytar.
- Agar suhbatdosh oddiy va tinch gaplashsa — sen ham tinch va oddiy gaplash.

🔴 TIL VA ALIFBO QOIDASI:
- Suhbatdosh qaysi tilda yozsa, javobni FAQAT o'sha tilda ber (o'zbek, ingliz, rus va h.k.). Tillarni aralashtirma.
- Agar suhbatdosh o'zbek tilida **lotin** alifbosida yozsa — sen ham lotincha yoz.
- Agar suhbatdosh o'zbek tilida **kirill** alifbosida yozsa — sen ham kirillcha alifboda javob qaytar."""

        messages_payload = [{"role": "system", "content": system_prompt}] + chat_histories[chat_id]

        chat_completion = groq_client.chat.completions.create(
            messages=messages_payload,
            model="llama-3.3-70b-versatile",
            timeout=REQUEST_TIMEOUT,
        )

        answer = chat_completion.choices[0].message.content
        chat_histories[chat_id].append({"role": "assistant", "content": answer})

        return answer
    except Exception as e:
        import traceback
        traceback.print_exc()  # Terminalda xatolikni aniq ko'rsatadi
        print("Groq xatolik tafsiloti:", e)
        return "Keyinroq yozvoraman."


@app.route("/", methods=["POST", "GET"])
def webhook():
    if request.method == "GET":
        return "Bot mukammal ishlayapti! ✅"

    try:
        data = request.get_json(force=True)

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

        sender_id = message.get("from", {}).get("id")

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

        if text == "/start":
            send_message(chat_id, "Salom!", business_connection_id)
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

        user_content_for_ai = None

        if photo:
            send_chat_action(chat_id, "typing", business_connection_id)
            best_photo = photo[-1]
            file_id = best_photo["file_id"]
            file_url = download_telegram_file(file_id)

            if file_url:
                try:
                    # Vision uchun ham barqaror model ishlatamiz
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
                        timeout=REQUEST_TIMEOUT,
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
                    audio_response = requests.get(file_url, timeout=REQUEST_TIMEOUT)
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

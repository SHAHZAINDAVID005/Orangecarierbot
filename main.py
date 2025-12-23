#!/usr/bin/env python3
import os
import re
import json
import asyncio
import logging
import aiohttp
import aioredis
import time
from datetime import datetime
from telegram import Bot
from telegram.error import TelegramError
from aiohttp import ClientSession, ClientConnectorError

# === Whisper AI ===
import openai
openai.api_key = os.getenv("OPENAI_API_KEY")

# ================== CONFIG ==================
EMAIL = os.getenv("EMAIL")
PASSWORD = os.getenv("PASSWORD")
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
REDIS_URL = os.getenv("REDIS_URL")

LOGIN_URL = "https://www.orangecarrier.com/login"
SOCKET_URL = "https://hub.orangecarrier.com/socket.io/"
LIVE_CALL_ENDPOINT = "https://www.orangecarrier.com/live/calls/lives"
SOUND_ENDPOINT = "https://www.orangecarrier.com/live/calls/sound"

DOWNLOAD_DIR = "downloads"
RECONNECT_DELAY = 5
CLEANUP_INTERVAL = 3600  # seconds
MAX_FILE_AGE = 3600  # seconds
# ============================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

COUNTRY_FLAGS = {
    'AF': '🇦🇫','AL': '🇦🇱','DZ': '🇩🇿','US': '🇺🇸','NG': '🇳🇬','GB': '🇬🇧','FR': '🇫🇷',
    'DE': '🇩🇪','IN': '🇮🇳','JP': '🇯🇵','CN': '🇨🇳','RU': '🇷🇺','BR': '🇧🇷',
    'AR': '🇦🇷','MX': '🇲🇽','CA': '🇨🇦','AU': '🇦🇺','IT': '🇮🇹','ES': '🇪🇸',
    'SE': '🇸🇪','PK': '🇵🇰','BD': '🇧🇩','ID': '🇮🇩','EG': '🇪🇬','ZA': '🇿🇦',
    'KE': '🇰🇪','GH': '🇬🇭'
}

def get_flag(code):
    return COUNTRY_FLAGS.get((code or "").upper(), "❓")

def mask_number(num):
    num = str(num).replace("+", "")
    if num.isdigit() and len(num) >= 7:
        return f"{num[:4]}****{num[-3:]}"
    return num

def create_caption(did, duration, country, code, otp=None):
    now = datetime.now().strftime("%I:%M:%S %p")
    caption = (
        f"🔥 <b>NEW CALL {country.upper()} {get_flag(code)}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🌍 <b>Country:</b> {country} {get_flag(code)}\n"
        f"📞 <b>DID:</b> +{mask_number(did)}\n"
        f"⏳ <b>Duration:</b> {duration}s\n"
        f"⏰ <b>Time:</b> {now}"
    )
    if otp:
        caption += f"\n🔑 <b>Detected OTP:</b> {otp}"
    return caption

def extract_otp(text):
    match = re.search(r'\b(\d{4,8})\b', text)
    return match.group(1) if match else None

def cleanup_downloads():
    if not os.path.exists(DOWNLOAD_DIR):
        return
    now = time.time()
    for filename in os.listdir(DOWNLOAD_DIR):
        path = os.path.join(DOWNLOAD_DIR, filename)
        if os.path.isfile(path):
            age = now - os.path.getmtime(path)
            if age > MAX_FILE_AGE:
                try:
                    os.remove(path)
                    logging.info(f"Deleted old file: {filename}")
                except Exception as e:
                    logging.error(f"Failed to delete {filename}: {e}")

class OrangeCarrierBot:
    def __init__(self):
        self.bot = Bot(BOT_TOKEN)
        self.session = None
        self.sid = None
        self.redis = None
        self.audio_tasks = set()

    async def connect_redis(self):
        self.redis = await aioredis.from_url(REDIS_URL, decode_responses=True)

    async def login(self):
        async with ClientSession() as session:
            async with session.get(LOGIN_URL, ssl=False) as r:
                text = await r.text()
            token = re.search(r'name="_token" value="([^"]+)"', text)
            if not token:
                await self.alert_admin("❌ Login token not found.")
                return False
            payload = {"_token": token.group(1), "email": EMAIL, "password": PASSWORD}
            async with session.post(LOGIN_URL, data=payload, ssl=False, allow_redirects=False) as r2:
                if r2.status in (200, 302):
                    logging.info("✅ Login successful")
                    self.session = session
                    return True
                await self.alert_admin(f"❌ Login failed with status {r2.status}")
                return False

    async def alert_admin(self, message):
        try:
            await self.bot.send_message(ADMIN_ID, message)
        except TelegramError as e:
            logging.error(f"Admin alert failed: {e}")

    async def socket_handshake(self):
        async with self.session.get(SOCKET_URL, params={"EIO": "4", "transport": "polling"}, ssl=False) as r:
            text = await r.text()
        try:
            self.sid = json.loads(text[1:])["sid"]
            logging.info(f"✅ Socket SID {self.sid}")
        except Exception as e:
            await self.alert_admin(f"❌ Socket handshake failed: {e}")

    async def join_room(self):
        p = {"EIO": "4", "transport": "polling", "sid": self.sid}
        await self.session.post(SOCKET_URL, params=p, data="40", ssl=False)
        await self.session.post(
            SOCKET_URL,
            params=p,
            data=f'42["join_user_room",{{"room":"user:{EMAIL}:orange:internal"}}]',
            ssl=False
        )
        logging.info("✅ Joined room")

    async def download_audio(self, call_id, uuid):
        if not uuid:
            return None
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        path = os.path.join(DOWNLOAD_DIR, f"{call_id}-{uuid}.mp3")
        try:
            async with self.session.get(f"{SOUND_ENDPOINT}?id={call_id}&uuid={uuid}", ssl=False) as r:
                with open(path, "wb") as f:
                    while True:
                        chunk = await r.content.read(8192)
                        if not chunk:
                            break
                        f.write(chunk)
            return path
        except Exception as e:
            await self.alert_admin(f"❌ Audio download failed: {e}")
            return None

    async def transcribe_audio(self, audio_path):
        if not audio_path or not os.path.exists(audio_path):
            return None
        try:
            with open(audio_path, "rb") as f:
                transcript = openai.Audio.transcriptions.create(
                    model="whisper-1",
                    file=f
                )
            return transcript.text
        except Exception as e:
            logging.error(f"Transcription failed: {e}")
            return None

    async def fetch_call_info(self, cid):
        try:
            async with self.session.post(LIVE_CALL_ENDPOINT, data={"id": cid}, ssl=False) as r:
                return await r.json()
        except Exception as e:
            await self.alert_admin(f"❌ Fetch call info failed: {e}")
            return {}

    async def process_call(self, cid, did, uuid, country, code):
        if await self.redis.sismember("seen_calls", cid):
            return
        await self.redis.sadd("seen_calls", cid)

        await asyncio.sleep(5)
        info = await self.fetch_call_info(cid)
        duration = info.get("duration", 0)
        audio = await self.download_audio(cid, uuid)

        async def handle_transcription_and_send(audio_path):
            otp = None
            if audio_path:
                transcript = await self.transcribe_audio(audio_path)
                otp = extract_otp(transcript) if transcript else None

            caption = create_caption(did, duration, country, code, otp)

            try:
                if audio_path:
                    with open(audio_path, "rb") as f:
                        await self.bot.send_audio(CHAT_ID, f, caption=caption, parse_mode="HTML")
                else:
                    await self.bot.send_message(CHAT_ID, caption, parse_mode="HTML")
                await self.redis.hincrby("stats", "success", 1)
            except TelegramError as e:
                await self.redis.hincrby("stats", "failed", 1)
                await self.alert_admin(f"❌ Telegram send failed: {e}")

        task = asyncio.create_task(handle_transcription_and_send(audio))
        self.audio_tasks.add(task)
        task.add_done_callback(self.audio_tasks.discard)

    async def poll_socket(self):
        try:
            async with self.session.get(SOCKET_URL, params={"EIO": "4", "transport": "polling", "sid": self.sid}, ssl=False) as r:
                text = await r.text()
            if text.strip() == "2":
                await self.session.post(SOCKET_URL, params={"EIO":"4","transport":"polling","sid":self.sid}, data="3", ssl=False)
                return
            for part in text.split("\n"):
                if part.startswith("42"):
                    ev, data = json.loads(part[2:])
                    if ev == "new_call":
                        asyncio.create_task(self.process_call(
                            data["id"], data["did"], data["uuid"], data["country"], data["country_code"]
                        ))
        except ClientConnectorError:
            await self.alert_admin("❌ Socket disconnected, reconnecting...")
            await asyncio.sleep(RECONNECT_DELAY)
            await self.socket_handshake()
            await self.join_room()
        except Exception as e:
            logging.error(f"Polling error: {e}")

    async def periodic_cleanup(self):
        while True:
            cleanup_downloads()
            await asyncio.sleep(CLEANUP_INTERVAL)

    async def run(self):
        await self.connect_redis()
        asyncio.create_task(self.periodic_cleanup())
        while True:
            try:
                if not await self.login():
                    await asyncio.sleep(RECONNECT_DELAY)
                    continue
                await self.socket_handshake()
                await self.join_room()
                logging.info("🚀 Monitor Active")
                while True:
                    await self.poll_socket()
                    await asyncio.sleep(0.5)
            except Exception as e:
                logging.error(f"Main loop error: {e}")
                await self.alert_admin(f"❌ Bot crashed: {e}")
                await asyncio.sleep(RECONNECT_DELAY)

if __name__ == "__main__":
    asyncio.run(OrangeCarrierBot().run())

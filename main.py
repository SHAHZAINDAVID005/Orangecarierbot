#!/usr/bin/env python3
import requests
import json
import re
import time
import logging
import threading
import os
import urllib3
from datetime import datetime
from telegram import Bot
import asyncio
import redis.asyncio as redis  # Updated Redis Asyncio
from telegram.error import TelegramError

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ================== CONFIG (FROM ENV) ==================
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

POLLING_INTERVAL = 2
RECONNECT_DELAY = 5
# ============================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

COUNTRY_FLAGS = {
    'AF': '🇦🇫','AL': '🇦🇱','DZ': '🇩🇿','US': '🇺🇸','NG': '🇳🇬','GB': '🇬🇧','FR': '🇫🇷',
    'DE': '🇩🇪','IN': '🇮🇳','JP': '🇯🇵','CN': '🇨🇳','RU': '🇷🇺','BR': '🇧🇷',
    'AR': '🇦🇷','MX': '🇲🇽','CA': '🇨🇦','AU': '🇦🇺','IT': '🇮🇹','ES': '🇪🇸',
    'SE': '🇸🇪','PK': '🇵🇰','BD': '🇧🇩','ID': '🇮🇩','EG': '🇪🇬','ZA': '🇿🇦',
    'KE': '🇰🇪','GH': '🇬🇭'
}

def get_flag(code):
    return COUNTRY_FLAGS.get((code or "").upper(), "❓")

def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except:
            pass
    return default

def save_json(path, data):
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    except:
        pass

def mask_number(num):
    num = str(num).replace("+", "")
    if num.isdigit() and len(num) >= 7:
        return f"{num[:4]}****{num[-3:]}"
    return num

def create_caption(did, duration, country, code):
    now = datetime.now().strftime("%I:%M:%S %p")
    return (
        f"🔥 <b>NEW CALL {country.upper()} {get_flag(code)}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🌍 <b>Country:</b> {country} {get_flag(code)}\n"
        f"📞 <b>DID:</b> +{mask_number(did)}\n"
        f"⏳ <b>Duration:</b> {duration}s\n"
        f"⏰ <b>Time:</b> {now}"
    )

def download_audio(session, call_id, uuid):
    if not uuid:
        return None
    path = os.path.join(DOWNLOAD_DIR, f"{call_id}-{uuid}.mp3")
    try:
        r = session.get(f"{SOUND_ENDPOINT}?id={call_id}&uuid={uuid}", stream=True, verify=False)
        with open(path, "wb") as f:
            for c in r.iter_content(8192):
                if c:
                    f.write(c)
        return path
    except:
        return None

class OrangeCarrier:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0"})
        self.bot = Bot(BOT_TOKEN)
        self.sid = None
        self.seen = set()
        self.running = True
        self.stats = {"success": 0, "failed": 0}
        # Redis connection
        self.redis = redis.from_url(REDIS_URL, decode_responses=True)
        asyncio.get_event_loop().run_until_complete(self.redis.ping())
        logging.info("✅ Connected to Redis")

    def login(self):
        r = self.session.get(LOGIN_URL, verify=False)
        token = re.search(r'name="_token" value="([^"]+)"', r.text)
        if not token:
            return False
        payload = {"_token": token.group(1), "email": EMAIL, "password": PASSWORD}
        r2 = self.session.post(LOGIN_URL, data=payload, allow_redirects=False, verify=False)
        logging.info("✅ Login successful")
        return r2.status_code in (200, 302)

    def socket_handshake(self):
        r = self.session.get(SOCKET_URL, params={"EIO": "4", "transport": "polling"}, verify=False)
        self.sid = json.loads(r.text[1:])["sid"]
        logging.info(f"✅ Socket SID {self.sid}")

    def join_room(self):
        p = {"EIO": "4", "transport": "polling", "sid": self.sid}
        self.session.post(SOCKET_URL, params=p, data="40", verify=False)
        self.session.post(
            SOCKET_URL,
            params=p,
            data=f'42["join_user_room",{{"room":"user:{EMAIL}:orange:internal"}}]',
            verify=False
        )
        logging.info("✅ Joined room")

    def poll(self):
        r = self.session.get(SOCKET_URL, params={"EIO": "4", "transport": "polling", "sid": self.sid}, verify=False)
        if r.text.strip() == "2":
            self.session.post(SOCKET_URL, params={"EIO":"4","transport":"polling","sid":self.sid}, data="3", verify=False)
            return
        for part in r.text.split("\n"):
            if part.startswith("42"):
                ev, data = json.loads(part[2:])
                if ev == "new_call":
                    threading.Thread(
                        target=self.process_call,
                        args=(data["id"], data["did"], data["uuid"], data["country"], data["country_code"]),
                        daemon=True
                    ).start()

    def process_call(self, cid, did, uuid, country, code):
        # Check Redis for duplicate
        if asyncio.get_event_loop().run_until_complete(self.redis.sismember("seen_calls", cid)):
            return
        asyncio.get_event_loop().run_until_complete(self.redis.sadd("seen_calls", cid))

        time.sleep(5)
        info = self.session.post(LIVE_CALL_ENDPOINT, data={"id": cid}, verify=False).json()
        duration = info.get("duration", 0)

        caption = create_caption(did, duration, country, code)
        audio = download_audio(self.session, cid, uuid)

        try:
            if audio:
                with open(audio, "rb") as f:
                    asyncio.run(self.bot.send_audio(CHAT_ID, f, caption=caption, parse_mode="HTML"))
            else:
                asyncio.run(self.bot.send_message(CHAT_ID, caption, parse_mode="HTML"))
            # Increment success
            asyncio.get_event_loop().run_until_complete(self.redis.hincrby("stats", "success", 1))
        except TelegramError as e:
            logging.error(f"Telegram error: {e}")
            asyncio.get_event_loop().run_until_complete(self.redis.hincrby("stats", "failed", 1))

    def start(self):
        if not self.login():
            logging.error("❌ Login failed")
            return
        self.socket_handshake()
        self.join_room()
        logging.info("🚀 Monitor Active")
        while self.running:
            try:
                self.poll()
            except Exception as e:
                logging.error(f"Polling error: {e}")
                time.sleep(RECONNECT_DELAY)
                self.socket_handshake()
                self.join_room()
            time.sleep(POLLING_INTERVAL)

if __name__ == "__main__":
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    OrangeCarrier().start()

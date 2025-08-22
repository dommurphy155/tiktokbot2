import os
from datetime import datetime
from dotenv import load_dotenv

# Load .env file
load_dotenv()

# ---------------------------
# Telegram Settings
# ---------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ---------------------------
# TikTok Settings
# ---------------------------
# Path to your TikTok JSON cookies
TIKTOK_COOKIES_FILE = os.getenv("TIKTOK_COOKIES_FILE", "tiktok_cookies.json")

# TikTok homepage URL for Selenium
TIKTOK_HOMEPAGE = "https://www.tiktok.com/"

# Directory to save downloaded videos (yt-dlp needs a folder)
OUTPUT_PATH = os.path.join(os.getcwd(), "downloads")

# ---------------------------
# Logging function
# ---------------------------
def log(message: str):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {message}")

# Ensure downloads folder exists
os.makedirs(OUTPUT_PATH, exist_ok=True)

#!/usr/bin/env python3
import os
import atexit
import signal
import time
import random
import asyncio

# ensure headless to work on VPS / micro VM
os.environ["DISPLAY"] = ":99"

try:
    import psutil  # optional; used for RAM/Firefox leak control
except Exception:
    psutil = None

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

import telegram  # for version log
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler, MessageHandler, filters

from config import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    TIKTOK_HOMEPAGE,
    OUTPUT_PATH,
    log,
)

from utills import (
    # state & tunables
    PRELOAD_TARGET,
    VIDEO_QUEUE,
    VIDEO_CACHE,
    HISTORY_MAX,
    SEEN_URLS_MAX,
    SCROLL_SLEEP_RANGE,
    OUTPUT_DISK_QUOTA_MB,
    OUTPUT_DISK_RESERVE_MB,
    JANITOR_INTERVAL_SEC,
    BROWSER_RESTART_PRELOADS,
    MEM_SOFT_LIMIT_MB,
    PRELOADED_VIDEOS,
    SEEN_URLS,
    PLAYED_VIDEOS,
    CURRENT_INDEX,
    PRELOAD_COUNTER,
    METADATA_BY_PATH,
    PENDING_POSTS,
    NETSCAPE_COOKIES_FILE,
    # utils
    cleanup_files,
    enforce_disk_budget,
    prune_seen_urls_if_needed,
    push_played_video,
    add_seen_url,
    current_process_rss_mb,
)

from scraper import (
    setup_browser,
    load_cookies,
    apply_cookies,
    convert_json_to_netscape,
    get_fresh_video_link,
)

from downloader import download_video, extract_video_metadata

log(f"Using python-telegram-bot version: {telegram.__version__}")

# This driver is kept in main and passed where needed
BROWSER_DRIVER = None

# ------------------- Recycle / preload -------------------
async def _recycle_browser_if_needed():
    global BROWSER_DRIVER, PRELOAD_COUNTER
    try:
        rss_mb = current_process_rss_mb() if psutil else -1
        need_restart = False
        if BROWSER_RESTART_PRELOADS > 0 and PRELOAD_COUNTER >= BROWSER_RESTART_PRELOADS:
            need_restart = True
            log(f"[INFO] Recycling Firefox after {PRELOAD_COUNTER} preloads")
        if rss_mb > 0 and rss_mb >= MEM_SOFT_LIMIT_MB:
            need_restart = True
            log(f"[INFO] Recycling Firefox due to RSS {rss_mb} MB >= {MEM_SOFT_LIMIT_MB} MB")
        if need_restart:
            _shutdown_driver()
            await asyncio.sleep(0.5)
            BROWSER_DRIVER = setup_browser()
            cookies = load_cookies()
            apply_cookies(BROWSER_DRIVER, cookies, TIKTOK_HOMEPAGE)
            PRELOAD_COUNTER = 0
    except Exception as e:
        log(f"[WARNING] Browser recycle failed: {e}")

async def init_browser_and_queue(n=PRELOAD_TARGET):
    """
    Startup sequence:
      - launch browser
      - load cookies and apply them once
      - convert json cookies to netscape file once (after cookies applied)
      - fill VIDEO_QUEUE with n URLs using Selenium scraping
    """
    global BROWSER_DRIVER
    BROWSER_DRIVER = setup_browser()
    cookies = load_cookies()
    apply_cookies(BROWSER_DRIVER, cookies, TIKTOK_HOMEPAGE)
    convert_json_to_netscape(os.getenv("TIKTOK_COOKIES_FILE", "tiktok_cookies.json"), NETSCAPE_COOKIES_FILE)

    while len(VIDEO_QUEUE) < n:
        video_url = get_fresh_video_link(BROWSER_DRIVER)
        VIDEO_QUEUE.append(video_url)
    log(f"Preloaded {len(VIDEO_QUEUE)} videos.")

async def preload_one_video_async():
    global BROWSER_DRIVER
    async with asyncio.Lock():
        if BROWSER_DRIVER is None:
            BROWSER_DRIVER = setup_browser()
            cookies = load_cookies()
            apply_cookies(BROWSER_DRIVER, cookies, TIKTOK_HOMEPAGE)
        await _recycle_browser_if_needed()
        if VIDEO_QUEUE:
            video_url = VIDEO_QUEUE.popleft()
            res = download_video(video_url, OUTPUT_PATH)
            if res:
                path, meta = res
                log("Added new video to ready cache")
        while len(VIDEO_QUEUE) < 1:
            next_url = get_fresh_video_link(BROWSER_DRIVER)
            VIDEO_QUEUE.append(next_url)
            log("Added new video URL to queue")

async def pre_download_task():
    """
    Always keep one downloaded video ready in VIDEO_CACHE.
    When VIDEO_CACHE length drops below 1, download the next URL.
    """
    global BROWSER_DRIVER
    while True:
        try:
            if len(VIDEO_CACHE) < 1 and VIDEO_QUEUE:
                next_video_url = VIDEO_QUEUE.popleft()
                res = download_video(next_video_url, OUTPUT_PATH)
                if res:
                    path, meta = res
                    log(f"Pre-downloaded next video: {path}")
            while len(VIDEO_QUEUE) < PRELOAD_TARGET:
                candidate = get_fresh_video_link(BROWSER_DRIVER)
                VIDEO_QUEUE.append(candidate)
                log("Added new video URL to queue")
            await asyncio.sleep(1.0)
        except Exception as e:
            log(f"[WARNING] pre_download_task failed: {e}")
            await asyncio.sleep(5.0)

# ------------------- Posting helper (Selenium) -------------------
def blocking_post_video(driver, video_path, caption, hashtags):
    """
    Blocking function that uses Selenium to post to TikTok.
    Designed to be executed in a thread via asyncio.to_thread.
    """
    try:
        driver.get(TIKTOK_HOMEPAGE)
        wait = WebDriverWait(driver, 15)
        time.sleep(random.uniform(1, 2))

        file_input = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, 'input[type="file"]')))
        file_input.send_keys(video_path)
        time.sleep(random.uniform(2, 4))

        caption_elem = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, '[contenteditable="true"]')))
        full_caption = (caption or "") + " " + " ".join(hashtags or [])
        for ch in full_caption:
            caption_elem.send_keys(ch)
            time.sleep(random.uniform(0.02, 0.08))

        time.sleep(random.uniform(1, 3))
        post_button = wait.until(EC.element_to_be_clickable((By.XPATH, '//button[contains(text(), "Post")]')))
        post_button.click()
        time.sleep(5)
        log(f"Posted video {video_path} via Selenium.")
        return True
    except Exception as e:
        log(f"[ERROR] blocking_post_video failed: {e}")
        return False

# ------------------- Telegram Bot -------------------
async def send_video(bot, chat_id, video_path, index, caption=None):
    """
    Send a video over Telegram. Include metadata caption and hashtags if available.
    Add Next / Previous / Post buttons.
    """
    start_time = time.time()
    if not os.path.exists(video_path):
        log(f"[ERROR] Video file {video_path} does not exist")
        return None

    metadata = METADATA_BY_PATH.get(video_path)
    if not metadata:
        video_id = os.path.basename(video_path).replace(".mp4", "")
        video_url = f"https://www.tiktok.com/video/{video_id}"
        metadata = extract_video_metadata(video_url, timeout_sec=8) or {"caption": "", "hashtags": []}
        METADATA_BY_PATH[video_path] = metadata

    caption_text_parts = []
    if caption:
        caption_text_parts.append(caption)
    if metadata and metadata.get("caption"):
        caption_text_parts.append(f"Original Caption: {metadata.get('caption')}")
    if metadata and metadata.get("hashtags"):
        caption_text_parts.append(f"Hashtags: {' '.join(metadata.get('hashtags'))}")
    caption_text = "\n\n".join(caption_text_parts) if caption_text_parts else None

    buttons_row = []
    if index > 0:
        buttons_row.append(InlineKeyboardButton("◀️ Previous", callback_data="prev_video"))
    buttons_row.append(InlineKeyboardButton("Post ⬆️", callback_data="post_video"))
    buttons_row.append(InlineKeyboardButton("Next ▶️", callback_data="next_video"))
    markup = InlineKeyboardMarkup([buttons_row])

    for attempt in range(3):
        try:
            log(f"Opening file {video_path} (attempt {attempt + 1})...")
            with open(video_path, "rb") as f:
                msg = await bot.send_video(chat_id=chat_id, video=f, reply_markup=markup, caption=caption_text)
            log(f"Sent video {video_path} in {time.time() - start_time:.2f} seconds")
            return msg.message_id
        except Exception as e:
            log(f"[ERROR] Failed to send video {video_path} (attempt {attempt + 1}): {e}")
            if attempt < 2:
                log("Retrying send_video...")
                await asyncio.sleep(1.0)
            continue
    log(f"[ERROR] Failed to send video {video_path} after 3 attempts")
    return None

async def _handle_next_action(bot):
    """
    Move next file from VIDEO_CACHE into PLAYED_VIDEOS and send it.
    """
    from utills import CURRENT_INDEX  # reflect live index
    if len(VIDEO_CACHE) > 0:
        next_path = VIDEO_CACHE.popleft()
        push_played_video(next_path, update_index=True)
        log(f"Moved ready video to played: {next_path}")
    else:
        if VIDEO_QUEUE:
            next_url = VIDEO_QUEUE.popleft()
            res = download_video(next_url, OUTPUT_PATH)
            if res:
                path, _meta = res
                push_played_video(path, update_index=True)
                log(f"Downloaded and moved to played for Next: {path}")
            else:
                log("[WARNING] Failed to download fallback next video")
                return
        else:
            log("[WARNING] No URL in queue for Next")
            return

    if 0 <= CURRENT_INDEX < len(PLAYED_VIDEOS):
        await send_video(bot, TELEGRAM_CHAT_ID, PLAYED_VIDEOS[CURRENT_INDEX], CURRENT_INDEX)

async def navigation_callback(update, context):
    """
    Handle Next/Previous/Post/Post-Next button presses.
    """
    from utills import CURRENT_INDEX  # keep in sync
    start_time = time.time()
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass

    try:
        await query.message.delete()
    except Exception:
        log(f"[WARNING] Failed to delete invoking message")

    if query.data == "next_video":
        await _handle_next_action(context.bot)
        log(f"Navigation completed in {time.time() - start_time:.2f} seconds")
        return

    if query.data == "prev_video":
        if CURRENT_INDEX > 0:
            # mutate module var safely
            import utills
            utills.CURRENT_INDEX -= 1
            log(f"Moving to previous video at index {utills.CURRENT_INDEX}")
            if 0 <= utills.CURRENT_INDEX < len(PLAYED_VIDEOS):
                await send_video(context.bot, TELEGRAM_CHAT_ID, PLAYED_VIDEOS[utills.CURRENT_INDEX], utills.CURRENT_INDEX)
        else:
            log("[WARNING] Already at the first video")
        return

    if query.data == "post_video":
        if 0 <= CURRENT_INDEX < len(PLAYED_VIDEOS):
            video_path = PLAYED_VIDEOS[CURRENT_INDEX]
            chat_id = query.message.chat.id if query.message and query.message.chat else TELEGRAM_CHAT_ID
            PENDING_POSTS[chat_id] = {
                "stage": 1,
                "video_path": video_path,
                "comment": None,
                "hashtags": None,
                "prompt_msg_ids": [],
            }
            msg = await context.bot.send_message(chat_id=chat_id, text="What would you like to comment?")
            PENDING_POSTS[chat_id]["prompt_msg_ids"].append(msg.message_id)
        else:
            log("[WARNING] No current video to post")
        return

    if query.data == "post_next":
        await _handle_next_action(context.bot)
        return

async def text_message_handler(update, context):
    """
    Handle user replies to the post flow prompts.
    """
    chat = update.effective_chat
    if chat is None:
        return
    chat_id = chat.id
    if chat_id not in PENDING_POSTS:
        return

    flow = PENDING_POSTS[chat_id]
    stage = flow.get("stage", 1)

    if stage == 1:
        flow["comment"] = update.message.text or ""
        for mid in flow.get("prompt_msg_ids", []):
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=mid)
            except Exception:
                pass
        flow["prompt_msg_ids"] = []
        msg = await context.bot.send_message(chat_id=chat_id, text="What would you like as your #?")
        flow["prompt_msg_ids"].append(msg.message_id)
        flow["stage"] = 2
        return

    if stage == 2:
        flow["hashtags"] = update.message.text or ""
        for mid in flow.get("prompt_msg_ids", []):
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=mid)
            except Exception:
                pass
        flow["prompt_msg_ids"] = []
        flow["stage"] = 3

        next_button = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Next ▶️", callback_data="post_next")]]
        )
        processing_msg = await context.bot.send_message(
            chat_id=chat_id,
            text="Your post is now processing. Please check your TikTok shortly to confirm.",
            reply_markup=next_button,
        )
        flow["processing_msg_id"] = processing_msg.message_id

        video_path = flow.get("video_path")
        comment = flow.get("comment") or ""
        hashtags_raw = flow.get("hashtags") or ""
        hashtags_list = [t for t in re.split(r"\s+", hashtags_raw) if t]

        async def do_post():
            try:
                ok = await asyncio.to_thread(blocking_post_video, BROWSER_DRIVER, video_path, comment, hashtags_list)
                if ok:
                    log(f"[INFO] Background post succeeded for {video_path}")
                else:
                    log(f"[WARNING] Background post failed for {video_path}")
            except Exception as e:
                log(f"[ERROR] do_post background raised: {e}")

        asyncio.create_task(do_post())
        return

    return

# ------------------- Janitor Task -------------------
async def janitor_task():
    while True:
        try:
            cleanup_files()
            enforce_disk_budget()
            prune_seen_urls_if_needed()
            await _recycle_browser_if_needed()
        except Exception as e:
            log(f"[WARNING] janitor_task failed: {e}")
        await asyncio.sleep(JANITOR_INTERVAL_SEC)

# ------------------- Graceful shutdown -------------------
def _shutdown_driver():
    global BROWSER_DRIVER
    try:
        if BROWSER_DRIVER is not None:
            BROWSER_DRIVER.quit()
            BROWSER_DRIVER = None
            log("Firefox WebDriver closed")
    except Exception as e:
        log(f"[WARNING] Failed to close WebDriver: {e}")

def _handle_exit(*_args):
    try:
        cleanup_files()
    finally:
        _shutdown_driver()

atexit.register(_handle_exit)
signal.signal(signal.SIGINT, _handle_exit)
signal.signal(signal.SIGTERM, _handle_exit)

# ------------------- Main -------------------
def main():
    from utills import CURRENT_INDEX  # ensure we read live index
    log("Starting TikTok downloader with Telegram bot...")
    loop = asyncio.get_event_loop()
    loop.run_until_complete(init_browser_and_queue(PRELOAD_TARGET))

    if len(VIDEO_QUEUE) >= 1:
        first_url = VIDEO_QUEUE.popleft()
        res1 = download_video(first_url, OUTPUT_PATH)
        if res1:
            first_path, _meta1 = res1
            push_played_video(first_path, update_index=True)
        else:
            log("[ERROR] Failed to download first video at startup")
            return
    else:
        log("[ERROR] No videos queued at startup (first)")
        return

    if len(VIDEO_QUEUE) >= 1:
        second_url = VIDEO_QUEUE.popleft()
        res2 = download_video(second_url, OUTPUT_PATH)
        if res2:
            second_path, _meta2 = res2
            log("Second video downloaded and kept ready for immediate 'Next'")
        else:
            log("[WARNING] Failed to download second startup video")
    else:
        log("[WARNING] Not enough preloaded URLs to download second startup video")

    while len(VIDEO_QUEUE) < 1:
        try:
            candidate = get_fresh_video_link(BROWSER_DRIVER)
            VIDEO_QUEUE.append(candidate)
        except Exception:
            break

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CallbackQueryHandler(navigation_callback, pattern="^(next_video|prev_video|post_video|post_next)$"))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), text_message_handler))

    loop.create_task(pre_download_task())
    loop.create_task(janitor_task())

    if 0 <= CURRENT_INDEX < len(PLAYED_VIDEOS):
        startup_message = (
            "Welcome to your TikTok Video Scraper! 🎉 "
            "Enjoy seamless browsing of fresh TikTok content with our intuitive 'Next' and 'Previous' buttons. "
            "Please note that video sending may take up to 10 seconds due to Telegram's processing."
        )
        loop.create_task(send_video(app.bot, TELEGRAM_CHAT_ID, PLAYED_VIDEOS[CURRENT_INDEX], CURRENT_INDEX, caption=startup_message))
    else:
        log("[ERROR] No first video to send at startup")

    cleanup_files()
    enforce_disk_budget()
    app.run_polling()
    _handle_exit()

if __name__ == "__main__":
    main()

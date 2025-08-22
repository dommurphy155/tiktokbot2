import os
import shutil
import re
import time
from collections import deque

# optional psutil import (same behavior as original)
try:
    import psutil
except Exception:
    psutil = None

from config import OUTPUT_PATH, log

# ------------------- Tunables (bloat + RAM control) -------------------
NETSCAPE_COOKIES_FILE = "tiktok_cookies.txt"

PRELOAD_TARGET = 3
VIDEO_QUEUE = deque(maxlen=PRELOAD_TARGET)  # holds URLs to download
VIDEO_CACHE = deque(maxlen=3)               # downloaded file paths ready to play
HISTORY_MAX = 3
SEEN_URLS_MAX = 250
SCROLL_SLEEP_RANGE = (1.0, 1.6)

OUTPUT_DISK_QUOTA_MB = int(os.getenv("OUTPUT_DISK_QUOTA_MB", "1024"))
OUTPUT_DISK_RESERVE_MB = int(os.getenv("OUTPUT_DISK_RESERVE_MB", "2048"))

JANITOR_INTERVAL_SEC = 180
BROWSER_RESTART_PRELOADS = 200
MEM_SOFT_LIMIT_MB = int(os.getenv("MEM_SOFT_LIMIT_MB", "1200"))

# ------------------- Globals -------------------
PRELOADED_VIDEOS = set()
SEEN_URLS = deque()
PLAYED_VIDEOS = []  # file paths already played (history)
CURRENT_INDEX = -1
PRELOAD_COUNTER = 0

# metadata storage: video_path -> metadata dict (duration, caption, hashtags)
METADATA_BY_PATH = {}

# pending post flows keyed by chat_id
PENDING_POSTS = {}  # chat_id -> dict {stage:int, video_path:str, comment:str, hashtags:str, prompt_msg_ids:[]}

# ------------------- Utils -------------------
def safe_delete(path: str):
    try:
        if path and os.path.exists(path):
            os.remove(path)
            log(f"Deleted file: {path}")
    except Exception as e:
        log(f"[WARNING] Failed to delete {path}: {e}")

def _keep_set():
    return set(PLAYED_VIDEOS) | set(VIDEO_CACHE)

def cleanup_files():
    try:
        keep = _keep_set()
        if not os.path.isdir(OUTPUT_PATH):
            return
        for name in os.listdir(OUTPUT_PATH):
            if not name.lower().endswith(".mp4"):
                continue
            candidate = os.path.join(OUTPUT_PATH, name)
            if candidate not in keep:
                safe_delete(candidate)
    except Exception as e:
        log(f"[WARNING] cleanup_files failed: {e}")

def prune_seen_urls_if_needed():
    while len(SEEN_URLS) > SEEN_URLS_MAX:
        oldest = SEEN_URLS.popleft()
        PRELOADED_VIDEOS.discard(oldest)

def add_seen_url(href: str):
    if href in PRELOADED_VIDEOS:
        return
    if len(SEEN_URLS) >= SEEN_URLS_MAX:
        oldest = SEEN_URLS.popleft()
        PRELOADED_VIDEOS.discard(oldest)
    PRELOADED_VIDEOS.add(href)
    SEEN_URLS.append(href)

def push_played_video(path: str, update_index=True):
    """Add a video to played history and keep index sane."""
    global CURRENT_INDEX
    PLAYED_VIDEOS.append(path)
    if len(PLAYED_VIDEOS) > HISTORY_MAX:
        evicted = PLAYED_VIDEOS.pop(0)
        if evicted != path:
            safe_delete(evicted)
        if CURRENT_INDEX > 0:
            CURRENT_INDEX -= 1
    if update_index:
        CURRENT_INDEX = len(PLAYED_VIDEOS) - 1
    cleanup_files()

def folder_size_bytes(folder: str) -> int:
    total = 0
    try:
        for entry in os.scandir(folder):
            if entry.is_file(follow_symlinks=False):
                total += entry.stat().st_size
    except Exception:
        pass
    return total

def enforce_disk_budget():
    try:
        if not os.path.isdir(OUTPUT_PATH):
            return
        cleanup_files()
        quota_bytes = OUTPUT_DISK_QUOTA_MB * 1024 * 1024
        reserve_bytes = OUTPUT_DISK_RESERVE_MB * 1024 * 1024
        folder_bytes = folder_size_bytes(OUTPUT_PATH)

        def constraints_ok() -> bool:
            u = shutil.disk_usage(OUTPUT_PATH)
            return (folder_bytes <= quota_bytes) and (u.free >= reserve_bytes)

        safety_counter = 0
        while not constraints_ok():
            safety_counter += 1
            if safety_counter > 100:
                log("[WARNING] Disk janitor safety stop hit.")
                break
            if len(PLAYED_VIDEOS) > 0:
                current_path = (
                    PLAYED_VIDEOS[CURRENT_INDEX]
                    if 0 <= CURRENT_INDEX < len(PLAYED_VIDEOS)
                    else None
                )
                did_delete = False
                while len(PLAYED_VIDEOS) > 0:
                    oldest = PLAYED_VIDEOS[0]
                    if oldest == current_path and len(PLAYED_VIDEOS) == 1:
                        break
                    if oldest == current_path:
                        if len(PLAYED_VIDEOS) >= 2:
                            second = PLAYED_VIDEOS[1]
                            PLAYED_VIDEOS.pop(1)
                            safe_delete(second)
                            did_delete = True
                            break
                        else:
                            break
                    else:
                        PLAYED_VIDEOS.pop(0)
                        safe_delete(oldest)
                        if CURRENT_INDEX > 0:
                            CURRENT_INDEX -= 1
                        did_delete = True
                        break
                if did_delete:
                    continue
            if len(VIDEO_CACHE) > 0:
                old_file = VIDEO_CACHE.popleft()
                protect = set(PLAYED_VIDEOS)
                if old_file not in protect:
                    safe_delete(old_file)
                continue
            keep = _keep_set()
            candidates = []
            for name in os.listdir(OUTPUT_PATH):
                if name.lower().endswith(".mp4"):
                    p = os.path.join(OUTPUT_PATH, name)
                    if p not in keep:
                        candidates.append(p)
            if candidates:
                candidates.sort(key=lambda p: os.stat(p).st_mtime)
                safe_delete(candidates[0])
            else:
                break
    except Exception as e:
        log(f"[WARNING] enforce_disk_budget failed: {e}")

def current_process_rss_mb() -> int:
    if not psutil:
        return -1
    try:
        p = psutil.Process(os.getpid())
        return int(p.memory_info().rss / (1024 * 1024))
    except Exception:
        return -1

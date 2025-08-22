import os
import re
import json
import subprocess

from config import log
from utills import (
    NETSCAPE_COOKIES_FILE,
    VIDEO_CACHE,
    METADATA_BY_PATH,
    cleanup_files,
    enforce_disk_budget,
)

def extract_video_metadata(video_url, timeout_sec=15):
    """
    Extract metadata using yt-dlp --dump-json.
    Return a dict with keys: duration (float), caption (str), hashtags (list[str]).
    If extraction fails, return None.
    """
    try:
        cmd = ["yt-dlp", "--dump-json", "--cookies", NETSCAPE_COOKIES_FILE, video_url]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=timeout_sec)
        metadata = json.loads(result.stdout)
        duration = float(metadata.get("duration", 0) or 0)
        description = metadata.get("description", "") or ""
        tags = metadata.get("tags", []) or []
        description_hashtags = re.findall(r"#\w+", description)
        tags_hash = [t for t in tags if isinstance(t, str) and t.startswith("#")]
        hashtags = list(dict.fromkeys(description_hashtags + tags_hash))
        return {"duration": duration, "caption": description.strip(), "hashtags": hashtags}
    except subprocess.TimeoutExpired:
        log(f"[ERROR] yt-dlp timed out extracting metadata for {video_url}")
        return None
    except subprocess.CalledProcessError as cpe:
        log(f"[ERROR] yt-dlp returned non-zero for metadata {video_url}: {cpe}")
        return None
    except Exception as e:
        log(f"[ERROR] Failed to extract metadata for {video_url}: {e}")
        return None

def download_video(video_url, output_folder):
    """
    Downloads video using yt-dlp and attempts to extract metadata.
    Returns a tuple (output_path, metadata_dict_or_None) on success, or None on failure.
    """
    if not os.path.exists(output_folder):
        os.makedirs(output_folder)
    # ensure cache churn
    if len(VIDEO_CACHE) == VIDEO_CACHE.maxlen:
        old_file = VIDEO_CACHE.popleft()
        try:
            os.remove(old_file)
        except Exception:
            pass
        METADATA_BY_PATH.pop(old_file, None)

    # attempt metadata (non-fatal if fails)
    metadata = extract_video_metadata(video_url, timeout_sec=12)

    # If metadata exists and duration is outside bounds, skip
    if metadata and not (5 <= metadata.get("duration", 0) <= 50):
        log(f"[INFO] Skipping {video_url}: Duration {metadata.get('duration', 0):.1f}s not in 5-50s")
        return None

    video_id = video_url.rstrip("/").split("/")[-1]
    output_path = os.path.join(output_folder, f"{video_id}.mp4")
    cmd = [
        "yt-dlp",
        "--no-part",
        "--no-mtime",
        "--cookies",
        NETSCAPE_COOKIES_FILE,
        "-o",
        output_path,
        video_url,
    ]
    try:
        subprocess.run(cmd, check=True, timeout=180)
        file_size = os.path.getsize(output_path) / (1024 * 1024)
        log(f"Video downloaded successfully to {output_path} ({file_size:.2f} MB)")
        if file_size > 50:
            log(f"[WARNING] Large video file ({file_size:.2f} MB) may slow Telegram upload")
    except subprocess.TimeoutExpired:
        log(f"[ERROR] yt-dlp timed out downloading {video_url}")
        return None
    except subprocess.CalledProcessError as cpe:
        log(f"[ERROR] yt-dlp returned error downloading {video_url}: {cpe}")
        return None
    except Exception as e:
        log(f"[ERROR] Failed to download video {video_url}: {e}")
        return None

    # store metadata for this path (may be None)
    METADATA_BY_PATH[output_path] = metadata or {"duration": None, "caption": "", "hashtags": []}
    # append to ready cache
    VIDEO_CACHE.append(output_path)
    cleanup_files()
    enforce_disk_budget()
    return output_path, METADATA_BY_PATH[output_path]

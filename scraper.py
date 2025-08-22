import os
import json
import random
import time

from selenium import webdriver
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.service import Service
from selenium.common.exceptions import TimeoutException, WebDriverException

from config import TIKTOK_COOKIES_FILE, TIKTOK_HOMEPAGE, log
from utills import (
    PRELOADED_VIDEOS,
    add_seen_url,
    prune_seen_urls_if_needed,
    SCROLL_SLEEP_RANGE,
)
from utills import PRELOAD_COUNTER as _PRELOAD_COUNTER_REF  # read/write via name in globals()

# ------------------- TikTok Scraper helpers -------------------
def convert_json_to_netscape(json_file, txt_file):
    log(f"Converting JSON cookies {json_file} to Netscape format {txt_file}...")
    with open(json_file, "r") as f:
        cookies = json.load(f)
    with open(txt_file, "w") as f:
        f.write("# Netscape HTTP Cookie File\n")
        for c in cookies:
            domain = c.get("domain", ".tiktok.com")
            include_subdomains = "TRUE"
            path = c.get("path", "/")
            secure = "TRUE" if c.get("secure", False) else "FALSE"
            expiration = str(int(c.get("expiry", 2147483647)))
            name = c["name"]
            value = c["value"]
            f.write(
                "\t".join([domain, include_subdomains, path, secure, expiration, name, value]) + "\n"
            )
    log("Cookie conversion completed.")

def load_cookies():
    log("Loading cookies from file...")
    with open(TIKTOK_COOKIES_FILE, "r") as f:
        cookies = json.load(f)
    log(f"Loaded {len(cookies)} cookies")
    return cookies

def setup_browser():
    """Start Firefox WebDriver in headless mode."""
    firefox_options = Options()
    firefox_options.add_argument("--no-sandbox")
    firefox_options.add_argument("--disable-dev-shm-usage")
    firefox_options.add_argument("--width=1200")
    firefox_options.add_argument("--height=900")
    firefox_options.headless = False
    try:
        driver = webdriver.Firefox(service=Service("/usr/bin/geckodriver"), options=firefox_options)
        log("Firefox WebDriver started")
        return driver
    except WebDriverException as e:
        log(f"[ERROR] Failed to start Firefox WebDriver: {e}")
        raise

def apply_cookies(driver, cookies, url=TIKTOK_HOMEPAGE):
    """
    Apply cookies directly by navigating to `url` and calling add_cookie() for each cookie.
    """
    driver.get(url)
    for cookie in cookies:
        cookie_dict = {
            "name": cookie["name"],
            "value": cookie["value"],
            "domain": cookie.get("domain", "https://www.tiktok.com/?lang=en-GB"),
            "path": cookie.get("path", "/"),
            "secure": cookie.get("secure", True),
            "httpOnly": cookie.get("httpOnly", False),
        }
        try:
            driver.add_cookie(cookie_dict)
        except Exception as e:
            log(f"[ERROR] Failed to add cookie {cookie.get('name', '<unknown>')}: {e}")
    driver.refresh()
    log("All cookies applied.")

def get_fresh_video_link(driver, retries=5, scroll=True):
    """
    Keep legacy behavior for finding links via Selenium.
    """
    # We need write access to PRELOAD_COUNTER in utills; emulate by importing its name
    import utills
    for attempt in range(retries):
        try:
            if scroll:
                driver.execute_script(f"window.scrollBy(0, {random.randint(400, 1200)});")
                time.sleep(random.uniform(*SCROLL_SLEEP_RANGE))
            videos = driver.find_elements(By.XPATH, "//a[contains(@href, '/video/')]")
            random.shuffle(videos)
            for video_link in videos:
                href = video_link.get_attribute("href")
                if href and href not in PRELOADED_VIDEOS:
                    add_seen_url(href)
                    log(f"Found video link: {href}")
                    prune_seen_urls_if_needed()
                    utills.PRELOAD_COUNTER += 1
                    return href
        except TimeoutException:
            log(f"[WARNING] Attempt {attempt + 1} failed to find video link.")
        if attempt % 2 == 0:
            driver.refresh()
            time.sleep(1.2)
    raise Exception("Failed to locate unique video link after retries")

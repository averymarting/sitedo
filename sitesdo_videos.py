#!/usr/bin/env python3
"""
sitesdo_videos.py
Scrapes unique video page links from elements with class "item-post" on one or
more listing URLs (auto-scrolling for infinite/lazy content), visits each video
page to extract the actual media URL (preferring .mp4), downloads only new
videos, uploads them in one batch to Mega.nz via rclone, and logs
File Name + Caption to a Google Sheet.

Duplicate prevention:
    Before anything else, urls_already_downloaded_videos.txt is pulled from the
    ROOT of the Mega remote (shared across every folder/run). Any scraped video
    page URL already in that file is skipped. Newly-seen URLs are written into
    the local copy BEFORE download starts, and the updated file OVERWRITES the
    Mega root copy at the end of the run.

Usage (local):
    python sitesdo_videos.py --url "https://example.com/videos" --folder-name "MyVideos"
    # or multi-line / comma-separated via env

Env vars (GitHub Actions):
    PAGE_URL, MEGA_FOLDER_NAME, MAX_IDLE_SCROLLS, MAX_VIDEOS, ...
"""

import argparse
import hashlib
import os
import re
import subprocess
import sys
import time
import datetime
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from playwright.sync_api import sync_playwright
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
ITEM_SELECTOR = ".item-post"
DOWNLOAD_DIR = Path("downloaded_videos")
DEDUP_FILENAME = "urls_already_downloaded_videos.txt"
DEDUP_LOCAL_PATH = Path(DEDUP_FILENAME)
STRIP_PHRASES_FILENAME = "strip_phrases.txt"
STRIP_PHRASES_LOCAL_PATH = Path(STRIP_PHRASES_FILENAME)
DEFAULT_SPREADSHEET_ID = "15_9D1UMPIYkq3vgeLxf4WdIxIrl_S_Oo_BmkIOpr7ng"
SHEET_HEADER = ["File Name", "Caption"]


def log(msg: str):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def parse_urls(raw: str | list[str] | None) -> list[str]:
    """Turn a string (newlines / commas) or list into a clean list of unique URLs."""
    if not raw:
        return []
    if isinstance(raw, str):
        raw = [raw]
    parts = []
    for item in raw:
        for line in str(item).splitlines():
            parts.extend(line.split(","))
    urls = []
    seen = set()
    for p in parts:
        u = p.strip()
        if u and u not in seen:
            seen.add(u)
            urls.append(u)
    return urls


def normalize_url(src: str, base_url: str) -> str:
    """Turn protocol-relative or relative URLs into absolute https URLs using base_url."""
    if not src:
        return src
    src = src.strip()
    if src.startswith("//"):
        return "https:" + src
    if src.startswith(("http://", "https://")):
        return src
    # relative path → join with base
    return urljoin(base_url, src)


def parse_args():
    p = argparse.ArgumentParser(
        description="Scrape .item-post video pages, download the videos and upload to Mega.nz"
    )
    p.add_argument(
        "--url",
        action="append",
        default=None,
        help="Listing page URL to scrape. Can be given multiple times. "
             "Also accepts a single value with commas or newlines.",
    )
    p.add_argument(
        "--folder-name",
        default=os.environ.get("MEGA_FOLDER_NAME"),
        help="Mega folder path to upload videos into",
    )
    p.add_argument(
        "--max-idle-scrolls",
        type=int,
        default=int(os.environ.get("MAX_IDLE_SCROLLS", "8")),
        help="Stop after this many consecutive scrolls with no new videos (default: 8)",
    )
    p.add_argument(
        "--max-videos",
        type=int,
        default=(int(os.environ["MAX_VIDEOS"]) if os.environ.get("MAX_VIDEOS") else None),
        help="Optional cap on total videos to scrape/download/upload across all URLs.",
    )
    p.add_argument(
        "--spreadsheet-id",
        default=os.environ.get("SPREADSHEET_ID", DEFAULT_SPREADSHEET_ID),
        help="Google Sheet ID to log file names + captions into",
    )
    p.add_argument(
        "--sheet-tab",
        default=os.environ.get("SHEET_TAB") or None,
        help="Tab name inside the spreadsheet. If omitted, folder name is used.",
    )
    p.add_argument(
        "--download-concurrency",
        type=int,
        default=int(os.environ.get("DOWNLOAD_CONCURRENCY", "4")),
        help="How many videos to download in parallel (default: 4 – keep low for large files)",
    )
    p.add_argument(
        "--upload-transfers",
        type=int,
        default=int(os.environ.get("UPLOAD_TRANSFERS", "4")),
        help="How many files rclone uploads to Mega in parallel (default: 4)",
    )
    p.add_argument(
        "--rclone-config",
        default=os.environ.get("RCLONE_CONFIG_PATH", "rclone.conf"),
    )
    p.add_argument(
        "--rclone-remote",
        default=os.environ.get("RCLONE_REMOTE_NAME", "mega"),
    )
    p.add_argument("--headless", action="store_true", default=True)
    args = p.parse_args()

    cli_urls = args.url or []
    env_urls = os.environ.get("PAGE_URL", "")
    all_raw = cli_urls + ([env_urls] if env_urls else [])
    args.urls = parse_urls(all_raw)
    if not args.urls:
        sys.exit("ERROR: at least one URL is required (--url or PAGE_URL env var)")
    if not args.folder_name:
        sys.exit("ERROR: --folder-name (or MEGA_FOLDER_NAME env var) is required")
    return args


def is_video_page_href(href: str) -> bool:
    """Keep only links that look like video detail pages."""
    if not href:
        return False
    path = urlparse(href).path.lower()
    return "/videos/" in path and not path.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))


def scrape_listing(url: str, max_idle_scrolls: int, max_videos: int | None = None) -> dict:
    """
    Scroll one listing page, collecting unique video-page URLs + captions.
    Returns: {video_page_url: caption}
    """
    found = {}
    idle_scrolls = 0
    scroll_count = 0

    log(f"Launching browser and opening listing: {url}")
    if max_videos:
        log(f"  (remaining video budget for this page: {max_videos})")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        log("Page loaded. Waiting for initial items to render...")
        page.wait_for_timeout(2000)

        extract_js = """
        els => els.map(el => {
            // The whole card is often an <a class="item-post">
            let a = el.tagName === 'A' ? el : el.querySelector('a[href*="/videos/"]');
            if (!a) a = el.querySelector('a');
            const href = a ? (a.getAttribute('href') || a.href) : null;

            let caption = '';
            // Prefer the visible title (h3 in the provided markup)
            const h3 = el.querySelector('h3');
            if (h3) caption = h3.textContent.trim();
            if (!caption) {
                const h2 = el.querySelector('h2, .info h2, .title, .elips');
                if (h2) caption = h2.textContent.trim();
            }
            if (!caption) {
                const img = el.querySelector('img[alt]');
                if (img) caption = (img.getAttribute('alt') || '').trim();
            }
            if (!caption) {
                caption = (el.textContent || '').trim().slice(0, 300);
            }
            return {href, caption};
        })
        """

        while True:
            items = page.eval_on_selector_all(ITEM_SELECTOR, extract_js)
            total_this_round = 0
            new_this_round = 0
            dup_this_round = 0

            for item in items:
                href = item.get("href")
                caption = (item.get("caption") or "").strip()
                if not href:
                    continue
                href = normalize_url(href, url)
                if not is_video_page_href(href):
                    continue

                total_this_round += 1
                if href in found:
                    dup_this_round += 1
                else:
                    found[href] = caption
                    new_this_round += 1
                    if max_videos and len(found) >= max_videos:
                        break

            log(
                f"  Scroll #{scroll_count}: found {total_this_round} video link(s) this scroll "
                f"→ +{new_this_round} new, {dup_this_round} duplicate(s) | "
                f"{len(found)} unique total (idle: {idle_scrolls}/{max_idle_scrolls})"
            )

            if max_videos and len(found) >= max_videos:
                log(f"  Reached video limit for this page ({max_videos}). Stopping.")
                break

            if new_this_round == 0:
                idle_scrolls += 1
            else:
                idle_scrolls = 0

            if idle_scrolls >= max_idle_scrolls:
                log(f"  No new videos for {max_idle_scrolls} consecutive scrolls. Stopping.")
                break

            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            scroll_count += 1
            log(f"  Scrolled to bottom (scroll #{scroll_count}), waiting...")
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                log("  (network still busy after 5s, continuing)")
            page.wait_for_timeout(1500)

        browser.close()
        log("Browser closed for this listing URL.")

    if max_videos and len(found) > max_videos:
        found = dict(list(found.items())[:max_videos])
    return found


def scrape_all_listings(urls: list[str], max_idle_scrolls: int, max_videos: int | None) -> dict:
    """Scrape every listing URL, merging results. Stops early if max_videos is hit."""
    all_found: dict[str, str] = {}
    remaining = max_videos
    for i, url in enumerate(urls, 1):
        log(f"=== Scraping listing URL {i}/{len(urls)} ===")
        page_found = scrape_listing(url, max_idle_scrolls, remaining)
        before = len(all_found)
        all_found.update(page_found)
        added = len(all_found) - before
        log(f"URL {i} contributed {added} new unique video pages (total so far: {len(all_found)})")
        if max_videos is not None:
            remaining = max_videos - len(all_found)
            if remaining <= 0:
                log(f"Global video limit of {max_videos} reached. Skipping remaining URLs.")
                break
    return all_found


def extract_video_src_from_page(page_url: str) -> str | None:
    """
    Visit a video detail page and return the best direct media URL we can find.
    Preference order: .mp4 > other video extensions > m3u8 / any video src.
    """
    log(f"  Extracting media from: {page_url}")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        )
        try:
            page.goto(page_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)

            # Try several common patterns
            candidates = page.evaluate("""
            () => {
                const urls = new Set();
                // <video src="...">
                document.querySelectorAll('video').forEach(v => {
                    if (v.src) urls.add(v.src);
                    if (v.currentSrc) urls.add(v.currentSrc);
                });
                // <source src="...">
                document.querySelectorAll('video source, source[type*="video"]').forEach(s => {
                    if (s.src) urls.add(s.src);
                });
                // data attributes often used by players
                document.querySelectorAll('[data-src], [data-video], [data-mp4], [data-file]').forEach(el => {
                    ['data-src','data-video','data-mp4','data-file'].forEach(attr => {
                        const val = el.getAttribute(attr);
                        if (val && (val.includes('.mp4') || val.includes('.m3u8') || val.includes('/video'))) {
                            urls.add(val);
                        }
                    });
                });
                // any <a> that points to a media file
                document.querySelectorAll('a[href*=".mp4"], a[href*=".webm"], a[href*=".m3u8"]').forEach(a => {
                    urls.add(a.href);
                });
                // common player config in scripts (best-effort)
                const scripts = Array.from(document.querySelectorAll('script')).map(s => s.textContent || '');
                const re = /https?:\\/\\/[^"'\\s]+\\.(mp4|webm|m3u8)(?:\\?[^"'\\s]*)?/gi;
                scripts.forEach(txt => {
                    let m;
                    while ((m = re.exec(txt)) !== null) {
                        urls.add(m[0]);
                    }
                });
                return Array.from(urls);
            }
            """)

            browser.close()

            if not candidates:
                log(f"    No media URL found on {page_url}")
                return None

            # Prefer direct mp4
            mp4s = [u for u in candidates if ".mp4" in u.lower()]
            if mp4s:
                chosen = normalize_url(mp4s[0], page_url)
                log(f"    Found mp4: {chosen}")
                return chosen

            # then other video files
            others = [u for u in candidates if any(ext in u.lower() for ext in (".webm", ".mov", ".m4v"))]
            if others:
                chosen = normalize_url(others[0], page_url)
                log(f"    Found other video: {chosen}")
                return chosen

            # finally m3u8 or whatever we have
            chosen = normalize_url(candidates[0], page_url)
            log(f"    Found candidate: {chosen}")
            return chosen

        except Exception as e:
            log(f"    Failed to extract from {page_url}: {e}")
            try:
                browser.close()
            except Exception:
                pass
            return None


def resolve_media_urls(page_url_to_caption: dict) -> dict:
    """
    For every video page URL, extract the real media URL.
    Returns: {media_url: caption}  (only those we could resolve)
    Also keeps a mapping so we can still dedup on the original page URL.
    """
    media_map = {}          # media_url → caption
    page_to_media = {}      # page_url → media_url  (for logging / debugging)

    for i, (page_url, caption) in enumerate(page_url_to_caption.items(), 1):
        log(f"[{i}/{len(page_url_to_caption)}] Resolving media for page...")
        media = extract_video_src_from_page(page_url)
        if media:
            media_map[media] = caption or page_url
            page_to_media[page_url] = media
        else:
            log(f"  Skipping (no media found): {page_url}")

    log(f"Successfully resolved {len(media_map)} media URLs out of {len(page_url_to_caption)} pages.")
    return media_map, page_to_media


_progress_lock = threading.Lock()


def _download_one(src: str, caption: str, headers: dict, used_names: set,
                  names_lock: threading.Lock, max_retries: int = 3):
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(src, headers=headers, timeout=120, stream=True)
            resp.raise_for_status()
            break
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                time.sleep(2 * attempt)
            else:
                raise last_err

    # Derive a sensible filename
    path = urlparse(src).path
    name = os.path.basename(path)
    if not name or "." not in name:
        # fall back to hash + extension guessed from content-type or url
        ext = ".mp4"
        if ".webm" in src.lower():
            ext = ".webm"
        elif ".m3u8" in src.lower():
            ext = ".m3u8"
        name = hashlib.sha1(src.encode()).hexdigest() + ext
    else:
        # clean query-string junk
        name = name.split("?")[0]

    with names_lock:
        if name in used_names:
            stem, ext = os.path.splitext(name)
            name = f"{stem}_{hashlib.sha1(src.encode()).hexdigest()[:6]}{ext}"
        used_names.add(name)

    dest = DOWNLOAD_DIR / name
    with open(dest, "wb") as f:
        for chunk in resp.iter_content(1024 * 256):  # 256 KB chunks for large files
            if chunk:
                f.write(chunk)
    return dest, src, caption


def download_videos(urls_captions: dict, concurrency: int = 4) -> list:
    """Returns list of (dest_path, source_url, caption) for successful downloads."""
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Referer": "https://www.google.com/",
    }
    items = sorted(urls_captions.items())
    total = len(items)
    used_names = set()
    names_lock = threading.Lock()
    saved = []
    completed = 0

    log(f"Starting parallel download of {total} videos ({concurrency} at a time)...")

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        future_to_src = {
            executor.submit(_download_one, src, caption, headers, used_names, names_lock): src
            for src, caption in items
        }
        for future in as_completed(future_to_src):
            src = future_to_src[future]
            with _progress_lock:
                completed += 1
                current = completed
            try:
                dest, source_url, caption = future.result()
                size_mb = dest.stat().st_size / (1024 * 1024)
                saved.append((dest, source_url, caption))
                log(f"  [{current}/{total}] saved: {dest.name} ({size_mb:.1f} MB)")
            except Exception as e:
                log(f"  [{current}/{total}] FAILED to download {src}: {e}")

    log(f"Download finished: {len(saved)}/{total} videos saved successfully.")
    return saved


# ---------------------------------------------------------------------------
# Mega.nz helpers (via rclone)
# ---------------------------------------------------------------------------
def rclone_remote_target(remote_name: str, config_path: str, folder_name: str) -> str:
    if not os.path.exists(config_path):
        sys.exit(f"ERROR: rclone config file not found at {config_path}")
    return f"{remote_name}:{folder_name}"


def pull_dedup_file(remote_root: str, config_path: str):
    result = subprocess.run(
        ["rclone", "--config", config_path, "copyto",
         f"{remote_root}{DEDUP_FILENAME}", str(DEDUP_LOCAL_PATH)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log("No existing urls_already_downloaded_videos.txt on Mega yet – starting fresh.")
        DEDUP_LOCAL_PATH.write_text("")
    else:
        log("Pulled existing urls_already_downloaded_videos.txt from Mega root.")


def pull_strip_phrases_file(remote_root: str, config_path: str):
    """Pull strip_phrases.txt from Mega root (one phrase per line)."""
    result = subprocess.run(
        ["rclone", "--config", config_path, "copyto",
         f"{remote_root}{STRIP_PHRASES_FILENAME}", str(STRIP_PHRASES_LOCAL_PATH)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log("No strip_phrases.txt on Mega root – captions will not be filtered.")
        STRIP_PHRASES_LOCAL_PATH.write_text("")
    else:
        log("Pulled strip_phrases.txt from Mega root.")


def load_strip_phrases() -> list[str]:
    """Load non-empty phrases from the local strip file (one phrase per line)."""
    if not STRIP_PHRASES_LOCAL_PATH.exists():
        return []
    phrases = []
    for line in STRIP_PHRASES_LOCAL_PATH.read_text(encoding="utf-8", errors="ignore").splitlines():
        phrase = line.strip()
        if phrase:
            phrases.append(phrase)
    return phrases


def strip_caption(caption: str, phrases: list[str]) -> str:
    """
    Remove whole phrases from caption (case-insensitive).
    Only matches the exact phrase text from each line — never individual words
    unless a line in strip_phrases.txt is a single word by itself.
    Cleans up leftover whitespace / punctuation artifacts afterwards.
    """
    if not caption or not phrases:
        return (caption or "").strip()

    result = caption
    for phrase in phrases:
        # Case-insensitive whole-phrase removal (not word-by-word)
        pattern = re.compile(re.escape(phrase), re.IGNORECASE)
        result = pattern.sub(" ", result)

    # Collapse multiple spaces / newlines and tidy edges
    result = re.sub(r"\s+", " ", result).strip()
    # Optional: strip leftover leading/trailing separators that phrases often leave
    result = re.sub(r"^[\s\-–—|:;,.]+|[\s\-–—|:;,]+$", "", result).strip()
    return result


def load_existing_urls() -> set:
    if DEDUP_LOCAL_PATH.exists():
        return {line.strip() for line in DEDUP_LOCAL_PATH.read_text().splitlines() if line.strip()}
    return set()


def record_new_urls(urls):
    with open(DEDUP_LOCAL_PATH, "a") as f:
        for u in urls:
            f.write(u + "\n")


def push_dedup_file(remote_root: str, config_path: str):
    result = subprocess.run(
        ["rclone", "--config", config_path, "copyto",
         str(DEDUP_LOCAL_PATH), f"{remote_root}{DEDUP_FILENAME}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log(f"⚠️ Failed to push updated urls_already_downloaded_videos.txt to Mega: {result.stderr.strip()[-500:]}")
    else:
        log("✅ urls_already_downloaded_videos.txt updated on Mega root.")


def rclone_upload_all(remote_target: str, config_path: str, transfers: int = 4) -> bool:
    """One-shot parallel copy of the whole download directory to Mega."""
    log(f"⬆️ Uploading batch to '{remote_target}' via rclone "
        f"({transfers} parallel transfers)...")
    result = subprocess.run(
        [
            "rclone", "--config", config_path, "copy", str(DOWNLOAD_DIR), remote_target,
            "--transfers", str(transfers),
            "--checkers", str(max(transfers * 2, 8)),
            "--retries", "5",
            "--low-level-retries", "10",
            "--contimeout", "60s",
            "--timeout", "600s",
            "--stats", "30s",
            "-v",
        ],
        capture_output=True, text=True,
    )
    if result.stdout:
        log(result.stdout.strip()[-2000:])
    if result.returncode != 0:
        log(f"⚠️ rclone copy failed: {result.stderr.strip()[-800:]}")
        return False
    log("✅ Batch upload to Mega complete.")
    return True


# ---------------------------------------------------------------------------
# Google Sheets helpers
# ---------------------------------------------------------------------------
def get_sheets_service():
    creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "google_creds.json")
    if not os.path.exists(creds_path):
        sys.exit(f"ERROR: Google credentials file not found at {creds_path}")
    creds = Credentials.from_service_account_file(creds_path, scopes=SHEETS_SCOPES)
    return build("sheets", "v4", credentials=creds)


def sanitize_sheet_tab_name(name: str) -> str:
    cleaned = re.sub(r'[\[\]\*\?/\\:]', "_", name).strip()
    return (cleaned or "Sheet")[:100]


def ensure_sheet_tab(service, spreadsheet_id: str, tab_name: str):
    meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    existing_titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
    if tab_name in existing_titles:
        return
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": tab_name}}}]},
    ).execute()
    log(f"📄 Created new sheet tab '{tab_name}'.")


def ensure_sheet_header(service, spreadsheet_id: str, sheet_tab: str):
    try:
        existing = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id, range=f"{sheet_tab}!A1:B1"
        ).execute().get("values", [])
    except Exception as e:
        log(f"! Could not read sheet header (check sharing/permissions): {e}")
        return
    if not existing:
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id, range=f"{sheet_tab}!A1:B1",
            valueInputOption="RAW", body={"values": [SHEET_HEADER]},
        ).execute()
        log("📝 Wrote sheet header row.")


def log_to_sheet(spreadsheet_id: str, sheet_tab: str, saved: list, strip_phrases: list[str] | None = None):
    if not spreadsheet_id:
        log("No spreadsheet ID configured — skipping sheet logging.")
        return
    if not saved:
        log("Nothing new to log to Sheets.")
        return
    phrases = strip_phrases or []
    if phrases:
        log(f"Stripping {len(phrases)} phrase(s) from captions before writing to sheet...")
    log(f"Writing {len(saved)} row(s) to Google Sheet ({sheet_tab})...")
    service = get_sheets_service()
    ensure_sheet_tab(service, spreadsheet_id, sheet_tab)
    ensure_sheet_header(service, spreadsheet_id, sheet_tab)
    rows = []
    for dest, _src, caption in saved:
        clean_caption = strip_caption(caption, phrases)
        rows.append([dest.name, clean_caption])
    try:
        result = service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=f"{sheet_tab}!A:B",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": rows},
        ).execute()
        updated_rows = result.get("updates", {}).get("updatedRows", 0)
        log(f"Sheet updated: {updated_rows} row(s) appended to '{sheet_tab}'.")
    except Exception as e:
        log(f"! Failed to write to Google Sheet: {e}")
        log("  (Make sure the spreadsheet ID/tab are correct and shared with the service account.)")


def main():
    args = parse_args()
    log("=== Starting video scrape run ===")
    log(f"Listing URLs to scrape ({len(args.urls)}):")
    for i, u in enumerate(args.urls, 1):
        log(f"  {i}. {u}")
    log(f"Mega folder: {args.folder_name}")
    if args.max_videos:
        log(f"Video limit (global): {args.max_videos}")
    else:
        log(f"Video limit: none (stop = {args.max_idle_scrolls} idle scrolls per page)")
    log(f"Download concurrency: {args.download_concurrency}")
    log(f"Upload transfers: {args.upload_transfers}")

    remote_target = rclone_remote_target(args.rclone_remote, args.rclone_config, args.folder_name)
    remote_root = f"{args.rclone_remote}:"

    log("🔎 Checking for previously-downloaded video page URLs on Mega (shared root file)...")
    pull_dedup_file(remote_root, args.rclone_config)
    existing_urls = load_existing_urls()
    log(f"{len(existing_urls)} video page URL(s) already recorded as downloaded.")

    log("🔎 Pulling strip_phrases.txt from Mega root (for caption cleaning)...")
    pull_strip_phrases_file(remote_root, args.rclone_config)
    strip_phrases = load_strip_phrases()
    if strip_phrases:
        log(f"Loaded {len(strip_phrases)} phrase(s) to strip from captions:")
        for ph in strip_phrases:
            log(f"  - {ph!r}")
    else:
        log("No phrases loaded — captions will be written as-is.")

    # 1. Scrape listing pages → unique video *page* URLs + captions
    all_pages = scrape_all_listings(args.urls, args.max_idle_scrolls, args.max_videos)
    log(f"=== Listing scrape complete: {len(all_pages)} unique video pages found ===")

    if not all_pages:
        log("No video pages found — nothing to do.")
        return

    # 2. Filter out already-seen pages (dedup key = video page URL)
    new_pages = {u: c for u, c in all_pages.items() if u not in existing_urls}
    skipped = len(all_pages) - len(new_pages)
    log(f"{skipped} already downloaded previously (skipped), {len(new_pages)} new.")

    if not new_pages:
        log("Nothing new to process — done.")
        return

    # Record intent BEFORE we start downloading (so a crash still marks them as seen)
    record_new_urls(new_pages.keys())

    # 3. Visit each new video page and extract the real media URL
    media_map, _page_to_media = resolve_media_urls(new_pages)

    if not media_map:
        log("Could not resolve any media URLs — nothing to download.")
        push_dedup_file(remote_root, args.rclone_config)
        return

    # 4. Download
    saved = download_videos(media_map, concurrency=args.download_concurrency)

    # 5. Upload
    if saved:
        rclone_upload_all(remote_target, args.rclone_config, transfers=args.upload_transfers)
        for dest, _src, _caption in saved:
            if dest.exists():
                dest.unlink()
    else:
        log("No videos were successfully downloaded — nothing to upload.")

    # 6. Push updated dedup file + log to sheet (captions cleaned of strip phrases)
    push_dedup_file(remote_root, args.rclone_config)
    log_to_sheet(
        args.spreadsheet_id,
        args.sheet_tab or sanitize_sheet_tab_name(args.folder_name),
        saved,
        strip_phrases=strip_phrases,
    )
    log("=== Done ===")


if __name__ == "__main__":
    main()

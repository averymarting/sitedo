#!/usr/bin/env python3
"""
scrape_upload.py

Scrapes <img> src values (and a best-effort caption) from elements with
class "item-post" on a given page URL, auto-scrolling to trigger lazy-loaded
/ infinite-scroll content. Only .jpg images are kept.

New images are downloaded, then uploaded to Mega.nz (via rclone) in a single
batch pass, and their File Name + Caption are logged to a Google Sheet.

Duplicate prevention:
    Before anything else, urls_already_downloaded.txt is pulled from the
    ROOT of the Mega remote (shared across every folder/run, not per-folder,
    so the same file protects against duplicates no matter which folder
    you're scraping into this run). Any scraped image URL already in that
    file is skipped entirely (not downloaded, not logged). Newly-seen URLs
    are written into the local copy BEFORE download starts (so a crash
    mid-run doesn't cause endless re-attempts), and the updated file
    OVERWRITES the Mega root copy at the end of the run (not appended).

Usage:
    python scrape_upload.py --url "https://example.com/page" --folder-name "MyFolder"

Env vars (used by the GitHub Actions workflow, but work locally too):
    PAGE_URL                 -> same as --url
    MEGA_FOLDER_NAME          -> same as --folder-name (folder path on the Mega remote)
    RCLONE_CONFIG_PATH        -> path to rclone.conf (default "rclone.conf")
    RCLONE_REMOTE_NAME        -> name of the remote inside rclone.conf (default "mega")
    GOOGLE_APPLICATION_CREDENTIALS -> path to Google service-account JSON
    MAX_IDLE_SCROLLS          -> (optional) override the default of 8
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
from urllib.parse import urlparse

import requests
from playwright.sync_api import sync_playwright

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
ITEM_SELECTOR = ".item-post"
DOWNLOAD_DIR = Path("downloaded_images")
DEDUP_FILENAME = "urls_already_downloaded.txt"
DEDUP_LOCAL_PATH = Path(DEDUP_FILENAME)

# Default target sheet. Can be overridden with the SPREADSHEET_ID env var / --spreadsheet-id flag.
DEFAULT_SPREADSHEET_ID = "1OQns3xUPeTQslsw0FaD-a85DAM0Sc_L6BnaGDMqGPmY"
SHEET_HEADER = ["File Name", "Caption"]


def log(msg: str):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def parse_args():
    p = argparse.ArgumentParser(description="Scrape .item-post images and upload to Mega.nz")
    p.add_argument("--url", default=os.environ.get("PAGE_URL"), help="Page URL to scrape")
    p.add_argument("--folder-name", default=os.environ.get("MEGA_FOLDER_NAME"),
                   help="Mega folder path to upload images into")
    p.add_argument("--max-idle-scrolls", type=int,
                   default=int(os.environ.get("MAX_IDLE_SCROLLS", "8")),
                   help="Stop after this many consecutive scrolls with no new images (default: 8)")
    p.add_argument("--max-images", type=int,
                   default=(int(os.environ["MAX_IMAGES"]) if os.environ.get("MAX_IMAGES") else None),
                   help="Optional cap on total images to scrape/download/upload.")
    p.add_argument("--spreadsheet-id", default=os.environ.get("SPREADSHEET_ID", DEFAULT_SPREADSHEET_ID),
                   help="Google Sheet ID to log file names + captions into")
    p.add_argument("--sheet-tab", default=os.environ.get("SHEET_TAB") or None,
                   help="Tab name inside the spreadsheet to append rows to. "
                        "If omitted, the folder name is used as the tab name "
                        "(created automatically if it doesn't exist yet).")
    p.add_argument("--download-concurrency", type=int,
                   default=int(os.environ.get("DOWNLOAD_CONCURRENCY", "10")),
                   help="How many images to download in parallel (default: 10)")
    p.add_argument("--rclone-config", default=os.environ.get("RCLONE_CONFIG_PATH", "rclone.conf"))
    p.add_argument("--rclone-remote", default=os.environ.get("RCLONE_REMOTE_NAME", "mega"))
    p.add_argument("--headless", action="store_true", default=True)
    args = p.parse_args()

    if not args.url:
        sys.exit("ERROR: --url (or PAGE_URL env var) is required")
    if not args.folder_name:
        sys.exit("ERROR: --folder-name (or MEGA_FOLDER_NAME env var) is required")
    return args


def is_jpg(url: str) -> bool:
    path = urlparse(url).path.lower()
    return path.endswith(".jpg") or path.endswith(".jpeg")


def scrape_images(url: str, max_idle_scrolls: int, max_images: int | None = None) -> dict:
    """Scroll the page repeatedly, collecting unique .jpg/.jpeg image URLs
    inside .item-post blocks, along with a best-effort caption for each.

    Returns: {image_url: caption}
    """
    found = {}
    idle_scrolls = 0
    scroll_count = 0

    log(f"Launching browser and opening: {url}")
    if max_images:
        log(f"Image limit set: will stop as soon as {max_images} images are found.")
    else:
        log(f"No image limit set: will scrape until {max_idle_scrolls} consecutive idle scrolls.")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        log("Page loaded. Waiting for initial images to render...")
        page.wait_for_timeout(2000)

        # Caption comes from the actual visible link text inside
        # .info h2.elips a (e.g. "Chef now for foods recipes"), never
        # from the alt/title attribute. Falls back to the same
        # element's text, then common caption classes, then the
        # block's own trimmed text as a last resort.
        extract_js = """
        els => els.map(el => {
            const img = el.querySelector('img');
            const src = img ? (img.getAttribute('src') || img.src) : null;
            let caption = '';
            const capLink = el.querySelector('.info h2.elips a');
            if (capLink) caption = capLink.textContent.trim();
            if (!caption) {
                const capHeading = el.querySelector('.info h2.elips, h2.elips');
                if (capHeading) caption = capHeading.textContent.trim();
            }
            if (!caption) {
                const capEl = el.querySelector('.caption, .title, figcaption, .desc, .description');
                if (capEl) caption = capEl.textContent.trim();
            }
            if (!caption) {
                caption = el.textContent.trim().slice(0, 300);
            }
            return {src, caption};
        })
        """

        while True:
            items = page.eval_on_selector_all(ITEM_SELECTOR, extract_js)
            new_this_round = 0
            for item in items:
                src = item.get("src")
                caption = (item.get("caption") or "").strip()
                if src and is_jpg(src) and src not in found:
                    found[src] = caption
                    new_this_round += 1
                    if max_images and len(found) >= max_images:
                        break

            log(f"Scroll #{scroll_count}: {len(found)} unique jpgs found so far "
                f"(+{new_this_round} new this round, idle streak: {idle_scrolls}/{max_idle_scrolls})")

            if max_images and len(found) >= max_images:
                log(f"Reached the requested limit of {max_images} images. Stopping scroll loop.")
                break

            if new_this_round == 0:
                idle_scrolls += 1
            else:
                idle_scrolls = 0

            if idle_scrolls >= max_idle_scrolls:
                log(f"No new images for {max_idle_scrolls} consecutive scrolls. Stopping scroll loop.")
                break

            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            scroll_count += 1
            log(f"Scrolled to bottom (scroll #{scroll_count}), waiting for new content to load...")
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                log("  (network still busy after 5s, continuing anyway)")
            page.wait_for_timeout(1500)

        browser.close()
        log("Browser closed.")

    if max_images and len(found) > max_images:
        found = dict(list(found.items())[:max_images])

    return found


_progress_lock = threading.Lock()


def _download_one(src: str, caption: str, headers: dict, used_names: set,
                   names_lock: threading.Lock, max_retries: int = 3):
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(src, headers=headers, timeout=30, stream=True)
            resp.raise_for_status()
            break
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                time.sleep(1.5 * attempt)
            else:
                raise last_err

    name = os.path.basename(urlparse(src).path)
    if not name.lower().endswith((".jpg", ".jpeg")):
        name = hashlib.sha1(src.encode()).hexdigest() + ".jpg"

    with names_lock:
        if name in used_names:
            stem, ext = os.path.splitext(name)
            name = f"{stem}_{hashlib.sha1(src.encode()).hexdigest()[:6]}{ext}"
        used_names.add(name)

    dest = DOWNLOAD_DIR / name
    with open(dest, "wb") as f:
        for chunk in resp.iter_content(8192):
            f.write(chunk)

    return dest, src, caption


def download_images(urls_captions: dict, concurrency: int = 10) -> list:
    """Returns list of (dest_path, source_url, caption) for successful downloads."""
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": "Mozilla/5.0"}
    items = sorted(urls_captions.items())
    total = len(items)
    used_names = set()
    names_lock = threading.Lock()
    saved = []
    completed = 0

    log(f"Starting parallel download of {total} images ({concurrency} at a time)...")
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
                saved.append((dest, source_url, caption))
                log(f"  [{current}/{total}] saved: {dest.name}")
            except Exception as e:
                log(f"  [{current}/{total}] FAILED to download {src}: {e}")

    log(f"Download finished: {len(saved)}/{total} images saved successfully.")
    return saved


# ---------------------------------------------------------------------------
# Mega.nz helpers (via rclone)
# ---------------------------------------------------------------------------
def rclone_remote_target(remote_name: str, config_path: str, folder_name: str) -> str:
    if not os.path.exists(config_path):
        sys.exit(f"ERROR: rclone config file not found at {config_path}")
    return f"{remote_name}:{folder_name}"


def pull_dedup_file(remote_root: str, config_path: str):
    """Fetch the existing dedup list from the ROOT of the Mega remote (shared
    across every folder this scraper is ever pointed at, not per-folder).
    If it doesn't exist yet (first run overall), start with an empty file."""
    result = subprocess.run(
        ["rclone", "--config", config_path, "copyto",
         f"{remote_root}{DEDUP_FILENAME}", str(DEDUP_LOCAL_PATH)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log("No existing urls_already_downloaded.txt on Mega yet - starting fresh.")
        DEDUP_LOCAL_PATH.write_text("")
    else:
        log("Pulled existing urls_already_downloaded.txt from Mega root.")


def load_existing_urls() -> set:
    if DEDUP_LOCAL_PATH.exists():
        return {line.strip() for line in DEDUP_LOCAL_PATH.read_text().splitlines() if line.strip()}
    return set()


def record_new_urls(urls):
    """Append newly-seen URLs to the local dedup file BEFORE download starts,
    so a crash mid-run won't cause the same URLs to be retried forever."""
    with open(DEDUP_LOCAL_PATH, "a") as f:
        for u in urls:
            f.write(u + "\n")


def push_dedup_file(remote_root: str, config_path: str):
    """Overwrite (not append) the copy at the Mega ROOT so it always
    reflects the current local state, shared across all folders."""
    result = subprocess.run(
        ["rclone", "--config", config_path, "copyto",
         str(DEDUP_LOCAL_PATH), f"{remote_root}{DEDUP_FILENAME}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log(f"⚠️ Failed to push updated urls_already_downloaded.txt to Mega: {result.stderr.strip()[-500:]}")
    else:
        log("✅ urls_already_downloaded.txt updated on Mega root.")


def rclone_upload_all(remote_target: str, config_path: str, transfers: int = 8) -> bool:
    """One-shot copy of the whole download directory to Mega, tuned for
    throughput and resilience since this can push many files in one call."""
    log(f"⬆️ Uploading batch to '{remote_target}' via rclone...")
    result = subprocess.run(
        [
            "rclone", "--config", config_path, "copy", str(DOWNLOAD_DIR), remote_target,
            "--transfers", str(transfers),
            "--checkers", str(transfers * 2),
            "--retries", "5",
            "--low-level-retries", "10",
            "--contimeout", "30s",
            "--timeout", "300s",
            "--stats", "30s",
            "-v",
        ],
        capture_output=True, text=True,
    )
    if result.stdout:
        log(result.stdout.strip()[-1500:])
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
    """Google Sheets tab names can't contain [ ] * ? / \\ : and have a
    100-char limit, so clean the folder name up before using it as a tab
    name."""
    cleaned = re.sub(r'[\[\]\*\?/\\:]', "_", name).strip()
    return (cleaned or "Sheet")[:100]


def ensure_sheet_tab(service, spreadsheet_id: str, tab_name: str):
    """Create the tab if it doesn't exist yet; do nothing (and definitely
    don't touch existing data) if it already does."""
    meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    existing_titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
    if tab_name in existing_titles:
        return
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": tab_name}}}]},
    ).execute()
    log(f"📄 Created new sheet tab '{tab_name}' (folder-specific).")


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


def log_to_sheet(spreadsheet_id: str, sheet_tab: str, saved: list):
    if not spreadsheet_id:
        log("No spreadsheet ID configured — skipping sheet logging.")
        return
    if not saved:
        log("Nothing new to log to Sheets.")
        return

    log(f"Writing {len(saved)} row(s) to Google Sheet ({sheet_tab})...")
    service = get_sheets_service()
    ensure_sheet_tab(service, spreadsheet_id, sheet_tab)
    ensure_sheet_header(service, spreadsheet_id, sheet_tab)

    rows = [[dest.name, caption] for dest, _src, caption in saved]

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

    log("=== Starting run ===")
    log(f"Page URL: {args.url}")
    log(f"Mega folder: {args.folder_name}")
    if args.max_images:
        log(f"Image limit: {args.max_images}")
    else:
        log(f"Image limit: none (stop condition = {args.max_idle_scrolls} consecutive idle scrolls)")

    remote_target = rclone_remote_target(args.rclone_remote, args.rclone_config, args.folder_name)
    remote_root = f"{args.rclone_remote}:"

    log("🔎 Checking for previously-downloaded URLs on Mega (shared root file)...")
    pull_dedup_file(remote_root, args.rclone_config)
    existing_urls = load_existing_urls()
    log(f"{len(existing_urls)} URL(s) already recorded as downloaded.")

    all_found = scrape_images(args.url, args.max_idle_scrolls, args.max_images)
    log(f"=== Scrape complete: {len(all_found)} unique .jpg images found on page ===")

    if not all_found:
        log("No images found — nothing to do.")
        return

    new_items = {u: c for u, c in all_found.items() if u not in existing_urls}
    skipped = len(all_found) - len(new_items)
    log(f"{skipped} already downloaded previously (skipped), {len(new_items)} new.")

    if not new_items:
        log("Nothing new to download — done.")
        return

    # Record intent BEFORE downloading, so a crash mid-run won't cause retries
    record_new_urls(new_items.keys())

    saved = download_images(new_items, concurrency=args.download_concurrency)

    if saved:
        rclone_upload_all(remote_target, args.rclone_config)
        for dest, _src, _caption in saved:
            if dest.exists():
                dest.unlink()
    else:
        log("No images were successfully downloaded — nothing to upload.")

    # Always push the updated dedup file, even if some downloads failed,
    # since their URLs were already recorded above. This goes to the
    # shared root file, not the per-folder location.
    push_dedup_file(remote_root, args.rclone_config)

    log_to_sheet(args.spreadsheet_id, args.sheet_tab or sanitize_sheet_tab_name(args.folder_name), saved)

    log("=== Done ===")


if __name__ == "__main__":
    main()

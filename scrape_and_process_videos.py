#!/usr/bin/env python3
"""
scrape_and_process_videos.py
=============================
Scrapes video src values (+ best-effort caption) from elements with class
"item-post" on one or more page URLs, auto-scrolling to trigger lazy-loaded /
infinite-scroll content. Only .mp4 / .m4v videos are kept.

Unlike the image pipeline, RAW scraped videos are never uploaded anywhere.
The flow is:

    scrape URLs -> dedupe against Mega root list -> download raw videos
    locally -> run video_auto_editor.py LOCALLY on the whole batch (trims
    intro/outro, removes the old credit/watermark, splits into N-second
    reel parts) -> upload ONLY the processed reel parts to Mega -> log each
    processed part to a Google Sheet -> delete all local video files
    (raw + processed) -> push updated dedupe list back to Mega root.

Duplicate prevention is identical to the image pipeline: before anything
else, urls_already_downloaded.txt is pulled from the ROOT of the Mega
remote (shared across every folder/run). Any scraped video URL already in
that file is skipped. Newly-seen URLs are written into the local copy
BEFORE download starts, and the updated file OVERWRITES the Mega root copy
at the end of the run.

Usage (local):
    python scrape_and_process_videos.py --url "https://example.com/page1" \
        --folder-name "MyFolder" --clip-seconds 60 --watermark-box-pct 0.62,0.80,0.35,0.12

Env vars (GitHub Actions):
    PAGE_URL          -> one or more URLs (newline or comma separated)
    MEGA_FOLDER_NAME  -> folder path on the Mega remote
    ...
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
DOWNLOAD_DIR = Path("downloaded_videos")
PROCESSED_DIR = Path("processed_reels")
DEDUP_FILENAME = "urls_already_downloaded_videos.txt"
DEDUP_LOCAL_PATH = Path(DEDUP_FILENAME)
VIDEO_EXTS = (".mp4", ".m4v")

DEFAULT_SPREADSHEET_ID = "1OQns3xUPeTQslsw0FaD-a85DAM0Sc_L6BnaGDMqGPmY"
SHEET_HEADER = ["Original Source Video", "Processed Reel File", "Caption"]

VIDEO_EDITOR_SCRIPT = os.environ.get("VIDEO_EDITOR_SCRIPT", "video_auto_editor.py")


def log(msg: str):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def parse_urls(raw) -> list:
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


def normalize_url(src: str, base_site: str) -> str:
    """Turn protocol-relative (//...) or scheme-less URLs into absolute https URLs."""
    if not src:
        return src
    src = src.strip()
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("/"):
        return base_site.rstrip("/") + src
    if not src.startswith(("http://", "https://")):
        return "https://" + src.lstrip("/")
    return src


def parse_args():
    p = argparse.ArgumentParser(
        description="Scrape .item-post videos, remove old credits locally, upload only processed reels to Mega.nz"
    )
    p.add_argument("--url", action="append", default=None,
                    help="Page URL to scrape. Can be given multiple times, or a single "
                         "value with commas/newlines.")
    p.add_argument("--folder-name", default=os.environ.get("MEGA_FOLDER_NAME"),
                    help="Mega folder path to upload PROCESSED reels into")
    p.add_argument("--base-site", default=os.environ.get("BASE_SITE", ""),
                    help="Base site URL used to resolve site-relative video paths (e.g. https://example.com)")
    p.add_argument("--max-idle-scrolls", type=int,
                    default=int(os.environ.get("MAX_IDLE_SCROLLS", "8")))
    p.add_argument("--max-videos", type=int,
                    default=(int(os.environ["MAX_VIDEOS"]) if os.environ.get("MAX_VIDEOS") else None),
                    help="Optional cap on total videos to scrape/download/process across all URLs.")
    p.add_argument("--spreadsheet-id", default=os.environ.get("SPREADSHEET_ID", DEFAULT_SPREADSHEET_ID))
    p.add_argument("--sheet-tab", default=os.environ.get("SHEET_TAB") or None)
    p.add_argument("--download-concurrency", type=int,
                    default=int(os.environ.get("DOWNLOAD_CONCURRENCY", "4")),
                    help="Videos are bigger than images -- default is lower than the image pipeline.")
    p.add_argument("--upload-transfers", type=int,
                    default=int(os.environ.get("UPLOAD_TRANSFERS", "4")))
    p.add_argument("--rclone-config", default=os.environ.get("RCLONE_CONFIG_PATH", "rclone.conf"))
    p.add_argument("--rclone-remote", default=os.environ.get("RCLONE_REMOTE_NAME", "mega"))
    p.add_argument("--headless", action="store_true", default=True)

    # --- pass-through options for video_auto_editor.py (the local processing step) ---
    p.add_argument("--clip-seconds", type=float,
                    default=float(os.environ.get("CLIP_SECONDS", "60")),
                    help="Length of each reel part (default 60s)")
    p.add_argument("--single-clip", action="store_true",
                    default=os.environ.get("SINGLE_CLIP", "false").lower() == "true",
                    help="Produce only ONE clip per video instead of splitting into multiple parts.")
    p.add_argument("--keep-remainder", action="store_true",
                    default=os.environ.get("KEEP_REMAINDER", "false").lower() == "true")
    p.add_argument("--watermark", choices=["auto", "manual", "none"],
                    default=os.environ.get("WATERMARK_MODE", "auto"))
    p.add_argument("--watermark-box", default=os.environ.get("WATERMARK_BOX") or None,
                    help="Fixed pixel box 'x,y,w,h' -- skips detection, same box for every video.")
    p.add_argument("--watermark-box-pct", default=os.environ.get("WATERMARK_BOX_PCT") or None,
                    help="Box as fractions of frame 'x,y,w,h' (0-1) -- rescaled per video. "
                         "Best choice for a batch of mixed-resolution scraped videos.")
    p.add_argument("--shared-watermark-from-first", action="store_true",
                    default=os.environ.get("SHARED_WATERMARK_FROM_FIRST", "true").lower() == "true",
                    help="Detect watermark once on the first downloaded video, reuse for the rest.")
    p.add_argument("--intro-sec", type=float,
                    default=(float(os.environ["INTRO_SEC"]) if os.environ.get("INTRO_SEC") else None))
    p.add_argument("--outro-sec", type=float,
                    default=(float(os.environ["OUTRO_SEC"]) if os.environ.get("OUTRO_SEC") else None))
    p.add_argument("--no-outro-detect", action="store_true",
                    default=os.environ.get("NO_OUTRO_DETECT", "false").lower() == "true")
    p.add_argument("--debug-preview", action="store_true",
                    default=os.environ.get("DEBUG_PREVIEW", "false").lower() == "true",
                    help="Save a red-box preview PNG per video during processing (uploaded as an artifact in CI).")

    args = p.parse_args()

    cli_urls = args.url or []
    env_urls = os.environ.get("PAGE_URL", "")
    args.urls = parse_urls(cli_urls + ([env_urls] if env_urls else []))

    if not args.urls:
        sys.exit("ERROR: at least one URL is required (--url or PAGE_URL env var)")
    if not args.folder_name:
        sys.exit("ERROR: --folder-name (or MEGA_FOLDER_NAME env var) is required")

    return args


def is_video(url: str) -> bool:
    path = urlparse(url).path.lower()
    return path.endswith(VIDEO_EXTS)


def scrape_videos(url: str, base_site: str, max_idle_scrolls: int, max_videos=None) -> dict:
    """Scroll one page, collecting unique .mp4/.m4v video URLs + captions.
    Tries, in order: <video src>, <video><source src>, common data-* attrs
    that point at a video file, then any <a href> ending in a video ext.
    Returns: {video_url: caption}
    """
    found = {}
    idle_scrolls = 0
    scroll_count = 0

    log(f"Launching browser and opening: {url}")
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
        log("Page loaded. Waiting for initial content to render...")
        page.wait_for_timeout(2000)

        extract_js = """
        els => els.map(el => {
            let src = null;
            const vid = el.querySelector('video');
            if (vid) {
                src = vid.getAttribute('src') || vid.src || null;
                if (!src) {
                    const s = vid.querySelector('source');
                    if (s) src = s.getAttribute('src') || s.src || null;
                }
            }
            if (!src) {
                const dataEl = el.querySelector('[data-video],[data-src],[data-mp4],[data-video-src]');
                if (dataEl) {
                    src = dataEl.getAttribute('data-video') || dataEl.getAttribute('data-src')
                        || dataEl.getAttribute('data-mp4') || dataEl.getAttribute('data-video-src');
                }
            }
            if (!src) {
                const a = el.querySelector('a[href$=".mp4"], a[href$=".m4v"]');
                if (a) src = a.getAttribute('href');
            }
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
            total_this_round = 0
            new_this_round = 0
            dup_this_round = 0
            for item in items:
                src = item.get("src")
                caption = (item.get("caption") or "").strip()
                if not src:
                    continue
                src = normalize_url(src, base_site)
                if not is_video(src):
                    continue
                total_this_round += 1
                if src in found:
                    dup_this_round += 1
                else:
                    found[src] = caption
                    new_this_round += 1
                    if max_videos and len(found) >= max_videos:
                        break

            log(
                f"  Scroll #{scroll_count}: found {total_this_round} video(s) this scroll "
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
        log("Browser closed for this URL.")

    if max_videos and len(found) > max_videos:
        found = dict(list(found.items())[:max_videos])
    return found


def scrape_all_urls(urls, base_site, max_idle_scrolls, max_videos) -> dict:
    all_found = {}
    remaining = max_videos

    for i, url in enumerate(urls, 1):
        log(f"=== Scraping URL {i}/{len(urls)} ===")
        page_found = scrape_videos(url, base_site, max_idle_scrolls, remaining)
        before = len(all_found)
        all_found.update(page_found)
        added = len(all_found) - before
        log(f"URL {i} contributed {added} new unique videos (total so far: {len(all_found)})")

        if max_videos is not None:
            remaining = max_videos - len(all_found)
            if remaining <= 0:
                log(f"Global video limit of {max_videos} reached. Skipping remaining URLs.")
                break

    return all_found


_progress_lock = threading.Lock()


def _download_one(src, caption, headers, used_names, names_lock, max_retries=3):
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(src, headers=headers, timeout=120, stream=True)
            resp.raise_for_status()
            break
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                time.sleep(2.0 * attempt)
            else:
                raise last_err

    name = os.path.basename(urlparse(src).path)
    if not name.lower().endswith(VIDEO_EXTS):
        name = hashlib.sha1(src.encode()).hexdigest() + ".mp4"

    with names_lock:
        if name in used_names:
            stem, ext = os.path.splitext(name)
            name = f"{stem}_{hashlib.sha1(src.encode()).hexdigest()[:6]}{ext}"
        used_names.add(name)

    dest = DOWNLOAD_DIR / name
    with open(dest, "wb") as f:
        for chunk in resp.iter_content(1 << 20):   # 1MB chunks -- videos are bigger than images
            f.write(chunk)
    return dest, src, caption


def download_videos(urls_captions: dict, concurrency: int = 4) -> list:
    """Returns list of (dest_path, source_url, caption) for successful downloads."""
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": "Mozilla/5.0"}
    items = sorted(urls_captions.items())
    total = len(items)
    used_names = set()
    names_lock = threading.Lock()
    saved = []
    completed = 0

    log(f"Starting parallel download of {total} video(s) ({concurrency} at a time)...")
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
                size_mb = dest.stat().st_size / (1024 * 1024)
                log(f"  [{current}/{total}] saved: {dest.name} ({size_mb:.1f} MB)")
            except Exception as e:
                log(f"  [{current}/{total}] FAILED to download {src}: {e}")

    log(f"Download finished: {len(saved)}/{total} video(s) saved successfully.")
    return saved


# ---------------------------------------------------------------------------
# Local processing step -- runs video_auto_editor.py as a subprocess so this
# script never needs to duplicate the detection/render logic.
# ---------------------------------------------------------------------------

def run_local_processing(args) -> bool:
    """Runs video_auto_editor.py in batch mode over DOWNLOAD_DIR, writing
    credit-removed, split reel parts into PROCESSED_DIR. Returns True on
    success (process exit 0), regardless of how many parts were produced."""
    if not any(DOWNLOAD_DIR.iterdir()) if DOWNLOAD_DIR.exists() else True:
        pass  # handled by caller checking saved list before calling this

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, VIDEO_EDITOR_SCRIPT,
        "--mode", "batch",
        "--batch", str(DOWNLOAD_DIR),
        "--outdir", str(PROCESSED_DIR),
        "--clip-seconds", str(args.clip_seconds),
        "--watermark", args.watermark,
    ]
    if args.single_clip:
        cmd.append("--single-clip")
    if args.keep_remainder:
        cmd.append("--keep-remainder")
    if args.watermark_box:
        cmd += ["--watermark-box", args.watermark_box]
    if args.watermark_box_pct:
        cmd += ["--watermark-box-pct", args.watermark_box_pct]
    if args.shared_watermark_from_first and not (args.watermark_box or args.watermark_box_pct):
        cmd.append("--shared-watermark-from-first")
    if args.intro_sec is not None:
        cmd += ["--intro-sec", str(args.intro_sec)]
    if args.outro_sec is not None:
        cmd += ["--outro-sec", str(args.outro_sec)]
    if args.no_outro_detect:
        cmd.append("--no-outro-detect")
    if args.debug_preview:
        cmd.append("--debug-preview")

    log(f"🎬 Running local processing: {' '.join(cmd)}")
    result = subprocess.run(cmd, text=True)
    if result.returncode != 0:
        log(f"⚠️ video_auto_editor.py exited with code {result.returncode}")
        return False
    log("✅ Local processing complete.")
    return True


def match_processed_files_to_source(saved: list) -> list:
    """Maps each file that landed in PROCESSED_DIR back to the original
    downloaded video (by stem prefix, since video_auto_editor.py names
    output <stem>.mp4 or <stem>_partNN.mp4). Returns list of
    (processed_path, source_url, caption)."""
    stem_to_source = {dest.stem: (source_url, caption) for dest, source_url, caption in saved}
    # sort longest-stem-first so e.g. "clip1_extra" doesn't get shadowed by "clip1"
    known_stems = sorted(stem_to_source.keys(), key=len, reverse=True)

    matched = []
    if not PROCESSED_DIR.exists():
        return matched

    for f in sorted(PROCESSED_DIR.glob("*.mp4")):
        base = f.stem
        source_url, caption = None, ""
        for stem in known_stems:
            if base == stem or base.startswith(stem + "_part"):
                source_url, caption = stem_to_source[stem]
                break
        matched.append((f, source_url or "unknown", caption))
    return matched


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
        log("No existing dedupe file on Mega yet - starting fresh.")
        DEDUP_LOCAL_PATH.write_text("")
    else:
        log("Pulled existing dedupe file from Mega root.")


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
        log(f"⚠️ Failed to push updated dedupe file to Mega: {result.stderr.strip()[-500:]}")
    else:
        log("✅ Dedupe file updated on Mega root.")


def rclone_upload_all(local_dir: Path, remote_target: str, config_path: str, transfers: int = 4) -> bool:
    """One-shot parallel copy of the given directory to Mega."""
    log(f"⬆️ Uploading '{local_dir}' to '{remote_target}' via rclone ({transfers} parallel transfers)...")
    result = subprocess.run(
        [
            "rclone", "--config", config_path, "copy", str(local_dir), remote_target,
            "--transfers", str(transfers),
            "--checkers", str(max(transfers * 2, 8)),
            "--retries", "5",
            "--low-level-retries", "10",
            "--contimeout", "30s",
            "--timeout", "600s",
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
    log("✅ Upload complete.")
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
            spreadsheetId=spreadsheet_id, range=f"{sheet_tab}!A1:C1"
        ).execute().get("values", [])
    except Exception as e:
        log(f"! Could not read sheet header (check sharing/permissions): {e}")
        return
    if not existing:
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id, range=f"{sheet_tab}!A1:C1",
            valueInputOption="RAW", body={"values": [SHEET_HEADER]},
        ).execute()
        log("📝 Wrote sheet header row.")


def log_to_sheet(spreadsheet_id: str, sheet_tab: str, matched: list):
    if not spreadsheet_id:
        log("No spreadsheet ID configured — skipping sheet logging.")
        return
    if not matched:
        log("Nothing new to log to Sheets.")
        return
    log(f"Writing {len(matched)} row(s) to Google Sheet ({sheet_tab})...")
    service = get_sheets_service()
    ensure_sheet_tab(service, spreadsheet_id, sheet_tab)
    ensure_sheet_header(service, spreadsheet_id, sheet_tab)
    rows = [[source_url, f.name, caption] for f, source_url, caption in matched]
    try:
        result = service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=f"{sheet_tab}!A:C",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": rows},
        ).execute()
        updated_rows = result.get("updates", {}).get("updatedRows", 0)
        log(f"Sheet updated: {updated_rows} row(s) appended to '{sheet_tab}'.")
    except Exception as e:
        log(f"! Failed to write to Google Sheet: {e}")
        log(" (Make sure the spreadsheet ID/tab are correct and shared with the service account.)")


def cleanup_local_videos():
    for d in (DOWNLOAD_DIR, PROCESSED_DIR):
        if not d.exists():
            continue
        for f in d.glob("*"):
            try:
                if f.is_file():
                    f.unlink()
            except Exception as e:
                log(f"  (could not remove {f}: {e})")
    log("🧹 Local raw + processed video files cleaned up.")


def main():
    args = parse_args()

    log("=== Starting run ===")
    log(f"URLs to scrape ({len(args.urls)}):")
    for i, u in enumerate(args.urls, 1):
        log(f"  {i}. {u}")
    log(f"Mega folder (processed reels only): {args.folder_name}")
    if args.max_videos:
        log(f"Video limit (global): {args.max_videos}")
    else:
        log(f"Video limit: none (stop = {args.max_idle_scrolls} idle scrolls per page)")
    log(f"Clip length: {args.clip_seconds}s | watermark mode: {args.watermark} "
        f"(box={args.watermark_box or args.watermark_box_pct or 'auto-detect'})")

    if not (os.path.exists(VIDEO_EDITOR_SCRIPT)):
        sys.exit(f"ERROR: {VIDEO_EDITOR_SCRIPT} not found next to this script "
                  f"(set VIDEO_EDITOR_SCRIPT env var if it lives elsewhere)")

    remote_target = rclone_remote_target(args.rclone_remote, args.rclone_config, args.folder_name)
    remote_root = f"{args.rclone_remote}:"

    log("🔎 Checking for previously-downloaded video URLs on Mega (shared root file)...")
    pull_dedup_file(remote_root, args.rclone_config)
    existing_urls = load_existing_urls()
    log(f"{len(existing_urls)} URL(s) already recorded as downloaded.")

    all_found = scrape_all_urls(args.urls, args.base_site, args.max_idle_scrolls, args.max_videos)
    log(f"=== Scrape complete: {len(all_found)} unique video(s) found across all pages ===")

    if not all_found:
        log("No videos found — nothing to do.")
        return

    new_items = {u: c for u, c in all_found.items() if u not in existing_urls}
    skipped = len(all_found) - len(new_items)
    log(f"{skipped} already downloaded previously (skipped), {len(new_items)} new.")

    if not new_items:
        log("Nothing new to download — done.")
        return

    # Record intent BEFORE downloading
    record_new_urls(new_items.keys())

    saved = download_videos(new_items, concurrency=args.download_concurrency)

    if not saved:
        log("No videos were successfully downloaded — nothing to process/upload.")
        push_dedup_file(remote_root, args.rclone_config)
        return

    ok = run_local_processing(args)
    if not ok:
        log("⚠️ Processing step failed or exited non-zero — check logs above before assuming "
            "any reels are ready. Continuing to upload whatever WAS produced, if anything.")

    matched = match_processed_files_to_source(saved)
    log(f"{len(matched)} processed reel part(s) ready to upload.")

    if matched:
        rclone_upload_all(PROCESSED_DIR, remote_target, args.rclone_config, transfers=args.upload_transfers)
    else:
        log("No processed reel files were produced — nothing to upload.")

    cleanup_local_videos()

    push_dedup_file(remote_root, args.rclone_config)
    log_to_sheet(
        args.spreadsheet_id,
        args.sheet_tab or sanitize_sheet_tab_name(args.folder_name),
        matched,
    )
    log("=== Done ===")


if __name__ == "__main__":
    main()

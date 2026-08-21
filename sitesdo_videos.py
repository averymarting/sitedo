#!/usr/bin/env python3
"""
sitesdo_videos.py
=================
Full pipeline in one script / one GitHub Actions workflow:

  1. Scrape unique video page links from .item-post elements (auto-scroll).
  2. Download only NEW videos (dedup via Mega-root urls_already_downloaded_videos.txt).
  3. Run video_auto_editor.py UNCHANGED on the downloads to:
       - strip intro / outro
       - remove logos / watermarks / credit text
       - split into N-second short clips
       - optimize (max-dim, bitrate, faststart)
  4. Upload ONLY the short optimized clips to Mega.nz (never the originals).
  5. Log short-clip File Name + Caption (phrases stripped) to Google Sheet.

Original full-length downloads are deleted after editing; only the edited
shorts are uploaded and logged.

Duplicate prevention:
    urls_already_downloaded_videos.txt lives at the ROOT of the Mega remote
    (shared across every folder/run). Scraped video *page* URLs already in
    that file are skipped. Newly-seen page URLs are recorded BEFORE download
    starts; the updated file is pushed back to Mega root at the end.

Caption cleaning:
    strip_phrases.txt is also pulled from Mega root (one full phrase per
    line). Those phrases are removed from captions before they are written
    to the Google Sheet.

Usage (local):
    python sitesdo_videos.py --url "https://example.com/videos" --folder-name "MyVideos"

Env vars (GitHub Actions):
    PAGE_URL, MEGA_FOLDER_NAME, MAX_IDLE_SCROLLS, MAX_VIDEOS,
    CLIP_SECONDS, ...
"""

import argparse
import hashlib
import json
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
DEFAULT_ITEM_SELECTOR = ".item-post"
DEFAULT_VIDEO_HREF_CONTAINS = "/videos/"
DOWNLOAD_DIR = Path("downloaded_videos")
EDITED_DIR = Path("edited_videos")
DEDUP_FILENAME = "urls_already_downloaded_videos.txt"
DEDUP_LOCAL_PATH = Path(DEDUP_FILENAME)
STRIP_PHRASES_FILENAME = "strip_phrases.txt"
STRIP_PHRASES_LOCAL_PATH = Path(STRIP_PHRASES_FILENAME)
EDITOR_SCRIPT = Path("video_auto_editor.py")
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
        help="Target number of NEW unique videos (not already on Mega). "
             "Keeps scrolling/paginating until this many new ones are found, "
             "or content is exhausted.",
    )
    p.add_argument(
        "--item-selector",
        default=os.environ.get("ITEM_SELECTOR", DEFAULT_ITEM_SELECTOR),
        help="CSS selector for each video card/item on the listing page "
             f"(default: {DEFAULT_ITEM_SELECTOR}). Examples: .item-post, "
             "div.video-card, #results .clip, article.post",
    )
    p.add_argument(
        "--pagination-selector",
        default=os.environ.get("PAGINATION_SELECTOR", "") or "",
        help="Optional CSS selector for the 'Next page' / 'Load more' control. "
             "If set, after idle scrolls the scraper clicks it and continues. "
             "Examples: a.next, .pagination a[rel=next], button.load-more, "
             ".pager .next a",
    )
    p.add_argument(
        "--video-href-contains",
        default=os.environ.get("VIDEO_HREF_CONTAINS", DEFAULT_VIDEO_HREF_CONTAINS),
        help="Only keep links whose href contains this substring "
             f"(default: {DEFAULT_VIDEO_HREF_CONTAINS}). Set empty to keep any link.",
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
    # --- video_auto_editor.py options (passed through unchanged) ---
    p.add_argument(
        "--clip-seconds",
        type=float,
        default=float(os.environ.get("CLIP_SECONDS", "60")),
        help="Length of each short part produced by the editor (default 60s)",
    )
    p.add_argument(
        "--skip-edit",
        action="store_true",
        default=os.environ.get("SKIP_EDIT", "").lower() in ("1", "true", "yes"),
        help="Skip video_auto_editor.py (upload raw downloads instead) — for debugging only",
    )
    p.add_argument(
        "--editor-extra-args",
        default=os.environ.get("EDITOR_EXTRA_ARGS", ""),
        help="Extra CLI flags passed straight to video_auto_editor.py "
             "(e.g. '--no-optimize --watermark none')",
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


def is_video_page_href(href: str, href_contains: str = DEFAULT_VIDEO_HREF_CONTAINS) -> bool:
    """Keep only links that look like video detail pages."""
    if not href:
        return False
    path = urlparse(href).path.lower()
    if path.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".css", ".js")):
        return False
    if not href_contains:
        return True
    return href_contains.lower() in href.lower()


def dismiss_overlays(page) -> None:
    """
    Dismiss common age-gates / cookie banners that intercept pointer events
    and block pagination clicks (e.g. #age-gate on erosberry).
    """
    # 1) Try common "I am 18 / Enter / Accept" buttons
    click_texts = [
        "I am 18 or older",
        "I am 18",
        "Enter",
        "Accept",
        "Agree",
        "Yes",
        "Continue",
    ]
    for text in click_texts:
        try:
            btn = page.get_by_role("button", name=re.compile(text, re.I))
            if btn.count() > 0:
                btn.first.click(timeout=2000)
                log(f"  Dismissed overlay via button matching {text!r}")
                page.wait_for_timeout(500)
                break
        except Exception:
            pass
        try:
            link = page.get_by_text(re.compile(rf"^{re.escape(text)}", re.I))
            if link.count() > 0:
                link.first.click(timeout=2000, force=True)
                log(f"  Dismissed overlay via text {text!r}")
                page.wait_for_timeout(500)
                break
        except Exception:
            pass

    # 2) Force-remove known overlay nodes from the DOM
    try:
        removed = page.evaluate(
            """
            () => {
              const selectors = [
                '#age-gate', '.age-gate', '#agegate', '.agegate',
                '#cookie-banner', '.cookie-banner', '.cookie-consent',
                '[class*="age-gate"]', '[id*="age-gate"]'
              ];
              let n = 0;
              for (const sel of selectors) {
                document.querySelectorAll(sel).forEach(el => {
                  el.remove();
                  n++;
                });
              }
              // Also clear body scroll lock if present
              document.body.style.overflow = '';
              document.documentElement.style.overflow = '';
              return n;
            }
            """
        )
        if removed:
            log(f"  Removed {removed} overlay element(s) from DOM")
            page.wait_for_timeout(300)
    except Exception as e:
        log(f"  Overlay cleanup note: {e}")


def _try_click_pagination(page, pagination_selector: str) -> bool:
    """
    Click the next-page / load-more control if present and enabled.
    Handles overlays, div wrappers, and falls back to navigating the href.
    Returns True if navigation/click succeeded.
    """
    if not pagination_selector:
        return False

    # Always clear overlays before trying to paginate
    dismiss_overlays(page)

    try:
        loc = page.locator(pagination_selector).first
        if loc.count() == 0:
            log(f"  Pagination selector {pagination_selector!r} not found on page.")
            return False

        # Skip if clearly disabled
        try:
            disabled = loc.get_attribute("disabled")
            aria = loc.get_attribute("aria-disabled")
            cls = (loc.get_attribute("class") or "").lower()
            if disabled is not None or (aria and aria.lower() == "true"):
                log("  Pagination control is disabled — no more pages.")
                return False
            # class "disabled" alone is not always terminal (some themes keep it on the wrapper)
            if "disabled" in cls and "next" not in cls:
                log("  Pagination control looks disabled — stopping.")
                return False
        except Exception:
            pass

        # Prefer an inner <a href="..."> if the selector points at a wrapper div
        click_target = loc
        href = None
        try:
            inner_a = loc.locator("a[href]").first
            if inner_a.count() > 0:
                href = inner_a.get_attribute("href")
                click_target = inner_a
            else:
                href = loc.get_attribute("href")
        except Exception:
            pass

        # Strategy 1: navigate directly via href (most reliable)
        if href and href not in ("#", "javascript:void(0)", "javascript:;"):
            abs_url = normalize_url(href, page.url)
            if abs_url and abs_url != page.url:
                log(f"  Pagination via href → {abs_url}")
                page.goto(abs_url, wait_until="domcontentloaded", timeout=60000)
                dismiss_overlays(page)
                page.wait_for_timeout(1500)
                return True

        # Strategy 2: force click (ignores overlays intercepting pointer events)
        try:
            click_target.scroll_into_view_if_needed(timeout=5000)
            page.wait_for_timeout(200)
            click_target.click(timeout=5000, force=True)
            log(f"  Force-clicked pagination: {pagination_selector!r}")
            page.wait_for_timeout(1500)
            dismiss_overlays(page)
            return True
        except Exception as e1:
            log(f"  Force-click failed: {e1}")

        # Strategy 3: JS click
        try:
            page.evaluate(
                """(sel) => {
                    const el = document.querySelector(sel);
                    if (!el) return false;
                    const a = el.closest('a') || el.querySelector('a') || el;
                    a.click();
                    return true;
                }""",
                pagination_selector,
            )
            log(f"  JS-clicked pagination: {pagination_selector!r}")
            page.wait_for_timeout(1500)
            dismiss_overlays(page)
            return True
        except Exception as e2:
            log(f"  JS-click failed: {e2}")

        return False
    except Exception as e:
        log(f"  Could not click pagination ({pagination_selector!r}): {e}")
        return False


def scrape_listing(
    url: str,
    max_idle_scrolls: int,
    target_new: int | None = None,
    existing_urls: set | None = None,
    item_selector: str = DEFAULT_ITEM_SELECTOR,
    pagination_selector: str = "",
    href_contains: str = DEFAULT_VIDEO_HREF_CONTAINS,
) -> dict:
    """
    Scroll (and optionally paginate) one listing, collecting unique video-page
    URLs + captions.

    target_new: stop once this many URLs that are NOT in existing_urls have been
                collected. Already-seen (dedup) URLs do not count toward the target.
                If None, collect until idle scrolls / no more pages.

    Returns: {video_page_url: caption}  — includes only URLs gathered this run
              (may still contain some already-known ones that appeared on the page;
               main() filters them again before download).
    """
    existing_urls = existing_urls or set()
    found: dict[str, str] = {}          # all unique hrefs seen this listing
    new_unique = 0                      # count of hrefs not in existing_urls
    idle_scrolls = 0
    scroll_count = 0
    page_num = 1
    max_pages = 200                     # safety cap

    log(f"Launching browser and opening listing: {url}")
    log(f"  item selector: {item_selector!r}")
    if pagination_selector:
        log(f"  pagination selector: {pagination_selector!r}")
    if target_new:
        log(f"  target NEW unique videos (not already on Mega): {target_new}")

    # Build extract JS; prefer links matching href_contains when provided
    href_filter_js = json.dumps(href_contains) if href_contains else '""'
    extract_js = f"""
    els => {{
        const prefer = {href_filter_js};
        return els.map(el => {{
            let a = null;
            if (el.tagName === 'A') {{
                a = el;
            }} else if (prefer) {{
                a = el.querySelector('a[href*="' + prefer + '"]');
            }}
            if (!a) a = el.querySelector('a');
            const href = a ? (a.getAttribute('href') || a.href) : null;

            let caption = '';
            const h3 = el.querySelector('h3');
            if (h3) caption = h3.textContent.trim();
            if (!caption) {{
                const h2 = el.querySelector('h2, .info h2, .title, .elips');
                if (h2) caption = h2.textContent.trim();
            }}
            if (!caption) {{
                const img = el.querySelector('img[alt]');
                if (img) caption = (img.getAttribute('alt') || '').trim();
            }}
            if (!caption) {{
                caption = (el.textContent || '').trim().slice(0, 300);
            }}
            return {{href, caption}};
        }});
    }}
    """

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
        page.wait_for_timeout(1500)
        dismiss_overlays(page)
        page.wait_for_timeout(500)

        while page_num <= max_pages:
            # --- harvest current DOM ---
            try:
                items = page.eval_on_selector_all(item_selector, extract_js)
            except Exception as e:
                log(f"  Selector {item_selector!r} failed: {e}")
                items = []

            total_this_round = 0
            new_this_round = 0
            already_known_this_round = 0
            dup_this_round = 0

            for item in items:
                href = item.get("href")
                caption = (item.get("caption") or "").strip()
                if not href:
                    continue
                href = normalize_url(href, page.url)
                if not is_video_page_href(href, href_contains):
                    continue

                total_this_round += 1
                if href in found:
                    dup_this_round += 1
                    continue

                found[href] = caption
                if href in existing_urls:
                    already_known_this_round += 1
                else:
                    new_this_round += 1
                    new_unique += 1

                if target_new and new_unique >= target_new:
                    break

            log(
                f"  Page {page_num} / scroll #{scroll_count}: "
                f"{total_this_round} link(s) → +{new_this_round} NEW, "
                f"{already_known_this_round} already-known, {dup_this_round} in-page dups | "
                f"{new_unique} NEW unique so far"
                + (f" / target {target_new}" if target_new else "")
                + f" (idle: {idle_scrolls}/{max_idle_scrolls})"
            )

            if target_new and new_unique >= target_new:
                log(f"  Reached target of {target_new} NEW unique videos. Stopping scrape.")
                break

            # Progress this pass?
            if new_this_round == 0 and already_known_this_round == 0 and dup_this_round == total_this_round:
                # nothing new at all on page
                idle_scrolls += 1
            elif new_this_round == 0:
                # only already-known or dups — keep going (need more NEW ones)
                idle_scrolls += 1
            else:
                idle_scrolls = 0

            # With a pagination selector, try the next page after fewer idle
            # rounds (page already fully known). Without pagination, wait the
            # full max_idle_scrolls for infinite-scroll sites.
            idle_limit = 2 if pagination_selector else max_idle_scrolls
            if idle_scrolls >= idle_limit:
                if pagination_selector and page_num < max_pages:
                    clicked = _try_click_pagination(page, pagination_selector)
                    if clicked:
                        page_num += 1
                        idle_scrolls = 0
                        scroll_count = 0
                        try:
                            page.wait_for_load_state("domcontentloaded", timeout=15000)
                        except Exception:
                            pass
                        page.wait_for_timeout(2000)
                        log(f"  Moved to page {page_num}, continuing harvest...")
                        continue
                    else:
                        log("  No further pagination — content exhausted.")
                        break
                elif idle_scrolls >= max_idle_scrolls:
                    log(
                        f"  No new content for {max_idle_scrolls} consecutive scrolls "
                        f"and no pagination (or pagination exhausted). Stopping."
                    )
                    break

            # Scroll for infinite-scroll / lazy load
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

    # Return only the NEW ones toward the caller budget (keep already-known out
    # of the "found for download" set — main also filters, but this keeps
    # remaining target accurate across multiple listing URLs).
    if target_new is not None:
        # Prefer returning new ones first, capped at target_new
        new_only = {u: c for u, c in found.items() if u not in existing_urls}
        if len(new_only) > target_new:
            new_only = dict(list(new_only.items())[:target_new])
        return new_only
    return found


def scrape_all_listings(
    urls: list[str],
    max_idle_scrolls: int,
    target_new: int | None,
    existing_urls: set,
    item_selector: str,
    pagination_selector: str,
    href_contains: str,
) -> dict:
    """
    Scrape every listing URL. target_new is the global count of NEW unique
    videos (not in existing_urls) to collect across all listing URLs.
    """
    all_found: dict[str, str] = {}
    remaining = target_new
    for i, url in enumerate(urls, 1):
        log(f"=== Scraping listing URL {i}/{len(urls)} ===")
        page_found = scrape_listing(
            url,
            max_idle_scrolls=max_idle_scrolls,
            target_new=remaining,
            existing_urls=existing_urls | set(all_found.keys()),
            item_selector=item_selector,
            pagination_selector=pagination_selector,
            href_contains=href_contains,
        )
        before = len(all_found)
        all_found.update(page_found)
        added = len(all_found) - before
        log(f"URL {i} contributed {added} new unique video pages (total NEW so far: {len(all_found)})")
        if target_new is not None:
            remaining = target_new - len(all_found)
            if remaining <= 0:
                log(f"Global target of {target_new} NEW unique videos reached.")
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


def rclone_upload_all(source_dir: Path, remote_target: str, config_path: str,
                      transfers: int = 4) -> bool:
    """One-shot parallel copy of source_dir to Mega."""
    log(f"⬆️ Uploading batch from '{source_dir}' to '{remote_target}' via rclone "
        f"({transfers} parallel transfers)...")
    result = subprocess.run(
        [
            "rclone", "--config", config_path, "copy", str(source_dir), remote_target,
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
# video_auto_editor.py integration (script is used as-is, never modified)
# ---------------------------------------------------------------------------
def run_video_editor(clip_seconds: float, extra_args: str = "") -> bool:
    """
    Run video_auto_editor.py in batch mode on DOWNLOAD_DIR → EDITED_DIR.
    The editor script is left completely untouched; we only call it.
    """
    if not EDITOR_SCRIPT.exists():
        log(f"ERROR: {EDITOR_SCRIPT} not found in the working directory.")
        return False

    EDITED_DIR.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "-u", str(EDITOR_SCRIPT),
        "--mode", "batch",
        "--batch", str(DOWNLOAD_DIR),
        "--outdir", str(EDITED_DIR),
        "--clip-seconds", str(clip_seconds),
        "--shared-watermark-from-first",
        "--watermark", "auto",
    ]
    if extra_args.strip():
        # naive split is fine for simple flags; users can quote carefully via env
        cmd.extend(extra_args.split())

    log(f"🎬 Running video_auto_editor.py on {DOWNLOAD_DIR} → {EDITED_DIR}")
    log(f"   cmd: {' '.join(cmd)}")
    result = subprocess.run(cmd, text=True)
    if result.returncode != 0:
        log(f"⚠️ video_auto_editor.py exited with code {result.returncode}")
        return False
    log("✅ video_auto_editor.py finished.")
    return True


def map_edited_files_to_captions(saved: list) -> list:
    """
    After the editor runs, map each output short clip back to the original
    video's caption.

    video_auto_editor naming:
      - single part  →  <stem>.mp4
      - multi parts  →  <stem>_part01.mp4, <stem>_part02.mp4, ...

    Returns list of (Path, caption) for every file in EDITED_DIR that we can
    match to an original download.
    """
    # original stem → caption
    stem_to_caption = {}
    for dest, _src, caption in saved:
        stem_to_caption[dest.stem] = caption

    results = []
    if not EDITED_DIR.exists():
        return results

    for path in sorted(EDITED_DIR.iterdir()):
        if not path.is_file():
            continue
        if path.suffix.lower() not in (".mp4", ".mov", ".mkv", ".webm", ".avi"):
            continue
        # strip _partNN suffix if present
        stem = path.stem
        m = re.match(r"^(.+)_part\d+$", stem, re.IGNORECASE)
        base_stem = m.group(1) if m else stem

        caption = stem_to_caption.get(base_stem, "")
        if not caption:
            # try a looser match (editor sometimes sanitizes names)
            for orig_stem, cap in stem_to_caption.items():
                if base_stem.startswith(orig_stem) or orig_stem.startswith(base_stem):
                    caption = cap
                    break
        results.append((path, caption))
        log(f"  mapped {path.name} ← caption={caption[:60]!r}...")
    return results


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


def log_to_sheet(spreadsheet_id: str, sheet_tab: str, items: list,
                 strip_phrases: list[str] | None = None):
    """
    items: list of (path_or_name, caption)  OR  legacy (dest, src, caption) triples.
    Only File Name + cleaned Caption are written.
    """
    if not spreadsheet_id:
        log("No spreadsheet ID configured — skipping sheet logging.")
        return
    if not items:
        log("Nothing new to log to Sheets.")
        return
    phrases = strip_phrases or []
    if phrases:
        log(f"Stripping {len(phrases)} phrase(s) from captions before writing to sheet...")
    log(f"Writing {len(items)} row(s) to Google Sheet ({sheet_tab})...")
    service = get_sheets_service()
    ensure_sheet_tab(service, spreadsheet_id, sheet_tab)
    ensure_sheet_header(service, spreadsheet_id, sheet_tab)
    rows = []
    for item in items:
        if len(item) == 3:
            dest, _src, caption = item
            name = dest.name if hasattr(dest, "name") else str(dest)
        else:
            path_or_name, caption = item
            name = path_or_name.name if hasattr(path_or_name, "name") else str(path_or_name)
        clean_caption = strip_caption(caption or "", phrases)
        rows.append([name, clean_caption])
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
        log(f"Target NEW unique videos: {args.max_videos} (keeps going until found or content ends)")
    else:
        log(f"Video limit: none (stop = {args.max_idle_scrolls} idle scrolls / no more pages)")
    log(f"Download concurrency: {args.download_concurrency}")
    log(f"Upload transfers: {args.upload_transfers}")
    log(f"Editor clip-seconds: {args.clip_seconds}")
    log(f"Skip edit: {args.skip_edit}")
    log(f"Item selector: {args.item_selector!r}")
    _pag = repr(args.pagination_selector) if args.pagination_selector else "(none)"
    log(f"Pagination selector: {_pag}")
    log(f"Video href contains: {args.video_href_contains!r}")

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

    # 1. Scrape listing pages until we have max_videos NEW unique pages
    #    (already-known URLs do not count toward the target; keeps scrolling /
    #    paginating until target is met or content is exhausted).
    all_pages = scrape_all_listings(
        args.urls,
        max_idle_scrolls=args.max_idle_scrolls,
        target_new=args.max_videos,
        existing_urls=existing_urls,
        item_selector=args.item_selector,
        pagination_selector=args.pagination_selector,
        href_contains=args.video_href_contains,
    )
    log(f"=== Listing scrape complete: {len(all_pages)} candidate video page(s) ===")

    if not all_pages:
        log("No video pages found — nothing to do.")
        return

    # 2. Final filter against dedup (scrape already prefers new ones)
    new_pages = {u: c for u, c in all_pages.items() if u not in existing_urls}
    skipped = len(all_pages) - len(new_pages)
    log(f"{skipped} already downloaded previously (skipped), {len(new_pages)} NEW unique.")

    if not new_pages:
        log(
            "Nothing NEW to process after dedup. "
            "Content may be exhausted, or raise max_videos / check selectors."
        )
        return

    if args.max_videos and len(new_pages) < args.max_videos:
        log(
            f"Note: only found {len(new_pages)} NEW unique (target was {args.max_videos}). "
            f"Listing content appears exhausted."
        )

    # Record intent BEFORE we start downloading (so a crash still marks them as seen)
    record_new_urls(new_pages.keys())

    # 3. Visit each new video page and extract the real media URL
    media_map, _page_to_media = resolve_media_urls(new_pages)

    if not media_map:
        log("Could not resolve any media URLs — nothing to download.")
        push_dedup_file(remote_root, args.rclone_config)
        return

    # 4. Download originals (kept only temporarily)
    saved = download_videos(media_map, concurrency=args.download_concurrency)
    if not saved:
        log("No videos were successfully downloaded — nothing to process.")
        push_dedup_file(remote_root, args.rclone_config)
        return

    # 5. Run video_auto_editor.py (UNCHANGED) → short optimized clips in EDITED_DIR
    #    Original full downloads are NOT uploaded and NOT logged to the sheet.
    upload_dir = DOWNLOAD_DIR
    sheet_items = [(dest, caption) for dest, _src, caption in saved]

    if not args.skip_edit:
        ok = run_video_editor(args.clip_seconds, extra_args=args.editor_extra_args)
        if not ok:
            log("⚠️ Editor failed — falling back to uploading original downloads.")
        else:
            mapped = map_edited_files_to_captions(saved)
            if mapped:
                upload_dir = EDITED_DIR
                sheet_items = mapped
                log(f"Will upload/log {len(sheet_items)} short clip(s) from {EDITED_DIR}")
            else:
                log("⚠️ Editor produced no output files — falling back to originals.")
    else:
        log("SKIP_EDIT set — uploading raw downloads without intro/outro/watermark processing.")

    # 6. Upload ONLY the short clips (or originals if editor was skipped/failed)
    rclone_upload_all(upload_dir, remote_target, args.rclone_config,
                      transfers=args.upload_transfers)

    # 7. Clean local files
    for dest, _src, _caption in saved:
        if dest.exists():
            dest.unlink()
    if EDITED_DIR.exists():
        for f in EDITED_DIR.iterdir():
            if f.is_file():
                f.unlink()

    # 8. Push updated dedup file + log SHORT clip names + cleaned captions to sheet
    push_dedup_file(remote_root, args.rclone_config)
    log_to_sheet(
        args.spreadsheet_id,
        args.sheet_tab or sanitize_sheet_tab_name(args.folder_name),
        sheet_items,
        strip_phrases=strip_phrases,
    )
    log("=== Done ===")


if __name__ == "__main__":
    main()

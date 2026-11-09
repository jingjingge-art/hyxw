#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Capture the signed Googlevideo HLS URL used by the Taiwan news player.

Uses several ordinary browser-observation paths: browser-context request and
response events (including iframe traffic), Chromium DevTools Protocol network
events, performance resource entries, and URLs embedded in page/frame source.
It does not bypass login, CAPTCHA, DRM, or other access controls. Signed URLs
are kept out of logs and written only to the playlist file.
"""

import asyncio
import html
import logging
import os
import re
import sys
import tempfile
from urllib.parse import urlparse

from playwright.async_api import async_playwright

SOURCE_URL = "https://tvivu.com/watch/globalnews-tv-taiwan-news"
OUTPUT_FILE = "8tv.txt"
CHANNEL_NAME = "8tv"
GENRE_HEADER = "iptv,#genre#"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
NAVIGATION_TIMEOUT_MS = 25_000
PLAYER_WAIT_MS = 10_000
CAPTURE_WINDOW_SECONDS = 25
RUN_TIMEOUT_SECONDS = 150

# The sample format has a host of manifest.googlevideo.com and a path ending
# in /index.m3u8. Query parameters carry expiring signatures and are preserved.
MANIFEST_URL_RE = re.compile(
    r"https?://[^\s\"'<>\\]+manifest\.googlevideo\.com/[^\s\"'<>\\]*?\.m3u8(?:\?[^\s\"'<>]*)?",
    re.IGNORECASE,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def is_googlevideo_m3u8(url):
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        return (
            parsed.scheme in ("http", "https")
            and (host == "googlevideo.com" or host.endswith(".googlevideo.com"))
            and parsed.path.lower().endswith(".m3u8")
        )
    except (TypeError, ValueError):
        return False


def safe_url(url):
    """Hide temporary signature and expiry parameters from workflow logs."""
    parsed = urlparse(url)
    return f"{parsed.hostname}{parsed.path}"


def add_candidate(candidates, url, status=None):
    if is_googlevideo_m3u8(url):
        record = candidates.setdefault(url, {"status": None, "success": False})
        if status is not None:
            record["status"] = status
            if 200 <= status < 300:
                record["success"] = True
        return True
    return False


def scan_text_for_urls(text, candidates, source):
    if not text:
        return
    normalized = html.unescape(text).replace("\\/", "/")
    for url in MANIFEST_URL_RE.findall(normalized):
        # Trim punctuation that commonly follows a URL in inline JS or markup.
        url = url.rstrip(").,;]}\n\r\t")
        if add_candidate(candidates, url):
            candidates[url]["embedded"] = True
            logging.info("Manifest URL found in %s: %s", source, safe_url(url))


def save_output(stream_url):
    content = f"{GENRE_HEADER}\n{CHANNEL_NAME},{stream_url}\n"
    output_dir = os.path.dirname(os.path.abspath(OUTPUT_FILE))
    fd, temp_path = tempfile.mkstemp(dir=output_dir, prefix=".8tv_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
            output.write(content)
        os.replace(temp_path, OUTPUT_FILE)
    except Exception:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise


async def click_player_controls(page):
    """Try normal playback controls on the top page and accessible frames."""
    selectors = (
        "button[aria-label*='play' i]",
        "[title*='play' i]",
        ".vjs-big-play-button",
        ".plyr__control--overlaid",
        "video",
    )
    for frame in list(page.frames):
        for selector in selectors:
            try:
                control = frame.locator(selector).first
                if not await control.is_visible(timeout=500):
                    continue
                if selector == "video":
                    await control.evaluate(
                        "video => { video.muted = true; video.play().catch(() => {}); }"
                    )
                else:
                    await control.click(timeout=1200, force=True)
                logging.info("Tried playback control %s in frame %s", selector, frame.url[:100])
                await page.wait_for_timeout(500)
            except Exception:
                continue


async def inspect_frames(page, candidates):
    """Collect resource URLs and embedded manifest URLs from each frame."""
    for frame in list(page.frames):
        try:
            resources = await frame.evaluate(
                "() => performance.getEntriesByType('resource').map(e => e.name)"
            )
            for url in resources or []:
                if add_candidate(candidates, url):
                    candidates[url]["resource_entry"] = True
                    logging.info("Manifest in performance entries: %s", safe_url(url))
        except Exception:
            pass
        try:
            source = await frame.content()
            scan_text_for_urls(source, candidates, "frame document")
        except Exception:
            pass


async def collect_stream():
    # True means the request got a successful HTTP response. Embedded candidates
    # are accepted only as a last resort if the page itself contains the URL.
    candidates = {}
    response_tasks = set()

    async with async_playwright() as playwright:
        # Headed Chromium is used when the workflow supplies Xvfb; otherwise
        # retain compatibility with local/headless runs.
        headed = bool(os.environ.get("DISPLAY")) and os.environ.get("BROWSER_HEADLESS", "0") != "1"
        browser = await playwright.chromium.launch(
            headless=not headed,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--autoplay-policy=no-user-gesture-required",
                "--window-size=1366,900",
            ],
        )
        try:
            context = await browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1366, "height": 900},
                screen={"width": 1366, "height": 900},
                locale="en-US",
                timezone_id="Asia/Kuala_Lumpur",
                java_script_enabled=True,
            )

            def on_request(request):
                if add_candidate(candidates, request.url):
                    logging.info("Observed manifest request: %s", safe_url(request.url))

            async def inspect_response(response):
                if not is_googlevideo_m3u8(response.url):
                    return
                if add_candidate(candidates, response.url, response.status):
                    logging.info("Manifest response HTTP %s: %s", response.status, safe_url(response.url))

            def on_response(response):
                task = asyncio.create_task(inspect_response(response))
                response_tasks.add(task)
                task.add_done_callback(response_tasks.discard)

            # Context-level events see network activity from embedded frames.
            context.on("request", on_request)
            context.on("response", on_response)
            page = await context.new_page()
            page.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)
            page.set_default_timeout(4000)

            # Add CDP capture as a second independent network observation path.
            try:
                cdp = await context.new_cdp_session(page)
                await cdp.send("Network.enable")

                def cdp_request(event):
                    url = event.get("request", {}).get("url", "")
                    if add_candidate(candidates, url):
                        logging.info("CDP observed manifest request: %s", safe_url(url))

                def cdp_response(event):
                    response = event.get("response", {})
                    url = response.get("url", "")
                    status = int(response.get("status", 0))
                    if add_candidate(candidates, url, status):
                        logging.info("CDP manifest response HTTP %s: %s", status, safe_url(url))

                cdp.on("Network.requestWillBeSent", cdp_request)
                cdp.on("Network.responseReceived", cdp_response)
            except Exception as error:
                logging.warning("CDP capture unavailable; context listeners remain active: %s", error)

            logging.info("Starting %s Chromium; source=%s", "headed" if headed else "headless", SOURCE_URL)
            try:
                await page.goto(SOURCE_URL, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
            except Exception as error:
                # Continue observing even if an ad or long-running document load
                # prevents DOMContentLoaded; never log full signed URLs.
                logging.warning("Navigation wait ended: %s", str(error)[:180])

            try:
                logging.info("Page title: %s", (await page.title())[:160])
            except Exception:
                pass
            logging.info("Frames after navigation: %d", len(page.frames))
            await page.wait_for_timeout(PLAYER_WAIT_MS)
            await click_player_controls(page)

            # Repeated resource scans catch URLs created after player startup.
            for _ in range(CAPTURE_WINDOW_SECONDS // 3):
                await inspect_frames(page, candidates)
                if any(record.get("success") for record in candidates.values()):
                    # Continue briefly for the player to issue its final signed URL.
                    await page.wait_for_timeout(1500)
                    await inspect_frames(page, candidates)
                    break
                await page.wait_for_timeout(1500)

            if response_tasks:
                await asyncio.gather(*tuple(response_tasks), return_exceptions=True)
            logging.info("Final frame count: %d", len(page.frames))
        finally:
            await browser.close()

    successful = [url for url, record in candidates.items() if record.get("success")]
    # An embedded URL can still be useful even if the player only requests it
    # after user interaction; do not select arbitrary non-Googlevideo URLs.
    if not successful:
        successful = [url for url, record in candidates.items() if record.get("embedded")]
    logging.info("Googlevideo HLS candidates: %d; successful responses: %d", len(candidates), sum(bool(r.get("success")) for r in candidates.values()))
    return successful


async def main():
    logging.info("8TV browser stream collector started")
    try:
        urls = await asyncio.wait_for(collect_stream(), timeout=RUN_TIMEOUT_SECONDS)
        if not urls:
            logging.error("No Googlevideo HLS manifest found by browser/network/DOM capture; existing playlist was not changed")
            return 1
        stream_url = urls[-1]
        save_output(stream_url)
        logging.info("Saved current manifest: %s", safe_url(stream_url))
        return 0
    except asyncio.TimeoutError:
        logging.error("Collection exceeded %d seconds; existing playlist was preserved", RUN_TIMEOUT_SECONDS)
        return 1
    except Exception:
        logging.exception("Collector failed; existing playlist was preserved")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import logging
import os
import re
import sys
import tempfile
from urllib.parse import urlparse

from playwright.async_api import async_playwright


# ==================================================
# CONFIGURATION
# ==================================================

SOURCE_URL = "https://tvivu.com/watch/globalnews-tv-taiwan-news"
OUTPUT_FILE = "8tv.txt"
CHANNEL_NAME = "8tv"
GENRE_HEADER = "iptv,#genre#"
WAIT_AFTER_LOAD = 12000
WAIT_AFTER_PLAY = 10000
BROWSER_TIMEOUT = 60000
MAX_RUNTIME = 90
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)

# Catch complete manifest URLs embedded in page/frame HTML or JavaScript.
GOOGLEVIDEO_M3U8_RE = re.compile(
    r"https?://[^\s\"'<>]+manifest\.googlevideo\.com/[^\s\"'<>]*?\.m3u8(?:\?[^\s\"'<>]*)?",
    re.IGNORECASE,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


def is_stream_url(url):
    """Accept signed Googlevideo HLS manifests, including expiring URLs."""
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        return (
            parsed.scheme in ("http", "https")
            and (host == "googlevideo.com" or host.endswith(".googlevideo.com"))
            and parsed.path.lower().endswith(".m3u8")
        )
    except Exception:
        return False


def safe_url(url):
    """Keep signed URL query parameters out of Actions logs."""
    parsed = urlparse(url)
    return f"{parsed.hostname}{parsed.path}"


def is_valid_playlist(data, content_type):
    if not data:
        return False
    text = data.decode("utf-8", errors="replace").lstrip("\ufeff \r\n\t")
    return text.startswith("#EXTM3U") or "mpegurl" in content_type.lower()


async def extract_embedded_manifests(page):
    """Find manifest URLs that are present in document or frame source."""
    found = set()
    for frame in page.frames:
        try:
            source = await frame.content()
        except Exception:
            continue
        if source:
            found.update(GOOGLEVIDEO_M3U8_RE.findall(source))
    return [url for url in found if is_stream_url(url)]


async def collect_stream():
    candidates = {}
    pending_tasks = set()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--autoplay-policy=no-user-gesture-required",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )
        try:
            context = await browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1366, "height": 768},
                java_script_enabled=True,
            )
            page = await context.new_page()
            page.set_default_timeout(BROWSER_TIMEOUT)

            def on_request(request):
                url = request.url
                if is_stream_url(url):
                    candidates.setdefault(url, {"valid": False, "status": None})
                    logging.info("Googlevideo HLS request detected: %s", safe_url(url))

            async def inspect_response(response):
                url = response.url
                if not is_stream_url(url):
                    return
                item = candidates.setdefault(url, {"valid": False, "status": None})
                item["status"] = response.status
                if not (200 <= response.status < 300):
                    logging.warning(
                        "Googlevideo HLS returned HTTP %s: %s",
                        response.status,
                        safe_url(url),
                    )
                    return
                try:
                    body = await response.body()
                    content_type = response.headers.get("content-type", "")
                    if is_valid_playlist(body, content_type):
                        item["valid"] = True
                        logging.info("Valid HLS playlist detected: %s", safe_url(url))
                except Exception as error:
                    logging.warning("Could not inspect HLS response: %s", error)

            def on_response(response):
                task = asyncio.create_task(inspect_response(response))
                pending_tasks.add(task)
                task.add_done_callback(pending_tasks.discard)

            # Context-level listeners also see requests from player iframes.
            context.on("request", on_request)
            context.on("response", on_response)

            logging.info("Opening Taiwan news source page: %s", SOURCE_URL)
            try:
                await page.goto(
                    SOURCE_URL,
                    wait_until="domcontentloaded",
                    timeout=BROWSER_TIMEOUT,
                )
            except Exception as error:
                logging.warning("Page navigation warning: %s", str(error)[:200])

            await page.wait_for_timeout(WAIT_AFTER_LOAD)

            # Attempt playback on the top page and its frames.
            for frame in page.frames:
                try:
                    await frame.evaluate("""
                        async () => {
                            for (const video of document.querySelectorAll('video')) {
                                video.muted = true;
                                try { await video.play(); } catch (e) {}
                            }
                        }
                    """)
                except Exception:
                    pass

            for selector in (
                ".vjs-big-play-button",
                ".plyr__control--overlaid",
                "button[aria-label*='play' i]",
            ):
                try:
                    button = page.locator(selector).first
                    if await button.is_visible(timeout=1000):
                        await button.click(timeout=2000)
                        logging.info("Player play button clicked")
                        break
                except Exception:
                    continue

            logging.info("Waiting for Googlevideo HLS requests...")
            await page.wait_for_timeout(WAIT_AFTER_PLAY)

            # Some pages embed a manifest in player HTML rather than requesting it
            # during the short observation window. Preserve the whole signed URL.
            for url in await extract_embedded_manifests(page):
                candidates.setdefault(url, {"valid": True, "status": None})
                candidates[url]["valid"] = True
                logging.info("Embedded manifest URL found: %s", safe_url(url))

            for frame in page.frames:
                logging.info("Frame inspected: %s", frame.url[:180])

            if pending_tasks:
                await asyncio.gather(*tuple(pending_tasks), return_exceptions=True)
        finally:
            await browser.close()

    valid_urls = [url for url, info in candidates.items() if info.get("valid")]
    logging.info("Googlevideo M3U8 candidates: %d", len(candidates))
    logging.info("Valid playlists: %d", len(valid_urls))
    return valid_urls


def save_output(stream_url):
    content = f"{GENRE_HEADER}\n{CHANNEL_NAME},{stream_url}\n"
    output_dir = os.path.dirname(os.path.abspath(OUTPUT_FILE))
    fd, temp_path = tempfile.mkstemp(dir=output_dir, prefix=".8tv_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file:
            file.write(content)
        os.replace(temp_path, OUTPUT_FILE)
        logging.info("8TV playlist updated successfully")
    except Exception:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise


async def main():
    logging.info("Taiwan news HLS collector started")
    logging.info("Source: %s", SOURCE_URL)
    try:
        urls = await asyncio.wait_for(collect_stream(), timeout=MAX_RUNTIME)
        if not urls:
            raise RuntimeError("No valid Googlevideo M3U8 playlist captured")
        stream_url = urls[0]
        logging.info("Selected live playlist: %s", safe_url(stream_url))
        save_output(stream_url)
        logging.info("Collection completed successfully")
        return 0
    except asyncio.TimeoutError:
        logging.error("Browser collection timed out")
        return 1
    except Exception as error:
        logging.error("Collection failed: %s", error)
        logging.warning("Old 8tv.txt has been preserved")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

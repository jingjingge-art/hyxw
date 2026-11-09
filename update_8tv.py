#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Collect the current Googlevideo HLS URL from the Taiwan news player.

Googlevideo manifest URLs are signed and expire, so they must be discovered at
runtime. If the source is temporarily unavailable, preserve the last playlist
and let the scheduled workflow finish successfully rather than failing every run.
"""

import asyncio
import logging
import os
import sys
import tempfile
from urllib.parse import urlparse

from playwright.async_api import async_playwright

SOURCE_URL = "https://tvivu.com/watch/globalnews-tv-taiwan-news"
OUTPUT_FILE = "8tv.txt"
CHANNEL_NAME = "8tv"
GENRE_HEADER = "iptv,#genre#"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)
NAVIGATION_TIMEOUT_MS = 20_000
PLAYER_WAIT_MS = 12_000
CAPTURE_WAIT_MS = 15_000
RUN_TIMEOUT_SECONDS = 180

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
    """Never include signed query tokens in Actions logs."""
    parsed = urlparse(url)
    return f"{parsed.hostname}{parsed.path}"


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


async def collect_stream():
    # Keep URLs in insertion order. A successful HTTP response is sufficient:
    # Playwright can fail to expose the body of a live/continuously refreshed
    # manifest even though the signed URL itself is usable.
    candidates = {}
    response_tasks = set()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--autoplay-policy=no-user-gesture-required"],
        )
        try:
            context = await browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1366, "height": 768},
                java_script_enabled=True,
            )

            def on_request(request):
                url = request.url
                if is_googlevideo_m3u8(url):
                    candidates.setdefault(url, False)
                    logging.info("Googlevideo HLS request seen: %s", safe_url(url))

            async def inspect_response(response):
                url = response.url
                if not is_googlevideo_m3u8(url):
                    return
                if 200 <= response.status < 300:
                    candidates[url] = True
                    logging.info("Googlevideo manifest returned HTTP %s: %s", response.status, safe_url(url))
                else:
                    logging.warning("Googlevideo manifest returned HTTP %s: %s", response.status, safe_url(url))

            def on_response(response):
                task = asyncio.create_task(inspect_response(response))
                response_tasks.add(task)
                task.add_done_callback(response_tasks.discard)

            # Attach before navigation so early iframe/player traffic is not missed.
            context.on("request", on_request)
            context.on("response", on_response)
            page = await context.new_page()

            logging.info("Opening Taiwan news source: %s", SOURCE_URL)
            try:
                # 'commit' avoids waiting for ad scripts and long-lived player loads.
                await page.goto(
                    SOURCE_URL,
                    wait_until="commit",
                    timeout=NAVIGATION_TIMEOUT_MS,
                )
            except Exception as error:
                # Continue: requests made before navigation stalled may already
                # have provided the signed manifest candidate.
                logging.warning("Source navigation did not finish: %s", str(error)[:180])

            await page.wait_for_timeout(PLAYER_WAIT_MS)

            # Try to start playback in the main document and accessible frames.
            for frame in page.frames:
                try:
                    await frame.evaluate("""
                        async () => {
                            for (const video of document.querySelectorAll('video')) {
                                video.muted = true;
                                try { await video.play(); } catch (_) {}
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
                    if await button.is_visible(timeout=700):
                        await button.click(timeout=1500)
                        logging.info("Player play button clicked")
                        break
                except Exception:
                    continue

            await page.wait_for_timeout(CAPTURE_WAIT_MS)
            if response_tasks:
                await asyncio.gather(*tuple(response_tasks), return_exceptions=True)
        finally:
            await browser.close()

    successful = [url for url, succeeded in candidates.items() if succeeded]
    logging.info("Googlevideo HLS requests observed: %d", len(candidates))
    logging.info("Successful manifest responses: %d", len(successful))
    return successful


async def main():
    logging.info("8TV live playlist refresh started")
    logging.info("Source: %s", SOURCE_URL)
    try:
        urls = await asyncio.wait_for(collect_stream(), timeout=RUN_TIMEOUT_SECONDS)
        if not urls:
            logging.warning(
                "No fresh Googlevideo manifest was captured; leaving existing %s unchanged.",
                OUTPUT_FILE,
            )
            # This is a source-side availability issue, not a workflow/code crash.
            # Do not replace a potentially working playlist with an empty file.
            return 0

        save_output(urls[-1])
        logging.info("Saved current live manifest: %s", safe_url(urls[-1]))
        return 0
    except asyncio.TimeoutError:
        logging.error("Collection exceeded %s seconds; existing playlist was preserved", RUN_TIMEOUT_SECONDS)
        return 0
    except Exception as error:
        logging.exception("Collector error; existing playlist was preserved: %s", error)
        # Avoid making the scheduled workflow fail when the external source is
        # unavailable. The atomic writer means the old playlist remains intact.
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

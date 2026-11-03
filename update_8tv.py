#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
8TV IPTV Auto Collector
Source: https://tvmalay.my/8tv/
Output: 8tv.txt

Output format:
iptv,#genre#
8tv,https://example.com/live/stream.m3u8
"""

import re
import sys
import html
import time
import logging
import os
import tempfile

from urllib.request import Request, urlopen
from urllib.parse import urljoin, urlparse, unquote
from html.parser import HTMLParser


# ================= Configuration =================

SOURCE_URL = "https://tvmalay.my/8tv/"
OUTPUT_FILE = "8tv.txt"

CHANNEL_NAME = "8tv"
GENRE_HEADER = "iptv,#genre#"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)

TIMEOUT = 25
MAX_DEPTH = 2
MAX_DOWNLOAD = 2 * 1024 * 1024

# Only accept HTTP/HTTPS URLs
ALLOWED_SCHEMES = {"http", "https"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)


# ================= HTML Parser =================

class PageParser(HTMLParser):

    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):

        attrs = dict(attrs)

        keys = [
            "src",
            "href",
            "data-src",
            "data-url",
            "data-hls",
            "data-file",
            "data-video"
        ]

        for key in keys:
            value = attrs.get(key)

            if value:
                self.links.append(value)


# ================= HTTP Request =================

def fetch_url(url, referer=None):

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": (
            "text/html,application/xhtml+xml,"
            "application/json,*/*"
        ),
        "Accept-Language": "en-US,en;q=0.9"
    }

    if referer:
        headers["Referer"] = referer

    req = Request(url, headers=headers)

    with urlopen(req, timeout=TIMEOUT) as response:

        data = response.read(MAX_DOWNLOAD)

        charset = response.headers.get_content_charset() or "utf-8"

        return data.decode(charset, errors="replace")


# ================= URL Normalization =================

def normalize_text(text):

    text = html.unescape(text)

    text = text.replace("\\/", "/")
    text = text.replace("\\u0026", "&")
    text = text.replace("\\u003d", "=")

    text = text.replace("\\x3A", ":")
    text = text.replace("\\x2F", "/")

    return text


def valid_http_url(url):

    try:
        parsed = urlparse(url)

        return (
            parsed.scheme.lower() in ALLOWED_SCHEMES
            and bool(parsed.netloc)
        )

    except Exception:
        return False


# ================= Extract URLs =================

def extract_stream_urls(content, base_url):

    content = normalize_text(content)

    patterns = [

        # Standard HLS
        r'https?://[^\s"\'<>]+?\.m3u8(?:\?[^\s"\'<>]*)?',

        # Escaped URLs
        r'https?://[^\s"\'<>]+?\.m3u8',

        # Relative HLS paths
        r'["\']([^"\']+?\.m3u8(?:\?[^"\']*)?)["\']',

        # Player configuration
        r'(?:file|source|src|hls|url)\s*[:=]\s*["\']([^"\']+)["\']',

    ]

    results = []

    for pattern in patterns:

        for match in re.finditer(
            pattern,
            content,
            re.IGNORECASE
        ):

            raw = match.group(1) if match.lastindex else match.group(0)

            raw = raw.strip()

            if not raw:
                continue

            candidate = urljoin(base_url, raw)

            candidate = unquote(candidate)

            if valid_http_url(candidate):

                if candidate not in results:
                    results.append(candidate)

    return results


# ================= Extract Embedded Pages =================

def extract_embedded_pages(content, base_url):

    parser = PageParser()

    try:
        parser.feed(content)
    except Exception:
        pass

    pages = []

    for link in parser.links:

        link = html.unescape(link.strip())

        if not link:
            continue

        full_url = urljoin(base_url, link)

        if not valid_http_url(full_url):
            continue

        parsed = urlparse(full_url)

        # Avoid downloading unrelated external resources
        if parsed.netloc != urlparse(base_url).netloc:
            continue

        if full_url not in pages:
            pages.append(full_url)

    return pages


# ================= Recursive Collection =================

def collect_streams(url, depth=0, visited=None):

    if visited is None:
        visited = set()

    if depth > MAX_DEPTH:
        return []

    if url in visited:
        return []

    visited.add(url)

    logging.info("Scanning: %s", url)

    try:

        content = fetch_url(url, SOURCE_URL)

    except Exception as error:

        logging.warning("Fetch failed: %s", error)
        return []

    candidates = extract_stream_urls(content, url)

    logging.info(
        "Found %d candidate stream(s)",
        len(candidates)
    )

    # Scan embedded player pages and local scripts
    if depth < MAX_DEPTH:

        pages = extract_embedded_pages(content, url)

        for page in pages:

            try:

                results = collect_streams(
                    page,
                    depth + 1,
                    visited
                )

                candidates.extend(results)

            except Exception as error:

                logging.warning(
                    "Subpage error: %s",
                    error
                )

    # Remove duplicates
    return list(dict.fromkeys(candidates))


# ================= Validate HLS =================

def validate_stream(url):

    logging.info("Checking stream: %s", url)

    headers = {
        "User-Agent": USER_AGENT,
        "Referer": SOURCE_URL,
        "Accept": "*/*",
        "Range": "bytes=0-65535"
    }

    try:

        req = Request(url, headers=headers)

        with urlopen(req, timeout=TIMEOUT) as response:

            if response.status not in (200, 206):
                return False

            content_type = response.headers.get(
                "Content-Type", ""
            ).lower()

            data = response.read(65536)

            body = data.decode(
                "utf-8",
                errors="replace"
            ).lstrip("\ufeff \r\n\t")

            # Standard HLS playlist signature
            if body.startswith("#EXTM3U"):
                return True

            if "mpegurl" in content_type:
                return True

            return False

    except Exception as error:

        logging.warning(
            "Stream validation failed: %s",
            error
        )

        return False


# ================= Save Output =================

def save_output(stream_url):

    content = (
        f"{GENRE_HEADER}\n"
        f"{CHANNEL_NAME},{stream_url}\n"
    )

    output_dir = os.path.dirname(
        os.path.abspath(OUTPUT_FILE)
    )

    fd, temp_path = tempfile.mkstemp(
        dir=output_dir,
        prefix=".8tv_",
        suffix=".tmp"
    )

    try:

        with os.fdopen(
            fd,
            "w",
            encoding="utf-8",
            newline="\n"
        ) as file:

            file.write(content)

        os.replace(temp_path, OUTPUT_FILE)

        logging.info(
            "Successfully updated %s",
            OUTPUT_FILE
        )

    except Exception:

        if os.path.exists(temp_path):
            os.remove(temp_path)

        raise


# ================= Main =================

def main():

    logging.info("Starting 8TV collector")

    old_content = None

    if os.path.exists(OUTPUT_FILE):

        with open(
            OUTPUT_FILE,
            "r",
            encoding="utf-8"
        ) as file:

            old_content = file.read()

    try:

        candidates = collect_streams(SOURCE_URL)

        if not candidates:

            raise RuntimeError(
                "No M3U8 candidates found"
            )

        logging.info(
            "Total candidates: %d",
            len(candidates)
        )

        # Validate each candidate
        for stream_url in candidates:

            if validate_stream(stream_url):

                save_output(stream_url)

                logging.info(
                    "Selected stream: %s",
                    stream_url
                )

                return 0

        raise RuntimeError(
            "No valid HLS stream found"
        )

    except Exception as error:

        logging.error("Collection failed: %s", error)

        logging.warning(
            "Previous output file has been preserved"
        )

        return 1


if __name__ == "__main__":

    sys.exit(main())

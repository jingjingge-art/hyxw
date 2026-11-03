#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import sys
import json
import html
import logging
import tempfile

from urllib.request import Request, urlopen
from urllib.parse import urljoin, urlparse, unquote
from html.parser import HTMLParser


# ==================================================
# Configuration
# ==================================================

SOURCE_URL = "https://tvmalay.my/8tv/"

OUTPUT_FILE = "8tv.txt"

CHANNEL_NAME = "8tv"

GENRE_HEADER = "iptv,#genre#"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)

TIMEOUT = 30

MAX_DEPTH = 2

MAX_DOWNLOAD = 2 * 1024 * 1024

MAX_CANDIDATES = 30

# Optional: only permit the CDN host observed in your sample.
# Change this if the authorized source uses another CDN.
ALLOWED_CDN_HOSTS = {
    "2bfd741e.b-cdn.net"
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)


# ==================================================
# HTML Parser
# ==================================================

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


# ==================================================
# HTTP Request
# ==================================================

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

    request = Request(
        url,
        headers=headers,
        method="GET"
    )

    with urlopen(request, timeout=TIMEOUT) as response:

        data = response.read(MAX_DOWNLOAD)

        charset = (
            response.headers.get_content_charset()
            or "utf-8"
        )

        return data.decode(
            charset,
            errors="replace"
        )


# ==================================================
# Text Normalization
# ==================================================

def normalize_text(content):

    content = html.unescape(content)

    replacements = {
        r"\/": "/",
        r"\u0026": "&",
        r"\u003d": "=",
        r"\u003a": ":",
        r"\x3A": ":",
        r"\x2F": "/"
    }

    for old, new in replacements.items():
        content = content.replace(old, new)

    return content


# ==================================================
# URL Validation
# ==================================================

def valid_http_url(url):

    try:

        parsed = urlparse(url)

        return (
            parsed.scheme.lower() in ("http", "https")
            and bool(parsed.netloc)
        )

    except Exception:
        return False


def is_allowed_stream(url):

    if not valid_http_url(url):
        return False

    parsed = urlparse(url)

    if not parsed.path.lower().endswith(".m3u8"):
        return False

    if ALLOWED_CDN_HOSTS:
        return parsed.hostname in ALLOWED_CDN_HOSTS

    return True


# ==================================================
# M3U8 Extraction
# ==================================================

def extract_stream_urls(content, base_url):

    content = normalize_text(content)

    patterns = [

        # Full HLS URLs, including query parameters
        r'https?://[^\s"\'<>]+?\.m3u8(?:\?[^\s"\'<>]*)?',

        # Relative M3U8 URLs
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

            raw = (
                match.group(1)
                if match.lastindex
                else match.group(0)
            )

            raw = raw.strip()

            if not raw:
                continue

            # Preserve signed URL query parameters.
            candidate = urljoin(base_url, raw)

            if is_allowed_stream(candidate):

                if candidate not in results:
                    results.append(candidate)

    return results


# ==================================================
# Extract iframe and script URLs
# ==================================================

def extract_embedded_pages(content, base_url):

    parser = PageParser()

    try:
        parser.feed(content)
    except Exception:
        pass

    results = []

    for link in parser.links:

        link = html.unescape(link.strip())

        if not link:
            continue

        full_url = urljoin(base_url, link)

        if not valid_http_url(full_url):
            continue

        parsed = urlparse(full_url)

        # Only inspect pages/scripts on the source domain.
        if parsed.netloc != urlparse(base_url).netloc:
            continue

        if full_url not in results:
            results.append(full_url)

    return results


# ==================================================
# Recursive Collector
# ==================================================

def collect_streams(url, depth=0, visited=None):

    if visited is None:
        visited = set()

    if depth > MAX_DEPTH:
        return []

    if url in visited:
        return []

    visited.add(url)

    logging.info(
        "Scanning [%s]: %s",
        depth,
        url
    )

    try:

        content = fetch_url(
            url,
            referer=SOURCE_URL
        )

    except Exception as error:

        logging.warning(
            "Page request failed: %s",
            error
        )

        return []

    candidates = extract_stream_urls(
        content,
        url
    )

    logging.info(
        "Found %d stream candidates",
        len(candidates)
    )

    if depth < MAX_DEPTH:

        embedded = extract_embedded_pages(
            content,
            url
        )

        for page in embedded:

            results = collect_streams(
                page,
                depth + 1,
                visited
            )

            candidates.extend(results)

            if len(candidates) >= MAX_CANDIDATES:
                break

    return list(dict.fromkeys(candidates))[
        :MAX_CANDIDATES
    ]


# ==================================================
# Validate HLS Stream
# ==================================================

def validate_stream(url):

    logging.info(
        "Validating stream: %s",
        url
    )

    headers = {
        "User-Agent": USER_AGENT,
        "Referer": SOURCE_URL,
        "Accept": "*/*",
        "Range": "bytes=0-65535"
    }

    try:

        request = Request(
            url,
            headers=headers
        )

        with urlopen(
            request,
            timeout=TIMEOUT
        ) as response:

            if response.status not in (200, 206):
                return False

            content_type = response.headers.get(
                "Content-Type",
                ""
            ).lower()

            data = response.read(65536)

            body = data.decode(
                "utf-8",
                errors="replace"
            ).lstrip("\ufeff \r\n\t")

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


# ==================================================
# Atomic File Update
# ==================================================

def save_output(stream_url):

    content = (
        f"{GENRE_HEADER}\n"
        f"{CHANNEL_NAME},{stream_url}\n"
    )

    output_dir = os.path.dirname(
        os.path.abspath(OUTPUT_FILE)
    )

    fd, temp_path = tempfile.mkstemp(
        prefix=".8tv_",
        suffix=".tmp",
        dir=output_dir
    )

    try:

        with os.fdopen(
            fd,
            "w",
            encoding="utf-8",
            newline="\n"
        ) as file:

            file.write(content)

        os.replace(
            temp_path,
            OUTPUT_FILE
        )

        logging.info(
            "Updated successfully: %s",
            OUTPUT_FILE
        )

    except Exception:

        if os.path.exists(temp_path):
            os.remove(temp_path)

        raise


# ==================================================
# Main
# ==================================================

def main():

    logging.info("=" * 50)
    logging.info("8TV IPTV Collector Started")
    logging.info("Source: %s", SOURCE_URL)
    logging.info("=" * 50)

    try:

        candidates = collect_streams(
            SOURCE_URL
        )

        if not candidates:

            raise RuntimeError(
                "No M3U8 stream found."
            )

        logging.info(
            "Total candidates: %d",
            len(candidates)
        )

        # Check every candidate until a valid stream is found.
        for index, stream_url in enumerate(
            candidates,
            start=1
        ):

            logging.info(
                "Testing candidate %d/%d",
                index,
                len(candidates)
            )

            if validate_stream(stream_url):

                save_output(stream_url)

                logging.info(
                    "Selected stream successfully."
                )

                return 0

        raise RuntimeError(
            "All M3U8 candidates failed validation."
        )

    except Exception as error:

        logging.error(
            "Collector failed: %s",
            error
        )

        logging.warning(
            "Existing 8tv.txt was not overwritten."
        )

        return 1


if __name__ == "__main__":
    sys.exit(main())

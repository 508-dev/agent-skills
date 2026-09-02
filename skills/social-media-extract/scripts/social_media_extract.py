#!/usr/bin/env python3
"""Turn a public Instagram or Facebook post/Reel into Google Maps search links.

This standalone extractor is a focused port of the 508.dev social-media
pipeline, whose public-fetch approach was inspired by Voy's public pipeline.
It uses ``curl_cffi`` for public fetches and an optional, owner-controlled
local CloakBrowser profile only when the source site requires authentication.
It deliberately stops at a Google Maps search URL: neither this command nor
the skill calls Google Places or needs a Google API key.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import html
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen


INSTAGRAM_HOSTS = {"instagram.com", "www.instagram.com", "instagr.am", "www.instagr.am"}
FACEBOOK_HOSTS = {"facebook.com", "www.facebook.com", "m.facebook.com", "web.facebook.com"}
SOCIAL_MEDIA_HOSTS = {"instagram.com", "cdninstagram.com", "facebook.com", "fbcdn.net"}
INSTAGRAM_KINDS = {"p", "reel", "reels"}
FACEBOOK_KINDS = {"reel", "reels"}
SUPPORTED_PLATFORMS = {"facebook", "instagram"}
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
MAX_HTML_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_MEDIA_BYTES = 30 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 25
DEFAULT_MAX_IMAGES = 8
DEFAULT_MAX_FRAMES = 8
DEFAULT_MAX_IMAGE_BYTES = 5 * 1024 * 1024
RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}
AGE_RESTRICTION = "this content may be inappropriate"
BLOCKED_PATTERNS = (
    "this content isn't available to everyone",
    "this content is not available to everyone",
    "this content isn't available in your region",
    "this content is not available in your region",
)
FACEBOOK_BLOCKED_PATTERNS = (
    "this content isn't available right now",
    "this content is not available right now",
    "this content isn't available",
    "this content is not available",
)
GENERIC_PLACE_NAMES = {
    "activity",
    "attractions",
    "bar",
    "bars",
    "beach",
    "beaches",
    "cafe",
    "cafes",
    "city",
    "food",
    "hotel",
    "hotels",
    "landmark",
    "landmarks",
    "museum",
    "museums",
    "park",
    "parks",
    "restaurant",
    "restaurants",
    "shopping",
    "things to do",
    "top places",
    "travel",
}


class InstagramToMapsError(RuntimeError):
    """A user-facing error from the social-media-extract command."""


class InstagramHttpError(InstagramToMapsError):
    """A public social-media request returned an HTTP error.

    The historic class name remains part of the local test/runtime interface.
    """

    def __init__(self, status_code: int, url: str) -> None:
        platform = platform_for_url(url)
        label = platform.title() if platform else "Social media"
        super().__init__(f"{label} returned HTTP {status_code} for {url}")
        self.status_code = status_code


@dataclass(frozen=True)
class InstagramTarget:
    platform: str
    original_url: str
    shortcode: str
    kind: str
    canonical_url: str
    candidate_urls: tuple[str, ...]


@dataclass
class ScrapedPost:
    source_url: str
    shortcode: str
    platform: str = "instagram"
    transport_url: str | None = None
    username: str | None = None
    caption: str | None = None
    location: str | None = None
    thumbnail_url: str | None = None
    image_urls: list[str] = field(default_factory=list)
    video_url: str | None = None
    content_type: str = "carousel"
    blocked: bool = False
    age_restricted: bool = False
    browser_screenshot: bytes | None = None
    diagnostics: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class LlmConfig:
    base_url: str
    api_key: str
    model: str


@dataclass(frozen=True)
class BrowserManagerConfig:
    base_url: str
    profile_name: str
    ssh_target: str | None
    auth_token: str | None


@dataclass
class BrowserFetch:
    html: str | None = None
    screenshot: bytes | None = None
    session_missing: bool = False
    login_required: bool = False
    login: dict[str, str] | None = None
    diagnostics: list[str] = field(default_factory=list)


class InstagramHtmlParser(HTMLParser):
    """Collect only the metadata and scripts needed for public post parsing."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.scripts: list[str] = []
        self._script_chunks: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.lower(): value or "" for key, value in attrs}
        if tag.lower() == "meta":
            name = (attributes.get("property") or attributes.get("name") or "").lower()
            content = attributes.get("content")
            if name and content and name not in self.meta:
                self.meta[name] = html.unescape(content).strip()
        elif tag.lower() == "script":
            self._script_chunks = []

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._script_chunks is not None:
            script = "".join(self._script_chunks).strip()
            if script:
                self.scripts.append(script)
            self._script_chunks = None

    def handle_data(self, data: str) -> None:
        if self._script_chunks is not None:
            self._script_chunks.append(data)


def unique(values: Iterable[str | None]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value:
            continue
        normalized = value.strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def normalize_instagram_url(value: str) -> InstagramTarget:
    """Validate a public post/Reel URL and mirror Voy's embed-first routing."""

    raw = value.strip()
    if not raw:
        raise InstagramToMapsError("An Instagram URL is required.")
    if "://" not in raw:
        raw = f"https://{raw}"

    parsed = urlparse(raw)
    hostname = (parsed.hostname or "").lower()
    if hostname not in INSTAGRAM_HOSTS:
        raise InstagramToMapsError("Only public instagram.com post and Reel URLs are supported.")

    segments = [segment for segment in parsed.path.split("/") if segment]
    kind_index: int | None = None
    for index, segment in enumerate(segments[:-1]):
        if segment.lower() in INSTAGRAM_KINDS:
            kind_index = index
            break
    if kind_index is None:
        raise InstagramToMapsError(
            "Use an Instagram post (/p/<shortcode>/) or Reel (/reel/<shortcode>/) URL."
        )

    shortcode = segments[kind_index + 1]
    if not re.fullmatch(r"[A-Za-z0-9_-]{4,}", shortcode):
        raise InstagramToMapsError("The Instagram URL does not contain a valid post shortcode.")

    raw_kind = segments[kind_index].lower()
    kind = "p" if raw_kind == "p" else "reel"
    canonical_url = f"https://www.instagram.com/{kind}/{shortcode}/"
    profile_embed_url: str | None = None
    if kind_index == 1 and re.fullmatch(r"[A-Za-z0-9._]+", segments[0]):
        profile_embed_url = (
            f"https://www.instagram.com/{segments[0]}/{kind}/{shortcode}/embed/captioned/"
        )

    return InstagramTarget(
        platform="instagram",
        original_url=value,
        shortcode=shortcode,
        kind=kind,
        canonical_url=canonical_url,
        candidate_urls=tuple(
            unique(
                (
                    # Voy's broadly compatible Instagram embed surface.
                    f"https://www.instagram.com/p/{shortcode}/embed/captioned/",
                    profile_embed_url,
                    f"{canonical_url}embed/captioned/",
                    canonical_url,
                    raw,
                )
            )
        ),
    )


def normalize_facebook_url(value: str) -> InstagramTarget:
    """Validate a public Facebook Reel URL without accepting profile/feed URLs."""

    raw = value.strip()
    if not raw:
        raise InstagramToMapsError("A Facebook Reel URL is required.")
    if "://" not in raw:
        raw = f"https://{raw}"

    parsed = urlparse(raw)
    hostname = (parsed.hostname or "").lower()
    if hostname not in FACEBOOK_HOSTS:
        raise InstagramToMapsError("Only public facebook.com Reel URLs are supported.")

    segments = [segment for segment in parsed.path.split("/") if segment]
    kind_index: int | None = None
    for index, segment in enumerate(segments[:-1]):
        if segment.lower() in FACEBOOK_KINDS:
            kind_index = index
            break
    if kind_index is None:
        raise InstagramToMapsError("Use a Facebook Reel (/reel/<id>) URL.")

    reel_id = segments[kind_index + 1]
    if not re.fullmatch(r"[A-Za-z0-9_-]{4,}", reel_id):
        raise InstagramToMapsError("The Facebook Reel URL does not contain a valid Reel ID.")

    canonical_url = f"https://www.facebook.com/reel/{reel_id}"
    return InstagramTarget(
        platform="facebook",
        original_url=value,
        shortcode=reel_id,
        kind="reel",
        canonical_url=canonical_url,
        candidate_urls=tuple(unique((canonical_url, raw))),
    )


def normalize_social_url(value: str) -> InstagramTarget:
    """Route a supported URL to its platform-specific validator."""

    raw = value.strip()
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    hostname = (parsed.hostname or "").lower()
    if hostname in INSTAGRAM_HOSTS:
        return normalize_instagram_url(value)
    if hostname in FACEBOOK_HOSTS:
        return normalize_facebook_url(value)
    raise InstagramToMapsError(
        "Only public Instagram posts/Reels and facebook.com Reel URLs are supported."
    )


def platform_for_url(value: str) -> str | None:
    hostname = (urlparse(value).hostname or "").lower()
    if hostname in INSTAGRAM_HOSTS or hostname.endswith(".cdninstagram.com"):
        return "instagram"
    if hostname in FACEBOOK_HOSTS or hostname.endswith(".fbcdn.net"):
        return "facebook"
    return None


def social_headers(
    url: str,
    *,
    document: bool = True,
    platform: str | None = None,
) -> dict[str, str]:
    """Return the small set of platform-specific headers we need.

    ``curl_cffi`` supplies the matching Chrome user-agent and browser headers
    for its impersonation target. Keeping this list narrow avoids presenting a
    Safari user-agent with a Chrome TLS/browser fingerprint.
    """

    headers = {
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": (
            "https://www.facebook.com/"
            if (platform or platform_for_url(url)) == "facebook"
            else "https://www.instagram.com/"
        ),
    }
    if document:
        headers.update(
            {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Dest": "document",
                "Upgrade-Insecure-Requests": "1",
            }
        )
    else:
        headers["Accept"] = "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
    return headers


def instagram_headers(*, document: bool = True) -> dict[str, str]:
    """Backward-compatible Instagram header helper."""

    return social_headers("https://www.instagram.com/", document=document)


class CurlInstagramTransport:
    """Bounded public social-media transport using curl_cffi Chrome impersonation."""

    def __init__(self) -> None:
        try:
            from curl_cffi import requests as curl_requests
        except ImportError as error:
            raise InstagramToMapsError(
                "The social-media-extract runtime is unavailable. Run the bundled "
                "social-media-extract launcher so uv can install its locked dependencies."
            ) from error
        self._session = curl_requests.Session(impersonate="chrome")
        self.platform_hint: str | None = None

    def __enter__(self) -> "CurlInstagramTransport":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._session.close()

    def get(self, url: str, *, timeout_seconds: int, document: bool) -> Any:
        return self._session.get(
            url,
            headers=social_headers(url, document=document, platform=self.platform_hint),
            timeout=timeout_seconds,
            allow_redirects=True,
            stream=not document,
        )


def _is_retryable_error(error: Exception) -> bool:
    if isinstance(error, InstagramHttpError):
        return error.status_code in RETRYABLE_STATUS_CODES
    return not isinstance(error, InstagramToMapsError)


def response_charset(content_type: str | None) -> str:
    match = re.search(r"charset=([^;\\s]+)", content_type or "", flags=re.IGNORECASE)
    return match.group(1).strip("\\\"'") if match else "utf-8"


def fetch_text(
    transport: CurlInstagramTransport,
    url: str,
    *,
    timeout_seconds: int,
    max_bytes: int = MAX_HTML_BYTES,
) -> str:
    """Fetch a public page with curl_cffi's consistent Chrome transport."""

    last_error: Exception | None = None
    for attempt in range(2):
        response: Any | None = None
        try:
            response = transport.get(url, timeout_seconds=timeout_seconds, document=True)
            status_code = int(response.status_code)
            if status_code >= 400:
                raise InstagramHttpError(status_code, url)
            body = response.content
            if len(body) > max_bytes:
                raise InstagramToMapsError("Social-media response was unexpectedly large.")
            return body.decode(response_charset(response.headers.get("content-type")), errors="replace")
        except Exception as error:  # curl_cffi normalizes several network errors differently.
            last_error = error
            if attempt == 0 and _is_retryable_error(error):
                time.sleep(0.25)
                continue
            break
        finally:
            if response is not None:
                with contextlib.suppress(Exception):
                    response.close()

    if isinstance(last_error, InstagramToMapsError):
        raise last_error
    raise InstagramToMapsError(f"Could not fetch social-media post {url}: {last_error}") from last_error


def resolve_browser_manager_config(
    args: argparse.Namespace,
    *,
    require: bool = False,
) -> BrowserManagerConfig | None:
    configured_url = (
        args.manager_url
        or os.getenv("SOCIAL_MEDIA_EXTRACT_CLOAK_MANAGER_URL")
        or os.getenv("INSTAGRAM_TO_MAPS_CLOAK_MANAGER_URL")
    )
    if not configured_url:
        if require:
            raise InstagramToMapsError(
                "CloakBrowser Manager is not configured. Pass --manager-url with a local "
                "loopback URL or set SOCIAL_MEDIA_EXTRACT_CLOAK_MANAGER_URL."
            )
        return None
    base_url = configured_url.rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or (parsed.hostname or "").lower() not in LOOPBACK_HOSTS:
        raise InstagramToMapsError("The CloakBrowser Manager URL must be an HTTP(S) loopback URL.")
    return BrowserManagerConfig(
        base_url=base_url,
        profile_name=(
            args.browser_profile
            or os.getenv("SOCIAL_MEDIA_EXTRACT_CLOAK_PROFILE")
            or os.getenv("INSTAGRAM_TO_MAPS_CLOAK_PROFILE")
            or "social-media-extract"
        ),
        ssh_target=(
            os.getenv("SOCIAL_MEDIA_EXTRACT_LOGIN_SSH_TARGET")
            or os.getenv("INSTAGRAM_TO_MAPS_LOGIN_SSH_TARGET")
            or None
        ),
        auth_token=(
            os.getenv("SOCIAL_MEDIA_EXTRACT_CLOAK_MANAGER_TOKEN")
            or os.getenv("INSTAGRAM_TO_MAPS_CLOAK_MANAGER_TOKEN")
            or None
        ),
    )


class CloakBrowserManager:
    """Small, local-only client for the CloakBrowser Manager profile API."""

    def __init__(self, config: BrowserManagerConfig, *, timeout_seconds: int) -> None:
        self.config = config
        self.timeout_seconds = timeout_seconds
        self.active_profile_name = config.profile_name

    def _request(self, path: str, *, method: str = "GET", payload: dict[str, Any] | None = None) -> Any:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if self.config.auth_token:
            headers["Authorization"] = f"Bearer {self.config.auth_token}"
        request = Request(
            f"{self.config.base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310 - URL is validated to loopback above.
                body = response.read(2 * 1024 * 1024)
        except HTTPError as error:
            raise InstagramToMapsError(f"CloakBrowser Manager returned HTTP {error.code}.") from error
        except URLError as error:
            raise InstagramToMapsError(
                "CloakBrowser Manager is not available on the configured loopback URL. "
                "Start a compatible owner-controlled local Manager or use the public fetch path."
            ) from error
        try:
            return json.loads(body.decode("utf-8")) if body else None
        except json.JSONDecodeError as error:
            raise InstagramToMapsError("CloakBrowser Manager returned an unrecognized response.") from error

    def find_profile(self) -> dict[str, Any] | None:
        profiles = self._request("/api/profiles")
        if not isinstance(profiles, list):
            raise InstagramToMapsError("CloakBrowser Manager returned an invalid profile list.")
        accepted_names = [self.config.profile_name]
        if self.config.profile_name == "social-media-extract":
            # Preserve any owner session created before the skill was renamed.
            accepted_names.append("instagram-to-maps")
        for name in accepted_names:
            profile = next(
                (
                    candidate
                    for candidate in profiles
                    if isinstance(candidate, dict) and candidate.get("name") == name
                ),
                None,
            )
            if profile is not None:
                self.active_profile_name = name
                return profile
        return None

    def ensure_profile(self) -> tuple[str, str, bool]:
        profile = self.find_profile()
        if profile is None:
            profile = self._request(
                "/api/profiles",
                method="POST",
                payload={
                    "name": self.config.profile_name,
                    "geoip": False,
                    "humanize": False,
                    "notes": "Persistent owner-authenticated Instagram and Facebook profile for social-media-extract.",
                },
            )
            self.active_profile_name = self.config.profile_name
        if not isinstance(profile, dict) or not isinstance(profile.get("id"), str):
            raise InstagramToMapsError("CloakBrowser Manager did not create a social-media profile.")
        profile_id = profile["id"]
        was_running = profile.get("status") == "running"
        launch = profile
        if not was_running:
            launch = self._request(f"/api/profiles/{quote(profile_id, safe='')}/launch", method="POST")
        if not isinstance(launch, dict):
            raise InstagramToMapsError("CloakBrowser Manager did not return a running profile.")
        cdp_path = launch.get("cdp_url") or profile.get("cdp_url")
        if not isinstance(cdp_path, str) or not cdp_path.startswith("/"):
            cdp_path = f"/api/profiles/{quote(profile_id, safe='')}/cdp"
        return profile_id, f"{self.config.base_url}{cdp_path}", was_running

    def stop_profile(self, profile_id: str) -> None:
        with contextlib.suppress(InstagramToMapsError):
            self._request(f"/api/profiles/{quote(profile_id, safe='')}/stop", method="POST")

    def login_handoff(self, platform: str = "instagram") -> dict[str, str]:
        if platform not in SUPPORTED_PLATFORMS:
            raise InstagramToMapsError(f"Unsupported login platform: {platform}")
        label = platform.title()
        port = urlparse(self.config.base_url).port or (443 if self.config.base_url.startswith("https://") else 80)
        handoff = {
            "platform": platform,
            "profile": self.active_profile_name,
            "manager_url": f"http://127.0.0.1:{port}",
            "instructions": (
                f"Open the private CloakBrowser Manager, complete {label} login and any "
                f"{label}-owned verification, then send the post again."
            ),
        }
        if self.config.ssh_target:
            handoff["ssh_tunnel"] = f"ssh -N -L {port}:127.0.0.1:{port} {self.config.ssh_target}"
        else:
            handoff["ssh_tunnel"] = (
                f"Create an SSH tunnel for local port {port} to the personal AI server, then open "
                f"http://127.0.0.1:{port}."
            )
        return handoff


def profile_has_instagram_session(context: Any) -> bool:
    try:
        cookies = context.cookies("https://www.instagram.com")
    except Exception:
        return False
    return any(
        isinstance(cookie, dict)
        and cookie.get("name") == "sessionid"
        and isinstance(cookie.get("value"), str)
        and bool(cookie["value"])
        for cookie in cookies
    )


def profile_has_facebook_session(context: Any) -> bool:
    try:
        cookies = context.cookies("https://www.facebook.com")
    except Exception:
        return False
    present = {
        cookie.get("name")
        for cookie in cookies
        if isinstance(cookie, dict)
        and isinstance(cookie.get("value"), str)
        and bool(cookie["value"])
    }
    return {"c_user", "xs"}.issubset(present)


def profile_has_platform_session(context: Any, platform: str) -> bool:
    if platform == "instagram":
        return profile_has_instagram_session(context)
    if platform == "facebook":
        return profile_has_facebook_session(context)
    return False


def login_url_for_platform(platform: str) -> str:
    if platform == "instagram":
        return "https://www.instagram.com/accounts/login/"
    if platform == "facebook":
        return "https://www.facebook.com/login/"
    raise InstagramToMapsError(f"Unsupported login platform: {platform}")


def page_requires_login(url: str, platform: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path.lower()
    if platform == "instagram":
        return path.startswith(("/accounts/login", "/challenge"))
    if platform == "facebook":
        return path.startswith(("/login", "/checkpoint")) or path.endswith("/login.php")
    return False


def connect_browser_profile(
    manager: CloakBrowserManager,
    cdp_url: str,
    *,
    timeout_seconds: int,
) -> Any:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise InstagramToMapsError(
            "The social-media runtime is unavailable. Run the bundled social-media-extract "
            "launcher so uv can install its locked dependencies."
        ) from error
    playwright = sync_playwright().start()
    headers = {"Authorization": f"Bearer {manager.config.auth_token}"} if manager.config.auth_token else None
    try:
        browser = playwright.chromium.connect_over_cdp(
            cdp_url,
            timeout=timeout_seconds * 1000,
            headers=headers,
        )
    except Exception as error:
        playwright.stop()
        raise InstagramToMapsError("Could not connect to the CloakBrowser login profile.") from error
    return playwright, browser


def request_browser_login(
    config: BrowserManagerConfig,
    *,
    timeout_seconds: int,
    platform: str = "instagram",
) -> dict[str, Any]:
    """Launch the persistent visible profile and leave it open for the owner."""

    try:
        manager = CloakBrowserManager(config, timeout_seconds=timeout_seconds)
        profile_id, cdp_url, _ = manager.ensure_profile()
        playwright, browser = connect_browser_profile(manager, cdp_url, timeout_seconds=timeout_seconds)
        try:
            contexts = browser.contexts
            if not contexts:
                raise InstagramToMapsError("The CloakBrowser profile did not expose a browser context.")
            page = contexts[0].new_page()
            page.goto(
                login_url_for_platform(platform),
                wait_until="domcontentloaded",
                timeout=timeout_seconds * 1000,
            )
            # Do not close this page: the manager owns the browser and the
            # owner needs to see this tab through their SSH tunnel.
        finally:
            playwright.stop()
        return {
            "schema_version": 2,
            "status": "login_requested",
            "platform": platform,
            "places": [],
            "login": manager.login_handoff(platform),
            "diagnostics": [
                f"The shared persistent CloakBrowser profile is open for owner-controlled {platform.title()} sign-in.",
                f"Do not share {platform.title()} credentials, cookies, or verification codes in chat.",
            ],
            "profile_id": profile_id,
        }
    except InstagramToMapsError as error:
        return {
            "schema_version": 2,
            "status": "login_unavailable",
            "platform": platform,
            "places": [],
            "diagnostics": [str(error)],
        }


def fetch_with_cloakbrowser(
    target: InstagramTarget,
    config: BrowserManagerConfig,
    *,
    timeout_seconds: int,
    request_login: bool,
) -> BrowserFetch:
    """Read a post through the owner-managed persistent CloakBrowser profile."""

    try:
        manager = CloakBrowserManager(config, timeout_seconds=timeout_seconds)
        profile_id, cdp_url, was_running = manager.ensure_profile()
    except InstagramToMapsError as error:
        return BrowserFetch(diagnostics=[str(error)])

    should_stop = not was_running
    login_required = False
    keep_page_open = False
    try:
        playwright, browser = connect_browser_profile(manager, cdp_url, timeout_seconds=timeout_seconds)
        try:
            contexts = browser.contexts
            if not contexts:
                raise InstagramToMapsError("The CloakBrowser profile did not expose a browser context.")
            context = contexts[0]
            page: Any | None = None
            try:
                label = target.platform.title()
                if not profile_has_platform_session(context, target.platform):
                    if not request_login:
                        return BrowserFetch(
                            session_missing=True,
                            diagnostics=[
                                f"The shared persistent CloakBrowser profile has no saved {label} session."
                            ],
                        )
                    login_required = True
                    keep_page_open = True
                    page = context.new_page()
                    page.goto(
                        login_url_for_platform(target.platform),
                        wait_until="domcontentloaded",
                        timeout=timeout_seconds * 1000,
                    )
                    return BrowserFetch(
                        login_required=True,
                        login=manager.login_handoff(target.platform),
                        diagnostics=[f"{label} requires an owner-controlled sign-in for this post."],
                    )
                page = context.new_page()
                page.goto(
                    target.canonical_url,
                    wait_until="domcontentloaded",
                    timeout=timeout_seconds * 1000,
                )
                page.wait_for_timeout(500)
                if page_requires_login(page.url, target.platform):
                    if not request_login:
                        return BrowserFetch(
                            session_missing=True,
                            diagnostics=[f"The saved {label} session needs to be refreshed by its owner."],
                        )
                    login_required = True
                    keep_page_open = True
                    return BrowserFetch(
                        login_required=True,
                        login=manager.login_handoff(target.platform),
                        diagnostics=[f"The saved {label} session needs to be refreshed by its owner."],
                    )
                screenshot: bytes | None = None
                with contextlib.suppress(Exception):
                    screenshot = page.screenshot(type="jpeg", quality=70, full_page=False)
                return BrowserFetch(html=page.content(), screenshot=screenshot)
            finally:
                if page is not None and not keep_page_open:
                    with contextlib.suppress(Exception):
                        page.close()
        finally:
            # Stopping Playwright disconnects our CDP client; it does not close
            # the manager-owned browser/profile that keeps the saved session.
            playwright.stop()
    except InstagramToMapsError as error:
        return BrowserFetch(diagnostics=[str(error)])
    except Exception as error:
        return BrowserFetch(diagnostics=[f"CloakBrowser could not load the post: {error}"])
    finally:
        if should_stop and not login_required:
            manager.stop_profile(profile_id)


def clean_caption(value: str | None) -> str | None:
    if not value:
        return None
    result = html.unescape(value).strip()
    result = re.sub(
        r"^[\d,.]+[KMB]?\s*likes?,?\s*[\d,.]+[KMB]?\s*comments?\s*-\s*\S+\s+on\s+[^:]+:\s*",
        "",
        result,
        flags=re.IGNORECASE,
    )
    result = result.strip(" \t\r\n\"'")
    return result or None


def facebook_caption_and_author(value: str | None) -> tuple[str | None, str | None]:
    """Remove Facebook's engagement prefix and trailing author from OG title text."""

    if not value:
        return None, None
    result = html.unescape(value).strip()
    result = re.sub(
        r"^[^|\n]{0,100}\bviews?\s*·\s*[^|\n]{0,100}\breactions?\s*\|\s*",
        "",
        result,
        flags=re.IGNORECASE,
    )
    author: str | None = None
    trailing = re.search(r"\s*\|\s*([^|\n]{1,100})\s*$", result)
    if trailing:
        author = trailing.group(1).strip() or None
        result = result[: trailing.start()].rstrip()
    result = result.strip(" \t\r\n\"'")
    return result or None, author


def username_from_og(title: str | None, description: str | None) -> str | None:
    source = " ".join(item for item in (title, description) if item)
    for pattern in (
        r"\(@([\w.]+)\)",
        r"-\s*([\w.]+)\s+on\s+Instagram",
        r"\b([\w.]+)\s+on\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\b",
        r"@([\w.]+)",
    ):
        match = re.search(pattern, source, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def decode_jsonish(value: str) -> str:
    """Decode a JSON string fragment without interpreting arbitrary script code."""

    try:
        return json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return (
            value.replace("\\u0026", "&")
            .replace("\\/", "/")
            .replace("\\\\", "\\")
        )


def extract_balanced_json(source: str, start: int) -> str | None:
    """Return the next balanced JSON object, honoring quoted strings."""

    object_start = source.find("{", start)
    if object_start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(object_start, len(source)):
        character = source[index]
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if character == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return source[object_start : index + 1]
    return None


def json_values_from_script(script: str) -> Iterator[Any]:
    """Extract data-only JSON surfaces that Instagram has used for post embeds."""

    candidates = [script, script.replace("\\/", "/")]
    for candidate in candidates:
        with contextlib.suppress(json.JSONDecodeError):
            yield json.loads(candidate)

        for pattern in (
            r"additionalDataLoaded\([^,]+,\s*",
            r"__additionalData\s*=\s*",
            r'"shortcode_media"\s*:\s*',
        ):
            for match in re.finditer(pattern, candidate, flags=re.DOTALL):
                block = extract_balanced_json(candidate, match.end())
                if not block:
                    continue
                with contextlib.suppress(json.JSONDecodeError):
                    yield json.loads(block)

        for match in re.finditer(r'"contextJSON"\s*:\s*"((?:\\.|[^"\\])*)"', candidate, flags=re.DOTALL):
            decoded = decode_jsonish(match.group(1))
            with contextlib.suppress(json.JSONDecodeError):
                yield json.loads(decoded)


def walk_json(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_json(child)


def as_string(value: Any) -> str | None:
    if isinstance(value, str):
        candidate = decode_jsonish(value).strip()
        return candidate or None
    return None


def is_http_url(value: str | None) -> bool:
    if not value:
        return False
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def is_session_media_url(value: str) -> bool:
    """Allow the authenticated browser to fetch only Meta-owned media hosts.

    Post HTML is untrusted input. The CloakBrowser profile must never be used as
    a general-purpose authenticated fetcher for arbitrary URLs or redirects.
    """

    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and any(
        host == allowed_host or host.endswith(f".{allowed_host}")
        for allowed_host in SOCIAL_MEDIA_HOSTS
    )


def first_string(*values: Any) -> str | None:
    for value in values:
        text = as_string(value)
        if text:
            return text
    return None


def list_sidecar_media(media: dict[str, Any]) -> tuple[list[str], str | None]:
    images: list[str] = []
    video: str | None = None
    sidecar = media.get("edge_sidecar_to_children")
    edges = sidecar.get("edges", []) if isinstance(sidecar, dict) else []
    if not isinstance(edges, list):
        return images, video
    for edge in edges:
        node = edge.get("node") if isinstance(edge, dict) else None
        if not isinstance(node, dict):
            continue
        resource_urls: list[str] = []
        resources = node.get("display_resources")
        if isinstance(resources, list):
            for resource in resources:
                if isinstance(resource, dict):
                    source = as_string(resource.get("src"))
                    if is_http_url(source):
                        resource_urls.append(source)
        image = first_string(node.get("display_url"), node.get("thumbnail_src"), node.get("thumbnail_url"))
        # Voy prefers a thumbnail resource for analysis; use the first resource
        # when available so carousel requests stay bounded.
        if resource_urls:
            images.append(resource_urls[0])
        elif is_http_url(image):
            images.append(image)
        if video is None:
            candidate_video = as_string(node.get("video_url"))
            if is_http_url(candidate_video):
                video = candidate_video
    return unique(images), video


def caption_from_media(media: dict[str, Any]) -> str | None:
    edge_caption = media.get("edge_media_to_caption")
    if isinstance(edge_caption, dict):
        edges = edge_caption.get("edges")
        if isinstance(edges, list) and edges:
            node = edges[0].get("node") if isinstance(edges[0], dict) else None
            if isinstance(node, dict):
                return as_string(node.get("text"))
    caption = media.get("caption")
    if isinstance(caption, dict):
        return as_string(caption.get("text"))
    return as_string(caption)


def media_score(media: dict[str, Any], shortcode: str) -> int:
    score = 0
    if as_string(media.get("shortcode")) == shortcode:
        score += 16
    if any(key in media for key in ("video_url", "display_url", "edge_sidecar_to_children")):
        score += 8
    if "edge_media_to_caption" in media:
        score += 4
    if "owner" in media:
        score += 2
    return score


def best_media_json(scripts: Sequence[str], shortcode: str) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    best_score = 0
    for script in scripts:
        for value in json_values_from_script(script):
            for candidate in walk_json(value):
                score = media_score(candidate, shortcode)
                if score > best_score:
                    best = candidate
                    best_score = score
    return best


def regex_urls_for_key(html_text: str, key: str) -> list[str]:
    results: list[str] = []
    key_pattern = re.escape(key)
    patterns = (
        rf'"{key_pattern}"\s*:\s*"((?:\\.|[^"\\])*)"',
        rf'{key_pattern}\\"\s*:\\"((?:\\.|[^"\\])*)',
    )
    for pattern in patterns:
        for match in re.finditer(pattern, html_text, flags=re.DOTALL):
            candidate = decode_jsonish(match.group(1))
            if is_http_url(candidate):
                results.append(candidate)
    return unique(results)


def parse_instagram_html(html_text: str, target: InstagramTarget) -> ScrapedPost:
    parser = InstagramHtmlParser()
    parser.feed(html_text)
    parser.close()
    lower_html = html_text.lower()
    blocked = any(pattern in lower_html for pattern in BLOCKED_PATTERNS)
    age_restricted = AGE_RESTRICTION in lower_html and "unavailable for certain audiences" in lower_html

    og_title = parser.meta.get("og:title") or parser.meta.get("twitter:title")
    og_description = parser.meta.get("og:description") or parser.meta.get("twitter:description")
    thumbnail_url = parser.meta.get("og:image") or parser.meta.get("twitter:image")
    video_url = parser.meta.get("og:video")
    media = best_media_json(parser.scripts, target.shortcode)

    username: str | None = None
    caption: str | None = None
    location: str | None = None
    image_urls: list[str] = []
    content_type = "video" if target.kind == "reel" or is_http_url(video_url) else "carousel"

    if media:
        owner = media.get("owner")
        username = as_string(owner.get("username")) if isinstance(owner, dict) else None
        caption = caption_from_media(media)
        location_value = media.get("location")
        location = as_string(location_value.get("name")) if isinstance(location_value, dict) else as_string(location_value)
        image_urls, sidecar_video = list_sidecar_media(media)
        display_url = first_string(media.get("display_url"), media.get("thumbnail_src"), media.get("thumbnail_url"))
        thumbnail_url = thumbnail_url or display_url
        video_url = first_string(media.get("video_url"), video_url, sidecar_video)
        if image_urls:
            content_type = "carousel"
        elif is_http_url(video_url):
            content_type = "video"

    username = username or username_from_og(og_title, og_description)
    caption = clean_caption(caption or og_description)
    if not image_urls:
        image_urls = regex_urls_for_key(html_text, "display_url")
    if not image_urls:
        image_urls = regex_urls_for_key(html_text, "thumbnail_src")
    if not thumbnail_url:
        thumbnail_url = image_urls[0] if image_urls else None
    if not video_url:
        videos = regex_urls_for_key(html_text, "video_url")
        video_url = videos[0] if videos else None
    if is_http_url(video_url) and not image_urls:
        content_type = "video"
    if not image_urls and is_http_url(thumbnail_url):
        image_urls = [thumbnail_url]

    return ScrapedPost(
        source_url=target.canonical_url,
        shortcode=target.shortcode,
        platform="instagram",
        username=username,
        caption=caption,
        location=location,
        thumbnail_url=thumbnail_url if is_http_url(thumbnail_url) else None,
        image_urls=unique(url for url in image_urls if is_http_url(url)),
        video_url=video_url if is_http_url(video_url) else None,
        content_type=content_type,
        blocked=blocked,
        age_restricted=age_restricted,
    )


def parse_facebook_html(html_text: str, target: InstagramTarget) -> ScrapedPost:
    """Parse the public OG and embedded media surfaces exposed for a Facebook Reel."""

    parser = InstagramHtmlParser()
    parser.feed(html_text)
    parser.close()

    og_title = parser.meta.get("og:title") or parser.meta.get("twitter:title")
    og_description = parser.meta.get("og:description") or parser.meta.get("twitter:description")
    caption, author = facebook_caption_and_author(og_title)
    if not caption:
        caption = clean_caption(og_description)

    thumbnail_url = parser.meta.get("og:image") or parser.meta.get("twitter:image")
    videos = regex_urls_for_key(html_text, "browser_native_hd_url")
    if not videos:
        videos = regex_urls_for_key(html_text, "browser_native_sd_url")
    video_url = videos[0] if videos else parser.meta.get("og:video")
    has_metadata = bool(caption or thumbnail_url or video_url)
    lower_html = html_text.lower()
    blocked = not has_metadata and any(pattern in lower_html for pattern in FACEBOOK_BLOCKED_PATTERNS)

    image_urls = [thumbnail_url] if is_http_url(thumbnail_url) else []
    return ScrapedPost(
        source_url=target.canonical_url,
        shortcode=target.shortcode,
        platform="facebook",
        username=author,
        caption=caption,
        thumbnail_url=thumbnail_url if is_http_url(thumbnail_url) else None,
        image_urls=image_urls,
        video_url=video_url if is_http_url(video_url) else None,
        content_type="video",
        blocked=blocked,
    )


def parse_social_html(html_text: str, target: InstagramTarget) -> ScrapedPost:
    if target.platform == "instagram":
        return parse_instagram_html(html_text, target)
    if target.platform == "facebook":
        return parse_facebook_html(html_text, target)
    raise InstagramToMapsError(f"Unsupported social platform: {target.platform}")


def is_usable_post(post: ScrapedPost) -> bool:
    return bool(
        post.blocked
        or post.age_restricted
        or post.username
        or post.caption
        or post.location
        or post.thumbnail_url
        or post.image_urls
        or post.video_url
    )


def scrape_instagram(
    transport: CurlInstagramTransport,
    target: InstagramTarget,
    *,
    timeout_seconds: int,
) -> ScrapedPost:
    """Attempt the same embed-first then canonical-page order used in Voy."""

    failures: list[str] = []
    best_partial: ScrapedPost | None = None
    for candidate_url in target.candidate_urls:
        try:
            post = parse_instagram_html(
                fetch_text(transport, candidate_url, timeout_seconds=timeout_seconds),
                target,
            )
            post.transport_url = candidate_url
            if post.blocked or post.age_restricted:
                return post
            if is_usable_post(post):
                return post
            best_partial = post
            failures.append(f"{candidate_url}: returned no post metadata")
        except InstagramToMapsError as error:
            failures.append(str(error))

    if best_partial:
        best_partial.diagnostics.extend(failures)
        return best_partial
    raise InstagramToMapsError("; ".join(failures) or "Instagram returned no usable post metadata.")


def scrape_social(
    transport: CurlInstagramTransport,
    target: InstagramTarget,
    *,
    timeout_seconds: int,
) -> ScrapedPost:
    if target.platform == "instagram":
        return scrape_instagram(transport, target, timeout_seconds=timeout_seconds)

    failures: list[str] = []
    best_partial: ScrapedPost | None = None
    for candidate_url in target.candidate_urls:
        try:
            post = parse_social_html(
                fetch_text(transport, candidate_url, timeout_seconds=timeout_seconds),
                target,
            )
            post.transport_url = candidate_url
            if post.blocked or post.age_restricted or is_usable_post(post):
                return post
            best_partial = post
            failures.append(f"{candidate_url}: returned no Reel metadata")
        except InstagramToMapsError as error:
            failures.append(str(error))

    if best_partial:
        best_partial.diagnostics.extend(failures)
        return best_partial
    raise InstagramToMapsError("; ".join(failures) or "Facebook returned no usable Reel metadata.")


def download_binary(
    transport: CurlInstagramTransport,
    url: str,
    destination: Path,
    *,
    timeout_seconds: int,
    max_bytes: int,
) -> tuple[Path, str | None]:
    response: Any | None = None
    try:
        response = transport.get(url, timeout_seconds=timeout_seconds, document=False)
        status_code = int(response.status_code)
        if status_code >= 400:
            raise InstagramHttpError(status_code, url)
        total = 0
        with destination.open("wb") as output:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise InstagramToMapsError(f"Media exceeds the {max_bytes // (1024 * 1024)} MiB limit.")
                output.write(chunk)
        if total == 0:
            raise InstagramToMapsError("Social-media response was empty.")
        return destination, response.headers.get("content-type")
    except InstagramToMapsError:
        raise
    except Exception as error:
        raise InstagramToMapsError(f"Could not fetch social-media content: {error}") from error
    finally:
        if response is not None:
            with contextlib.suppress(Exception):
                response.close()


def media_cache_directory() -> Path:
    """Create an owner-only, per-extraction media cache directory.

    ``SOCIAL_MEDIA_EXTRACT_CACHE_DIR`` can select an explicit cache root.
    Otherwise the XDG cache directory (or ``~/.cache``) is used. ``mkdtemp``
    gives every extraction a unique, mode-0700 directory without exposing
    browser-session data.
    """

    configured_root = os.getenv("SOCIAL_MEDIA_EXTRACT_CACHE_DIR")
    if configured_root:
        cache_root = Path(configured_root).expanduser()
    else:
        xdg_cache_home = os.getenv("XDG_CACHE_HOME")
        cache_home = Path(xdg_cache_home).expanduser() if xdg_cache_home else Path.home() / ".cache"
        cache_root = cache_home / "social-media-extract"
    cache_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return Path(tempfile.mkdtemp(prefix="media-", dir=cache_root))


def download_images_with_cloakbrowser(
    sources: Sequence[tuple[int, str, Path]],
    config: BrowserManagerConfig,
    *,
    timeout_seconds: int,
    max_bytes: int,
    platform: str = "instagram",
) -> tuple[set[int], dict[int, str]]:
    """Download allowlisted media through the saved browser session.

    Playwright's BrowserContext request client shares the profile's cookie jar,
    but this function never reads, serializes, or returns cookie values. Public
    ``curl_cffi`` remains the fallback for a missing session or failed request.
    """

    completed: set[int] = set()
    failures: dict[int, str] = {}
    allowed_sources = [source for source in sources if is_session_media_url(source[1])]
    for index, _url, _destination in sources:
        if not is_session_media_url(_url):
            failures[index] = "CloakBrowser session downloads only allow approved Meta media hosts."
    if not allowed_sources:
        return completed, failures

    manager: CloakBrowserManager | None = None
    profile_id: str | None = None
    should_stop = False
    try:
        manager = CloakBrowserManager(config, timeout_seconds=timeout_seconds)
        if manager.find_profile() is None:
            message = "No saved shared CloakBrowser profile is available."
            failures.update({index: message for index, _url, _destination in allowed_sources})
            return completed, failures
        profile_id, cdp_url, was_running = manager.ensure_profile()
        should_stop = not was_running
        playwright, browser = connect_browser_profile(manager, cdp_url, timeout_seconds=timeout_seconds)
        try:
            contexts = browser.contexts
            if not contexts:
                raise InstagramToMapsError("The CloakBrowser profile did not expose a browser context.")
            context = contexts[0]
            if not profile_has_platform_session(context, platform):
                message = f"The saved CloakBrowser profile has no active {platform.title()} session."
                failures.update({index: message for index, _url, _destination in allowed_sources})
                return completed, failures
            for index, url, destination in allowed_sources:
                response: Any | None = None
                try:
                    response = context.request.get(
                        url,
                        timeout=timeout_seconds * 1000,
                        headers={"Range": f"bytes=0-{max_bytes - 1}"},
                        max_redirects=0,
                    )
                    status_code = int(response.status)
                    if status_code >= 300:
                        raise InstagramHttpError(status_code, url)
                    headers = response.headers
                    content_length = headers.get("content-length") or headers.get("Content-Length")
                    if content_length and int(content_length) > max_bytes:
                        raise InstagramToMapsError(
                            f"Media exceeds the {max_bytes // (1024 * 1024)} MiB limit."
                        )
                    body = response.body()
                    if not body:
                        raise InstagramToMapsError("Social-media response was empty.")
                    if len(body) > max_bytes:
                        raise InstagramToMapsError(
                            f"Media exceeds the {max_bytes // (1024 * 1024)} MiB limit."
                        )
                    destination.write_bytes(body)
                    completed.add(index)
                except Exception as error:
                    failures[index] = f"CloakBrowser session download failed: {error}"
                finally:
                    if response is not None:
                        dispose = getattr(response, "dispose", None)
                        if callable(dispose):
                            with contextlib.suppress(Exception):
                                dispose()
        finally:
            # This disconnects our CDP client only; it preserves the persistent
            # manager-owned profile and its cookies.
            playwright.stop()
    except InstagramToMapsError as error:
        failures.update({index: str(error) for index, _url, _destination in allowed_sources if index not in completed})
    except Exception as error:
        failures.update(
            {
                index: f"CloakBrowser session download failed: {error}"
                for index, _url, _destination in allowed_sources
                if index not in completed
            }
        )
    finally:
        if manager is not None and should_stop and profile_id is not None:
            manager.stop_profile(profile_id)
    return completed, failures


def materialize_media_handoff(
    media: dict[str, Any],
    transport: CurlInstagramTransport,
    browser_config: BrowserManagerConfig | None,
    *,
    timeout_seconds: int,
    max_bytes: int,
    max_video_bytes: int | None = None,
    max_frames: int = DEFAULT_MAX_FRAMES,
) -> list[str]:
    """Save image/video inputs to an owner-only local cache without exposing cookies."""

    platform = media.get("platform") if media.get("platform") in SUPPORTED_PLATFORMS else "instagram"
    raw_images = media.get("images")
    images = raw_images if isinstance(raw_images, list) else []
    sources: list[tuple[int, str, Path]] = []
    download_dir: Path | None = None
    for index, image in enumerate(images):
        url = image.get("download_url") if isinstance(image, dict) else None
        if not isinstance(url, str) or not is_http_url(url):
            continue
        if download_dir is None:
            download_dir = media_cache_directory()
        sources.append((index, url, download_dir / f"image-{index + 1}.img"))
    video_url = media.get("video_url")
    has_video = isinstance(video_url, str) and is_http_url(video_url)
    if not sources and not has_video:
        return ["No downloadable image or video URLs were available from this post."]

    downloaded: set[int] = set()
    session_failures: dict[int, str] = {}
    if browser_config is not None:
        downloaded, session_failures = download_images_with_cloakbrowser(
            sources,
            browser_config,
            timeout_seconds=timeout_seconds,
            max_bytes=max_bytes,
            platform=platform,
        )

    diagnostics: list[str] = []
    for index, url, destination in sources:
        image = images[index]
        if not isinstance(image, dict):
            continue
        if index in downloaded:
            image["local_path"] = str(destination)
            image["downloaded_via"] = "cloakbrowser_session"
            continue
        try:
            download_binary(
                transport,
                url,
                destination,
                timeout_seconds=timeout_seconds,
                max_bytes=max_bytes,
            )
            image["local_path"] = str(destination)
            image["downloaded_via"] = "curl_cffi"
            continue
        except InstagramToMapsError as error:
            failures = [session_failures.get(index), f"Public curl_cffi download failed: {error}"]
            image["download_error"] = "; ".join(failure for failure in failures if failure)
            diagnostics.append(f"image {index + 1}: {image['download_error']}")

    if has_video and isinstance(video_url, str):
        if download_dir is None:
            download_dir = media_cache_directory()
        video_limit = max_video_bytes or max_bytes
        video_path = download_dir / "reel.mp4"
        video_downloaded = False
        video_session_failure: str | None = None
        if browser_config is not None:
            completed, failures = download_images_with_cloakbrowser(
                [(0, video_url, video_path)],
                browser_config,
                timeout_seconds=timeout_seconds,
                max_bytes=video_limit,
                platform=platform,
            )
            video_downloaded = 0 in completed
            video_session_failure = failures.get(0)
        downloaded_via = "cloakbrowser_session"
        if not video_downloaded:
            try:
                download_binary(
                    transport,
                    video_url,
                    video_path,
                    timeout_seconds=timeout_seconds,
                    max_bytes=video_limit,
                )
                video_downloaded = True
                downloaded_via = "curl_cffi"
            except InstagramToMapsError as error:
                failures = [video_session_failure, f"Public curl_cffi download failed: {error}"]
                media["video_download_error"] = "; ".join(failure for failure in failures if failure)
                diagnostics.append(f"reel video: {media['video_download_error']}")
        if video_downloaded:
            media["video_local_path"] = str(video_path)
            media["video_downloaded_via"] = downloaded_via
            frames = video_frames(
                video_path,
                download_dir / "frames",
                max_frames=max_frames,
                timeout_seconds=timeout_seconds,
            )
            media["frames"] = [{"local_path": str(frame)} for frame in frames]
            if not frames:
                diagnostics.append("Reel video downloaded, but no visual frames could be sampled.")

    if any(isinstance(image, dict) and image.get("local_path") for image in images) or media.get(
        "video_local_path"
    ):
        media["download_directory"] = str(download_dir)
    elif download_dir is not None:
        with contextlib.suppress(OSError):
            download_dir.rmdir()
    return diagnostics


def run_ffmpeg(arguments: list[str], *, timeout_seconds: int) -> bool:
    if shutil.which("ffmpeg") is None:
        return False
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *arguments],
            check=False,
            capture_output=True,
            timeout=timeout_seconds,
            text=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def image_as_jpeg(source: Path, destination: Path, *, timeout_seconds: int) -> Path | None:
    if run_ffmpeg(
        [
            "-i",
            str(source),
            "-frames:v",
            "1",
            "-vf",
            "scale='min(1280,iw)':-2",
            "-q:v",
            "3",
            str(destination),
        ],
        timeout_seconds=timeout_seconds,
    ) and destination.exists():
        return destination
    return None


def video_duration_seconds(video_path: Path, *, timeout_seconds: int) -> float | None:
    if shutil.which("ffprobe") is None:
        return None
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            check=False,
            capture_output=True,
            timeout=timeout_seconds,
            text=True,
        )
        if result.returncode != 0:
            return None
        duration = float(result.stdout.strip())
        return duration if duration > 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def video_frames(
    video_path: Path,
    output_dir: Path,
    *,
    max_frames: int,
    timeout_seconds: int,
) -> list[Path]:
    """Sample the Reel across its duration instead of relying on one cover frame."""

    duration = video_duration_seconds(video_path, timeout_seconds=timeout_seconds)
    if not duration:
        return []
    output_dir.mkdir(parents=True, exist_ok=True)
    frames: list[Path] = []
    for index in range(max_frames):
        timestamp = duration * (index + 1) / (max_frames + 1)
        frame = output_dir / f"frame-{index + 1}.jpg"
        if run_ffmpeg(
            [
                "-ss",
                f"{timestamp:.3f}",
                "-i",
                str(video_path),
                "-frames:v",
                "1",
                "-vf",
                "scale='min(1280,iw)':-2",
                "-q:v",
                "3",
                str(frame),
            ],
            timeout_seconds=timeout_seconds,
        ) and frame.exists():
            frames.append(frame)
    return frames


def data_url(path: Path, content_type: str | None = None) -> str:
    media_type = content_type or mimetypes.guess_type(path.name)[0] or "image/jpeg"
    return f"data:{media_type};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def media_attachments(
    post: ScrapedPost,
    temp_dir: Path,
    transport: CurlInstagramTransport,
    *,
    timeout_seconds: int,
    max_images: int,
    max_frames: int,
    max_media_bytes: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Download temporary analysis inputs; failures leave caption analysis intact."""

    diagnostics: list[str] = []
    attachments: list[dict[str, Any]] = []
    downloaded_images: list[Path] = []
    if post.browser_screenshot:
        if len(post.browser_screenshot) <= DEFAULT_MAX_IMAGE_BYTES:
            screenshot_path = temp_dir / "authenticated-browser.jpg"
            screenshot_path.write_bytes(post.browser_screenshot)
            attachments.append(
                {
                    "type": "image_url",
                    "image_url": {"url": data_url(screenshot_path, "image/jpeg"), "detail": "low"},
                }
            )
        else:
            diagnostics.append("Authenticated browser screenshot exceeded the image size limit.")
    source_images = unique([*post.image_urls, post.thumbnail_url])[:max_images]
    for index, url in enumerate(source_images):
        raw_path = temp_dir / f"image-{index + 1}"
        try:
            source_path, content_type = download_binary(
                transport,
                url,
                raw_path,
                timeout_seconds=timeout_seconds,
                max_bytes=min(max_media_bytes, DEFAULT_MAX_IMAGE_BYTES),
            )
            jpeg_path = temp_dir / f"image-{index + 1}.jpg"
            prepared = image_as_jpeg(source_path, jpeg_path, timeout_seconds=timeout_seconds) or source_path
            downloaded_images.append(prepared)
            attachment_type = "image/jpeg" if prepared == jpeg_path else content_type
            attachments.append(
                {
                    "type": "image_url",
                    "image_url": {"url": data_url(prepared, attachment_type), "detail": "low"},
                }
            )
        except InstagramToMapsError as error:
            diagnostics.append(f"image {index + 1}: {error}")

    # Carousels already provide visual context. For a Reel, frames provide more
    # coverage than the thumbnail alone, so prefer them if the public URL works.
    if not downloaded_images and post.video_url:
        video_path = temp_dir / "reel.mp4"
        try:
            download_binary(
                transport,
                post.video_url,
                video_path,
                timeout_seconds=timeout_seconds,
                max_bytes=max_media_bytes,
            )
            frames = video_frames(
                video_path,
                temp_dir / "frames",
                max_frames=max_frames,
                timeout_seconds=timeout_seconds,
            )
            for frame in frames:
                attachments.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": data_url(frame, "image/jpeg"), "detail": "low"},
                    }
                )
            if not frames:
                diagnostics.append("Reel frames were unavailable; used text metadata only.")
        except InstagramToMapsError as error:
            diagnostics.append(f"reel media: {error}")
    return attachments, diagnostics


def resolve_llm_config(args: argparse.Namespace) -> LlmConfig | None:
    base_url = (
        args.llm_base_url
        or os.getenv("SOCIAL_MEDIA_EXTRACT_LLM_BASE_URL")
        or os.getenv("INSTAGRAM_TO_MAPS_LLM_BASE_URL")
        or os.getenv("HERMES_LITELLM_BASE_URL")
    )
    model = (
        args.llm_model
        or os.getenv("SOCIAL_MEDIA_EXTRACT_LLM_MODEL")
        or os.getenv("INSTAGRAM_TO_MAPS_LLM_MODEL")
        or os.getenv("HERMES_LITELLM_MODEL")
    )
    key_env = args.llm_api_key_env or "SOCIAL_MEDIA_EXTRACT_LLM_API_KEY"
    api_key = (
        os.getenv(key_env)
        or os.getenv("INSTAGRAM_TO_MAPS_LLM_API_KEY")
        or os.getenv("HERMES_LITELLM_API_KEY")
    )
    if not (base_url and model and api_key):
        return None
    return LlmConfig(base_url=base_url.rstrip("/"), api_key=api_key, model=model)


def place_prompt(post: ScrapedPost, media_count: int) -> str:
    platform_label = post.platform.title()
    return f"""Extract travel locations from this public {platform_label} post. Return JSON only with this shape:
{{"overall_region": string|null, "places": [{{"name": string, "region": string|null, "evidence": string|null}}]}}

Return every distinct, specific named venue, landmark, neighborhood, park, beach, hotel, restaurant, bar, museum, or attraction mentioned or visibly identified. A place must be specific enough to search on a map. Do not return countries, states, provinces, continents, generic categories, post titles, or listicle headings. Do not invent places. Use region only to disambiguate a place; use overall_region when it applies to most places. Caption and visual text can be multilingual.

{platform_label} author: {post.username or "unknown"}
{platform_label} tagged location: {post.location or "none"}
Caption: {post.caption or "none"}
Visual inputs attached: {media_count}
"""


def parse_json_response(value: Any) -> dict[str, Any]:
    if isinstance(value, list):
        value = "".join(item.get("text", "") for item in value if isinstance(item, dict))
    if not isinstance(value, str):
        raise InstagramToMapsError("The vision endpoint returned no JSON content.")
    text = value.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise InstagramToMapsError("The vision endpoint did not return valid JSON.") from error
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError as nested_error:
            raise InstagramToMapsError("The vision endpoint did not return valid JSON.") from nested_error
    if not isinstance(payload, dict):
        raise InstagramToMapsError("The vision endpoint returned a non-object JSON result.")
    return payload


def analyze_places(
    post: ScrapedPost,
    attachments: list[dict[str, Any]],
    config: LlmConfig,
    *,
    timeout_seconds: int,
) -> dict[str, Any]:
    payload = {
        "model": config.model,
        "temperature": 0,
        "max_tokens": 1800,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": "You are a careful travel-location extractor. Return only the requested JSON object.",
            },
            {
                "role": "user",
                "content": [{"type": "text", "text": place_prompt(post, len(attachments))}, *attachments],
            },
        ],
    }
    request = Request(
        f"{config.base_url}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=max(timeout_seconds, 60)) as response:  # noqa: S310 - endpoint is supplied by the owner.
            response_body = response.read(4 * 1024 * 1024)
    except HTTPError as error:
        raise InstagramToMapsError(f"Vision analysis returned HTTP {error.code}.") from error
    except URLError as error:
        raise InstagramToMapsError(f"Could not reach the configured vision endpoint: {error.reason}") from error
    try:
        response_json = json.loads(response_body.decode("utf-8"))
        content = response_json["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
        raise InstagramToMapsError("The vision endpoint returned an unrecognized response.") from error
    return parse_json_response(content)


def valid_place_name(name: str, region: str | None) -> bool:
    normalized = " ".join(name.split()).strip()
    lower = normalized.casefold()
    if len(normalized) < 2 or len(normalized) > 180 or lower in GENERIC_PLACE_NAMES:
        return False
    if lower.startswith(("top ", "best ", "things to do", "places to ")):
        return False
    if region and lower == " ".join(region.split()).casefold():
        return False
    return True


def google_maps_search_url(query: str) -> str:
    """Build a navigation-only Maps URL without resolving a Google Place ID."""

    return f"https://www.google.com/maps/search/?api=1&query={quote(query, safe='')}"


def maps_places(analysis: dict[str, Any], post: ScrapedPost) -> list[dict[str, str | None]]:
    raw_places = analysis.get("places")
    if not isinstance(raw_places, list):
        raw_places = analysis.get("placeCandidates")
    overall_region = as_string(analysis.get("overall_region")) or as_string(analysis.get("overallRegion"))
    places: list[dict[str, str | None]] = []
    seen: set[str] = set()
    if isinstance(raw_places, list):
        for raw_place in raw_places:
            if not isinstance(raw_place, dict):
                continue
            name = as_string(raw_place.get("name"))
            region = as_string(raw_place.get("region")) or overall_region
            if not name or not valid_place_name(name, region):
                continue
            query = name if not region or region.casefold() in name.casefold() else f"{name}, {region}"
            dedupe_key = query.casefold()
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            places.append(
                {
                    "name": name,
                    "region": region,
                    "evidence": as_string(raw_place.get("evidence")),
                    "maps_url": google_maps_search_url(query),
                }
            )
    if not places and post.location and valid_place_name(post.location, None):
        platform_label = post.platform.title()
        places.append(
            {
                "name": post.location,
                "region": None,
                "evidence": f"{platform_label} tagged location",
                "maps_url": google_maps_search_url(post.location),
            }
        )
    return places


def media_handoff(post: ScrapedPost, *, max_images: int) -> dict[str, Any]:
    """Return short-lived public media URLs and a local-download command.

    The structured download command saves media into a local owner-only cache.
    It reuses CloakBrowser's session internally when configured and never
    copies cookies into the result or ``curl_cffi`` transport.
    """

    image_urls = unique([*post.image_urls, post.thumbnail_url])[:max_images]
    return {
        "platform": post.platform,
        "images": [{"download_url": url} for url in image_urls],
        "video_url": post.video_url,
        "download_command": [
            str(Path(__file__).with_name("social-media-extract")),
            "--json",
            "--scrape-only",
            "--download-media",
            post.source_url,
        ],
        "download_method": (
            "Run media.download_command to save images, video, and sampled Reel frames in an "
            "owner-only local cache. It reuses a configured CloakBrowser session for this platform "
            "when available and otherwise uses curl_cffi."
        ),
    }


def post_from_browser_fetch(fetch: BrowserFetch, target: InstagramTarget) -> ScrapedPost | None:
    if not fetch.html:
        return None
    post = parse_social_html(fetch.html, target)
    post.transport_url = target.canonical_url
    post.browser_screenshot = fetch.screenshot
    post.diagnostics.extend(fetch.diagnostics)
    return post


def result_for_target(target: InstagramTarget, args: argparse.Namespace) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": 2,
        "platform": target.platform,
        "source_url": target.canonical_url,
        "status": "scrape_failed",
        "post": None,
        "instagram": None,
        "media": None,
        "places": [],
        "diagnostics": [],
    }
    browser_config: BrowserManagerConfig | None = None
    browser_probe = BrowserFetch()
    try:
        browser_config = resolve_browser_manager_config(args)
        if browser_config is not None:
            manager = CloakBrowserManager(browser_config, timeout_seconds=args.timeout)
        else:
            manager = None
        if manager is not None and manager.find_profile() is not None:
            # Once the owner has created a profile, prefer its saved first-party
            # session. Cookies stay inside CloakBrowser and are never copied to
            # curl_cffi's public transport.
            browser_probe = fetch_with_cloakbrowser(
                target,
                browser_config,
                timeout_seconds=args.timeout,
                request_login=False,
            )
    except InstagramToMapsError as error:
        # Public posts remain usable when the optional browser layer is down.
        browser_probe = BrowserFetch(diagnostics=[str(error)])

    try:
        transport = CurlInstagramTransport()
        transport.platform_hint = target.platform
    except InstagramToMapsError as error:
        result["diagnostics"] = [str(error), *browser_probe.diagnostics]
        return result
    with transport:
        post = post_from_browser_fetch(browser_probe, target)
        public_failure: str | None = None
        if post is None or not is_usable_post(post):
            try:
                post = scrape_social(transport, target, timeout_seconds=args.timeout)
            except InstagramToMapsError as error:
                public_failure = str(error)

        # curl_cffi is the public fallback. Only after neither the saved
        # session nor that public route can expose a post do we open the
        # configured owner-controlled login profile.
        if post is None or not is_usable_post(post):
            browser = browser_probe
            if browser_config is not None:
                browser = fetch_with_cloakbrowser(
                    target,
                    browser_config,
                    timeout_seconds=args.timeout,
                    request_login=True,
                )
            if browser.login_required:
                result["status"] = "login_required"
                result["login"] = browser.login
                result["diagnostics"] = [
                    *([f"Public curl_cffi fetch: {public_failure}"] if public_failure else []),
                    *browser.diagnostics,
                ]
                return result
            browser_post = post_from_browser_fetch(browser, target)
            if browser_post is not None:
                post = browser_post
            elif post is None:
                result["diagnostics"] = [
                    *([f"Public curl_cffi fetch: {public_failure}"] if public_failure else []),
                    *browser.diagnostics,
                ]
                return result
            else:
                post.diagnostics.extend(browser.diagnostics)

        post_payload = {
            "platform": post.platform,
            "id": post.shortcode,
            "shortcode": post.shortcode,
            "username": post.username,
            "author": post.username,
            "caption": post.caption,
            "location": post.location,
            "content_type": post.content_type,
            "transport_url": post.transport_url,
        }
        result["post"] = post_payload
        if target.platform == "instagram":
            # Retain the v1 field while consumers migrate to the generic post object.
            result["instagram"] = post_payload
        result["media"] = media_handoff(post, max_images=args.max_images)
        if post.blocked or post.age_restricted:
            result["status"] = "restricted"
            restriction = "age restricted" if post.age_restricted else "not available to this public audience"
            result["diagnostics"] = [
                f"{target.platform.title()} marked this post as {restriction}; no bypass was attempted."
            ]
            return result

        if not is_usable_post(post):
            result["diagnostics"] = [
                *post.diagnostics,
                f"{target.platform.title()} returned no usable post metadata.",
            ]
            return result

        download_diagnostics: list[str] = []
        if args.download_media:
            download_diagnostics = materialize_media_handoff(
                result["media"],
                transport,
                browser_config,
                timeout_seconds=args.timeout,
                max_bytes=min(args.max_media_bytes, DEFAULT_MAX_IMAGE_BYTES),
                max_video_bytes=args.max_media_bytes,
                max_frames=args.max_frames,
            )

        if args.scrape_only:
            result["status"] = "scraped"
            result["diagnostics"] = [*post.diagnostics, *download_diagnostics]
            return result

        config = resolve_llm_config(args)
        if not config:
            result["status"] = "metadata_ready"
            result["diagnostics"] = [
                *post.diagnostics,
                *download_diagnostics,
                "No optional vision endpoint is configured. Returned the caption, tagged location, "
                "and bounded media download command for the caller to analyze.",
            ]
            result["places"] = maps_places({}, post)
            result["analysis"] = {
                "mode": "metadata_ready",
                "media_inputs": len(result["media"]["images"]),
            }
            return result

        with tempfile.TemporaryDirectory(prefix="social-media-extract-") as directory:
            attachments, media_diagnostics = media_attachments(
                post,
                Path(directory),
                transport,
                timeout_seconds=args.timeout,
                max_images=args.max_images,
                max_frames=args.max_frames,
                max_media_bytes=args.max_media_bytes,
            )
            try:
                analysis = analyze_places(post, attachments, config, timeout_seconds=args.timeout)
            except InstagramToMapsError as error:
                result["status"] = "analysis_failed"
                result["diagnostics"] = [*post.diagnostics, *download_diagnostics, *media_diagnostics, str(error)]
                result["places"] = maps_places({}, post)
                return result

        result["status"] = "ok"
        result["places"] = maps_places(analysis, post)
        result["diagnostics"] = [*post.diagnostics, *download_diagnostics, *media_diagnostics]
        result["analysis"] = {
            "overall_region": as_string(analysis.get("overall_region")) or as_string(analysis.get("overallRegion")),
            "media_inputs": len(attachments),
            "model": config.model,
        }
        return result


def render_result(result: dict[str, Any]) -> str:
    platform = result.get("platform") or (result.get("login") or {}).get("platform") or "social media"
    platform_label = str(platform).title()
    source_url = result.get("source_url") or f"{platform_label} login"
    status = result["status"]
    post = result.get("post") or result.get("instagram") or result.get("facebook") or {}
    username = post.get("username")
    heading = f"{platform_label}: {source_url}"
    if username:
        heading += f" — {username}"
    lines = [heading]
    places = result.get("places") or []
    if status in {"metadata_ready", "hermes_fallback"}:
        lines.append("Metadata is ready for caption and local-media analysis.")
        if post.get("location"):
            lines.append(f"{platform_label} tagged location: {post['location']}")
        if post.get("caption"):
            lines.append(f"Caption: {post['caption']}")
        if places:
            for place in places:
                label = place["name"]
                if place.get("region"):
                    label = f"{label} — {place['region']}"
                lines.append(f"- [{label}]({place['maps_url']})")
            lines.append("Google Maps search links only; no Google Places API was used.")
        media = result.get("media") or {}
        images = media.get("images") or []
        for index, image in enumerate(images, start=1):
            url = image.get("download_url") if isinstance(image, dict) else None
            if url:
                lines.append(f"- [Download image {index}]({url})")
            local_path = image.get("local_path") if isinstance(image, dict) else None
            if local_path:
                lines.append(f"  Downloaded locally via {image.get('downloaded_via')}: `{local_path}`")
        if media.get("video_url"):
            lines.append(f"- [Download Reel video]({media['video_url']})")
        if media.get("video_local_path"):
            lines.append(
                f"  Downloaded locally via {media.get('video_downloaded_via')}: "
                f"`{media['video_local_path']}`"
            )
        for frame in media.get("frames") or []:
            if isinstance(frame, dict) and frame.get("local_path"):
                lines.append(f"  Sampled frame: `{frame['local_path']}`")
    elif places:
        for place in places:
            label = place["name"]
            if place.get("region"):
                label = f"{label} — {place['region']}"
            lines.append(f"- [{label}]({place['maps_url']})")
        lines.append("Google Maps search links only; no Google Places API was used.")
    elif status == "scraped":
        lines.append("Scraped successfully; no automated analysis was requested.")
    elif status in {"login_required", "login_requested"}:
        login = result.get("login") or {}
        login_platform = str(login.get("platform") or platform).title()
        lines.append(f"{login_platform} needs an owner-controlled login before this post can be read.")
        if login.get("ssh_tunnel"):
            lines.append(f"Run: `{login['ssh_tunnel']}`")
        if login.get("manager_url"):
            lines.append(
                f"Then open {login['manager_url']} and use the "
                f"`{login.get('profile', 'social-media-extract')}` profile."
            )
        lines.append(
            f"Complete login or {login_platform}-owned verification in the browser, then send the post again."
        )
    else:
        lines.append("No Google Maps links were generated.")
    for diagnostic in result.get("diagnostics") or []:
        lines.append(f"Note: {diagnostic}")
    return "\n".join(lines)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Extract Instagram posts/Reels and Facebook Reels into Google Maps search links "
            "without Google Places."
        ),
    )
    parser.add_argument("urls", nargs="*", help="Instagram post/Reel or Facebook Reel URLs")
    parser.add_argument(
        "--login",
        nargs="?",
        const="instagram",
        choices=sorted(SUPPORTED_PLATFORMS),
        metavar="PLATFORM",
        help=(
            "Open a configured, owner-controlled local CloakBrowser profile for login; "
            "defaults to Instagram when PLATFORM is omitted"
        ),
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument("--scrape-only", action="store_true", help="Fetch and parse social metadata without vision analysis")
    parser.add_argument(
        "--download-media",
        action="store_true",
        help="Save images, video, and Reel frames in an owner-only local cache using a saved session when available",
    )
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="Per-request timeout in seconds (default: %(default)s)")
    parser.add_argument("--max-images", type=int, default=DEFAULT_MAX_IMAGES, help="Maximum carousel images to analyze (default: %(default)s)")
    parser.add_argument("--max-frames", type=int, default=DEFAULT_MAX_FRAMES, help="Maximum Reel frames to analyze (default: %(default)s)")
    parser.add_argument("--max-media-bytes", type=int, default=DEFAULT_MAX_MEDIA_BYTES, help="Maximum bytes downloaded for a Reel (default: %(default)s)")
    parser.add_argument("--llm-base-url", help="Optional OpenAI-compatible vision endpoint URL")
    parser.add_argument("--llm-model", help="Vision model name for the optional endpoint")
    parser.add_argument(
        "--llm-api-key-env",
        help=(
            "Environment variable containing the optional endpoint key; defaults to "
            "SOCIAL_MEDIA_EXTRACT_LLM_API_KEY"
        ),
    )
    parser.add_argument(
        "--manager-url",
        help="Optional loopback CloakBrowser Manager URL; required for --login",
    )
    parser.add_argument(
        "--browser-profile",
        help="Shared persistent CloakBrowser profile name; defaults to SOCIAL_MEDIA_EXTRACT_CLOAK_PROFILE",
    )
    args = parser.parse_args(argv)
    if args.login and args.urls:
        parser.error("--login does not accept post URLs.")
    if not args.login and not args.urls:
        parser.error("Provide a supported social post/Reel URL, or use --login [PLATFORM].")
    if args.timeout < 1 or args.timeout > 300:
        parser.error("--timeout must be between 1 and 300 seconds.")
    if not 1 <= args.max_images <= 12:
        parser.error("--max-images must be between 1 and 12.")
    if not 1 <= args.max_frames <= 12:
        parser.error("--max-frames must be between 1 and 12.")
    if args.max_media_bytes < 1024 * 1024:
        parser.error("--max-media-bytes must be at least 1 MiB.")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.login:
        try:
            browser_config = resolve_browser_manager_config(args, require=True)
            assert browser_config is not None
            login_result = request_browser_login(
                browser_config,
                timeout_seconds=args.timeout,
                platform=args.login,
            )
        except InstagramToMapsError as error:
            login_result = {
                "schema_version": 2,
                "status": "login_unavailable",
                "platform": args.login,
                "places": [],
                "diagnostics": [str(error)],
            }
        if args.json:
            print(json.dumps(login_result, ensure_ascii=False, indent=2))
        else:
            print(render_result(login_result))
        return 0

    results: list[dict[str, Any]] = []
    for value in args.urls:
        try:
            target = normalize_social_url(value)
        except InstagramToMapsError as error:
            results.append(
                {
                    "schema_version": 2,
                    "source_url": value,
                    "status": "invalid_url",
                    "platform": None,
                    "post": None,
                    "instagram": None,
                    "media": None,
                    "places": [],
                    "diagnostics": [str(error)],
                }
            )
            continue
        results.append(result_for_target(target, args))

    if args.json:
        output: Any = results[0] if len(results) == 1 else results
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print("\n\n".join(render_result(result) for result in results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

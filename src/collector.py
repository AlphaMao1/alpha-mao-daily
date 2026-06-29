from __future__ import annotations

import base64
import http.cookiejar
import html
import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup
from dateutil import parser as date_parser

try:
    from readability import Document
except Exception:  # pragma: no cover - readability is a best-effort extractor.
    Document = None


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "sources.yaml"
DATA_DIR = ROOT / "data"

UTC = timezone.utc
DEFAULT_TZ = "Asia/Shanghai"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 AlphaMaoDaily/1.0"
)


class SilentYtdlpLogger:
    def debug(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        pass

    def error(self, msg: str) -> None:
        pass


NOISE_TERMS = {
    "game",
    "gaming",
    "anime",
    "face swap",
    "faceswap",
    "deepfake",
    "job search",
    "jobs",
    "crypto",
    "casino",
    "nft",
    "meme",
    "wallpaper",
    "porn",
}


class Collector:
    def __init__(self) -> None:
        self.config = self.load_config()
        self.timezone_name = self.config.get("timezone") or DEFAULT_TZ
        self.local_tz = self.get_zoneinfo(self.timezone_name)
        self.now_utc = datetime.now(UTC)
        self.now_local = self.now_utc.astimezone(self.local_tz)
        self.date = self.now_local.strftime("%Y-%m-%d")
        self.limits = self.config.get("limits", {})
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})
        self.youtube_cookie_file: str | None = None
        self.youtube_proxy_url = normalize_secret(os.getenv("YOUTUBE_PROXY_URL"))
        self.youtube_ytdlp_proxy_url = normalize_ytdlp_proxy_url(self.youtube_proxy_url)
        self.youtube_proxy_dict = self.build_youtube_proxy_dict(self.youtube_proxy_url)

    @staticmethod
    def load_config() -> dict[str, Any]:
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    @staticmethod
    def get_zoneinfo(name: str):
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(name)
        except Exception:
            return timezone(timedelta(hours=8))

    def run(self) -> dict[str, Any]:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        dated_dir = DATA_DIR / self.date
        dated_dir.mkdir(parents=True, exist_ok=True)

        self.youtube_cookie_file = self.prepare_youtube_cookie_file()
        try:
            raw = {
                "date": self.date,
                "generated_at": isoformat(self.now_utc),
                "timezone": self.timezone_name,
                "windows": {
                    "rss": "72h, expanding to 7d if candidate volume is low",
                    "youtube": "72h per channel, expanding to 7d if no recent candidate",
                    "github": "7d",
                    "aihot": "selected latest",
                },
                "rss": self.collect_rss(),
                "youtube": self.collect_youtube(),
                "github": self.collect_github(),
                "aihot": self.collect_aihot(),
            }
        finally:
            self.cleanup_youtube_cookie_file()

        brief = self.build_brief(raw)

        raw_path = dated_dir / "alpha_mao_daily_raw.json"
        brief_path = dated_dir / "alpha_mao_daily_brief.json"
        latest_path = DATA_DIR / "latest.json"
        pointer_path = DATA_DIR / "latest_raw_pointer.json"

        write_json(raw_path, raw)
        write_json(brief_path, brief)
        write_json(latest_path, brief)
        write_json(
            pointer_path,
            {
                "date": self.date,
                "generated_at": raw["generated_at"],
                "timezone": self.timezone_name,
                "raw_json_path": str(raw_path.relative_to(ROOT)).replace("\\", "/"),
                "brief_json_path": str(brief_path.relative_to(ROOT)).replace("\\", "/"),
                "latest_json_path": str(latest_path.relative_to(ROOT)).replace("\\", "/"),
            },
        )

        print(f"Wrote {latest_path.relative_to(ROOT)}")
        print(f"Wrote {pointer_path.relative_to(ROOT)}")
        return brief

    def prepare_youtube_cookie_file(self) -> str | None:
        cookie_b64 = os.getenv("YOUTUBE_COOKIES_B64")
        cookie_file = os.getenv("YOUTUBE_COOKIES_FILE")
        if cookie_file and Path(cookie_file).exists():
            return cookie_file
        if not cookie_b64:
            return None
        try:
            decoded = base64.b64decode(cookie_b64, validate=True)
            temp = tempfile.NamedTemporaryFile("wb", delete=False, suffix=".cookies.txt")
            temp.write(decoded)
            temp.close()
            return temp.name
        except Exception:
            return None

    def cleanup_youtube_cookie_file(self) -> None:
        if not self.youtube_cookie_file:
            return
        if os.getenv("YOUTUBE_COOKIES_FILE") == self.youtube_cookie_file:
            return
        try:
            Path(self.youtube_cookie_file).unlink(missing_ok=True)
        except Exception:
            pass

    @staticmethod
    def build_youtube_proxy_dict(proxy_url: str | None) -> dict[str, str] | None:
        if not proxy_url:
            return None
        return {"http": proxy_url, "https": proxy_url}

    def collect_rss(self) -> dict[str, Any]:
        feeds = self.config.get("rss", {}).get("feeds", [])
        recent_cutoff = self.now_utc - timedelta(hours=72)
        expanded_cutoff = self.now_utc - timedelta(days=7)
        min_candidates = int(self.limits.get("rss_min_candidates", 6))
        per_feed = int(self.limits.get("rss_max_items_per_feed", 4))

        recent_entries: list[dict[str, Any]] = []
        expanded_entries: list[dict[str, Any]] = []
        source_failures: list[dict[str, str]] = []

        for feed in feeds:
            feed_name = feed.get("name") or feed.get("url") or "unknown"
            feed_url = feed.get("url")
            if not feed_url:
                continue
            try:
                parsed = feedparser.parse(feed_url)
                if parsed.bozo and not parsed.entries:
                    source_failures.append(
                        failure("rss", feed_name, feed_url, f"feed_parse_failed: {parsed.bozo_exception}")
                    )
                    continue
                entries = []
                for entry in parsed.entries[: per_feed * 3]:
                    item = self.normalize_feed_entry(feed_name, entry)
                    if not item.get("url"):
                        continue
                    published = parse_datetime(item.get("published_at"))
                    if not published:
                        entries.append(item)
                        continue
                    if published >= recent_cutoff:
                        recent_entries.append(item)
                    if published >= expanded_cutoff:
                        expanded_entries.append(item)
                if not entries and not parsed.entries:
                    source_failures.append(failure("rss", feed_name, feed_url, "feed_empty"))
            except Exception as exc:
                source_failures.append(failure("rss", feed_name, feed_url, f"feed_fetch_failed: {exc}"))

        selected = recent_entries
        window = "72h"
        if len(unique_by_url(selected)) < min_candidates:
            selected = expanded_entries or recent_entries
            window = "7d" if expanded_entries else "72h"

        selected = unique_by_url(selected)
        selected.sort(key=lambda x: parse_datetime(x.get("published_at")) or datetime.min.replace(tzinfo=UTC), reverse=True)

        max_total = max(min_candidates, len(feeds) * per_feed)
        items: list[dict[str, Any]] = []
        for item in selected[:max_total]:
            item["lookback_7d"] = window == "7d"
            result = self.fetch_text(item["url"], int(self.limits.get("full_text_max_chars", 60000)))
            if result["ok"]:
                item["full_text_status"] = "full_text_success"
                item["full_text"] = result["text"]
                item["failure_reason"] = ""
                item["text_truncated"] = result["truncated"]
            else:
                item["full_text_status"] = "full_text_failed"
                item["full_text"] = ""
                item["failure_reason"] = result["reason"]
                item["text_truncated"] = False
            items.append(item)

        return {
            "window": window,
            "source_failures": source_failures,
            "items": items,
        }

    def normalize_feed_entry(self, feed_name: str, entry: Any) -> dict[str, Any]:
        published_at = None
        if getattr(entry, "published_parsed", None):
            published_at = isoformat(datetime(*entry.published_parsed[:6], tzinfo=UTC))
        elif getattr(entry, "updated_parsed", None):
            published_at = isoformat(datetime(*entry.updated_parsed[:6], tzinfo=UTC))
        else:
            published_at = parse_datetime_to_iso(entry.get("published") or entry.get("updated"))

        summary = entry.get("summary") or entry.get("description") or ""
        return {
            "source": feed_name,
            "title": clean_text(entry.get("title") or ""),
            "url": entry.get("link") or "",
            "published_at": published_at or "",
            "summary": clean_text(strip_html(summary)),
            "full_text_status": "full_text_failed",
            "full_text": "",
            "failure_reason": "",
        }

    def collect_youtube(self) -> dict[str, Any]:
        channels = self.config.get("youtube", {}).get("channels", [])
        per_channel = int(self.limits.get("youtube_videos_per_channel", 5))
        recent_cutoff = self.now_utc - timedelta(hours=72)
        expanded_cutoff = self.now_utc - timedelta(days=7)
        items: list[dict[str, Any]] = []
        channel_failures: list[dict[str, str]] = []

        for channel in channels:
            try:
                videos = self.list_channel_videos(channel, per_channel)
            except Exception as exc:
                channel_failures.append(
                    failure("youtube", channel.get("name", ""), channel.get("url", ""), f"channel_list_failed: {exc}")
                )
                continue

            recent = [v for v in videos if is_in_window(v.get("published_at"), recent_cutoff, include_unknown=True)]
            expanded = [v for v in videos if is_in_window(v.get("published_at"), expanded_cutoff, include_unknown=True)]
            selected = recent if recent else expanded
            lookback_7d = bool(selected and not recent)

            for video in selected[:per_channel]:
                video["lookback_7d"] = lookback_7d
                transcript = self.fetch_transcript(video["video_id"], video["url"])
                if transcript["ok"]:
                    video["transcript_status"] = "transcript_success"
                    video["transcript_text"] = limit_text(
                        transcript["text"], int(self.limits.get("transcript_max_chars", 180000))
                    )[0]
                    video["transcript_source"] = transcript["source"]
                    video["failure_reason"] = ""
                    video["text_truncated"] = len(transcript["text"]) > len(video["transcript_text"])
                else:
                    video["transcript_status"] = "transcript_failed"
                    video["transcript_text"] = ""
                    video["transcript_source"] = ""
                    video["failure_reason"] = transcript["reason"]
                    video["text_truncated"] = False
                items.append(video)

        return {
            "window": "72h_or_7d",
            "channel_failures": channel_failures,
            "items": items,
        }

    def list_channel_videos(self, channel: dict[str, str], limit: int) -> list[dict[str, Any]]:
        import yt_dlp

        url = (channel.get("url") or "").rstrip("/") + "/videos"
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "logger": SilentYtdlpLogger(),
            "skip_download": True,
            "extract_flat": "in_playlist",
            "playlistend": max(limit, 5),
        }
        if self.youtube_cookie_file:
            opts["cookiefile"] = self.youtube_cookie_file
        if self.youtube_ytdlp_proxy_url:
            opts["proxy"] = self.youtube_ytdlp_proxy_url

        info = self.extract_with_ytdlp(yt_dlp, opts, url)

        videos = []
        for entry in (info or {}).get("entries") or []:
            video_id = entry.get("id") or parse_youtube_id(entry.get("url") or "")
            if not video_id:
                continue
            published = parse_yt_date(entry.get("upload_date")) or parse_timestamp(entry.get("timestamp"))
            videos.append(
                {
                    "channel": channel.get("name", ""),
                    "channel_handle": channel.get("handle", ""),
                    "title": clean_text(entry.get("title") or ""),
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                    "video_id": video_id,
                    "published_at": published or "",
                    "transcript_status": "transcript_failed",
                    "transcript_text": "",
                    "transcript_source": "",
                    "failure_reason": "",
                }
            )
        return videos

    def fetch_transcript(self, video_id: str, video_url: str) -> dict[str, Any]:
        errors: list[str] = []
        try:
            result = self.fetch_transcript_with_api(video_id)
            if result["ok"]:
                return result
            errors.append(result["reason"])
        except Exception as exc:
            errors.append(f"youtube_transcript_api_error: {exc}")

        try:
            result = self.fetch_transcript_with_ytdlp(video_url)
            if result["ok"]:
                return result
            errors.append(result["reason"])
        except Exception as exc:
            errors.append(f"yt_dlp_subtitle_error: {exc}")

        return {"ok": False, "text": "", "source": "", "reason": "; ".join(errors) or "transcript_unavailable"}

    def fetch_transcript_with_api(self, video_id: str) -> dict[str, Any]:
        from youtube_transcript_api import YouTubeTranscriptApi
        from youtube_transcript_api.proxies import GenericProxyConfig

        languages = ["en", "en-US", "en-GB", "zh-Hans", "zh-Hant"]
        try:
            proxy_config = (
                GenericProxyConfig(http_url=self.youtube_proxy_url, https_url=self.youtube_proxy_url)
                if self.youtube_proxy_url
                else None
            )
            api = YouTubeTranscriptApi(
                proxy_config=proxy_config,
                http_client=self.build_youtube_transcript_http_client(),
            )
            fetched = api.fetch(video_id, languages=languages)
            snippets = fetched.to_raw_data() if hasattr(fetched, "to_raw_data") else list(fetched)
        except TypeError:
            snippets = YouTubeTranscriptApi.get_transcript(video_id, languages=languages)
        except AttributeError:
            snippets = YouTubeTranscriptApi.get_transcript(video_id, languages=languages)

        text = normalize_transcript(snippets)
        if len(text) < 200:
            return {"ok": False, "text": "", "source": "", "reason": "transcript_too_short"}
        return {"ok": True, "text": text, "source": "youtube_transcript_api", "reason": ""}

    def fetch_transcript_with_ytdlp(self, video_url: str) -> dict[str, Any]:
        import yt_dlp

        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "logger": SilentYtdlpLogger(),
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": ["en.*", "en", "zh.*", "zh-Hans", "zh-Hant"],
        }
        if self.youtube_cookie_file:
            opts["cookiefile"] = self.youtube_cookie_file
        if self.youtube_ytdlp_proxy_url:
            opts["proxy"] = self.youtube_ytdlp_proxy_url

        info = self.extract_with_ytdlp(yt_dlp, opts, video_url)

        subtitle = choose_subtitle(info.get("subtitles") or {}) or choose_subtitle(info.get("automatic_captions") or {})
        if not subtitle:
            return {"ok": False, "text": "", "source": "", "reason": "yt_dlp_no_subtitle_track"}

        resp = self.session.get(subtitle["url"], timeout=30, proxies=self.youtube_proxy_dict)
        if resp.status_code >= 400:
            return {"ok": False, "text": "", "source": "", "reason": f"subtitle_fetch_http_{resp.status_code}"}
        text = parse_subtitle_payload(resp.text, subtitle.get("ext", ""))
        if len(text) < 200:
            return {"ok": False, "text": "", "source": "", "reason": "subtitle_text_too_short"}
        return {"ok": True, "text": text, "source": f"yt_dlp_{subtitle.get('lang', 'subtitle')}", "reason": ""}

    def extract_with_ytdlp(self, yt_dlp: Any, opts: dict[str, Any], url: str) -> dict[str, Any]:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)
        except Exception as exc:
            if not opts.get("proxy") or not is_unsupported_proxy_error(exc):
                raise
            direct_opts = dict(opts)
            direct_opts.pop("proxy", None)
            with yt_dlp.YoutubeDL(direct_opts) as ydl:
                return ydl.extract_info(url, download=False)

    def build_youtube_transcript_http_client(self) -> requests.Session:
        client = requests.Session()
        client.headers.update({"User-Agent": USER_AGENT})
        if not self.youtube_cookie_file:
            return client
        try:
            jar = http.cookiejar.MozillaCookieJar(self.youtube_cookie_file)
            jar.load(ignore_discard=True, ignore_expires=True)
            client.cookies = jar
        except Exception:
            pass
        return client

    def collect_github(self) -> dict[str, Any]:
        since = (self.now_utc - timedelta(days=7)).strftime("%Y-%m-%d")
        per_query = int(self.limits.get("github_per_query", 12))
        max_repos = int(self.limits.get("github_max_repos", 30))
        queries = self.config.get("github", {}).get("queries", [])
        supplement_urls = self.config.get("github", {}).get("supplements", {})

        candidates: dict[str, dict[str, Any]] = {}
        source_failures: list[dict[str, str]] = []

        for query_template in queries:
            query = query_template.format(since=since)
            try:
                for repo in self.github_search(query, per_query):
                    candidates.setdefault(repo["full_name"].lower(), repo)
            except Exception as exc:
                source_failures.append(failure("github", "search", query, f"github_search_failed: {exc}"))

        for name, url in supplement_urls.items():
            try:
                for full_name in self.scrape_repo_names(url):
                    if full_name.lower() in candidates:
                        continue
                    repo = self.github_repo_metadata(full_name, source=name)
                    if repo:
                        candidates.setdefault(repo["full_name"].lower(), repo)
            except Exception as exc:
                source_failures.append(failure("github", name, url, f"supplement_failed: {exc}"))

        items: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for repo in sorted(candidates.values(), key=lambda x: x.get("stars") or 0, reverse=True):
            if len(items) >= max_repos:
                break
            if not (20 <= int(repo.get("stars") or 0) <= 5000):
                repo["readme_status"] = "readme_failed"
                repo["readme_text"] = ""
                repo["failure_reason"] = "filtered_by_star_range"
                skipped.append(repo)
                continue
            if is_noise_repo(repo):
                repo["readme_status"] = "readme_failed"
                repo["readme_text"] = ""
                repo["failure_reason"] = "filtered_noise"
                skipped.append(repo)
                continue
            readme = self.fetch_readme(repo["full_name"])
            if readme["ok"]:
                repo["readme_status"] = "readme_success"
                repo["readme_text"] = readme["text"]
                repo["failure_reason"] = ""
                repo["text_truncated"] = readme["truncated"]
            else:
                repo["readme_status"] = "readme_failed"
                repo["readme_text"] = ""
                repo["failure_reason"] = readme["reason"]
                repo["text_truncated"] = False
            items.append(repo)

        return {
            "window": "7d",
            "since": since,
            "source_failures": source_failures,
            "skipped": skipped,
            "items": items,
        }

    def github_headers(self, raw: bool = False) -> dict[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
            "Accept": "application/vnd.github.raw" if raw else "application/vnd.github+json",
        }
        token = os.getenv("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def github_search(self, query: str, per_page: int) -> list[dict[str, Any]]:
        resp = self.session.get(
            "https://api.github.com/search/repositories",
            params={"q": query, "sort": "updated", "order": "desc", "per_page": per_page},
            headers=self.github_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        repos = []
        for item in resp.json().get("items", []):
            repos.append(normalize_github_repo(item, source="search"))
        return repos

    def github_repo_metadata(self, full_name: str, source: str) -> dict[str, Any] | None:
        resp = self.session.get(
            f"https://api.github.com/repos/{full_name}",
            headers=self.github_headers(),
            timeout=30,
        )
        if resp.status_code >= 400:
            return None
        return normalize_github_repo(resp.json(), source=source)

    def scrape_repo_names(self, url: str) -> list[str]:
        resp = self.session.get(url, timeout=30)
        resp.raise_for_status()
        names = set(re.findall(r"href=[\"']/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)[\"']", resp.text))
        names.update(re.findall(r"github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", resp.text))
        return [name for name in names if not name.lower().startswith(("topics/", "trending"))][:15]

    def fetch_readme(self, full_name: str) -> dict[str, Any]:
        max_chars = int(self.limits.get("readme_max_chars", 90000))
        resp = self.session.get(
            f"https://api.github.com/repos/{full_name}/readme",
            headers=self.github_headers(raw=True),
            timeout=30,
        )
        if resp.status_code < 400 and resp.text:
            text, truncated = limit_text(clean_text(resp.text), max_chars)
            if len(text) >= 200:
                return {"ok": True, "text": text, "truncated": truncated, "reason": ""}
        reason = f"readme_api_http_{resp.status_code}"
        for branch in ("main", "master"):
            raw_url = f"https://raw.githubusercontent.com/{full_name}/{branch}/README.md"
            r = self.session.get(raw_url, timeout=30)
            if r.status_code < 400 and r.text:
                text, truncated = limit_text(clean_text(r.text), max_chars)
                if len(text) >= 200:
                    return {"ok": True, "text": text, "truncated": truncated, "reason": ""}
                reason = "readme_too_short"
        return {"ok": False, "text": "", "truncated": False, "reason": reason}

    def collect_aihot(self) -> dict[str, Any]:
        endpoint = self.config.get("aihot", {}).get("endpoint")
        verify_limit = int(self.limits.get("aihot_verify_limit", 25))
        items: list[dict[str, Any]] = []
        source_failures: list[dict[str, str]] = []

        if not endpoint:
            return {"window": "selected latest", "api_returned": 0, "source_failures": [], "items": []}

        try:
            resp = self.session.get(endpoint, timeout=30)
            resp.raise_for_status()
            payload = resp.json()
            raw_items = extract_aihot_items(payload)
        except Exception as exc:
            return {
                "window": "selected latest",
                "api_returned": 0,
                "source_failures": [failure("aihot", "api", endpoint, f"api_fetch_failed: {exc}")],
                "items": [],
            }

        for raw in raw_items[:verify_limit]:
            item = normalize_aihot_item(raw, endpoint)
            original_url = item.get("original_url") or ""
            if not original_url:
                item["verification_status"] = "only_aggregated_summary"
                item["original_text"] = ""
                item["failure_reason"] = "missing_original_url"
            elif is_social_url(original_url):
                item["verification_status"] = "second_hand"
                item["original_text"] = ""
                item["failure_reason"] = "social_url_requires_manual_verification"
            else:
                result = self.fetch_text(original_url, int(self.limits.get("original_text_max_chars", 50000)))
                if result["ok"]:
                    item["verification_status"] = "original_verified"
                    item["original_text"] = result["text"]
                    item["failure_reason"] = ""
                    item["text_truncated"] = result["truncated"]
                else:
                    item["verification_status"] = "original_failed"
                    item["original_text"] = ""
                    item["failure_reason"] = result["reason"]
                    item["text_truncated"] = False
            items.append(item)

        for raw in raw_items[verify_limit:]:
            item = normalize_aihot_item(raw, endpoint)
            item["verification_status"] = "pending"
            item["original_text"] = ""
            item["failure_reason"] = "not_verified_due_to_limit"
            items.append(item)

        return {
            "window": "selected latest",
            "api_returned": len(raw_items),
            "source_failures": source_failures,
            "items": items,
        }

    def fetch_text(self, url: str, max_chars: int) -> dict[str, Any]:
        if not url:
            return {"ok": False, "text": "", "truncated": False, "reason": "missing_url"}
        try:
            resp = self.session.get(url, timeout=30, allow_redirects=True)
        except Exception as exc:
            return {"ok": False, "text": "", "truncated": False, "reason": f"fetch_failed: {exc}"}
        if resp.status_code >= 400:
            return {"ok": False, "text": "", "truncated": False, "reason": f"http_{resp.status_code}"}
        content_type = resp.headers.get("content-type", "").lower()
        if "application/pdf" in content_type:
            return {"ok": False, "text": "", "truncated": False, "reason": "pdf_not_supported"}
        text = extract_text_from_html(resp.text)
        if len(text) < 300:
            return {"ok": False, "text": "", "truncated": False, "reason": "extracted_text_too_short"}
        limited, truncated = limit_text(text, max_chars)
        return {"ok": True, "text": limited, "truncated": truncated, "reason": ""}

    def build_brief(self, raw: dict[str, Any]) -> dict[str, Any]:
        excerpt_chars = int(self.limits.get("brief_excerpt_chars", 2400))
        eligible = {
            "rss": [
                brief_item(item, ["source", "title", "url", "published_at", "summary", "lookback_7d"], "full_text", excerpt_chars)
                for item in raw["rss"]["items"]
                if item.get("full_text_status") == "full_text_success"
            ],
            "youtube": [
                brief_item(
                    item,
                    ["channel", "channel_handle", "title", "url", "video_id", "published_at", "transcript_source", "lookback_7d"],
                    "transcript_text",
                    excerpt_chars,
                )
                for item in raw["youtube"]["items"]
                if item.get("transcript_status") == "transcript_success"
            ],
            "github": [
                brief_item(
                    item,
                    ["name", "full_name", "url", "description", "stars", "pushed_at", "topics", "source"],
                    "readme_text",
                    excerpt_chars,
                )
                for item in raw["github"]["items"]
                if item.get("readme_status") == "readme_success"
            ],
            "aihot": [
                brief_item(
                    item,
                    ["title", "aihot_url", "original_url", "source", "category", "published_at", "summary", "score"],
                    "original_text",
                    excerpt_chars,
                )
                for item in raw["aihot"]["items"]
                if item.get("verification_status") == "original_verified"
            ],
        }
        failures = {
            "rss": raw["rss"].get("source_failures", [])
            + [
                failure("rss", item.get("title", ""), item.get("url", ""), item.get("failure_reason", "full_text_failed"))
                for item in raw["rss"]["items"]
                if item.get("full_text_status") != "full_text_success"
            ],
            "youtube": raw["youtube"].get("channel_failures", [])
            + [
                failure("youtube", item.get("title", ""), item.get("url", ""), item.get("failure_reason", "transcript_failed"))
                for item in raw["youtube"]["items"]
                if item.get("transcript_status") != "transcript_success"
            ],
            "github": raw["github"].get("source_failures", [])
            + [
                failure("github", item.get("full_name", ""), item.get("url", ""), item.get("failure_reason", "readme_failed"))
                for item in raw["github"].get("skipped", []) + raw["github"]["items"]
                if item.get("readme_status") != "readme_success"
            ],
            "aihot": raw["aihot"].get("source_failures", [])
            + [
                failure("aihot", item.get("title", ""), item.get("original_url") or item.get("aihot_url", ""), item.get("failure_reason", item.get("verification_status", "")))
                for item in raw["aihot"]["items"]
                if item.get("verification_status") != "original_verified"
            ],
        }
        counts = {
            "rss": count_status(raw["rss"]["items"], "full_text_status", "full_text_success", len(failures["rss"])),
            "youtube": count_status(raw["youtube"]["items"], "transcript_status", "transcript_success", len(failures["youtube"])),
            "github": count_status(raw["github"]["items"], "readme_status", "readme_success", len(failures["github"])),
            "aihot": {
                "api_returned": raw["aihot"].get("api_returned", 0),
                "candidates": len(raw["aihot"]["items"]),
                "original_verified": len(eligible["aihot"]),
                "eligible": len(eligible["aihot"]),
                "failed_or_pending": len(failures["aihot"]),
            },
        }
        return {
            "date": raw["date"],
            "generated_at": raw["generated_at"],
            "timezone": raw["timezone"],
            "windows": {
                "rss": raw["rss"].get("window", ""),
                "youtube": raw["youtube"].get("window", ""),
                "github": raw["github"].get("window", ""),
                "aihot": raw["aihot"].get("window", ""),
            },
            "counts": counts,
            "eligible_items": eligible,
            "failures": failures,
        }


def isoformat(dt: datetime) -> str:
    return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = date_parser.parse(str(value))
        except Exception:
            return None
    if not dt.tzinfo:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def parse_datetime_to_iso(value: Any) -> str | None:
    dt = parse_datetime(value)
    return isoformat(dt) if dt else None


def parse_yt_date(value: Any) -> str | None:
    if not value:
        return None
    try:
        dt = datetime.strptime(str(value), "%Y%m%d").replace(tzinfo=UTC)
        return isoformat(dt)
    except Exception:
        return parse_datetime_to_iso(value)


def parse_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return isoformat(datetime.fromtimestamp(int(value), tz=UTC))
    except Exception:
        return None


def is_in_window(value: Any, cutoff: datetime, include_unknown: bool = False) -> bool:
    dt = parse_datetime(value)
    if not dt:
        return include_unknown
    return dt >= cutoff


def strip_html(value: str) -> str:
    if not value:
        return ""
    soup = BeautifulSoup(value, "lxml")
    return soup.get_text(" ")


def clean_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_secret(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.lstrip("\ufeff").strip()
    return value or None


def normalize_ytdlp_proxy_url(value: str | None) -> str | None:
    if not value:
        return None
    if value.lower().startswith("socks5h://"):
        return "socks5://" + value[len("socks5h://") :]
    return value


def is_unsupported_proxy_error(exc: Exception) -> bool:
    return "Unsupported proxy type" in str(exc)


def extract_text_from_html(markup: str) -> str:
    html_text = markup or ""
    if Document is not None:
        try:
            html_text = Document(markup).summary(html_partial=True)
        except Exception:
            html_text = markup
    soup = BeautifulSoup(html_text, "lxml")
    for tag in soup(["script", "style", "noscript", "svg", "form", "nav", "footer", "header"]):
        tag.decompose()
    return clean_text(soup.get_text(" "))


def limit_text(text: str, max_chars: int) -> tuple[str, bool]:
    text = clean_text(text)
    if max_chars <= 0 or len(text) <= max_chars:
        return text, False
    return text[:max_chars].rstrip() + "\n\n[TRUNCATED]", True


def unique_by_url(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    result = []
    for item in items:
        url = item.get("url")
        key = normalize_url(url)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def normalize_url(url: str | None) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    return parsed._replace(fragment="").geturl().rstrip("/")


def failure(kind: str, title: str, url: str, reason: str) -> dict[str, str]:
    return {
        "type": kind,
        "title": clean_text(title),
        "url": url or "",
        "failure_reason": clean_text(reason),
    }


def normalize_transcript(snippets: Any) -> str:
    texts = []
    for snippet in snippets or []:
        if isinstance(snippet, dict):
            value = snippet.get("text")
        else:
            value = getattr(snippet, "text", "")
        if value:
            texts.append(str(value).replace("\n", " "))
    return clean_text(" ".join(texts))


def choose_subtitle(subtitles: dict[str, Any]) -> dict[str, Any] | None:
    language_order = ["en", "en-US", "en-GB", "zh-Hans", "zh-Hant", "zh"]
    for lang in language_order:
        tracks = subtitles.get(lang)
        if not tracks:
            continue
        for ext in ("json3", "vtt", "srv3", "ttml"):
            for track in tracks:
                if track.get("ext") == ext and track.get("url"):
                    chosen = dict(track)
                    chosen["lang"] = lang
                    return chosen
    for lang, tracks in subtitles.items():
        for track in tracks or []:
            if track.get("url"):
                chosen = dict(track)
                chosen["lang"] = lang
                return chosen
    return None


def parse_subtitle_payload(payload: str, ext: str) -> str:
    if ext == "json3":
        try:
            data = json.loads(payload)
            pieces = []
            for event in data.get("events", []):
                for seg in event.get("segs", []) or []:
                    pieces.append(seg.get("utf8", ""))
            return clean_text(" ".join(pieces))
        except Exception:
            pass
    lines = []
    for line in payload.splitlines():
        line = line.strip()
        if not line or line.startswith(("WEBVTT", "Kind:", "Language:")):
            continue
        if "-->" in line or re.match(r"^\d+$", line):
            continue
        line = re.sub(r"<[^>]+>", "", line)
        lines.append(line)
    return clean_text(" ".join(lines))


def parse_youtube_id(value: str) -> str | None:
    patterns = [r"watch\?v=([A-Za-z0-9_-]{11})", r"youtu\.be/([A-Za-z0-9_-]{11})", r"^([A-Za-z0-9_-]{11})$"]
    for pattern in patterns:
        match = re.search(pattern, value or "")
        if match:
            return match.group(1)
    return None


def normalize_github_repo(item: dict[str, Any], source: str) -> dict[str, Any]:
    return {
        "name": item.get("name") or "",
        "full_name": item.get("full_name") or "",
        "url": item.get("html_url") or "",
        "description": clean_text(item.get("description") or ""),
        "stars": int(item.get("stargazers_count") or 0),
        "pushed_at": item.get("pushed_at") or "",
        "topics": item.get("topics") or [],
        "source": source,
        "readme_status": "readme_failed",
        "readme_text": "",
        "failure_reason": "",
    }


def is_noise_repo(repo: dict[str, Any]) -> bool:
    text = " ".join(
        [
            repo.get("name", ""),
            repo.get("full_name", ""),
            repo.get("description", ""),
            " ".join(repo.get("topics") or []),
        ]
    ).lower()
    return any(term in text for term in NOISE_TERMS)


def extract_aihot_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("items", "data", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
        if isinstance(value, dict):
            nested = extract_aihot_items(value)
            if nested:
                return nested
    return []


def normalize_aihot_item(raw: dict[str, Any], endpoint: str) -> dict[str, Any]:
    original_url = first_present(raw, ["source_url", "sourceUrl", "original_url", "originalUrl", "url", "link"])
    aihot_url = first_present(raw, ["aihot_url", "aihotUrl", "item_url", "itemUrl", "share_url", "shareUrl"]) or endpoint
    return {
        "title": clean_text(first_present(raw, ["title", "name"]) or ""),
        "aihot_url": aihot_url,
        "original_url": original_url or "",
        "source": clean_text(first_present(raw, ["source", "site", "provider"]) or ""),
        "category": clean_text(first_present(raw, ["category", "categoryName", "type"]) or ""),
        "published_at": parse_datetime_to_iso(first_present(raw, ["publishedAt", "published_at", "createdAt", "created_at", "date"])) or "",
        "summary": clean_text(first_present(raw, ["summary", "description", "content", "abstract"]) or ""),
        "score": raw.get("score") if raw.get("score") is not None else raw.get("rank_score"),
        "verification_status": "pending",
        "original_text": "",
        "failure_reason": "",
    }


def first_present(data: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return None


def is_social_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(domain in host for domain in ("twitter.com", "x.com", "t.co", "threads.net", "bsky.app"))


def brief_item(item: dict[str, Any], fields: list[str], text_field: str, excerpt_chars: int) -> dict[str, Any]:
    result = {field: item.get(field) for field in fields if field in item}
    text = item.get(text_field) or ""
    result["text_excerpt"] = limit_text(text, excerpt_chars)[0]
    result["text_source_field"] = text_field
    result["text_truncated_in_raw"] = bool(item.get("text_truncated"))
    return result


def count_status(items: list[dict[str, Any]], field: str, success_value: str, failures_count: int) -> dict[str, int]:
    success = sum(1 for item in items if item.get(field) == success_value)
    return {
        "candidates": len(items),
        success_value: success,
        "eligible": success,
        "failed": failures_count,
    }


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def main() -> None:
    Collector().run()


if __name__ == "__main__":
    main()

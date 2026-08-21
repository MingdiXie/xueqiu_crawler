#!/usr/bin/env python3
"""抓取雪球用户公开发言并保存为 JSON。"""

from __future__ import annotations

import argparse
import html
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright


BASE_URL = "https://xueqiu.com"
TIMELINE_API = f"{BASE_URL}/v4/statuses/user_timeline.json"
STATUS_API = f"{BASE_URL}/statuses/show.json"
IMAGE_CDN = "https://xqimg.imedao.com"
FETCH_JSON_JS = """
async ({url, timeoutMs}) => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
        const response = await fetch(url, {
            credentials: "include",
            signal: controller.signal,
        });
        const text = await response.text();
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}: ${text.slice(0, 200)}`);
        }
        try {
            return JSON.parse(text);
        } catch {
            throw new Error(`接口未返回 JSON: ${text.slice(0, 200)}`);
        }
    } catch (error) {
        if (error?.name === "AbortError") {
            throw new Error(`请求超时（${timeoutMs / 1000} 秒）`);
        }
        throw error;
    } finally {
        clearTimeout(timer);
    }
}
"""
DEFAULT_USER_ID = "2206399908"
TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")
IMG_SRC_RE = re.compile(r"""<img[^>]+src=["']([^"']+)["']""", re.IGNORECASE)


def plain_text(value: str | None) -> str:
    """把接口中的简单 HTML 文本转换成易读纯文本。"""
    if not value:
        return ""
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    value = TAG_RE.sub("", value)
    value = html.unescape(value)
    return SPACE_RE.sub(" ", value).strip()


def absolutize_image_url(value: str | None) -> str | None:
    if not value:
        return None
    url = html.unescape(value.strip())
    if not url or url.startswith("data:"):
        return None
    if url.startswith("//"):
        return f"https:{url}"
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if url.startswith("/"):
        return f"{IMAGE_CDN}{url}"
    return f"{IMAGE_CDN}/{url.lstrip('/')}"


def extract_image_urls(status: dict[str, Any]) -> list[str]:
    """提取并去重图片地址，统一返回未缩放的原图 URL。"""
    urls_by_source: dict[str, str] = {}

    def add(raw: Any) -> None:
        if not isinstance(raw, str):
            return
        absolute = absolutize_image_url(raw)
        if not absolute:
            return
        source = absolute.split("?", 1)[0].split("!", 1)[0]
        urls_by_source.setdefault(source, source)

    for field in ("text", "description"):
        html_text = status.get(field) or ""
        if isinstance(html_text, str):
            for match in IMG_SRC_RE.finditer(html_text):
                add(match.group(1))

    for info in status.get("image_info_list") or []:
        if isinstance(info, dict):
            for key in ("url", "original_url", "original", "filename"):
                if info.get(key):
                    add(info[key])
                    break
        elif isinstance(info, str):
            add(info)

    pic = status.get("pic")
    if isinstance(pic, str) and pic.strip():
        for part in re.split(r"[\s,]+", pic.strip()):
            add(part)
    elif isinstance(pic, list):
        for part in pic:
            add(part)

    add(status.get("first_img"))
    return list(urls_by_source.values())


def image_source_key(url: str) -> str:
    """Ignore Xueqiu size suffixes such as !thumb.jpg / !custom.jpg."""
    return url.split("?", 1)[0].split("!", 1)[0]


def merge_image_records(
    existing: list[Any] | None,
    urls: list[str],
) -> list[dict[str, str]]:
    """Keep local paths when the same photo comes back as a different size URL."""
    by_source: dict[str, dict[str, str]] = {}
    for image in existing or []:
        if not isinstance(image, dict) or not image.get("url"):
            continue
        by_source[image_source_key(str(image["url"]))] = image
    merged: list[dict[str, str]] = []
    for url in urls:
        source = image_source_key(url)
        previous = by_source.get(source)
        if previous:
            record = dict(previous)
            record["url"] = url
            merged.append(record)
        else:
            merged.append({"url": url})
    return merged


def image_needs_download(image: Any) -> bool:
    if not isinstance(image, dict) or not image.get("url"):
        return False
    path_value = image.get("path")
    if not path_value:
        return True
    return not Path(str(path_value)).expanduser().is_file()


def post_images_need_download(post: dict[str, Any]) -> bool:
    if any(image_needs_download(image) for image in post.get("images") or []):
        return True
    quoted = post.get("retweeted_status")
    if isinstance(quoted, dict):
        return any(image_needs_download(image) for image in quoted.get("images") or [])
    return False


def guess_image_extension(url: str, content_type: str | None = None) -> str:
    path = url.split("?", 1)[0]
    # 雪球常见：xxx.png!800.jpg，优先取原始扩展名。
    bare = path.split("!")[0]
    suffix = Path(bare).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    if content_type:
        mapping = {
            "image/jpeg": ".jpg",
            "image/jpg": ".jpg",
            "image/png": ".png",
            "image/gif": ".gif",
            "image/webp": ".webp",
        }
        for mime, ext in mapping.items():
            if mime in content_type.lower():
                return ext
    return ".jpg"


def download_images(
    page_obj: Any,
    post_id: Any,
    image_urls: list[str],
    images_dir: Path,
) -> list[dict[str, str]]:
    """下载帖子图片；失败时仍保留远程 URL。"""
    saved: list[dict[str, str]] = []
    if not image_urls:
        return saved
    post_dir = images_dir / str(post_id)
    post_dir.mkdir(parents=True, exist_ok=True)
    for index, url in enumerate(image_urls, start=1):
        item: dict[str, str] = {"url": url}
        try:
            response = page_obj.request.get(
                url,
                headers={"Referer": f"{BASE_URL}/"},
                timeout=30_000,
            )
            if not response.ok:
                item["error"] = f"HTTP {response.status}"
                saved.append(item)
                continue
            content_type = response.headers.get("content-type")
            extension = guess_image_extension(url, content_type)
            filename = f"{index:02d}{extension}"
            path = post_dir / filename
            path.write_bytes(response.body())
            item["path"] = str(path)
        except Exception as exc:  # noqa: BLE001 - 单张失败不中断整页
            item["error"] = str(exc).splitlines()[0]
        saved.append(item)
    return saved


def ensure_post_images(page_obj: Any, post: dict[str, Any], images_dir: Path) -> int:
    """Download any missing local files for a post and its quoted original."""
    saved = 0
    images = post.get("images") or []
    urls = [
        str(image["url"])
        for image in images
        if isinstance(image, dict) and image.get("url")
    ]
    if urls and any(image_needs_download(image) for image in images):
        post["images"] = download_images(page_obj, post.get("id"), urls, images_dir)
        saved += sum(1 for image in post["images"] if image.get("path"))
    quoted = post.get("retweeted_status")
    if isinstance(quoted, dict):
        quote_images = quoted.get("images") or []
        quote_urls = [
            str(image["url"])
            for image in quote_images
            if isinstance(image, dict) and image.get("url")
        ]
        if quote_urls and any(image_needs_download(image) for image in quote_images):
            quoted["images"] = download_images(
                page_obj, f"{post.get('id')}_rt", quote_urls, images_dir
            )
            saved += sum(1 for image in quoted["images"] if image.get("path"))
    return saved


def iso_time(milliseconds: int | None) -> str | None:
    if not milliseconds:
        return None
    return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc).astimezone().isoformat()


def browser_cookies(cookie_header: str) -> list[dict[str, str]]:
    """把请求头格式的 Cookie 转换成 Playwright Cookie。"""
    parsed = SimpleCookie()
    parsed.load(cookie_header)
    cookies = [
        {
            "name": name,
            "value": morsel.value,
            "domain": ".xueqiu.com",
            "path": "/",
        }
        for name, morsel in parsed.items()
    ]
    if not cookies:
        raise ValueError("Cookie 格式无效，应类似：xq_a_token=xxx; u=123")
    return cookies


def normalize_status(status: dict[str, Any]) -> dict[str, Any]:
    user = status.get("user") or {}
    retweeted = status.get("retweeted_status") or {}
    image_urls = extract_image_urls(status)
    retweet_images = extract_image_urls(retweeted) if retweeted else []
    return {
        "id": status.get("id"),
        "url": f"{BASE_URL}{status.get('target', '')}" if status.get("target") else None,
        "created_at": iso_time(status.get("created_at")),
        "created_at_ms": status.get("created_at"),
        "author": {
            "id": user.get("id"),
            "screen_name": user.get("screen_name"),
        },
        "title": plain_text(status.get("title")),
        "text": plain_text(status.get("text") or status.get("description")),
        "source": plain_text(status.get("source")),
        "reply_count": status.get("reply_count", 0),
        "retweet_count": status.get("retweet_count", 0),
        "like_count": status.get("like_count", status.get("fav_count", 0)),
        "view_count": status.get("view_count", 0),
        "images": [{"url": url} for url in image_urls],
        "full_text": False,
        "is_retweet": bool(status.get("retweet_status_id") or retweeted),
        "retweeted_status": (
            {
                "id": retweeted.get("id"),
                "author": (retweeted.get("user") or {}).get("screen_name"),
                "text": plain_text(retweeted.get("text") or retweeted.get("description")),
                "images": [{"url": url} for url in retweet_images],
            }
            if retweeted
            else None
        ),
    }


def apply_detail_to_post(post: dict[str, Any], detail: dict[str, Any] | None) -> dict[str, Any]:
    """用详情接口补全文、图片和关联原帖。"""
    if not detail or detail.get("error_code"):
        return post

    text = plain_text(detail.get("text") or detail.get("description"))
    if text:
        post["text"] = text
    if detail.get("title"):
        post["title"] = plain_text(detail.get("title"))

    image_urls = extract_image_urls(detail)
    if image_urls:
        post["images"] = merge_image_records(post.get("images"), image_urls)

    quoted_detail = detail.get("retweeted_status")
    quoted = post.get("retweeted_status")
    if isinstance(quoted_detail, dict):
        quote_text = plain_text(quoted_detail.get("text") or quoted_detail.get("description"))
        quote_images = extract_image_urls(quoted_detail)
        if not isinstance(quoted, dict):
            quoted = {
                "id": quoted_detail.get("id"),
                "author": (quoted_detail.get("user") or {}).get("screen_name"),
                "text": quote_text,
                "images": [{"url": url} for url in quote_images],
            }
            post["retweeted_status"] = quoted
            post["is_retweet"] = True
        else:
            if quote_text:
                quoted["text"] = quote_text
            if quote_images:
                quoted["images"] = merge_image_records(quoted.get("images"), quote_images)

    post["full_text"] = True
    return post


def write_checkpoint(
    output_path: Path,
    user_id: str,
    pages: int,
    max_page: int | None,
    last_completed_page: int,
    posts: list[dict[str, Any]],
    complete: bool,
    started_at: str,
) -> dict[str, Any]:
    """原子写入抓取进度，避免中断时留下半个 JSON 文件。"""
    metadata = {
        "user_id": user_id,
        "profile_url": f"{BASE_URL}/u/{user_id}",
        "started_at": started_at,
        "fetched_at": datetime.now().astimezone().isoformat(),
        "requested_pages": pages,
        "available_pages": max_page,
        "last_completed_page": last_completed_page,
        "complete": complete,
        "item_count": len(posts),
    }
    output = {"metadata": metadata, "posts": posts}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_suffix(f"{output_path.suffix}.tmp")
    temporary_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    backup_path = output_path.with_suffix(f"{output_path.suffix}.bak")
    if output_path.exists() and output_path.stat().st_size > 0:
        output_path.replace(backup_path)
    temporary_path.replace(output_path)
    return metadata


def load_checkpoint(
    output_path: Path,
    user_id: str,
) -> tuple[list[dict[str, Any]], dict[str, Any], int]:
    backup_path = output_path.with_suffix(f"{output_path.suffix}.bak")
    try:
        data = json.loads(output_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as primary_error:
        if not backup_path.exists() or backup_path.stat().st_size == 0:
            raise ValueError(
                f"{output_path} 为空或已损坏，且没有可用备份；"
                "请移走该文件后从第 1 页重新抓取"
            ) from primary_error
        try:
            data = json.loads(backup_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as backup_error:
            raise ValueError(
                f"{output_path} 和备份 {backup_path} 都无法解析"
            ) from backup_error
        print(f"主断点损坏，已自动读取备份：{backup_path}", file=sys.stderr)
    metadata = data.get("metadata") or {}
    saved_user_id = str(metadata.get("user_id", ""))
    if saved_user_id and saved_user_id != user_id:
        raise ValueError(
            f"{output_path} 属于用户 {saved_user_id}，不能用于续爬用户 {user_id}"
        )
    posts = data.get("posts")
    if not isinstance(posts, list):
        raise ValueError(f"{output_path} 中缺少有效的 posts 数组")

    last_completed_page = metadata.get("last_completed_page")
    if last_completed_page is None:
        # 兼容旧版输出；旧版仅在全部请求页完成后才写文件。
        last_completed_page = metadata.get("requested_pages", 0) if posts else 0
    return posts, metadata, int(last_completed_page)


def fetch_json(
    page_obj: Any,
    url: str,
    timeout_seconds: float,
    retries: int,
    waf_cooldown: float,
    label: str,
) -> dict[str, Any]:
    """在浏览器会话中请求 JSON，并对临时错误自动重试。"""
    for attempt in range(1, retries + 2):
        try:
            return page_obj.evaluate(
                FETCH_JSON_JS,
                {"url": url, "timeoutMs": int(timeout_seconds * 1000)},
            )
        except PlaywrightError as exc:
            message = str(exc)
            retryable = any(
                marker in message
                for marker in (
                    "请求超时",
                    "Failed to fetch",
                    "NetworkError",
                    "HTTP 405",
                    "HTTP 429",
                    "HTTP 500",
                    "HTTP 502",
                    "HTTP 503",
                    "HTTP 504",
                )
            )
            if not retryable or attempt > retries:
                raise
            is_waf_limit = "HTTP 405" in message or "HTTP 429" in message
            if is_waf_limit:
                wait_seconds = waf_cooldown * attempt + random.uniform(0, 10)
            else:
                wait_seconds = min(60.0, 5.0 * (2 ** (attempt - 1))) + random.uniform(0, 2)
            print(
                f"{label}请求失败，{wait_seconds:.1f} 秒后重试"
                f"（{attempt}/{retries}）：{message.splitlines()[0]}",
                file=sys.stderr,
            )
            time.sleep(wait_seconds)

    raise RuntimeError("不可达的重试状态")


def fetch_timeline_page(
    page_obj: Any,
    user_id: str,
    page: int,
    timeout_seconds: float,
    retries: int,
    waf_cooldown: float,
) -> dict[str, Any]:
    url = f"{TIMELINE_API}?page={page}&user_id={user_id}"
    return fetch_json(page_obj, url, timeout_seconds, retries, waf_cooldown, f"第 {page} 页")


def fetch_status_detail(
    page_obj: Any,
    status_id: Any,
    timeout_seconds: float,
    retries: int,
    waf_cooldown: float,
) -> dict[str, Any] | None:
    url = f"{STATUS_API}?id={status_id}"
    payload = fetch_json(page_obj, url, timeout_seconds, retries, waf_cooldown, f"帖子 {status_id}")
    if payload.get("error_code"):
        return None
    return payload


def pause_between_details(delay: float) -> None:
    if delay > 0:
        time.sleep(delay + random.uniform(0, min(delay, 1.0)))


def crawl(
    user_id: str,
    pages: int,
    delay: float,
    headed: bool,
    profile_dir: Path,
    cookie: str | None,
    cdp_url: str | None,
    output_path: Path,
    resume: bool,
    request_timeout: float,
    retries: int,
    recycle_every: int,
    waf_cooldown: float,
    save_images: bool = True,
    images_dir: Path | None = None,
    full_text: bool = True,
    detail_delay: float = 1.0,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    existing_metadata: dict[str, Any] = {}
    last_completed_page = 0
    if resume and output_path.exists():
        results, existing_metadata, last_completed_page = load_checkpoint(output_path, user_id)
        print(
            f"读取断点：已完成到第 {last_completed_page} 页，共 {len(results)} 条",
            file=sys.stderr,
        )
    else:
        results = []

    seen_ids = {post["id"] for post in results if post.get("id") is not None}
    max_page = existing_metadata.get("available_pages")
    started_at = existing_metadata.get("started_at") or datetime.now().astimezone().isoformat()
    image_root = (images_dir or Path("output/images")).expanduser().resolve()

    pending_full_text = (
        [post for post in results if post.get("id") and not post.get("full_text")]
        if full_text
        else []
    )
    pending_images = (
        [post for post in results if post_images_need_download(post)]
        if save_images
        else []
    )
    timeline_done = last_completed_page >= pages
    if timeline_done and not pending_full_text and not pending_images:
        metadata = write_checkpoint(
            output_path,
            user_id,
            pages,
            max_page,
            last_completed_page,
            results,
            True,
            started_at,
        )
        print(f"目标页数已完成，无需继续抓取", file=sys.stderr)
        return results, metadata

    with sync_playwright() as playwright:
        owns_context = cdp_url is None
        if cdp_url:
            print(f"正在连接 Chrome：{cdp_url}", file=sys.stderr)
            browser = playwright.chromium.connect_over_cdp(cdp_url, timeout=30_000)
            if not browser.contexts:
                raise RuntimeError("已连接 Chrome，但没有可用的浏览器上下文")
            context = browser.contexts[0]
        else:
            context = playwright.chromium.launch_persistent_context(
                str(profile_dir.resolve()),
                channel="chrome",
                headless=not headed,
                locale="zh-CN",
            )
        try:
            if cookie:
                context.add_cookies(browser_cookies(cookie))
            # 不复用上次可能已卡死的标签页；同一 context 仍会继承登录会话。
            page_obj = context.new_page()
            page_obj.goto(f"{BASE_URL}/u/{user_id}", wait_until="domcontentloaded", timeout=30_000)
            page_obj.wait_for_timeout(2_000)
            verification = "Verification" in page_obj.title() or "alichlgref=" in page_obj.url
            if verification and not headed:
                hint = "Cookie 可能已失效，请从正常 Chrome 重新复制" if cookie else "请传入 Cookie"
                raise RuntimeError(f"雪球要求人机验证，{hint}")
            if verification:
                print("请在 Chrome 窗口中完成雪球滑块验证，最多等待 120 秒……", file=sys.stderr)
                page_obj.wait_for_function(
                    "() => document.title !== 'Verification' && !location.href.includes('alichlgref=')",
                    timeout=120_000,
                )

            # 重抓断点页并按 ID 去重，降低断点期间出现新帖造成分页错位的风险。
            page = max(1, last_completed_page)
            complete = timeline_done
            pages_on_current_tab = 0
            print(f"准备从第 {page} 页开始请求", file=sys.stderr)
            if save_images:
                print(f"图片将保存到：{image_root}", file=sys.stderr)
            if full_text:
                print("将逐条请求帖子详情接口，补全正文全文", file=sys.stderr)
                pending = [post for post in results if post.get("id") and not post.get("full_text")]
                if pending:
                    print(f"已有数据中有 {len(pending)} 条尚无全文，先补全", file=sys.stderr)
                for index, post in enumerate(pending, start=1):
                    detail = fetch_status_detail(
                        page_obj,
                        post["id"],
                        request_timeout,
                        retries,
                        waf_cooldown,
                    )
                    apply_detail_to_post(post, detail)
                    if save_images:
                        ensure_post_images(page_obj, post, image_root)
                    if index % 10 == 0 or index == len(pending):
                        write_checkpoint(
                            output_path,
                            user_id,
                            pages,
                            max_page,
                            last_completed_page,
                            results,
                            timeline_done,
                            started_at,
                        )
                        print(
                            f"已补全文 {index}/{len(pending)} 条",
                            file=sys.stderr,
                        )
                    pause_between_details(detail_delay)
            if save_images:
                missing_images = [
                    post for post in results if post_images_need_download(post)
                ]
                if missing_images:
                    print(
                        f"有 {len(missing_images)} 条帖子缺少本地图片，开始补下载",
                        file=sys.stderr,
                    )
                for index, post in enumerate(missing_images, start=1):
                    saved = ensure_post_images(page_obj, post, image_root)
                    if index % 10 == 0 or index == len(missing_images):
                        write_checkpoint(
                            output_path,
                            user_id,
                            pages,
                            max_page,
                            last_completed_page,
                            results,
                            timeline_done,
                            started_at,
                        )
                        print(
                            f"已补图片 {index}/{len(missing_images)} 条，本批落盘 {saved} 张",
                            file=sys.stderr,
                        )
                    pause_between_details(min(detail_delay, 0.5))
            if timeline_done:
                print("时间线已抓完，仅补全文和图片", file=sys.stderr)
            while page <= pages and not timeline_done:
                payload = fetch_timeline_page(
                    page_obj,
                    user_id,
                    page,
                    request_timeout,
                    retries,
                    waf_cooldown,
                )
                statuses = payload.get("statuses") or []
                max_page = payload.get("maxPage", max_page)

                if not statuses:
                    complete = True
                    break
                page_image_count = 0
                for status in statuses:
                    status_id = status.get("id")
                    if status_id in seen_ids:
                        continue
                    if status_id is not None:
                        seen_ids.add(status_id)
                    post = normalize_status(status)
                    if full_text and post.get("id") and not post.get("full_text"):
                        detail = fetch_status_detail(
                            page_obj,
                            post["id"],
                            request_timeout,
                            retries,
                            waf_cooldown,
                        )
                        apply_detail_to_post(post, detail)
                        pause_between_details(detail_delay)
                    if save_images:
                        page_image_count += ensure_post_images(
                            page_obj, post, image_root
                        )
                    results.append(post)

                last_completed_page = page
                complete = page >= pages or bool(max_page and page >= int(max_page))
                metadata = write_checkpoint(
                    output_path,
                    user_id,
                    pages,
                    max_page,
                    last_completed_page,
                    results,
                    complete,
                    started_at,
                )
                extra = f"，下载图片 {page_image_count} 张" if save_images else ""
                full_text_count = sum(1 for item in results if item.get("full_text"))
                extra += f"，全文 {full_text_count} 条" if full_text else ""
                print(
                    f"已抓取并保存第 {page} 页，本页 {len(statuses)} 条，累计 {len(results)} 条{extra}",
                    file=sys.stderr,
                )
                if complete:
                    break
                page += 1
                pages_on_current_tab += 1
                if pages_on_current_tab >= recycle_every:
                    print("定期更换 Chrome 标签页，释放长时间抓取资源", file=sys.stderr)
                    page_obj.close()
                    page_obj = context.new_page()
                    page_obj.goto(
                        f"{BASE_URL}/u/{user_id}",
                        wait_until="domcontentloaded",
                        timeout=30_000,
                    )
                    page_obj.wait_for_timeout(1_000)
                    pages_on_current_tab = 0
                if page <= pages:
                    time.sleep(delay + random.uniform(0, min(delay, 2.0)))
        finally:
            if owns_context:
                context.close()

    metadata = write_checkpoint(
        output_path,
        user_id,
        pages,
        max_page,
        last_completed_page,
        results,
        complete,
        started_at,
    )
    return results, metadata


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="抓取雪球用户公开发言并保存成 JSON")
    parser.add_argument("user_id", nargs="?", default=DEFAULT_USER_ID, help="雪球用户 ID")
    parser.add_argument("-p", "--pages", type=int, default=3, help="抓取页数，默认 3 页")
    parser.add_argument("-o", "--output", type=Path, default=Path("xueqiu_posts.json"), help="输出文件")
    parser.add_argument("--delay", type=float, default=3, help="翻页间隔秒数，默认 3")
    parser.add_argument("--headed", action="store_true", help="显示 Chrome 窗口，便于处理登录或验证")
    cookie_group = parser.add_mutually_exclusive_group()
    cookie_group.add_argument(
        "--cookie",
        help="雪球请求头中的完整 Cookie；也可设置 XUEQIU_COOKIE 环境变量",
    )
    cookie_group.add_argument("--cookie-file", type=Path, help="从文件读取完整 Cookie（更安全）")
    parser.add_argument(
        "--profile-dir",
        type=Path,
        default=Path(".xueqiu-browser"),
        help="浏览器会话目录，默认 .xueqiu-browser",
    )
    parser.add_argument(
        "--cdp-url",
        help="连接已手动启动的 Chrome，例如 http://127.0.0.1:9222",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="读取输出 JSON 的断点并继续，--pages 表示目标总页数",
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=30,
        help="单页请求超时秒数，默认 30",
    )
    parser.add_argument("--retries", type=int, default=3, help="临时错误重试次数，默认 3")
    parser.add_argument(
        "--recycle-every",
        type=int,
        default=50,
        help="每抓取多少页更换一次标签页，默认 50",
    )
    parser.add_argument(
        "--waf-cooldown",
        type=float,
        default=120,
        help="遇到 405/429 后的基础冷却秒数，默认 120",
    )
    parser.add_argument(
        "--no-save-images",
        action="store_true",
        help="不下载图片到本地，只在 JSON 中保留图片 URL",
    )
    parser.add_argument(
        "--save-images",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--images-dir",
        type=Path,
        default=Path("output/images"),
        help="图片保存目录，默认 output/images",
    )
    parser.add_argument(
        "--no-full-text",
        action="store_true",
        help="只用时间线摘要，不请求每条帖子的全文",
    )
    parser.add_argument(
        "--detail-delay",
        type=float,
        default=1.0,
        help="请求每条帖子全文的间隔秒数，默认 1",
    )
    args = parser.parse_args()
    if args.request_timeout <= 0:
        parser.error("--request-timeout 必须大于 0")
    if args.retries < 0:
        parser.error("--retries 不能小于 0")
    if args.recycle_every < 1:
        parser.error("--recycle-every 必须大于 0")
    if args.waf_cooldown < 0:
        parser.error("--waf-cooldown 不能小于 0")
    if args.detail_delay < 0:
        parser.error("--detail-delay 不能小于 0")
    return args


def main() -> int:
    args = parse_args()
    try:
        cookie = args.cookie or os.environ.get("XUEQIU_COOKIE")
        if args.cookie_file:
            cookie = args.cookie_file.read_text(encoding="utf-8").strip()
        posts, metadata = crawl(
            args.user_id,
            args.pages,
            args.delay,
            args.headed,
            args.profile_dir,
            cookie,
            args.cdp_url,
            args.output,
            args.resume,
            args.request_timeout,
            args.retries,
            args.recycle_every,
            args.waf_cooldown,
            not args.no_save_images,
            args.images_dir,
            not args.no_full_text,
            args.detail_delay,
        )
    except (OSError, PlaywrightError, ValueError, RuntimeError) as exc:
        print(f"抓取失败：{exc}", file=sys.stderr)
        return 1

    print(f"完成：{len(posts)} 条发言已写入 {args.output}")
    if not args.no_full_text:
        full_text_count = sum(1 for post in posts if post.get("full_text"))
        print(f"全文：已补全 {full_text_count} / {len(posts)} 条")
    if not args.no_save_images:
        saved = sum(
            1
            for post in posts
            for img in (post.get("images") or [])
            if img.get("path")
        )
        print(f"图片：已落盘 {saved} 张到 {args.images_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

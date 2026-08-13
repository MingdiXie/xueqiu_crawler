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
DEFAULT_USER_ID = "8790885129"
TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")


def plain_text(value: str | None) -> str:
    """把接口中的简单 HTML 文本转换成易读纯文本。"""
    if not value:
        return ""
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    value = TAG_RE.sub("", value)
    value = html.unescape(value)
    return SPACE_RE.sub(" ", value).strip()


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
        "is_retweet": bool(status.get("retweet_status_id") or retweeted),
        "retweeted_status": (
            {
                "id": retweeted.get("id"),
                "author": (retweeted.get("user") or {}).get("screen_name"),
                "text": plain_text(retweeted.get("text") or retweeted.get("description")),
            }
            if retweeted
            else None
        ),
    }


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


def fetch_timeline_page(
    page_obj: Any,
    user_id: str,
    page: int,
    timeout_seconds: float,
    retries: int,
    waf_cooldown: float,
) -> dict[str, Any]:
    """抓取一页；网络超时和临时服务错误会自动重试。"""
    for attempt in range(1, retries + 2):
        try:
            return page_obj.evaluate(
                """async ({api, userId, page, timeoutMs}) => {
                    const url = new URL(api);
                    url.searchParams.set("page", String(page));
                    url.searchParams.set("user_id", userId);
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
                }""",
                {
                    "api": TIMELINE_API,
                    "userId": user_id,
                    "page": page,
                    "timeoutMs": int(timeout_seconds * 1000),
                },
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
                f"第 {page} 页请求失败，{wait_seconds:.1f} 秒后重试"
                f"（{attempt}/{retries}）：{message.splitlines()[0]}",
                file=sys.stderr,
            )
            time.sleep(wait_seconds)

    raise RuntimeError("不可达的重试状态")


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

    if last_completed_page >= pages:
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
            complete = False
            pages_on_current_tab = 0
            print(f"准备从第 {page} 页开始请求", file=sys.stderr)
            while page <= pages:
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
                for status in statuses:
                    status_id = status.get("id")
                    if status_id in seen_ids:
                        continue
                    if status_id is not None:
                        seen_ids.add(status_id)
                    results.append(normalize_status(status))

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
                print(
                    f"已抓取并保存第 {page} 页，本页 {len(statuses)} 条，累计 {len(results)} 条",
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
    args = parser.parse_args()
    if args.request_timeout <= 0:
        parser.error("--request-timeout 必须大于 0")
    if args.retries < 0:
        parser.error("--retries 不能小于 0")
    if args.recycle_every < 1:
        parser.error("--recycle-every 必须大于 0")
    if args.waf_cooldown < 0:
        parser.error("--waf-cooldown 不能小于 0")
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
        )
    except (OSError, PlaywrightError, ValueError, RuntimeError) as exc:
        print(f"抓取失败：{exc}", file=sys.stderr)
        return 1

    print(f"完成：{len(posts)} 条发言已写入 {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Generate a clean, printable PDF from xueqiu_posts.json."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any, Iterable

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    HRFlowable,
    KeepTogether,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
)


PAGE_WIDTH, PAGE_HEIGHT = A4
ACCENT = colors.HexColor("#356B87")
TEXT = colors.HexColor("#27333A")
MUTED = colors.HexColor("#7A858C")
LIGHT = colors.HexColor("#E6ECEF")
QUOTE_BG = colors.HexColor("#F4F7F8")
HOT = "#C0392B"
MUTED_HEX = "#7A858C"
# 评论≥100、点赞≥1000 时标红。
DEFAULT_HOT_REPLY = 300
DEFAULT_HOT_LIKE = 1000
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="将雪球帖子 JSON 排版为简洁的 A4 PDF 文档。"
    )
    parser.add_argument("input", nargs="?", default="xueqiu_posts.json", help="JSON 文件")
    parser.add_argument(
        "-o",
        "--output",
        default="output/pdf/xueqiu_posts.pdf",
        help="输出 PDF 路径",
    )
    parser.add_argument("--title", default="雪球帖子精选", help="封面标题")
    parser.add_argument("--limit", type=int, help="只导出前 N 条，适合预览")
    parser.add_argument("--start-date", help="只保留该日期及之后的帖子（YYYY-MM-DD）")
    parser.add_argument("--end-date", help="只保留该日期及之前的帖子（YYYY-MM-DD）")
    parser.add_argument(
        "--min-likes",
        type=int,
        help="只保留点赞数大于该值的帖子，例如 --min-likes 1000",
    )
    parser.add_argument(
        "--include-quoted-post",
        action="store_true",
        help="显示转发或回复所关联的原帖摘要",
    )
    parser.add_argument(
        "--hot-reply",
        type=int,
        default=DEFAULT_HOT_REPLY,
        help=f"评论数达到该值时标红，默认 {DEFAULT_HOT_REPLY}",
    )
    parser.add_argument(
        "--hot-like",
        type=int,
        default=DEFAULT_HOT_LIKE,
        help=f"点赞数达到该值时标红，默认 {DEFAULT_HOT_LIKE}",
    )
    return parser.parse_args()


def register_fonts() -> tuple[str, str]:
    """Use an embedded Unicode font when available, with a portable CJK fallback."""
    candidates = [
        Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
        Path("/Library/Fonts/Arial Unicode.ttf"),
        Path.home() / "Library/Fonts/Arial Unicode.ttf",
    ]
    for font_path in candidates:
        if font_path.exists():
            pdfmetrics.registerFont(TTFont("XQSans", str(font_path)))
            return "XQSans", "XQSans"

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    return "STSong-Light", "STSong-Light"


def load_posts(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    posts = payload.get("posts")
    if not isinstance(posts, list):
        raise ValueError("JSON 中缺少 posts 数组")
    return payload.get("metadata", {}), posts


def post_datetime(post: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(str(post["created_at"]).replace("Z", "+00:00"))


def select_posts(
    posts: Iterable[dict[str, Any]],
    start_date: str | None,
    end_date: str | None,
    limit: int | None,
    min_likes: int | None = None,
) -> list[dict[str, Any]]:
    start = datetime.strptime(start_date, "%Y-%m-%d").date() if start_date else None
    end = datetime.strptime(end_date, "%Y-%m-%d").date() if end_date else None
    selected = []
    for post in posts:
        created = post_datetime(post).date()
        if start and created < start:
            continue
        if end and created > end:
            continue
        if min_likes is not None and int(post.get("like_count") or 0) <= min_likes:
            continue
        selected.append(post)
        if limit is not None and len(selected) >= max(limit, 0):
            break
    return selected


def safe_text(value: Any) -> str:
    value = CONTROL_CHARS.sub("", str(value or ""))
    return escape(value).replace("\n", "<br/>")


def make_styles(font_name: str, bold_font: str) -> dict[str, ParagraphStyle]:
    styles = getSampleStyleSheet()
    return {
        "cover_title": ParagraphStyle(
            "CoverTitle",
            parent=styles["Title"],
            fontName=bold_font,
            fontSize=28,
            leading=38,
            textColor=TEXT,
            alignment=TA_CENTER,
            spaceAfter=10 * mm,
        ),
        "cover_author": ParagraphStyle(
            "CoverAuthor",
            fontName=font_name,
            fontSize=13,
            leading=21,
            textColor=ACCENT,
            alignment=TA_CENTER,
        ),
        "cover_meta": ParagraphStyle(
            "CoverMeta",
            fontName=font_name,
            fontSize=9.5,
            leading=17,
            textColor=MUTED,
            alignment=TA_CENTER,
        ),
        "section": ParagraphStyle(
            "Section",
            fontName=bold_font,
            fontSize=20,
            leading=28,
            textColor=TEXT,
            spaceAfter=7 * mm,
        ),
        "date": ParagraphStyle(
            "Date",
            fontName=bold_font,
            fontSize=11,
            leading=16,
            textColor=ACCENT,
            spaceBefore=1.5 * mm,
            spaceAfter=2.3 * mm,
        ),
        "body": ParagraphStyle(
            "Body",
            fontName=font_name,
            fontSize=9.6,
            leading=16,
            textColor=TEXT,
            alignment=TA_LEFT,
            splitLongWords=True,
            spaceAfter=2.2 * mm,
        ),
        "quote": ParagraphStyle(
            "Quote",
            fontName=font_name,
            fontSize=8.7,
            leading=14,
            textColor=colors.HexColor("#59666D"),
            leftIndent=4 * mm,
            rightIndent=2 * mm,
            borderColor=colors.HexColor("#BED0D8"),
            borderWidth=0,
            borderLeftWidth=1.5,
            borderPadding=3 * mm,
            backColor=QUOTE_BG,
            spaceAfter=2.3 * mm,
        ),
        "small": ParagraphStyle(
            "Small",
            fontName=font_name,
            fontSize=8,
            leading=12,
            textColor=MUTED,
        ),
    }


class PostsDocTemplate(BaseDocTemplate):
    def __init__(
        self, filename: str, font_name: str, display_author: str, **kwargs: Any
    ):
        super().__init__(filename, pagesize=A4, **kwargs)
        self.font_name = font_name
        self.author_name = display_author
        cover_frame = Frame(
            23 * mm,
            25 * mm,
            PAGE_WIDTH - 46 * mm,
            PAGE_HEIGHT - 50 * mm,
            id="cover",
        )
        body_frame = Frame(
            19 * mm,
            18 * mm,
            PAGE_WIDTH - 38 * mm,
            PAGE_HEIGHT - 35 * mm,
            id="body",
        )
        self.addPageTemplates(
            [
                PageTemplate(id="Cover", frames=[cover_frame], onPage=self.draw_cover_page),
                PageTemplate(id="Body", frames=[body_frame], onPage=self.draw_body_page),
            ]
        )

    def draw_cover_page(self, canvas: Any, doc: Any) -> None:
        canvas.saveState()
        canvas.setFillColor(ACCENT)
        canvas.rect(0, PAGE_HEIGHT - 7 * mm, PAGE_WIDTH, 7 * mm, stroke=0, fill=1)
        canvas.setFillColor(colors.HexColor("#DCE7EC"))
        canvas.circle(PAGE_WIDTH / 2, PAGE_HEIGHT - 54 * mm, 4 * mm, stroke=0, fill=1)
        canvas.restoreState()

    def draw_body_page(self, canvas: Any, doc: Any) -> None:
        canvas.saveState()
        canvas.setFont(self.font_name, 7.5)
        canvas.setFillColor(MUTED)
        canvas.drawString(19 * mm, PAGE_HEIGHT - 10.5 * mm, f"雪球公开发言  ·  {self.author_name}")
        canvas.setStrokeColor(LIGHT)
        canvas.setLineWidth(0.5)
        canvas.line(19 * mm, PAGE_HEIGHT - 13 * mm, PAGE_WIDTH - 19 * mm, PAGE_HEIGHT - 13 * mm)
        canvas.drawRightString(PAGE_WIDTH - 19 * mm, 9 * mm, str(max(doc.page - 1, 1)))
        canvas.restoreState()


def date_range(posts: list[dict[str, Any]]) -> str:
    if not posts:
        return "无符合条件的帖子"
    dates = [post_datetime(post).date() for post in posts]
    return f"{min(dates):%Y-%m-%d}  至  {max(dates):%Y-%m-%d}"


def metric_html(label: str, value: int, threshold: int) -> str:
    color = HOT if value >= threshold else MUTED_HEX
    return f'<font color="{color}" size="8">{label} {value:,}</font>'


def build_story(
    metadata: dict[str, Any],
    posts: list[dict[str, Any]],
    title: str,
    include_quoted_post: bool,
    styles: dict[str, ParagraphStyle],
    hot_reply: int,
    hot_like: int,
) -> list[Any]:
    author = (
        posts[0].get("author", {}).get("screen_name")
        if posts
        else f"用户 {metadata.get('user_id', '')}"
    )
    story: list[Any] = [
        Spacer(1, 58 * mm),
        Paragraph(safe_text(title), styles["cover_title"]),
        Paragraph(safe_text(author), styles["cover_author"]),
        Spacer(1, 9 * mm),
        HRFlowable(
            width=22 * mm,
            thickness=1.5,
            color=ACCENT,
            spaceBefore=0,
            spaceAfter=8 * mm,
            hAlign="CENTER",
        ),
        Paragraph(f"收录 {len(posts):,} 条公开发言", styles["cover_meta"]),
        Paragraph(date_range(posts), styles["cover_meta"]),
        Spacer(1, 36 * mm),
        Paragraph("内容按发布时间倒序排列", styles["cover_meta"]),
        NextPageTemplate("Body"),
        PageBreak(),
        Paragraph("帖子详情", styles["section"]),
    ]

    for index, post in enumerate(posts):
        created = post_datetime(post)
        replies = int(post.get("reply_count") or 0)
        likes = int(post.get("like_count") or 0)
        url = escape(str(post.get("url") or ""), quote=True)
        date_line = (
            f'<link href="{url}" color="#356B87">{created:%Y-%m-%d}</link>'
            f"  {metric_html('评论', replies, hot_reply)}"
            f"　{metric_html('点赞', likes, hot_like)}"
        )
        date_paragraph = Paragraph(date_line, styles["date"])
        item: list[Any] = []
        if index == 0:
            item.append(date_paragraph)
        else:
            item.extend(
                [
                    Spacer(1, 2.2 * mm),
                    HRFlowable(width="100%", thickness=0.5, color=LIGHT),
                    Spacer(1, 4.5 * mm),
                    date_paragraph,
                ]
            )
        if post.get("title"):
            item.append(
                Paragraph(
                    f'<font color="#27333A"><b>{safe_text(post["title"])}</b></font>',
                    styles["body"],
                )
            )
        item.append(Paragraph(safe_text(post.get("text")), styles["body"]))

        quoted = post.get("retweeted_status")
        if include_quoted_post and isinstance(quoted, dict) and quoted.get("text"):
            quote_author = safe_text(quoted.get("author") or "原帖")
            quote_text = safe_text(quoted["text"])
            item.append(
                Paragraph(f"<b>{quote_author}</b><br/>{quote_text}", styles["quote"])
            )
        story.append(KeepTogether(item))

    return story


def main() -> None:
    args = parse_args()
    input_path = Path(args.input).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()
    metadata, posts = load_posts(input_path)
    posts = select_posts(
        posts, args.start_date, args.end_date, args.limit, args.min_likes
    )
    font_name, bold_font = register_fonts()
    styles = make_styles(font_name, bold_font)
    author = (
        posts[0].get("author", {}).get("screen_name")
        if posts
        else f"用户 {metadata.get('user_id', '')}"
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    document = PostsDocTemplate(
        str(output_path),
        font_name=font_name,
        display_author=author,
        title=args.title,
        author=author,
        subject=f"{author}的雪球公开发言",
        creator="generate_posts_pdf.py",
        leftMargin=19 * mm,
        rightMargin=19 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
    )
    story = build_story(
        metadata,
        posts,
        args.title,
        args.include_quoted_post,
        styles,
        args.hot_reply,
        args.hot_like,
    )
    document.build(story)
    print(f"已生成：{output_path}（{len(posts):,} 条帖子）")


if __name__ == "__main__":
    main()

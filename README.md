# 雪球用户发言爬虫

抓取雪球用户的公开发言，并保存为 UTF-8 JSON 文件。默认目标是“超级鹿鼎公”（用户 ID `8790885129`）。

## 使用

```bash
python3 -m pip install -r requirements.txt
python3 xueqiu_crawler.py
```

默认抓取 3 页（约 60 条），输出到 `xueqiu_posts.json`。时间线接口经常截断长文，脚本会默认再请求每条帖子的详情接口，把完整正文写入 `text`，并标记 `full_text: true`。也可以指定用户、页数和文件名：

```bash
python3 xueqiu_crawler.py 8790885129 --pages 10 --output data/chaojiludinggong.json
```

已有旧 JSON 只有摘要时，用相同输出文件加 `--resume` 即可只补全文，不必重抓时间线。如果只想用时间线摘要、加快抓取，可加 `--no-full-text`。

抓取时默认下载帖子原图到 `output/images/<帖子ID>/`，并在 JSON 的 `images` 中写入本地 `path`。补全文时会按去掉 `!thumb` / `!custom` 后的原图地址匹配，避免弄丢已下载路径。若 JSON 里已有图片 URL 但缺少 `path`，同样用 `--resume` 补下载：

```bash
python3 xueqiu_crawler.py \
  --cdp-url http://127.0.0.1:9222 \
  --pages 110 \
  --resume
```

不需要本地图片时加 `--no-save-images`。生成 PDF 时会把本地图片按单列大图排进文档。

## 导出 PDF

安装依赖后，可将 JSON 中的帖子排版为带封面、页眉、页码和互动数据的 A4 PDF：

```bash
python3 generate_posts_pdf.py xueqiu_posts.json
```

默认输出到 `output/pdf/xueqiu_posts.pdf`。超过 1000 页时会自动拆成 `xueqiu_posts_part01.pdf`、`part02.pdf` 等多份，例如 2500 页会分成 3 个文件。可用 `--pages-per-file` 调整每份页数。日期可点击并跳转到对应雪球帖子。常用选项：

```bash
# 按日期筛选，并显示关联原帖摘要
python3 generate_posts_pdf.py xueqiu_posts.json \
  --start-date 2025-01-01 --end-date 2025-12-31 \
  --include-quoted-post --title "2025 年雪球帖子"

# 自定义高互动标红阈值（默认：评论≥100、点赞≥1000）
python3 generate_posts_pdf.py xueqiu_posts.json \
  --hot-reply 50 --hot-like 200

# 只导出点赞数大于 1000 的帖子
python3 generate_posts_pdf.py xueqiu_posts.json --min-likes 1000 \
  --output output/pdf/xueqiu_posts_hot.pdf --title "高赞帖子"
```

运行 `python3 generate_posts_pdf.py --help` 可查看全部参数。

评论与点赞会分别判断：达到阈值的那一项变为红色，另一项仍保持灰色。`--min-likes` 是内容筛选，与标红阈值互不影响。
脚本每完成一页就会原子保存进度，并把上一版保留为 `xueqiu_posts.json.bak`。若主文件意外损坏，`--resume` 会自动回退到备份。抓取中断后，使用相同输出文件并增加 `--resume`：

```bash
python3 xueqiu_crawler.py \
  --cdp-url http://127.0.0.1:9222 \
  --pages 100 \
  --output xueqiu_posts.json \
  --resume
```

`--pages 100` 表示最终目标是第 100 页，不是再抓 100 页。断点页会重抓一次并按帖子 ID 去重，以降低中断期间新发言导致分页移动而漏数据的风险。JSON 的 `metadata.last_completed_page` 表示已保存到哪一页，`metadata.complete` 表示目标是否完成

抓取期间不要在编辑器中修改或保存体积很大的输出 JSON，以免编辑器将文件截断。需要查看进度时，只读取文件开头的 `metadata` 即可。

每页请求默认 30 秒超时，并对网络错误自动重试 3 次。遇到 WAF 返回 405/429 时默认先冷却 120 秒再重试。长时间抓取时每 50 页自动更换 Chrome 标签页，避免页面积累资源后失去响应。可按需调整：

```bash
python3 xueqiu_crawler.py --resume --pages 1228 \
  --cdp-url http://127.0.0.1:9222 \
  --request-timeout 45 --retries 5 --recycle-every 30 \
  --waf-cooldown 180
```

如果遇到 401、403 或 429，可增大请求间隔：

```bash
python3 xueqiu_crawler.py --pages 10 --delay 3
```

脚本使用系统安装的 Google Chrome，并把会话保存在 `.xueqiu-browser`。如果自动化窗口中的滑块验证失败，请在正常 Chrome 中登录雪球，从开发者工具的 Network 请求头复制完整的 `Cookie`，保存到 `cookie.txt` 后运行：

```bash
python3 xueqiu_crawler.py --cookie-file cookie.txt
```

也可以直接传入（Cookie 会进入终端历史，不推荐）：

```bash
python3 xueqiu_crawler.py --cookie 'xq_a_token=xxx; u=123'
```

Cookie 具有账号访问权限，请勿提交或分享；失效后需要重新复制。脚本不会尝试绕过验证码。

若 Cookie 仍触发验证（验证状态可能与浏览器环境绑定），请启动一个可供脚本连接的正常 Chrome：

```bash
open -na "Google Chrome" --args \
  --remote-debugging-port=9222 \
  --user-data-dir="$PWD/.xueqiu-real-chrome"
```

在新打开的 Chrome 中登录雪球并完成验证，保持窗口开启，然后运行：

```bash
python3 xueqiu_crawler.py \
  --cdp-url http://127.0.0.1:9222 \
  --pages 1228 \
  --resume
```

请只抓取公开内容，控制访问频率，并遵守雪球服务条款及适用法律。

```bash
python3 generate_posts_pdf.py xueqiu_posts.json
```

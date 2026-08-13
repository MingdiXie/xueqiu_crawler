# 雪球用户发言爬虫

抓取雪球用户的公开发言，并保存为 UTF-8 JSON 文件。默认目标是“超级鹿鼎公”（用户 ID `8790885129`）。

## 使用

```bash
python3 -m pip install -r requirements.txt
python3 xueqiu_crawler.py
```

默认抓取 3 页（约 60 条），输出到 `xueqiu_posts.json`。也可以指定用户、页数和文件名：

```bash
python3 xueqiu_crawler.py 8790885129 --pages 10 --output data/chaojiludinggong.json
```

## 导出 PDF

安装依赖后，可将 JSON 中的帖子排版为带封面、页眉、页码和互动数据的 A4 PDF：

```bash
python3 generate_posts_pdf.py xueqiu_posts.json
```

默认输出到 `output/pdf/xueqiu_posts.pdf`。日期可点击并跳转到对应雪球帖子。常用选项：

```bash
# 先生成 20 条帖子预览
python3 generate_posts_pdf.py xueqiu_posts.json --limit 20 \
  --output output/pdf/xueqiu_posts_preview.pdf

# 按日期筛选，并显示关联原帖摘要
python3 generate_posts_pdf.py xueqiu_posts.json \
  --start-date 2025-01-01 --end-date 2025-12-31 \
  --include-quoted-post --title "2025 年雪球帖子"
```

运行 `python3 generate_posts_pdf.py --help` 可查看全部参数。

脚本每完成一页就会原子保存进度，并把上一版保留为 `xueqiu_posts.json.bak`。若主文件意外损坏，`--resume` 会自动回退到备份。抓取中断后，使用相同输出文件并增加 `--resume`：

```bash
python3 xueqiu_crawler.py \
  --cdp-url http://127.0.0.1:9222 \
  --pages 100 \
  --output xueqiu_posts.json \
  --resume
```

`--pages 100` 表示最终目标是第 100 页，不是再抓 100 页。断点页会重抓一次并按帖子 ID 去重，以降低中断期间新发言导致分页移动而漏数据的风险。JSON 的 `metadata.last_completed_page` 表示已保存到哪一页，`metadata.complete` 表示目标是否完成。

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


"""Self-contained callback presentation; OAuth settlement stays with its owner."""

from html import escape
from string import Template

from aiohttp import web


def oauth_callback_response(
    title: str, description: str, *, status: int = 200
) -> web.Response:
    # No query values, remote assets or scripts belong on the callback page.
    return web.Response(
        status=status,
        text=_PAGE.substitute(title=escape(title), description=escape(description)),
        content_type="text/html",
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


_PAGE = Template("""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>$title · Pulsara</title>
  <style>
    :root {
      color-scheme: light dark;
      --paper: #f3f0e8; --card: #fdfbf6; --ink: #17191f;
      --muted: #7d7b74; --line: rgba(29,31,38,.12);
      --accent: #bd8739; --soft: #f5e6ca;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0; min-height: 100vh; min-height: 100dvh;
      display: grid; place-items: center; padding: 28px;
      background: radial-gradient(ellipse at 50% 32%, var(--soft), transparent 65%), var(--paper);
      color: var(--ink); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      -webkit-font-smoothing: antialiased;
    }
    main { width: min(100%, 500px); }
    .brand { display: flex; align-items: center; gap: 12px; margin: 0 0 26px 4px; }
    .brand-mark {
      display: grid; place-items: center; width: 42px; height: 42px;
      border-radius: 13px; background: #17191f; color: #d79a42;
    }
    .brand-name { font: 600 22px Georgia, "Songti SC", serif; letter-spacing: -.4px; }
    .brand small { display: block; margin-top: 4px; color: var(--muted); font-size: 12px; }
    .card {
      padding: 38px; border: 1px solid var(--line); border-radius: 22px;
      background: var(--card); box-shadow: 0 16px 52px rgba(33,31,25,.06);
    }
    .eyebrow { display: flex; align-items: center; gap: 8px; color: var(--accent); font-size: 12px; }
    .eyebrow::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
    h1 { margin: 20px 0 14px; font: 500 clamp(24px,5vw,30px)/1.4 Georgia, "Songti SC", "Noto Serif CJK SC", serif; }
    p { margin: 0; color: var(--muted); font-size: 14px; line-height: 1.9; }
    .return-note {
      display: flex; align-items: flex-start; gap: 10px; margin-top: 30px;
      padding-top: 22px; border-top: 1px solid var(--line); font-size: 13px; line-height: 1.8;
    }
    .return-note svg { flex: none; margin-top: 3px; color: var(--accent); }
    footer { margin-top: 22px; text-align: center; color: var(--muted); font-size: 12px; line-height: 1.8; }
    @media (max-width: 480px) { body { padding: 22px; } .card { padding: 28px 24px; } }
    @media (prefers-color-scheme: dark) {
      :root { --paper: #1c1c1b; --card: #242421; --ink: #ede9df; --muted: #aaa69b;
        --line: rgba(237,233,223,.12); --accent: #d8ac65; --soft: #302a20; }
    }
  </style>
</head>
<body>
  <main aria-labelledby="callback-title">
    <header class="brand">
      <span class="brand-mark" aria-hidden="true">
        <svg width="32" height="32" viewBox="0 0 32 32" fill="none">
          <circle cx="16" cy="16" r="10" stroke="currentColor" stroke-width=".8"/>
          <ellipse cx="16" cy="16" rx="15" ry="5.5" transform="rotate(-24 16 16)" stroke="currentColor"/>
          <path d="M16 10.5 17.3 14.7 21.5 16 17.3 17.3 16 21.5 14.7 17.3 10.5 16 14.7 14.7Z" fill="currentColor"/>
        </svg>
      </span>
      <div><span class="brand-name">Pulsara</span><small>本机连接 · 浏览器授权</small></div>
    </header>
    <section class="card">
      <div class="eyebrow">授权回调</div>
      <h1 id="callback-title">$title</h1>
      <p>$description</p>
      <div class="return-note">
        <svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
          <path d="m9 10-4 4 4 4M5 14h9a5 5 0 0 0 5-5V5"/>
        </svg>
        <span>回到 Pulsara，在能力页查看授权状态。</span>
      </div>
    </section>
    <footer>可以关闭此标签页，继续你的工作。</footer>
  </main>
</body>
</html>
""")

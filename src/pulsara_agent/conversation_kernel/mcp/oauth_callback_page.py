"""Self-contained callback presentation; OAuth settlement stays with its owner."""

from base64 import b64encode
from functools import cache
from importlib.resources import files
from html import escape
from string import Template
from typing import Literal

from aiohttp import web


def oauth_callback_response(
    title: str,
    description: str,
    *,
    status: int = 200,
    outcome: Literal["success", "error", "cancelled"] = "error",
) -> web.Response:
    # No query values, remote assets or scripts belong on the callback page.
    return web.Response(
        status=status,
        text=_PAGE.substitute(
            title=escape(title),
            description=escape(description),
            logo=_logo(),
            outcome=escape(outcome, quote=True),
        ),
        content_type="text/html",
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@cache
def _logo() -> str:
    # Reuse the desktop brand asset, embedded so the one-shot listener can close.
    asset = files("pulsara_agent.desktop_app").joinpath(
        "static/assets/pulsara-icon.png"
    )
    return b64encode(asset.read_bytes()).decode("ascii")


_PAGE = Template("""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>$title · Pulsara</title>
  <style>
    :root {
      color-scheme: light dark;
      --card: #fcfaf5; --line: #e6e0d3; --status-gap: 20px;
      --paper: #f3f0e8; --ink: #24262b; --muted: #77786f; --accent: #bd8739;
      --button: #292c29; --button-hover: #3c403a; --button-ink: #faf8f1;
      --success: #478269; --success-bg: #e3ece3;
      --error: #ab6155; --error-bg: #f1e4dc;
      --neutral: #7b7b70; --neutral-bg: #e8e5dc;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0; min-height: 100vh; min-height: 100dvh;
      display: grid; place-items: center; padding: 40px 24px calc(40px + 8vh);
      background: var(--paper); color: var(--ink);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      -webkit-font-smoothing: antialiased;
    }
    main {
      width: min(100%, 440px); padding: 44px 36px; text-align: center;
      background: var(--card); border: 1px solid var(--line); border-radius: 22px;
      box-shadow: 0 20px 52px -24px rgba(64,48,23,.22), 0 2px 8px rgba(64,48,23,.03);
      animation: appear .35s ease-out both;
    }
    .brand { display: flex; align-items: center; justify-content: center; gap: 10px; margin-bottom: var(--status-gap); }
    .brand-mark { display: block; width: 36px; height: 36px; object-fit: contain; }
    .brand-name { font: 600 23px Georgia, "Songti SC", serif; letter-spacing: -.5px; }
    .status-icon {
      position: relative; display: grid; place-items: center; margin: 0 auto var(--status-gap);
      width: 52px; height: 52px; border-radius: 50%;
      color: var(--error); background: var(--error-bg);
    }
    .status-icon::before, .status-icon::after { content: ""; position: absolute; border-radius: 1px; }
    .status-icon.error::before { width: 2px; height: 14px; background: currentColor; top: 15px; }
    .status-icon.error::after { width: 3px; height: 3px; background: currentColor; top: 33px; }
    .status-icon.success { color: var(--success); background: var(--success-bg); }
    .status-icon.success::before {
      width: 19px; height: 10px; border-left: 2px solid; border-bottom: 2px solid;
      transform: translateY(-3px) rotate(-45deg);
    }
    .status-icon.cancelled { color: var(--neutral); background: var(--neutral-bg); }
    .status-icon.cancelled::before { width: 18px; height: 2px; background: currentColor; }
    h1 { margin: 0; font: 600 clamp(26px,6vw,30px)/1.4 Georgia, "Songti SC", "Noto Serif CJK SC", serif; letter-spacing: .02em; }
    p { margin: 14px 0 0; color: var(--muted); font-size: 15px; line-height: 1.8; overflow-wrap: anywhere; }
    p:empty { display: none; }
    .return-button {
      display: inline-flex; align-items: center; justify-content: center;
      min-height: 44px; min-width: 176px; max-width: 100%; margin-top: 30px;
      padding: 11px 24px; border-radius: 9px; background: var(--button);
      color: var(--button-ink); font-size: 14px; font-weight: 500; text-decoration: none;
      transition: background .15s;
    }
    .return-button:hover { background: var(--button-hover); }
    .return-button:focus-visible { outline: 2px solid var(--accent); outline-offset: 4px; }
    @keyframes appear { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: translateY(0); } }
    @media (max-width: 480px) {
      body { padding-left: 18px; padding-right: 18px; }
      main { padding: 36px 24px; border-radius: 20px; }
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --card: #242621; --line: #393c32;
        --paper: #1c1e1b; --ink: #eeeadd; --muted: #b4b1a5; --accent: #d7a34f;
        --button: #e7e3d8; --button-hover: #f7f3e9; --button-ink: #252820;
        --success: #95c4a6; --success-bg: #2a3a30;
        --error: #dca194; --error-bg: #3d2d28;
        --neutral: #b8b8a9; --neutral-bg: #32352d;
      }
    }
  </style>
</head>
<body>
  <main aria-labelledby="callback-title">
    <header class="brand">
      <img class="brand-mark" src="data:image/png;base64,$logo" alt="" width="36" height="36">
      <span class="brand-name">Pulsara</span>
    </header>
    <div class="status-icon $outcome" aria-hidden="true"></div>
    <h1 id="callback-title">$title</h1>
    <p>$description</p>
    <a class="return-button" href="pulsara://oauth/return">返回 Pulsara</a>
  </main>
</body>
</html>
""")

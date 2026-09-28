#!/usr/bin/env python3
"""Render the README illustrations in both languages.

These are hand-drawn HTML/CSS illustrations of what the Codex model picker looks
like once Codex Ultra is installed. They are deliberately kept as source: an
illustration that ships with its own generator cannot be mistaken for a
screenshot of a specific build, and anyone can regenerate a PNG after a copy
change.

    python3 docs/assets/src/render_assets.py

Needs a Chromium-family browser. The script looks for Playwright's
chrome-headless-shell, then falls back to CHROME_PATH.
"""

from __future__ import annotations

import html
import os
import pathlib
import shutil
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE.parent

CANDIDATES = (
    pathlib.Path.home() / "Library/Caches/ms-playwright",
    pathlib.Path.home() / ".cache/ms-playwright",
)


def find_chrome():
    override = os.environ.get("CHROME_PATH")
    if override and pathlib.Path(override).exists():
        return override
    for root in CANDIDATES:
        if not root.exists():
            continue
        for path in sorted(root.glob("chromium_headless_shell-*/**/chrome-headless-shell")):
            if path.is_file():
                return str(path)
        for path in sorted(root.glob("chromium-*/**/Chromium")):
            if path.is_file():
                return str(path)
    for name in ("chromium", "google-chrome", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    raise SystemExit("no Chromium found; set CHROME_PATH")


FONT = ('-apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", '
        '"Helvetica Neue", Arial, sans-serif')

COPY = {
    "zh": {
        "window_title": "Codex",
        "chat_a": "上下文已经压缩过一次，任务继续。",
        "chat_b": "已用 DeepSeek V4 Flash 完成压缩交接。",
        "chat_c": "继续：把剩下的三个模块接上。",
        "picker_title": "选择模型",
        "picker_hint": "装在 Codex Ultra 后面的所有模型",
        "selected_tag": "当前",
        "native_tag": "原生",
        "input_placeholder": "输入消息…",
        "before_title": "没有 Codex Ultra",
        "before_steps": ["把模型切到 DeepSeek", "上下文满了，触发压缩", "502 Bad Gateway，任务断在这里"],
        "after_title": "装上 Codex Ultra",
        "after_steps": ["把模型切到 DeepSeek", "Codex Ultra 接管压缩并加密存本机", "任务接着跑，历史一条不丢"],
        "footnote": "示意图：展示安装后模型选择与压缩接续的效果，不是某个具体版本的真实截图。",
        "switch_title_a": "在 Codex 外面切",
        "switch_sub_a": "CC Switch 这类方案的路径",
        "switch_steps_a": ["退出或切走 Codex", "打开供应商切换工具", "切换供应商 / 配置",
                           "回到 Codex，重载配置", "继续干活"],
        "switch_title_b": "在 Codex 里面切",
        "switch_sub_b": "Codex Ultra 的路径",
        "switch_steps_b": ["点 Codex 右下角的模型按钮", "选中你要的模型", "继续干活"],
        "switch_note": "CC Switch 管的是「用哪个供应商」，Codex Ultra 管的是「这个模型怎么在 Codex 里跑」。两者不冲突，安装时我们还会帮你同步它的记录。",
        "caps_title": "换的是模型，不是体验",
        "caps_col": "Codex 的能力",
        "caps_right": "换成第三方模型之后",
        "caps": [("Skill", "不受影响"), ("MCP", "不受影响"), ("工具调用", "不受影响"),
                 ("Computer Use", "不受影响"), ("Memory", "不受影响"), ("Sub Agent", "不受影响"),
                 ("自动压缩", "由你指定的模型接管"), ("图片", "支持的模型照常送图")],
        "caps_note": "适配层不改动 Codex 下发的工具声明、指令和这些能力所需的字段，转发时原样保留——测试钉住了这一点。至于模型能不能真正驱动它们，取决于模型自身：文本模型不会因为装了 Codex Ultra 就获得视觉，但它不会再因为一张图把整个请求搞崩。Codex Ultra 做的是让 Codex 正确认识每个模型能干什么。",
    },
    "en": {
        "window_title": "Codex",
        "chat_a": "Context was compacted once; the task continued.",
        "chat_b": "Handoff completed on DeepSeek V4 Flash.",
        "chat_c": "Keep going: wire up the last three modules.",
        "picker_title": "Choose a model",
        "picker_hint": "Everything behind Codex Ultra",
        "selected_tag": "current",
        "native_tag": "native",
        "input_placeholder": "Send a message…",
        "before_title": "Without Codex Ultra",
        "before_steps": ["Switch the model to DeepSeek", "Context fills up, compaction fires", "502 Bad Gateway, the task dies here"],
        "after_title": "With Codex Ultra",
        "after_steps": ["Switch the model to DeepSeek", "Codex Ultra compacts and encrypts locally", "The task continues, nothing lost"],
        "footnote": "Illustration: the post-install model picker and compaction handoff, not a screenshot of a specific build.",
        "switch_title_a": "Switching outside Codex",
        "switch_sub_a": "How provider switchers work",
        "switch_steps_a": ["Leave or switch away from Codex", "Open the provider switcher",
                           "Switch provider / edit config", "Return to Codex and reload", "Get back to work"],
        "switch_title_b": "Switching inside Codex",
        "switch_sub_b": "How Codex Ultra works",
        "switch_steps_b": ["Click the model button in Codex", "Pick the model you want", "Get back to work"],
        "switch_note": "A provider switcher decides *which provider* you use. Codex Ultra decides *how that model runs inside Codex*. They do not conflict — the installer keeps the switcher's record in sync.",
        "caps_title": "A different model, not a different experience",
        "caps_col": "Codex feature",
        "caps_right": "With a third-party model",
        "caps": [("Skill", "unaffected"), ("MCP", "unaffected"), ("Tool calling", "unaffected"),
                 ("Computer Use", "unaffected"), ("Memory", "unaffected"), ("Sub Agent", "unaffected"),
                 ("Auto-compaction", "served by the model you pick"),
                 ("Images", "routed to models that take them")],
        "caps_note": "The adapter does not rewrite the tool declarations, instructions or fields these features rely on, and a test pins that pass-through. Whether a model can actually drive them is up to the model: a text-only model does not gain vision, but it stops breaking the whole request over one image. Codex Ultra's job is making Codex understand what each model can do.",
    },
}

# Model rows: name, context badge, whether the route takes images, and which one
# is selected. Kept in one place so both languages stay identical.
MODELS = [
    ("GPT-6 Sol", "272K", True, True),
    ("DeepSeek V4 Flash", "1M", False, False),
    ("Gemini 3.8 Flash", "1M", True, False),
    ("Claude Opus 5.5", "1M", True, False),
    ("Grok 4.7", "210K", True, False),
    ("MiniMax M2", "1M", True, False),
    ("Kimi K2", "256K", False, False),
    ("GLM 4.6", "131K", False, False),
]

EYE = ('<svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">'
       '<path d="M8 3.2c3.1 0 5.4 2.3 6.4 4.8-1 2.5-3.3 4.8-6.4 4.8S2.6 10.5 1.6 8C2.6 5.5 4.9 3.2 8 3.2Z" '
       'fill="none" stroke="#7d8590" stroke-width="1.3"/>'
       '<circle cx="8" cy="8" r="1.9" fill="#7d8590"/></svg>')


def model_rows(copy):
    out = []
    for name, window, image, selected in MODELS:
        mark = ('<span class="check">✓</span>' if selected else '<span class="check"></span>')
        tag = ""
        if selected:
            tag = '<span class="tag tag-selected">%s</span>' % copy["selected_tag"]
        elif name.startswith("GPT-"):
            tag = '<span class="tag">%s</span>' % copy["native_tag"]
        badge = ('<span class="ctx%s">%s</span>' % (" ctx-wide" if window == "1M" else "", window))
        eye = EYE if image else '<span class="eye-empty"></span>'
        hovered = name.startswith("DeepSeek")
        cursor = ('<span class="cursor">'
                  '<svg viewBox="0 0 20 20" width="19" height="19">'
                  '<path d="M4 2.2 16 12.1h-5.2l2.7 5.4-2.4 1.1-2.6-5.5-3.6 3.6z" '
                  'fill="#f0f6fc" stroke="#0d1117" stroke-width="1.1" stroke-linejoin="round"/>'
                  '</svg></span>' if hovered else "")
        out.append(
            '<div class="row%s%s">%s<span class="name">%s</span>%s%s%s%s</div>'
            % (" row-selected" if selected else "", " row-hover" if hovered else "",
               mark, html.escape(name), tag, eye, badge, cursor))
    return "\n      ".join(out)


def model_picker(copy):
    # Only the first turn is labelled: a stray label on the narrator line looked
    # like a rendering bug.
    steps = ('<div class="bubble bubble-dim"><span class="who">Codex</span>%s</div>'
             % html.escape(copy["chat_a"]))
    steps += "\n        " + "\n        ".join(
        '<div class="bubble bubble-faint">%s</div>' % html.escape(text)
        for text in (copy["chat_b"], copy["chat_c"]))
    return f"""<!doctype html>
<html lang="{'zh-CN' if copy is COPY['zh'] else 'en'}"><head><meta charset="utf-8">
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: #0d1117; font-family: {FONT}; -webkit-font-smoothing: antialiased; }}
  .wrap {{ width: 820px; padding: 22px; }}
  .win {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; overflow: hidden; }}
  .bar {{ display: flex; align-items: center; gap: 8px; padding: 11px 14px;
         background: #1c2128; border-bottom: 1px solid #30363d; }}
  .dot {{ width: 11px; height: 11px; border-radius: 50%; }}
  .title {{ margin-left: 8px; color: #c9d1d9; font-size: 13px; font-weight: 600; }}
  .body {{ display: flex; height: 470px; }}
  .chat {{ flex: 1; padding: 20px 22px; display: flex; flex-direction: column; gap: 11px; }}
  .bubble {{ color: #8b949e; font-size: 13px; line-height: 1.5; padding: 9px 13px;
            background: #1c2128; border: 1px solid #262c36; border-radius: 6px; max-width: 470px; }}
  .bubble-dim {{ color: #6e7681; }}
  .bubble-faint {{ color: #57606a; }}
  .who {{ display: block; color: #484f58; font-size: 10px; letter-spacing: .06em;
         text-transform: uppercase; margin-bottom: 3px; }}
  .foot {{ margin-top: auto; border: 1px solid #30363d; border-radius: 8px; background: #1c2128; }}
  .foot-top {{ padding: 13px 15px 6px; color: #57606a; font-size: 13px; }}
  .foot-bottom {{ display: flex; justify-content: flex-end; padding: 4px 11px 11px; }}
  .pill {{ display: flex; align-items: center; gap: 7px; padding: 6px 12px; font-size: 12.5px;
          color: #e6edf3; background: #262c36; border: 1px solid #3d444d; border-radius: 6px; }}
  .caret {{ color: #8b949e; font-size: 10px; }}
  .picker {{ position: absolute; width: 372px; background: #1c2128;
            border: 1px solid #3d444d; border-radius: 8px; padding: 8px;
            box-shadow: 0 16px 34px rgba(1,4,9,.6); }}
  .picker-head {{ padding: 6px 9px 3px; }}
  .picker-title {{ color: #e6edf3; font-size: 12.5px; font-weight: 600; }}
  .picker-hint {{ color: #6e7681; font-size: 11px; margin-top: 3px; }}
  .rows {{ margin-top: 7px; }}
  .row {{ display: flex; align-items: center; gap: 8px; padding: 8px 9px;
         border-radius: 6px; color: #c9d1d9; font-size: 13px; }}
  .row-selected {{ background: #262c36; color: #f0f6fc; }}
  .row-hover {{ background: #21262d; }}
  .cursor {{ position: absolute; left: -7px; top: 15px; }}
  .check {{ width: 12px; color: #3fb950; font-size: 12px; }}
  .name {{ flex: 1; }}
  .tag {{ color: #8b949e; font-size: 10px; border: 1px solid #3d444d; border-radius: 4px;
         padding: 1px 5px; }}
  .tag-selected {{ color: #58a6ff; border-color: #1f4b7a; }}
  .eye-empty {{ width: 13px; }}
  .ctx {{ color: #6e7681; font-size: 11px; font-variant-numeric: tabular-nums;
         min-width: 40px; text-align: right; }}
  .ctx-wide {{ color: #3fb950; }}
  .note {{ margin-top: 12px; color: #6e7681; font-size: 11.5px; }}
</style></head>
<body><div class="wrap">
  <div class="win">
    <div class="bar">
      <span class="dot" style="background:#f85149"></span>
      <span class="dot" style="background:#d29922"></span>
      <span class="dot" style="background:#3fb950"></span>
      <span class="title">{copy['window_title']}</span>
    </div>
    <div class="body">
      <div class="chat">
        {steps}
        <div class="foot">
          <div class="foot-top">{copy['input_placeholder']}</div>
          <div class="foot-bottom">
            <span class="pill">GPT-6 Sol <span class="caret">▲</span></span>
          </div>
        </div>
      </div>
    </div>
  </div>
  <div class="picker" style="top:196px; right:44px;">
    <div class="picker-head">
      <div class="picker-title">{copy['picker_title']}</div>
      <div class="picker-hint">{copy['picker_hint']}</div>
    </div>
    <div class="rows">
      {model_rows(copy)}
    </div>
  </div>
  <div class="note">{copy['footnote']}</div>
</div></body></html>
"""


def compaction_flow(copy):
    def panel(title, steps, tone):
        rows = "".join(
            '<div class="step"><span class="idx">{i}</span>{t}</div>'.format(
                i=i + 1, t=html.escape(text))
            for i, text in enumerate(steps))
        return ('<div class="panel panel-%s"><div class="panel-title">%s</div>%s</div>'
                % (tone, html.escape(title), rows))

    return f"""<!doctype html>
<html lang="{'zh-CN' if copy is COPY['zh'] else 'en'}"><head><meta charset="utf-8">
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: #0d1117; font-family: {FONT}; -webkit-font-smoothing: antialiased; }}
  .wrap {{ width: 820px; padding: 22px; }}
  .cols {{ display: flex; gap: 16px; }}
  .panel {{ flex: 1; background: #161b22; border: 1px solid #30363d; border-radius: 8px;
           padding: 16px 17px 18px; }}
  .panel-bad {{ border-color: #4d2b2b; }}
  .panel-good {{ border-color: #1f4b2c; }}
  .panel-title {{ font-size: 13.5px; font-weight: 600; margin-bottom: 13px; }}
  .panel-bad .panel-title {{ color: #f85149; }}
  .panel-good .panel-title {{ color: #3fb950; }}
  .step {{ display: flex; gap: 10px; color: #8b949e; font-size: 12.5px; line-height: 1.45;
          padding: 7px 0; border-bottom: 1px solid #21262d; }}
  .step:last-child {{ border-bottom: 0; }}
  .idx {{ color: #484f58; font-size: 11px; min-width: 13px; }}
  .panel-bad .step:last-child {{ color: #f0a6a0; }}
  .panel-good .step:last-child {{ color: #7ee787; }}
  .note {{ margin-top: 13px; color: #6e7681; font-size: 11.5px; }}
</style></head>
<body><div class="wrap">
  <div class="cols">
    {panel(copy['before_title'], copy['before_steps'], 'bad')}
    {panel(copy['after_title'], copy['after_steps'], 'good')}
  </div>
  <div class="note">{copy['footnote']}</div>
</div></body></html>
"""


def switching_flow(copy):
    """Selling point 2: where the switch happens, and how many steps it costs."""
    def panel(title, sub, steps, tone):
        rows = "".join(
            '<div class="step"><span class="idx">{i}</span>{t}</div>'.format(
                i=i + 1, t=html.escape(text))
            for i, text in enumerate(steps))
        return ('<div class="panel panel-%s"><div class="panel-head">'
                '<div class="panel-title">%s</div>'
                '<div class="panel-sub">%s</div></div>%s</div>'
                % (tone, html.escape(title), html.escape(sub), rows))

    return f"""<!doctype html>
<html lang="{'zh-CN' if copy is COPY['zh'] else 'en'}"><head><meta charset="utf-8">
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: #0d1117; font-family: {FONT}; -webkit-font-smoothing: antialiased; }}
  .wrap {{ width: 820px; padding: 22px; }}
  .cols {{ display: flex; gap: 16px; align-items: flex-start; }}
  .panel {{ flex: 1; background: #161b22; border: 1px solid #30363d; border-radius: 8px;
           padding: 15px 17px 17px; }}
  .panel-slow {{ border-color: #4a3a1e; }}
  .panel-fast {{ border-color: #1f4b2c; }}
  .panel-head {{ margin-bottom: 11px; }}
  .panel-title {{ font-size: 14px; font-weight: 600; }}
  .panel-slow .panel-title {{ color: #d29922; }}
  .panel-fast .panel-title {{ color: #3fb950; }}
  .panel-sub {{ color: #6e7681; font-size: 11.5px; margin-top: 4px; }}
  .step {{ display: flex; gap: 10px; color: #8b949e; font-size: 12.5px; line-height: 1.45;
          padding: 6px 0; border-bottom: 1px solid #21262d; }}
  .step:last-child {{ border-bottom: 0; }}
  .idx {{ color: #484f58; font-size: 11px; min-width: 13px; }}
  .panel-slow .step:last-child {{ color: #c9a227; }}
  .panel-fast .step:last-child {{ color: #7ee787; }}
  .note {{ margin-top: 13px; color: #6e7681; font-size: 11.5px; line-height: 1.55; }}
</style></head>
<body><div class="wrap">
  <div class="cols">
    {panel(copy['switch_title_a'], copy['switch_sub_a'], copy['switch_steps_a'], 'slow')}
    {panel(copy['switch_title_b'], copy['switch_sub_b'], copy['switch_steps_b'], 'fast')}
  </div>
  <div class="note">{copy['switch_note']}</div>
</div></body></html>
"""


def capabilities(copy):
    """Selling point 3: Codex's own features keep working on another model."""
    rows = "".join(
        '<div class="row"><span class="cap">%s</span>'
        '<span class="tick">✓</span><span class="val">%s</span></div>'
        % (html.escape(name), html.escape(value))
        for name, value in copy["caps"])
    return f"""<!doctype html>
<html lang="{'zh-CN' if copy is COPY['zh'] else 'en'}"><head><meta charset="utf-8">
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: #0d1117; font-family: {FONT}; -webkit-font-smoothing: antialiased; }}
  .wrap {{ width: 820px; padding: 22px; }}
  .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px;
          padding: 18px 20px 20px; }}
  .title {{ font-size: 15px; font-weight: 600; color: #e6edf3; }}
  .head {{ display: flex; justify-content: space-between; margin: 13px 0 6px;
          padding-bottom: 8px; border-bottom: 1px solid #30363d;
          color: #6e7681; font-size: 11px; letter-spacing: .05em; text-transform: uppercase; }}
  .row {{ display: flex; align-items: center; padding: 8px 0;
         border-bottom: 1px solid #21262d; font-size: 13px; }}
  .row:last-child {{ border-bottom: 0; }}
  .cap {{ color: #c9d1d9; min-width: 168px; }}
  .tick {{ color: #3fb950; font-size: 12px; min-width: 26px; }}
  .val {{ color: #8b949e; font-size: 12px; }}
  .note {{ margin-top: 13px; color: #6e7681; font-size: 11.5px; line-height: 1.55; }}
</style></head>
<body><div class="wrap">
  <div class="card">
    <div class="title">{copy['caps_title']}</div>
    <div class="head"><span>{copy['caps_col']}</span><span>{copy['caps_right']}</span></div>
    {rows}
  </div>
  <div class="note">{copy['caps_note']}</div>
</div></body></html>
"""


def render(chrome, page, target, width, height, scale=2):
    source = OUT / ".render-tmp.html"
    source.write_text(page, encoding="utf-8")
    try:
        subprocess.run([
            chrome, "--headless", "--disable-gpu", "--hide-scrollbars",
            "--force-device-scale-factor=%d" % scale,
            "--default-background-color=00000000",
            "--screenshot=%s" % target,
            "--window-size=%d,%d" % (width, height),
            source.as_uri(),
        ], check=True, capture_output=True)
    finally:
        source.unlink(missing_ok=True)
    return target


def main():
    chrome = find_chrome()
    print("renderer:", chrome)
    jobs = []
    for lang, copy in COPY.items():
        jobs.append((f"model-picker.{lang}.png", model_picker(copy), 864, 620))
        jobs.append((f"compaction.{lang}.png", compaction_flow(copy), 864, 232))
        jobs.append((f"switching.{lang}.png", switching_flow(copy), 864, 300))
        jobs.append((f"capabilities.{lang}.png", capabilities(copy), 864, 512))
    for name, page, width, height in jobs:
        target = OUT / name
        render(chrome, page, target, width, height)
        print("wrote %-28s %6d B" % (name, target.stat().st_size))
    return 0


if __name__ == "__main__":
    sys.exit(main())

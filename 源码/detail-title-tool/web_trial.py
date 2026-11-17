"""Local, review-only viewer for browser-operated ChatGPT pilot artifacts.

Browser authentication and submissions remain in the user's visible browser.
This module does not read cookies, call private endpoints, or submit generations.
"""
import json
import re
from pathlib import Path

from flask import abort, g, render_template_string, send_file


FILES = {'before.png', 'background.png', 'candidate.png', 'receipt.json', 'analysis.txt'}
PAGE = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ChatGPT 首屏试点</title><link rel="stylesheet" href="/static/web-trial.css"></head>
<body><main><a href="/">返回商品详情页工作台</a><h1>ChatGPT 首屏试点</h1>
<p>{{ code }} · {{ receipt.get('name', '商品') }} · 待审核设计候选</p>
<p>网页登录操作由当前聊天驱动；本页用于取回结果和对照审核，不会自动发起生成。</p>
<p>未确认成稿 · 正式 Logo 与文案仍待核准 · 当前不提供可编辑 PSD</p>
{% if receipt.get('conversation_url') %}<p><a href="{{ receipt['conversation_url'] }}" target="_blank" rel="noopener noreferrer">查看 ChatGPT 原始对话</a></p>{% endif %}
<div class="comparison">{% for name, title in [('candidate.png','首屏候选'), ('before.png','原首屏')] %}
<section><h2>{{ title }}</h2>{% if name in available %}<a href="{{ base }}/files/{{ name }}"><img src="{{ base }}/files/{{ name }}" alt="{{ title }}"></a>{% else %}<p>等待结果取回</p>{% endif %}</section>{% endfor %}</div>
{% if 'background.png' in available %}<p><a href="{{ base }}/files/background.png">查看生成场景底图</a> · <a href="{{ base }}/files/receipt.json">查看来源记录</a></p>{% endif %}
<h2>分析记录</h2><pre>{{ analysis }}</pre></main></body></html>'''


def register(app, directory):
    root = Path(directory).resolve()

    def folder(code):
        if g.owner != 'local' or not re.fullmatch(r'[0-9]{10,16}', code):
            abort(404)
        path = (root / code).resolve()
        if not path.is_relative_to(root) or not path.is_dir():
            abort(404, '该商品还没有网页首屏试点记录。')
        return path

    @app.get('/web-trial/<code>')
    def web_trial(code):
        path = folder(code)
        receipt = json.loads((path / 'receipt.json').read_text(encoding='utf-8'))
        url = receipt.get('conversation_url', '')
        if not re.fullmatch(r'https://chatgpt\.com/c/[a-zA-Z0-9-]+', url):
            receipt.pop('conversation_url', None)
        analysis = path / 'analysis.txt'
        return render_template_string(PAGE, code=code, receipt=receipt,
            analysis=analysis.read_text(encoding='utf-8') if analysis.is_file() else '等待网页分析完成。',
            available={name for name in FILES if (path / name).is_file()}, base=f'/web-trial/{code}')

    @app.get('/web-trial/<code>/files/<name>')
    def web_trial_file(code, name):
        path = folder(code)
        if name not in FILES:
            abort(404)
        target = (path / name).resolve()
        if not target.is_relative_to(path) or not target.is_file():
            abort(404)
        return send_file(target, conditional=True)

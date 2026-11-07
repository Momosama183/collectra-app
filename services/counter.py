"""Collectra usage counter for the mobile web app.

Counts one "visit" each time a real person opens the app's main page, and sends it to the same Supabase
database as the web checker (via the Vercel site's /api/log). Results are shown on
https://collectra-marketplace.vercel.app/stats in the "แอปมือถือ" section.

- Card checks are NOT counted here: the app's checks go through the Vercel checker, which already counts them
  (tagged as the app because of the 'collectra-mobile' User-Agent). Counting here too would double them.
- It never slows a page down (sent in a background thread) and never raises: if Vercel/Supabase is
  unreachable, the app behaves exactly as before.
- Turn it off with COLLECTRA_COUNTER=off. Change the target with COLLECTRA_COUNTER_URL.
"""
from __future__ import annotations
import json, os, re, secrets, threading, urllib.request
from urllib.parse import urlparse, parse_qs

ENABLED = os.getenv('COLLECTRA_COUNTER', 'on').strip().lower() not in ('off', '0', 'false', 'no')
LOG_URL = (os.getenv('COLLECTRA_COUNTER_URL')
           or (os.getenv('COLLECTRA_WEB_AI_URL', 'https://collectra-marketplace.vercel.app').rstrip('/') + '/api/log'))
COOKIE = 'cx_vid'
BOT = re.compile(r'bot|crawl|spider|slurp|preview|facebookexternalhit|line-poker|whatsapp|telegram|headless|'
                 r'lighthouse|curl|wget|python-urllib|uptime|render|monitor|health', re.I)
PAGES = ('', '/', '/index.html')


def _post(visitor: str, src: str) -> None:
    try:
        body = json.dumps({'kind': 'visit', 'visitor': visitor, 'src': src}).encode()
        req = urllib.request.Request(LOG_URL, data=body, method='POST',
                                     headers={'Content-Type': 'application/json', 'User-Agent': 'collectra-counter/1'})
        with urllib.request.urlopen(req, timeout=8) as r:
            r.read()
            print('[counter] logged', r.status, flush=True)
    except Exception as e:
        print('[counter] log failed:', repr(e)[:200], flush=True)   # a counter must never break the app


def _count_visit(h) -> None:
    path = urlparse(h.path).path
    if path not in PAGES:
        return
    ua = h.headers.get('User-Agent', '') or ''
    print('[counter] page-open ua=%s dest=%s' % (ua[:90], h.headers.get('Sec-Fetch-Dest')), flush=True)
    if not ua or BOT.search(ua):
        print('[counter] skipped (bot/empty UA)', flush=True)
        return
    if 'text/html' not in (h.headers.get('Accept', '') or ''):
        print('[counter] skipped (not html)', flush=True)
        return
    dest = h.headers.get('Sec-Fetch-Dest')
    if dest and dest != 'document':
        print('[counter] skipped (dest=%s)' % dest, flush=True)
        return
    if re.search(r'prefetch|prerender', (h.headers.get('Sec-Purpose', '') or '') + (h.headers.get('Purpose', '') or ''), re.I):
        return
    m = re.search(r'(?:^|;\s*)' + COOKIE + r'=([0-9a-f]{24})', h.headers.get('Cookie', '') or '')
    vid = m.group(1) if m else secrets.token_hex(12)
    if not m:
        orig_end = h.end_headers
        secure = '; Secure' if h.headers.get('X-Forwarded-Proto') == 'https' else ''

        def end_headers():
            h.send_header('Set-Cookie', f'{COOKIE}={vid}; Max-Age=31536000; Path=/; SameSite=Lax; HttpOnly{secure}')
            orig_end()
        h.end_headers = end_headers
    src = 'qr' if (parse_qs(urlparse(h.path).query).get('src', [''])[0] or '').lower() == 'qr' else ''
    threading.Thread(target=_post, args=(vid, src), daemon=True).start()


def install(handler_cls) -> None:
    """Wrap handler_cls.do_GET so page opens are counted. Call once, after the Handler class is defined."""
    if not ENABLED:
        return
    original = handler_cls.do_GET

    def do_GET(self):
        try:
            _count_visit(self)
        except Exception as e:
            print('[counter] error:', repr(e)[:200], flush=True)
        return original(self)
    handler_cls.do_GET = do_GET

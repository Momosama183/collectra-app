"""Card authenticity check that uses the live Collectra web AI system.

Instead of keeping a second copy of the prompt, this module sends the FRONT and BACK photos to the Collectra web
site (the same checker as the /verify page) and turns the answer into the format this app already understands.
The web system holds everything we tuned: the improved prompt, the 4-level verdict, the generic-template-back
(lomo) rule, the verified reference cards and the code-based image matching.

Settings (all optional, see .env.example):
  COLLECTRA_WEB_AI            on (default) / off  -> off = use the old built-in Claude path
  COLLECTRA_WEB_AI_URL        default https://collectra-marketplace.vercel.app
  COLLECTRA_AI_PROVIDER       claude (default) / openai / gemini
  COLLECTRA_AI_FALLBACKS      comma list tried in order if the main one fails (default: openai)
  COLLECTRA_WEB_AI_TIMEOUT    seconds per attempt (default 70)
  COLLECTRA_WEB_AI_BUDGET     max total seconds for one card (default 120)

Only the front and back photos are sent. The edges/corners and surface/logo/foil photos are still required by the
app and stored as evidence, but the web checker reads two views.
If every provider fails the card is sent to manual review, never auto-approved.
"""
from __future__ import annotations
import io, json, os, time, uuid, urllib.request, urllib.error
from pathlib import Path

DEFAULT_URL = 'https://collectra-marketplace.vercel.app'
ENDPOINTS = {
    'claude': '/app/api/check-card',
    'openai': '/app/api/check-card-openai',
    'gemini': '/app/api/check-card-gemini',
}
VERDICTS = ('Likely Authentic', 'Needs Manual Review', 'Suspicious', 'Likely Counterfeit')
MAX_SIDE = 1800            # px, longest side sent to the web checker
MAX_BYTES = 1_900_000      # per image; the web host rejects request bodies over ~4.5 MB in total


def enabled() -> bool:
    return os.getenv('COLLECTRA_WEB_AI', 'on').strip().lower() not in ('off', '0', 'false', 'no')


def base_url() -> str:
    return (os.getenv('COLLECTRA_WEB_AI_URL', '') or DEFAULT_URL).strip().rstrip('/')


def provider_chain() -> list[str]:
    main = (os.getenv('COLLECTRA_AI_PROVIDER', 'claude') or 'claude').strip().lower()
    rest = (os.getenv('COLLECTRA_AI_FALLBACKS', 'openai') or '').split(',')
    chain = []
    for p in [main] + [x.strip().lower() for x in rest]:
        if p in ENDPOINTS and p not in chain:
            chain.append(p)
    return chain or ['claude']


def _prep_image(path: Path) -> bytes:
    """Shrink a photo so two of them fit in one request. Falls back to the original bytes if Pillow fails."""
    raw = Path(path).read_bytes()
    try:
        from PIL import Image, ImageOps
        im = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert('RGB')
        im.thumbnail((MAX_SIDE, MAX_SIDE))
        for q in (90, 82, 74, 66, 58):
            buf = io.BytesIO(); im.save(buf, 'JPEG', quality=q, optimize=True)
            if buf.tell() <= MAX_BYTES:
                break
        return buf.getvalue()
    except Exception:
        return raw


def _multipart(files: dict[str, bytes]):
    boundary = '----collectra' + uuid.uuid4().hex
    parts = []
    for name, data in files.items():
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{name}.jpg"\r\n'
                      f'Content-Type: image/jpeg\r\n\r\n').encode() + data + b'\r\n')
    parts.append(f'--{boundary}--\r\n'.encode())
    return b''.join(parts), f'multipart/form-data; boundary={boundary}'


def _post(provider: str, front: bytes, back: bytes, timeout: float) -> dict:
    body, ctype = _multipart({'front': front, 'back': back})
    req = urllib.request.Request(base_url() + ENDPOINTS[provider], data=body, method='POST',
                                 headers={'Content-Type': ctype, 'Accept': 'application/json',
                                          'User-Agent': 'collectra-mobile/4'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode('utf-8')).get('error', '')
        except Exception:
            msg = ''
        err = RuntimeError(f'HTTP {e.code} {msg}'[:300]); err.retryable = e.code >= 500  # type: ignore[attr-defined]
        raise err
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        err = RuntimeError(f'network: {e}'[:300]); err.retryable = True  # type: ignore[attr-defined]
        raise err
    score = data.get('confidence_score')
    if not isinstance(score, (int, float)) or data.get('verdict') not in VERDICTS:
        err = RuntimeError('unexpected response from web checker'); err.retryable = False  # type: ignore[attr-defined]
        raise err
    return data


def _to_app_result(data: dict, provider: str, attempts: list, quality: dict, issues: list, views: list, ref_count: int):
    conf = max(0, min(100, int(round(data['confidence_score']))))
    obs = data.get('observations') or []
    checks = [('✓ ' if o.get('type') == 'positive' else '⚠ ') + str(o.get('text', '')).strip()
              for o in obs if isinstance(o, dict) and o.get('text')]
    return {
        'verdict': data['verdict'],
        'confidence': conf,
        'summary': str(data.get('summary', '')).strip() or 'AI screening completed.',
        'details': {
            'checks': checks[:10],
            'verdict_th': data.get('verdict_th', ''),
            'disclaimer': data.get('disclaimer', ''),
            'observations': obs,
            'web_provider': provider,
            'attempts': attempts,
            'photo_quality': quality,
            'issues': issues,
            'views_sent_to_web_ai': ['front', 'back'],
            'required_views_received': views,
            'local_reference_count_ignored': ref_count,
        },
        'provider': f'collectra-web:{provider}',
    }


def verify(front: Path, back: Path, quality: dict, issues: list, views: list, ref_count: int) -> dict:
    t_all = float(os.getenv('COLLECTRA_WEB_AI_TIMEOUT', '70') or 70)
    budget = float(os.getenv('COLLECTRA_WEB_AI_BUDGET', '120') or 120)
    start = time.time()
    fb, bb = _prep_image(front), _prep_image(back)
    attempts: list[dict] = []
    for provider in provider_chain():
        for n in (1, 2):
            left = budget - (time.time() - start)
            if left < 10:
                break
            try:
                data = _post(provider, fb, bb, min(t_all, left))
                attempts.append({'provider': provider, 'try': n, 'ok': True})
                return _to_app_result(data, provider, attempts, quality, issues, views, ref_count)
            except Exception as e:  # noqa: BLE001
                attempts.append({'provider': provider, 'try': n, 'ok': False, 'error': str(e)})
                if not getattr(e, 'retryable', False):
                    break
                time.sleep(2)
    return {
        'verdict': 'Needs Manual Review',
        'confidence': 0,
        'summary': 'The AI service could not complete this check, so the card was sent to manual review.',
        'details': {'attempts': attempts, 'photo_quality': quality, 'issues': issues, 'error': 'web_ai_unavailable',
                    'required_views_received': views},
        'provider': 'collectra-web-error',
    }

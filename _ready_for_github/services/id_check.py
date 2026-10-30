"""Advisory checks on a seller's ID registration photos.

This does NOT decide whether the selfie and the ID show the same person: that is a face-comparison
decision and is left to the human admin, who sees both photos side by side. What it does check:
  - the one-time code written on paper is visible in the selfie (stops re-using someone else's old selfie)
  - the selfie looks like a live person actually holding a card (not a screenshot or a photo of a photo)
  - the ID looks like a real card rather than a screen/paper copy
  - the ID number and name printed on the card match what the seller typed
It only runs when COLLECTRA_ID_AI=on AND ANTHROPIC_API_KEY is set, because it sends the ID photos to the
Anthropic API. Results are shown to the admin as hints; they never approve or reject anyone.
"""
from __future__ import annotations
import base64, json, os, re
from pathlib import Path

SYSTEM = (
    "You assist a human reviewer who verifies marketplace seller identities. You are given two images: "
    "IMAGE 1 is a photo of a national ID card. IMAGE 2 is a selfie in which the person should hold the same "
    "card and a handwritten paper showing a short numeric code. Do NOT judge whether the faces are the same "
    "person; a human does that. Report only what is visibly true. Be conservative: use 'unsure' when unclear. "
    "Reply with ONE JSON object and nothing else, with exactly these keys: "
    '"selfie_shows_live_person" ("yes"|"no"|"unsure"), "person_holds_id_card" ("yes"|"no"|"unsure"), '
    '"code_visible" ("yes"|"no"|"unsure"), "code_read" (digits you read on the paper, or ""), '
    '"card_looks_genuine" ("yes"|"no"|"unsure"), "screen_or_paper_copy_suspected" (true|false), '
    '"id_number_read" (the 13-digit number printed on the card in IMAGE 1, digits only, or ""), '
    '"name_read" (name printed on the card, or ""), "notes" (one short sentence).'
)

def _block(path: Path):
    ext = path.suffix.lower()
    mt = {'.png': 'image/png', '.webp': 'image/webp'}.get(ext, 'image/jpeg')
    return {'type': 'image', 'source': {'type': 'base64', 'media_type': mt, 'data': base64.b64encode(path.read_bytes()).decode()}}

def enabled(ai) -> bool:
    return os.getenv('COLLECTRA_ID_AI', '').strip().lower() in ('on', '1', 'true', 'yes') and bool(getattr(ai, 'key', ''))

def _name_overlap(typed: str, read: str) -> str:
    t = [w for w in re.split(r'\W+', typed.lower()) if len(w) > 1]
    r = read.lower()
    if not t or not read.strip(): return 'unsure'
    return 'yes' if any(w in r for w in t) else 'no'

def run(ai, id_path: Path, selfie_path: Path, challenge: str, typed_name: str, typed_id: str) -> dict:
    if not enabled(ai):
        return {'status': 'not_run', 'note': 'Automatic ID hints are switched off. Review the photos by eye.'}
    try:
        text = ai._call_claude(SYSTEM, [_block(id_path), _block(selfie_path),
                                        {'type': 'text', 'text': 'Analyse these two images and return the JSON.'}], 600)
        m = re.search(r'\{.*\}', text, re.S)
        raw = json.loads(m.group(0)) if m else {}
    except Exception as e:  # never let a failed hint block registration
        return {'status': 'error', 'note': f'Automatic check failed ({type(e).__name__}). Review by eye.'}
    code_read = re.sub(r'\D', '', str(raw.get('code_read', '')))
    id_read = re.sub(r'\D', '', str(raw.get('id_number_read', '')))
    checks = {
        'code_matches': 'yes' if code_read and code_read == challenge else ('no' if code_read else 'unsure'),
        'id_number_matches': 'yes' if len(id_read) == 13 and id_read == typed_id else ('no' if len(id_read) == 13 else 'unsure'),
        'name_matches': _name_overlap(typed_name, str(raw.get('name_read', ''))),
        'live_person': raw.get('selfie_shows_live_person', 'unsure'),
        'holds_card': raw.get('person_holds_id_card', 'unsure'),
        'card_genuine': raw.get('card_looks_genuine', 'unsure'),
        'copy_suspected': bool(raw.get('screen_or_paper_copy_suspected', False)),
    }
    bad = checks['code_matches'] == 'no' or checks['id_number_matches'] == 'no' or checks['live_person'] == 'no' \
        or checks['card_genuine'] == 'no' or checks['copy_suspected'] or checks['holds_card'] == 'no'
    good = all(checks[k] == 'yes' for k in ('code_matches', 'id_number_matches', 'live_person', 'holds_card', 'card_genuine')) \
        and not checks['copy_suspected']
    return {'status': 'looks_ok' if good else ('flagged' if bad else 'unclear'), 'checks': checks,
            'notes': str(raw.get('notes', ''))[:300]}

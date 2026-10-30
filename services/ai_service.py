from __future__ import annotations
import os, json, base64, urllib.request, re, shutil, subprocess, tempfile
from pathlib import Path
try:
    from services import web_checker
except ImportError:  # run from inside services/
    import web_checker


def _read_env_file(root: Path):
    env_path = root / '.env'
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, val = line.split('=', 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def _media_type(path: Path) -> str:
    ext = path.suffix.lower().lstrip('.') or 'jpeg'
    if ext == 'jpg':
        ext = 'jpeg'
    if ext not in ('jpeg', 'png', 'gif', 'webp'):
        ext = 'jpeg'
    return f'image/{ext}'


def image_to_base64_block(path: Path) -> dict:
    """Anthropic Messages API image content block from a file on disk."""
    data = base64.b64encode(path.read_bytes()).decode('ascii')
    return {
        'type': 'image',
        'source': {'type': 'base64', 'media_type': _media_type(path), 'data': data},
    }


def bytes_to_base64_block(data: bytes, media_type: str) -> dict:
    """Build an Anthropic Messages API image content block directly from raw bytes (no disk write)."""
    b64 = base64.b64encode(data).decode('ascii')
    if not media_type or not media_type.startswith('image/'):
        media_type = 'image/jpeg'
    return {'type': 'image', 'source': {'type': 'base64', 'media_type': media_type, 'data': b64}}


def local_quality(path: Path) -> dict:
    try:
        from PIL import Image, ImageStat, ImageFilter
        img = Image.open(path).convert('RGB')
        w, h = img.size
        gray = img.convert('L')
        brightness = ImageStat.Stat(gray).mean[0]
        sharpness = ImageStat.Stat(gray.filter(ImageFilter.FIND_EDGES)).var[0]
        issues = []
        if min(w, h) < 700:
            issues.append('Resolution is low; move closer and use a higher-resolution photo.')
        if brightness < 45:
            issues.append('Photo is too dark.')
        if brightness > 225:
            issues.append('Photo is overexposed or has strong glare.')
        if sharpness < 120:
            issues.append('Photo may be blurry; keep the phone steady and refocus.')
        score = 100 - min(60, len(issues) * 18)
        return {
            'width': w, 'height': h,
            'brightness': round(brightness, 1),
            'sharpness': round(sharpness, 1),
            'issues': issues,
            'quality_score': score,
        }
    except Exception as e:
        return {'issues': ['Image quality analysis unavailable.'], 'quality_score': 60, 'error': str(e)}


def _clean_json_text(raw: str) -> str:
    raw = raw.strip()
    raw = re.sub(r'^```json', '', raw, flags=re.I).strip()
    raw = re.sub(r'^```', '', raw).strip()
    raw = re.sub(r'```$', '', raw).strip()
    return raw


def _extract_json(text: str) -> dict:
    cleaned = _clean_json_text(text)
    match = re.search(r'\{.*\}', cleaned, flags=re.S)
    return json.loads(match.group(0) if match else cleaned)


def ffmpeg_available() -> bool:
    return bool(shutil.which('ffmpeg')) and bool(shutil.which('ffprobe'))


def _video_duration(path: Path) -> float:
    out = subprocess.run(
        ['ffprobe', '-v', 'quiet', '-show_entries', 'format=duration', '-of', 'csv=p=0', str(path)],
        capture_output=True, text=True, timeout=30,
    )
    try:
        return max(0.5, float(out.stdout.strip()))
    except Exception:
        return 6.0


def extract_frames(path: Path, count: int = 8) -> list[Path]:
    """Grab evenly-spaced frames across the video's timeline as JPEGs in a temp dir."""
    if not ffmpeg_available():
        raise RuntimeError('ffmpeg/ffprobe not found on this server. Install ffmpeg to enable video AI checks.')
    duration = _video_duration(path)
    tmpdir = Path(tempfile.mkdtemp(prefix='collectra_frames_'))
    frames = []
    fractions = [i / (count - 1) for i in range(count)] if count > 1 else [0.5]
    fractions = [min(max(f, 0.02), 0.98) for f in fractions]
    for i, frac in enumerate(fractions):
        ts = round(duration * frac, 2)
        out_path = tmpdir / f'frame_{i:02d}.jpg'
        subprocess.run(
            ['ffmpeg', '-y', '-ss', str(ts), '-i', str(path), '-frames:v', '1', '-q:v', '3', str(out_path)],
            capture_output=True, timeout=30,
        )
        if out_path.exists() and out_path.stat().st_size > 0:
            frames.append(out_path)
    if not frames:
        raise RuntimeError('Could not extract any frames from the uploaded video.')
    return frames


def cleanup_frames(frames: list[Path]):
    if frames:
        shutil.rmtree(frames[0].parent, ignore_errors=True)


SYSTEM_PROMPT_VERIFY = """You are Collectra's AI-assisted authenticity risk-screening assistant for a \
collectible-card marketplace (K-pop and T-pop photo cards). You do NOT guarantee authenticity — this is a \
risk screen, and every result should be traceable to specific visual evidence.

The seller/buyer submits four views of the SAME physical card:
1. front — full front, flat and centered
2. back — full back, all corners visible
3. edges_corners — close-up of the borders and corner shapes
4. surface_logo_foil — close-up of surface texture, print detail, logo placement, or foil

You may also receive up to three verified reference images of the same card/set — treat these as ground \
truth for direct comparison when present.

## How to score

Score EACH of the 8 criteria below from 0-100, independently. For each one, first write one short sentence \
of concrete visual reasoning (what you actually see, not a vague impression), THEN give the number. Anchor \
your number against these reference points so scores stay meaningfully spread out instead of clustering in \
the middle:
- 90-100 = clearly strong, no visible issues at all on this specific criterion
- 60-75 = mostly fine, one minor imperfection or something you can't fully confirm from the angle given
- 30-50 = a real, specific problem you can point to
- 0-20 = a serious, unambiguous red flag on this criterion

Criteria:
1. proportions — does the card's shape/aspect ratio look like a standard trading-card cut (not stretched, \
   not oddly cropped)?
2. borders — are the borders around the printed image even and symmetric on all sides?
3. corners — do the corners have a clean, consistent die-cut shape (not hand-cut, frayed, or mismatched)?
4. print_quality — is the print sharp with no visible dot pattern, pixelation, moiré, or screen-recapture \
   artifacts (a photo of a screen or a low-res reprint looks soft/dotted under close inspection)?
5. color_accuracy — do colors look like a properly calibrated print (not washed out, overly saturated, or \
   color-shifted in a way that suggests a bad scan/reprint), accounting for normal camera/lighting variance?
6. logo_text — is logo/text placement, font, and spacing consistent with a professional print (not shifted, \
   wrong font, misspelled, or blurry)?
7. surface_consistency — does the surface finish (matte/gloss/foil) look consistent and intentional rather \
   than patchy, inconsistent, or like a sticker/overlay?
8. front_back_consistency — do the front and back plausibly belong to the same official release (matching \
   era/company/print style), with no signs the two sides were photographed from different cards?

## Red flags

Separately, set "major_red_flag" to true ONLY if you see at least one CLEAR, specific, unambiguous sign of \
inauthenticity (e.g. visible moiré/screen pattern, obviously wrong font, clearly mismatched front/back, \
visible pixelation under the close-up shots). Do not set this for mere uncertainty or a slightly awkward \
photo angle — only for something you could point to and defend.

## Reference images

If reference images were provided, add "reference_match_score" (0-100: how well the submitted card matches \
the reference in print details, not just the general design) and briefly say what you compared. If no \
references were provided, omit this field entirely — do not guess at how a card "should" look without one.

Respond with ONLY a JSON object, no markdown fences, no preamble:
{
  "scores": {
    "proportions": {"reasoning": "<one sentence>", "score": <0-100>},
    "borders": {"reasoning": "<one sentence>", "score": <0-100>},
    "corners": {"reasoning": "<one sentence>", "score": <0-100>},
    "print_quality": {"reasoning": "<one sentence>", "score": <0-100>},
    "color_accuracy": {"reasoning": "<one sentence>", "score": <0-100>},
    "logo_text": {"reasoning": "<one sentence>", "score": <0-100>},
    "surface_consistency": {"reasoning": "<one sentence>", "score": <0-100>},
    "front_back_consistency": {"reasoning": "<one sentence>", "score": <0-100>},
    "reference_match_score": <0-100, omit key entirely if no references given>
  },
  "major_red_flag": true | false,
  "red_flag_reason": "<empty string if false, else the specific unambiguous issue>",
  "summary": "<1-2 sentence plain-language summary of the overall picture>",
  "checks": ["<short positive observation>", ...],
  "warnings": ["<short concern, if any>", ...]
}
Include 3-7 short entries across checks/warnings combined. Do not include a "verdict" or overall \
"confidence" field — those are computed separately from your per-criterion scores."""


SYSTEM_PROMPT_PACKING = """You are Collectra's AI packing-evidence reviewer for a K-pop/T-pop photo card \
marketplace. A seller recorded ONE continuous video while packing a card for shipment. You are given a \
sequence of frames sampled evenly across that video, in chronological order, plus the exact steps the \
seller was told to follow.

Your job:
1. Check whether the required steps appear to have been followed (front shown, back shown, edges/corners \
   shown, card kept visible while sleeving/boxing, and finally a sealed parcel with a shipping label/order \
   number shown).
2. Compare the card's visible design across the early frames (front/back/detail) to check it is the SAME \
   physical card throughout — look at the printed artwork, color, any visible wear, scratches, or print \
   marks. Flag if the card in later frames looks like a different card than the one shown at the start \
   (a swap), or if there's a suspicious cut/jump in continuity.
3. Confirm the final frames show a sealed/closed parcel with a visible shipping label or order reference — \
   this proves the card went into that specific parcel.
4. Separately, using only the clearest front/back/detail frames, give a preliminary authenticity read of the \
   card itself: print sharpness, color accuracy, edge/corner shape, logo and text placement, surface/foil \
   consistency, and any signs of screen recapture or low-quality reprint. This is a supplementary, lower- \
   resolution read compared to a dedicated photo authentication — treat it as such and prefer caution.

Only reason from what is visible in the frames — do not assume off-camera events. If frames are too few, \
blurry, or steps are unclear, prefer "Needs Review" rather than asserting certainty.

Respond with ONLY a JSON object, no markdown fences:
{
  "status": "Verified" | "Needs Review" | "Suspicious",
  "confidence": <integer 0-100>,
  "summary": "<1-2 sentence summary covering steps, continuity, AND the authenticity read>",
  "checks": ["<short observation confirming a step, continuity, or a positive authenticity sign>", ...],
  "warnings": ["<short concern, e.g. step skipped, possible swap, parcel not clearly sealed, authenticity red flag>", ...],
  "authenticity_status": "Likely Authentic" | "Needs Manual Review" | "Suspicious",
  "authenticity_confidence": <integer 0-100>
}
confidence >= 70 -> "Verified"; 40-69 -> "Needs Review"; < 40 -> "Suspicious". Same thresholds for \
authenticity_status/authenticity_confidence. If the video frames are too low-quality to judge authenticity \
at all, use "Needs Manual Review" with a low authenticity_confidence rather than guessing."""


SYSTEM_PROMPT_UNBOXING = """You are Collectra's AI unboxing-evidence reviewer for a K-pop/T-pop photo card \
marketplace. A buyer recorded ONE continuous video while unboxing a parcel. You are given frames sampled \
evenly across that video in chronological order, the steps the buyer was told to follow, and — when \
available — reference frames taken from the SELLER's packing video of the same order.

Your job:
1. Check whether the required steps appear followed (unopened parcel + label shown first, continuous \
   opening, card shown immediately on removal, then front/back/edges/corners of the received card).
2. If seller packing-video reference frames are provided, compare the card shown in THIS unboxing video \
   against the card shown in the packing video — same printed artwork, color, and any distinguishing marks. \
   State whether they appear to be the same physical card, appear different, or it's inconclusive from the \
   images given.
3. Note anything suggesting the parcel was already opened/tampered with before this video started.
4. Separately, using only the clearest front/back/detail frames of the RECEIVED card, give a preliminary \
   authenticity read: print sharpness, color accuracy, edge/corner shape, logo and text placement, surface/ \
   foil consistency, and any signs of screen recapture or low-quality reprint. This is a supplementary, \
   lower-resolution read compared to a dedicated photo authentication — treat it as such and prefer caution.

Only reason from what is visible — do not assume anything off-camera. Prefer "Needs Review" over false \
certainty when frames are unclear or reference frames are missing.

Respond with ONLY a JSON object, no markdown fences:
{
  "status": "Verified" | "Needs Review" | "Suspicious",
  "confidence": <integer 0-100>,
  "summary": "<1-2 sentence summary covering steps, card-matching, AND the authenticity read>",
  "checks": ["<short observation>", ...],
  "warnings": ["<short concern>", ...],
  "matches_packing_card": true | false | null,
  "authenticity_status": "Likely Authentic" | "Needs Manual Review" | "Suspicious",
  "authenticity_confidence": <integer 0-100>
}
confidence >= 70 -> "Verified"; 40-69 -> "Needs Review"; < 40 -> "Suspicious". Same thresholds for \
authenticity_status/authenticity_confidence. Use null for matches_packing_card only if no packing reference \
frames were given or it's genuinely inconclusive. If frames are too low-quality to judge authenticity at \
all, use "Needs Manual Review" with a low authenticity_confidence rather than guessing."""


class AIService:
    def __init__(self, root: Path):
        _read_env_file(root)
        self.root = root
        # Server-side only — never expose this key to public/app.js or any browser file.
        self.key = os.getenv('ANTHROPIC_API_KEY', '').strip()
        self.model = os.getenv('ANTHROPIC_MODEL', 'claude-sonnet-4-6').strip()

    @property
    def provider_name(self):
        if web_checker.enabled():
            return 'collectra-web:' + '>'.join(web_checker.provider_chain())
        return 'anthropic' if self.key else 'local-photo-quality'

    def _call_claude(self, system: str, content: list, max_tokens: int = 2000):
        body = {
            'model': self.model,
            'max_tokens': max_tokens,
            'system': system,
            'messages': [{'role': 'user', 'content': content}],
        }
        req = urllib.request.Request(
            'https://api.anthropic.com/v1/messages',
            data=json.dumps(body).encode('utf-8'),
            headers={
                'content-type': 'application/json',
                'x-api-key': self.key,
                'anthropic-version': '2023-06-01',
            },
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=90) as r:
            data = json.loads(r.read().decode('utf-8'))
        return ''.join(
            block.get('text', '') for block in data.get('content', []) if block.get('type') == 'text'
        ).strip()

    # Fixed weights for the 8 scored criteria — tuned so print quality (the strongest signal of a
    # screen-recapture/reprint) and logo/text placement carry the most weight. Computed in code, not
    # left to the model to self-aggregate, so results stay consistent run-to-run.
    _VERIFY_WEIGHTS = {
        'proportions': 0.12, 'borders': 0.12, 'corners': 0.10, 'print_quality': 0.18,
        'color_accuracy': 0.12, 'logo_text': 0.14, 'surface_consistency': 0.12, 'front_back_consistency': 0.10,
    }

    @staticmethod
    def _weighted_confidence(avg_scores: dict, has_reference: bool) -> float:
        w = AIService._VERIFY_WEIGHTS
        if has_reference and avg_scores.get('reference_match_score') is not None:
            ref_weight = 0.35
            scale = 1 - ref_weight
            total = ref_weight * avg_scores['reference_match_score']
            total += sum((weight * scale) * avg_scores.get(key, 50) for key, weight in w.items())
            return total
        return sum(weight * avg_scores.get(key, 50) for key, weight in w.items())

    # ---------------------------------------------------------------- photo verify
    def verify(self, front: Path, back: Path, edges: Path | None, surface: Path | None, category: str, card_name: str, refs: list[Path]):
        submitted = {k: v for k, v in {'front': front, 'back': back, 'edges_corners': edges, 'surface_logo_foil': surface}.items() if v}  # edges/surface are optional
        quality = {name: local_quality(path) for name, path in submitted.items()}
        all_issues = []
        for q in quality.values():
            all_issues += q.get('issues', [])

        # Collectra web AI (improved prompt, 4-level verdict, lomo rule, reference cards, code image matching).
        # Set COLLECTRA_WEB_AI=off in .env to use the older built-in Claude path below instead.
        if web_checker.enabled():
            return web_checker.verify(front, back, quality, all_issues, list(submitted.keys()), len(refs))

        if not self.key:
            confidence = max(35, min(65, int(sum(q.get('quality_score', 60) for q in quality.values()) / len(quality))))
            return {
                'verdict': 'Needs Manual Review',
                'confidence': confidence,
                'summary': 'All required card views reached the backend. A real vision provider is not configured yet, so this result checks capture quality only and does not guarantee authenticity.',
                'details': {'photo_quality': quality, 'issues': all_issues, 'required_views_received': list(submitted.keys()), 'reference_count': len(refs)},
                'provider': self.provider_name,
            }

        content = [
            {'type': 'text', 'text': f'Category: {category or "Unknown"}. Card name/set: {card_name or "Unknown"}. Verified reference images attached: {len(refs)}.'},
            {'type': 'text', 'text': 'Submitted FRONT:'}, image_to_base64_block(front),
            {'type': 'text', 'text': 'Submitted BACK:'}, image_to_base64_block(back),
        ]
        if edges:
            content += [{'type': 'text', 'text': 'Submitted EDGES/CORNERS detail:'}, image_to_base64_block(edges)]
        if surface:
            content += [{'type': 'text', 'text': 'Submitted SURFACE/LOGO/FOIL detail:'}, image_to_base64_block(surface)]
        if not (edges and surface):
            content.append({'type': 'text', 'text': 'The buyer did not supply every optional close-up. Judge only what was provided; do not penalise or fail the card just because a close-up is missing, but lower confidence a little.'})
        for ref in refs[:3]:
            content.append({'type': 'text', 'text': 'Verified reference image:'})
            content.append(image_to_base64_block(ref))
        content.append({'type': 'text', 'text': 'Score the submitted card per the system instructions. Respond with JSON only.'})

        has_reference = len(refs) > 0
        num_passes = max(1, min(3, int(os.getenv('ANTHROPIC_VERIFY_PASSES', '2') or '2')))
        criteria_keys = list(self._VERIFY_WEIGHTS.keys())

        try:
            passes = []
            for _ in range(num_passes):
                text = self._call_claude(SYSTEM_PROMPT_VERIFY, content, max_tokens=2000)
                parsed = _extract_json(text)
                scores = parsed.get('scores', {})
                clean_scores = {}
                for key in criteria_keys:
                    entry = scores.get(key, {})
                    val = entry.get('score', 50) if isinstance(entry, dict) else entry
                    clean_scores[key] = max(0, min(100, int(val if val is not None else 50)))
                if 'reference_match_score' in scores and scores['reference_match_score'] is not None:
                    clean_scores['reference_match_score'] = max(0, min(100, int(scores['reference_match_score'])))
                passes.append({
                    'scores': clean_scores,
                    'major_red_flag': bool(parsed.get('major_red_flag', False)),
                    'red_flag_reason': parsed.get('red_flag_reason', ''),
                    'summary': parsed.get('summary', ''),
                    'checks': parsed.get('checks', []),
                    'warnings': parsed.get('warnings', []),
                    'confidence': self._weighted_confidence(clean_scores, has_reference),
                })

            avg_scores = {}
            for key in criteria_keys:
                avg_scores[key] = sum(p['scores'][key] for p in passes) / len(passes)
            ref_scores = [p['scores']['reference_match_score'] for p in passes if 'reference_match_score' in p['scores']]
            if ref_scores:
                avg_scores['reference_match_score'] = sum(ref_scores) / len(ref_scores)

            base_confidence = self._weighted_confidence(avg_scores, has_reference)
            pass_confidences = [p['confidence'] for p in passes]
            spread = max(pass_confidences) - min(pass_confidences) if len(passes) > 1 else 0
            red_flag_count = sum(1 for p in passes if p['major_red_flag'])

            confidence = base_confidence
            notes = []
            if red_flag_count >= 2:
                confidence = min(confidence, 35)
                notes.append('Multiple independent passes flagged a specific authenticity concern.')
            elif red_flag_count == 1:
                confidence = min(confidence, 58)
                notes.append('One pass flagged a specific concern — recommend a closer manual look.')
            if spread > 25:
                confidence = min(confidence, 65)
                notes.append(f'Repeated analysis gave inconsistent results (spread of {spread:.0f} points), so this was capped as Needs Manual Review rather than risk a false "Likely Authentic".')

            confidence = max(0, min(100, round(confidence)))
            verdict = 'Likely Authentic' if confidence >= 70 else ('Needs Manual Review' if confidence >= 40 else 'Suspicious')

            checks, warnings = [], []
            for p in passes:
                checks += p['checks']; warnings += p['warnings']
                if p['major_red_flag'] and p['red_flag_reason']:
                    warnings.append(p['red_flag_reason'])
            checks = list(dict.fromkeys(checks))[:7]
            warnings = list(dict.fromkeys(warnings + notes))[:7]
            summary = passes[-1]['summary'] or 'AI screening completed.'

            return {
                'verdict': verdict,
                'confidence': confidence,
                'summary': summary,
                'details': {
                    'photo_quality': quality,
                    'checks': checks,
                    'warnings': warnings,
                    'scores': {k: round(v, 1) for k, v in avg_scores.items()},
                    'passes': num_passes,
                    'pass_confidences': [round(c, 1) for c in pass_confidences],
                    'required_views_received': list(submitted.keys()),
                    'reference_count': len(refs),
                },
                'provider': 'anthropic',
            }
        except Exception as e:
            return {
                'verdict': 'Needs Manual Review',
                'confidence': 45,
                'summary': 'The AI service could not complete this check, so the card was sent to manual review.',
                'details': {'photo_quality': quality, 'issues': all_issues, 'error': str(e), 'required_views_received': list(submitted.keys()), 'reference_count': len(refs)},
                'provider': 'anthropic-error',
            }

    # ---------------------------------------------------------------- packing video verify
    def verify_slip(self, image_path: Path) -> dict:
        """Sanity-check a payment slip photo/screenshot. This cannot confirm the transfer actually
        cleared in a bank's system — it only checks whether the image plausibly looks like a real
        transfer slip (has an amount, date/time, reference, bank branding) and isn't obviously
        edited or unrelated to a payment."""
        if not self.key:
            return {'status': 'Needs Review', 'confidence': 50,
                    'summary': 'Slip received. AI review is not configured yet (no ANTHROPIC_API_KEY), so this was not checked.',
                    'checks': [], 'warnings': ['AI provider not configured.']}
        system = """You are Collectra's AI payment-slip screener. The person uploaded a photo or \
screenshot that should be a bank transfer / PromptPay payment slip as proof of payment for an order.

You CANNOT confirm the transfer actually settled in any real banking system — you only have a \
single image. Your job is a plausibility screen:
1. Does this image look like an actual transfer slip/receipt (bank app screenshot, PromptPay \
   receipt, ATM slip, etc.), not an unrelated photo, a blank/empty image, or something else \
   entirely (e.g. a product photo, a random screenshot)?
2. Are the expected elements present: an amount, a date/time, and either a reference number, \
   sender/recipient name, or bank/app branding?
3. Any visible signs of digital editing — mismatched fonts, misaligned text, blurring/smudging \
   over specific fields, inconsistent shadows — that would suggest a number was edited?

Do not attempt to read or verify the exact amount matches any specific order total — you don't \
know the expected amount. Just report what you can see. Prefer "Needs Review" over false certainty.

Respond with ONLY a JSON object, no markdown fences:
{
  "status": "Verified" | "Needs Review" | "Suspicious",
  "confidence": <integer 0-100>,
  "summary": "<1-2 sentence summary>",
  "checks": ["<short observation, e.g. 'Amount and timestamp visible'>", ...],
  "warnings": ["<short concern, e.g. 'No bank branding visible', 'Possible edited amount field'>", ...]
}
confidence >= 70 -> "Verified" (plausible real slip); 40-69 -> "Needs Review"; < 40 -> "Suspicious" \
(doesn't look like a real slip at all, or shows clear tampering signs)."""
        try:
            content = [
                {'type': 'text', 'text': 'Payment slip image to screen:'},
                image_to_base64_block(image_path),
                {'type': 'text', 'text': 'Screen this slip per the system instructions. Respond with JSON only.'},
            ]
            text = self._call_claude(system, content, max_tokens=800)
            cleaned = _clean_json_text(text)
            match = re.search(r'\{.*\}', cleaned, flags=re.S)
            parsed = json.loads(match.group(0) if match else cleaned)
            status = parsed.get('status', 'Needs Review')
            if status not in ('Verified', 'Needs Review', 'Suspicious'):
                status = 'Needs Review'
            return {
                'status': status,
                'confidence': max(0, min(100, int(parsed.get('confidence', 50)))),
                'summary': parsed.get('summary', 'Payment slip reviewed.'),
                'checks': parsed.get('checks', []),
                'warnings': parsed.get('warnings', []),
            }
        except Exception as e:
            return {'status': 'Needs Review', 'confidence': 45,
                    'summary': 'The AI could not fully screen this slip, so it was sent to manual review.',
                    'checks': [], 'warnings': [str(e)]}

    def verify_packing_video(self, video_path: Path, guide_steps: list[str]) -> dict:
        if not self.key:
            return {
                'status': 'Needs Review', 'confidence': 50,
                'summary': 'Video received. AI review is not configured yet (no ANTHROPIC_API_KEY), so this was not analyzed.',
                'checks': [], 'warnings': ['AI provider not configured.'],
                'authenticity_status': 'Needs Manual Review', 'authenticity_confidence': 0,
            }
        frames = []
        try:
            frames = extract_frames(video_path, count=8)
            content = [{'type': 'text', 'text': 'Required packing steps the seller was shown, in order:\n' + '\n'.join(f'{i+1}. {s}' for i, s in enumerate(guide_steps))}]
            for i, f in enumerate(frames):
                content.append({'type': 'text', 'text': f'Frame {i+1} of {len(frames)} (chronological order):'})
                content.append(image_to_base64_block(f))
            content.append({'type': 'text', 'text': 'Analyze this packing video per the system instructions. Respond with JSON only.'})
            text = self._call_claude(SYSTEM_PROMPT_PACKING, content, max_tokens=1500)
            parsed = _extract_json(text)
            status = parsed.get('status', 'Needs Review')
            if status not in ('Verified', 'Needs Review', 'Suspicious'):
                status = 'Needs Review'
            astatus = parsed.get('authenticity_status', 'Needs Manual Review')
            if astatus not in ('Likely Authentic', 'Needs Manual Review', 'Suspicious'):
                astatus = 'Needs Manual Review'
            return {
                'status': status,
                'confidence': max(0, min(100, int(parsed.get('confidence', 50)))),
                'summary': parsed.get('summary', 'Packing video reviewed.'),
                'checks': parsed.get('checks', []),
                'warnings': parsed.get('warnings', []),
                'authenticity_status': astatus,
                'authenticity_confidence': max(0, min(100, int(parsed.get('authenticity_confidence', 50)))),
            }
        except Exception as e:
            return {'status': 'Needs Review', 'confidence': 45, 'summary': 'The AI could not fully analyze this packing video, so it was sent to manual review.', 'checks': [], 'warnings': [str(e)], 'authenticity_status': 'Needs Manual Review', 'authenticity_confidence': 45}
        finally:
            cleanup_frames(frames)

    # ---------------------------------------------------------------- unboxing video verify (+ cross-check)
    def verify_unboxing_video(self, video_path: Path, guide_steps: list[str], packing_video_path: Path | None) -> dict:
        if not self.key:
            return {
                'status': 'Needs Review', 'confidence': 50,
                'summary': 'Video received. AI review is not configured yet (no ANTHROPIC_API_KEY), so this was not analyzed.',
                'checks': [], 'warnings': ['AI provider not configured.'], 'matches_packing_card': None,
                'authenticity_status': 'Needs Manual Review', 'authenticity_confidence': 0,
            }
        frames, packing_frames = [], []
        try:
            frames = extract_frames(video_path, count=8)
            content = [{'type': 'text', 'text': 'Required unboxing steps the buyer was shown, in order:\n' + '\n'.join(f'{i+1}. {s}' for i, s in enumerate(guide_steps))}]
            for i, f in enumerate(frames):
                content.append({'type': 'text', 'text': f'Unboxing frame {i+1} of {len(frames)} (chronological order):'})
                content.append(image_to_base64_block(f))

            if packing_video_path and packing_video_path.exists():
                try:
                    packing_frames = extract_frames(packing_video_path, count=4)
                    content.append({'type': 'text', 'text': "Reference frames from the SELLER's packing video of this same order, for card-matching comparison only:"})
                    for i, f in enumerate(packing_frames):
                        content.append({'type': 'text', 'text': f'Packing reference frame {i+1} of {len(packing_frames)}:'})
                        content.append(image_to_base64_block(f))
                except Exception:
                    content.append({'type': 'text', 'text': 'No usable packing-video reference frames were available.'})
            else:
                content.append({'type': 'text', 'text': 'No packing video is on file for this order — skip the card-matching comparison and set matches_packing_card to null.'})

            content.append({'type': 'text', 'text': 'Analyze this unboxing video per the system instructions. Respond with JSON only.'})
            text = self._call_claude(SYSTEM_PROMPT_UNBOXING, content, max_tokens=1500)
            parsed = _extract_json(text)
            status = parsed.get('status', 'Needs Review')
            if status not in ('Verified', 'Needs Review', 'Suspicious'):
                status = 'Needs Review'
            astatus = parsed.get('authenticity_status', 'Needs Manual Review')
            if astatus not in ('Likely Authentic', 'Needs Manual Review', 'Suspicious'):
                astatus = 'Needs Manual Review'
            return {
                'status': status,
                'confidence': max(0, min(100, int(parsed.get('confidence', 50)))),
                'summary': parsed.get('summary', 'Unboxing video reviewed.'),
                'checks': parsed.get('checks', []),
                'warnings': parsed.get('warnings', []),
                'matches_packing_card': parsed.get('matches_packing_card', None),
                'authenticity_status': astatus,
                'authenticity_confidence': max(0, min(100, int(parsed.get('authenticity_confidence', 50)))),
            }
        except Exception as e:
            return {'status': 'Needs Review', 'confidence': 45, 'summary': 'The AI could not fully analyze this unboxing video, so it was sent to manual review.', 'checks': [], 'warnings': [str(e)], 'matches_packing_card': None, 'authenticity_status': 'Needs Manual Review', 'authenticity_confidence': 45}
        finally:
            cleanup_frames(frames)
            cleanup_frames(packing_frames)

    def check_step_frame(self, image_bytes: bytes, media_type: str, step_text: str, kind: str) -> dict:
        """Fast, cheap, single-frame check used live while the user is recording, so they get
        told immediately if a step looks wrong instead of finding out after the whole video uploads."""
        if not self.key:
            return {'ok': True, 'message': ''}  # AI not configured — don't block recording, just stay silent
        system = (
            "You are a fast, real-time camera-guidance check for Collectra, a collectible card "
            "marketplace. The seller/buyer is recording a continuous verification video and just "
            f"reached this required step: \"{step_text}\" (part of the {kind} video guide). You are "
            "given ONE still frame captured at this moment. Decide only whether this single frame is "
            "broadly consistent with that step being shown right now — do not judge authenticity, "
            "print quality, or anything beyond whether the requested thing is plausibly visible/in "
            "frame (e.g. if the step says show the back of the card, is a card back plausibly "
            "visible; if it says show the sealed parcel, is a parcel visible). Be lenient — camera "
            "framing is imperfect and this is a live guidance nudge, not a final verdict. Only say "
            "not-ok if the frame looks clearly unrelated to the step (e.g. step asks for the card "
            "but the frame is just a hand, an empty table, or the camera pointed away).\n\n"
            "Respond with ONLY a JSON object, no markdown fences:\n"
            '{"ok": true|false, "message": "<empty string if ok, else a short 3-8 word nudge like '
            '\'Point the camera at the card\' or \'Show the sealed parcel\'>"}'
        )
        try:
            content = [bytes_to_base64_block(image_bytes, media_type), {'type': 'text', 'text': 'Check this frame against the step above.'}]
            text = self._call_claude(system, content, max_tokens=120)
            cleaned = _clean_json_text(text)
            match = re.search(r'\{.*\}', cleaned, flags=re.S)
            parsed = json.loads(match.group(0) if match else cleaned)
            return {'ok': bool(parsed.get('ok', True)), 'message': str(parsed.get('message', ''))[:120]}
        except Exception:
            return {'ok': True, 'message': ''}  # never block the recording on an AI hiccup

    def chat(self, message: str) -> str:
        if not self.key:
            m = message.lower()
            if 'verify' in m or 'real' in m or 'authentic' in m:
                return 'Open Verify Card and follow all four required photo steps: front, back, edges/corners, and surface/logo/foil detail. If the result is uncertain, request human review.'
            if 'order' in m or 'track' in m:
                return 'Open Profile → Orders to see your order status, payment slip, tracking, packing evidence, and unboxing evidence.'
            if 'seller' in m or 'sell' in m:
                return 'Open Sell to apply as a seller. After approval, your store can upload real stock using its store token.'
            if 'packing' in m or 'unboxing' in m or 'video' in m:
                return 'Open your order and choose the guided Packing Video or Unboxing Video recorder. Keep the card and parcel continuously in frame and follow every on-screen step — recording must be done in the app.'
            return 'I can help with verification, orders, seller applications, stores, favorites, addresses, evidence videos, or connecting you with a human admin.'
        try:
            text = self._call_claude('You are Collectra support. Be concise and helpful.', [{'type': 'text', 'text': message}], max_tokens=500)
            return text[:900] if text else 'I’m having trouble reaching the AI service. You can still request a human admin from this chat.'
        except Exception:
            return 'I’m having trouble reaching the AI service. You can still request a human admin from this chat.'

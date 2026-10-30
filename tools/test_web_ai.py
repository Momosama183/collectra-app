"""Quick live test of the AI card check.
Usage (from the project folder):   python3 tools/test_web_ai.py FRONT.jpg BACK.jpg [claude|openai|gemini]
Sends the two photos to the Collectra website checker and prints the result the app would show."""
import sys, json, os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from services import web_checker
if len(sys.argv) < 3:
    sys.exit(__doc__)
if len(sys.argv) > 3:
    os.environ['COLLECTRA_AI_PROVIDER'] = sys.argv[3]; os.environ['COLLECTRA_AI_FALLBACKS'] = ''
r = web_checker.verify(Path(sys.argv[1]), Path(sys.argv[2]), {}, [], ['front', 'back'], 0)
print(json.dumps({k: r[k] for k in ('verdict', 'confidence', 'provider', 'summary')}, ensure_ascii=False, indent=2))
print('\n'.join(r['details'].get('checks', [])))
print('attempts:', r['details'].get('attempts'))

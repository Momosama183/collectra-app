# Plugging the AI into Collectra

The whole site talks to the AI through ONE object (`AI` in `server.py`). Nothing else in the site needs to change.

## Two ways to connect your AI
1. Edit `services/ai_service.py` (the class `AIService`) and keep the method names/returns below.
2. Or write your own class anywhere and set `COLLECTRA_AI_CLASS="your_package.module:YourClass"`. The class is created with one argument, the project root path.

## Safety net (already built)
Every AI call goes through `ai_call()` in `server.py`. If your AI raises an error, times out or returns nothing, the site does NOT crash: the item is marked "Needs Manual Review" / "Needs Review" and a human admin handles it. The shopper sees a normal message.

## Methods and what they must return
| Method | Used when | Returns |
|---|---|---|
| `provider_name` (attribute) | health check | string |
| `verify(front, back, edges, surface, category, card_name, refs)` | Verify Card. `front` and `back` are always file paths; **`edges` and `surface` are optional and are `None` when the buyer skipped them** — never fail or penalise a card just for that | `{verdict, confidence, summary, details, provider}` |
| `verify_slip(image_path)` | buyer uploads payment slip | `{status, confidence, summary, checks, warnings}` |
| `check_step_frame(image_bytes, media_type, step_text, kind)` | live check of a packing/unboxing step | `{ok, message}` |
| `verify_packing_video(video_path, guide_steps)` | seller packing video | `{status, confidence, summary, checks, warnings, authenticity_status, authenticity_confidence}` |
| `verify_unboxing_video(video_path, guide_steps, packing_video_path or None)` | buyer unboxing video | same as above plus `matches_packing_card` |
| `chat(message)` | support chat | string reply |

`status` values the site understands: `Passed`, `Needs Review`, `Failed`. `verdict`: `Likely Authentic`, `Needs Manual Review`, `Suspicious`, `Likely Counterfeit` (the app still understands the older `Likely Fake`).

## Optional: ID helper
`services/id_check.py` can ask the AI for hints about a seller's ID photos (never face-matching; a human admin always decides). It only runs if `COLLECTRA_ID_AI=on` and uses `AI._call_claude`.

## Built-in Claude provider
Set `ANTHROPIC_API_KEY` (and optionally `ANTHROPIC_MODEL`, `ANTHROPIC_VERIFY_PASSES`). Not tested live yet, test it once with your key.

## Who the AI/database owner should know about
- All buyer-owned rows use `device_id` = `buyer:<id>`. Real user data is in `data/collectra.db` and private files in `data/private/`. See `DATABASE_NOTES.md`.
- Seller ID photos are in `data/private/ids` and must never be served publicly.


## Card check now uses the live Collectra web AI (added)
`verify()` sends the FRONT and BACK photos to the Collectra website checker (`services/web_checker.py`), the same system as
the /verify page: improved prompt, 4-level verdict, lomo/generic-back rule, verified reference cards and code-based image matching.
- Choose the AI with `COLLECTRA_AI_PROVIDER` (claude / openai / gemini). If it fails, `COLLECTRA_AI_FALLBACKS` are tried in order (default: openai).
- If every provider fails, the card goes to **Needs Manual Review** (never auto-approved).
- Front and back are required; edges/corners and surface/logo/foil photos are optional. They are stored as evidence when supplied, but the web checker reads front + back only.
- The app's local reference-card list is not used for this path; the web system has its own library.
- Packing/unboxing video, payment slip, step check and chat still use the built-in AIService methods (they need ANTHROPIC_API_KEY).
- Live test: `python3 tools/test_web_ai.py FRONT.jpg BACK.jpg`.
- Switch back to the old path any time with `COLLECTRA_WEB_AI=off`.

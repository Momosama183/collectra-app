# Notes for whoever connects the database and AI (Supabase etc.)

Everything below currently lives in one SQLite file (`data/collectra.db`) plus folders under `data/`.
Nothing here needs to change for the site to work; these notes say what to move and what to keep.

## Accounts (new)
| Table | What it holds |
|---|---|
| `buyers` | email (unique, case-insensitive), scrypt password hash + salt, full name, phone, reset_requested_at |
| `buyer_sessions` | SHA-256 of the login token, buyer_id, expiry (30 days). The raw token only lives in the user's browser. |
| `buyer_resets` | SHA-256 of reset tokens, buyer_id, expiry, used flag |
| `sellers` | same login fields + shop name, ID number *hash* and last 4 digits only, ID photo file names, selfie code, status (pending/approved/rejected), store_id |
| `seller_sessions`, `seller_resets` | same idea as the buyer tables |
| `email_verifications` | SHA-256 of confirmation tokens, kind (buyer/seller), account_id, 48h expiry. `buyers.email_verified` and `sellers.email_verified` are set when the link is opened. Accounts that existed before this feature are marked confirmed automatically. |

If you move to Supabase Auth, replace `hash_password / check_password / new_*_session` in `server.py` and keep the
rest. Sessions are sent as `Authorization: Bearer <token>`.

## Who owns a row
Orders, addresses, favorites, reviews, verification results and support tickets keep their old `device_id` column,
but it now holds **`buyer:<buyers.id>`** and is always set by the server from the login session (never taken from the
browser). When you add a real `buyer_id` foreign key, backfill it from this value.

## Files that must be private
- `data/private/ids/` holds seller ID photos and selfies. Move to a **private** storage bucket; never public.
  Only `GET /api/admin/seller-id` (admin key) reads them.
- `data/uploads/` (listing photos, slips, packing/unboxing videos) is served publicly by URL. Consider signed URLs
  for slips and videos.

## AI hooks (owned elsewhere)
- `services/ai_service.py`: card verification, slip check, packing/unboxing video checks, step checks.
- `services/id_check.py`: optional *advisory* hints on seller ID photos (code on paper, ID number match, live
  person). Off unless `COLLECTRA_ID_AI=on` and `ANTHROPIC_API_KEY` are set. It never approves anyone and does not
  compare faces; the admin does that by eye.
- `/api/verify` and `/api/evidence-step-check` need a logged-in buyer or approved seller (AI cost control).

## Rules enforced by the server (keep when porting)
- Email must be confirmed to: place orders, upload slips, review, record unboxing videos, use card verify (buyers); to be approved (sellers).
- Reviews: only a logged-in buyer who has an order with payment uploaded from that shop; one review per buyer per shop.
- Packing video upload: only the approved seller whose store has items in that order.
- Unboxing video and payment slip: only the buyer who owns the order.
- Sellers see a buyer's delivery address only after payment, and never see the buyer's account id.
- Passwords: min 8 chars, scrypt; 5 wrong logins locks that email+IP for 5 minutes; reset links are single use.

## Environment variables
`COLLECTRA_ADMIN_KEY` (required for a public site), `COLLECTRA_SECRET`, `COLLECTRA_PUBLIC_URL`,
`SMTP_HOST/PORT/USER/PASS/FROM` (password-reset email), `ANTHROPIC_API_KEY`, `COLLECTRA_ID_AI`.


## email_codes (added in the final version)
`email_codes(kind, account_id, code_hash, expires_at, attempts)`: one row per pending 6-digit confirmation code (hashed, 30 minutes, max 5 wrong tries). Env vars added: `ADMIN_NOTIFY_EMAIL`, `COLLECTRA_AI_CLASS`.

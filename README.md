# Collectra Mobile Full-Stack Prototype — v4

This build keeps the previous Collectra UI/UX and adds the requested stricter evidence flows:

- Verification cannot start until **4 required card views** are uploaded: front, back, edges/corners, and surface/logo/foil detail.
- Seller packing evidence is **video only** and follows a 5-step guide.
- Buyer unboxing evidence is **video only** and follows a 5-step guide.
- The guided recorder keeps an on-screen **current step + “Next” instruction** while recording when the browser allows in-app camera access.
- If a local-network mobile browser blocks in-app camera recording because the site is not HTTPS, users can record one continuous video with the phone camera and then choose it in the guided screen.
- The backend refuses packing/unboxing uploads that are not videos and requires the completed guide-step metadata.
- The 10 supplied stock photos are already loaded as optimized mobile listings with the requested categories, prices and quantities.

## Initial stock already included

| Listing | Category | Price | Stock |
|---|---|---:|---:|
| T-pop Photocard 01 | T-pop | ฿40 | 1 |
| T-pop Photocard 02 | T-pop | ฿90 | 1 |
| T-pop Photocard 03 | T-pop | ฿30 | 1 |
| T-pop Photocard 04 | T-pop | ฿40 | 1 |
| T-pop Photocard 05 | T-pop | ฿40 | 1 |
| K-pop Photocard 01 | K-pop | ฿90 | 1 |
| K-pop Photocard 02 | K-pop | ฿90 | 1 |
| K-pop Photocard 03 | K-pop | ฿130 | 2 |
| K-pop Photocard 04 | K-pop | ฿130 | 2 |
| K-pop Photocard 05 | K-pop | ฿130 | 1 |

The people/card identities are intentionally not guessed from photos. Rename the product titles later in Seller Tools when you have the exact official card names.

## 1. Open in VS Code

1. Unzip the project.
2. Open VS Code.
3. Choose **File → Open Folder**.
4. Select the `collectra-mobile-fullstack-v4` folder.
5. Open **Terminal → New Terminal**.

## 2. Start the app

### Mac

```bash
cp .env.example .env
python3 -m pip install -r requirements.txt
python3 server.py
```

Or double-click `run.command`.

### Windows

```bat
copy .env.example .env
python -m pip install -r requirements.txt
python server.py
```

Or double-click `run_windows.bat`.

The terminal prints:

- Computer: `http://localhost:8000`
- Phone on same Wi-Fi: something like `http://192.168.1.20:8000`

## 3. Test on a phone

1. Keep `server.py` running.
2. Put the phone and computer on the same Wi-Fi.
3. Open the printed phone URL in Safari/Chrome.
4. On iPhone, choose **Share → Add to Home Screen** for the app-style launcher.

If the phone cannot connect, allow Python through the computer firewall.

### Important camera note

Normal photo inputs and the native phone video recorder work on the local Wi-Fi URL. The **live in-app guided MediaRecorder** can require HTTPS on mobile browsers. The app automatically provides a fallback: record one uninterrupted video with the phone camera and select it on the same evidence-guide page. Once the website is deployed with HTTPS, the full live guided recorder can use the rear camera directly.

## 4. Card verification — required views

The backend requires the first two; 3 and 4 are optional (the AI receives `None` for any it did not get):

1. Full front
2. Full back
3. Edges and corners close-up
4. Surface / logo / foil / print-detail close-up

The browser compresses images before upload to improve speed. The backend rejects the request if any required view is missing.

## 5. Packing video guide

Seller packing evidence must be one continuous video:

1. Show card front.
2. Show same card back.
3. Show edges/corners/details.
4. Keep card visible while sleeving/top-loading and placing it into the package — no cuts or switching.
5. Show sealed package and shipping label/order number.

## 6. Unboxing video guide

Buyer unboxing evidence must be one continuous video:

1. Show unopened parcel and shipping label/order number.
2. Keep parcel in frame while opening — no cuts.
3. Show card immediately after removal.
4. Show full front.
5. Show full back, edges/corners and details.

## 7. Real stores and additional stock

Sellers apply from **Sell**. The admin approves them and gets a private store token. Approved sellers use **Sell → Seller Tools** to upload:

- product title
- category
- price
- real stock quantity
- condition
- description
- real listing photo

Stock is stored in SQLite and reduced when checkout creates an order.

## 8. AI backend

Collectra currently supports **K-pop and T-pop photo cards only**.

Without an AI key, the full upload pipeline works but returns **Needs Manual Review** after local photo-quality checks.

To enable server-side vision (card photo verification + packing/unboxing video verification):

```env
ANTHROPIC_API_KEY=your_key_here
ANTHROPIC_MODEL=claude-sonnet-4-6
```

Restart the server after editing `.env`.

The photo verifier receives the front and back, plus the optional edges/corners and surface/logo/foil close-ups when supplied, plus up to three matching verified references from the admin reference database. It is designed as AI-assisted risk screening, not a guaranteed professional authenticity certificate.

### Packing / unboxing video AI verification

Both the seller's packing video and the buyer's unboxing video are now analyzed automatically on upload:

- **Packing video:** checks that the required steps were followed (front, back, edges/corners shown, card kept visible while boxed, sealed parcel + shipping label shown), and checks whether the card looks consistent (same card) across the whole video to flag a possible swap mid-recording.
- **Unboxing video:** checks the required steps were followed, and — when a packing video exists for the same order — compares frames from both videos to judge whether it's the same physical card, flagging a likely mismatch if not.
- Recording must be done live inside the app; there is no file-upload fallback, so a video can't be pre-edited or swapped in before it reaches Collectra.

This requires **ffmpeg** installed on the server (used to pull frames from the uploaded video):
- Mac: `brew install ffmpeg`
- Windows: `choco install ffmpeg` (or download from ffmpeg.org and add to PATH)
- Linux: `sudo apt install ffmpeg`

Without ffmpeg or an API key, uploads still succeed but are marked **Needs Review** with a note that AI analysis wasn't run.

## 9. Main files

- `public/app.js` — app behavior and guided capture flows
- `public/styles.css` — same Collectra responsive UI + v4 evidence UI
- `server.py` — APIs, uploads and validation
- `services/db.py` — SQLite schema + initial stock
- `services/ai_service.py` — quality checks + optional vision AI

## 10. Reset the prototype database

Stop the server and delete:

```text
data/collectra.db
```

Restart. The 10 supplied initial listings will be recreated with the original stock quantities.

from __future__ import annotations
import os, sqlite3, secrets
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.getenv('COLLECTRA_DATA_DIR') or (ROOT / 'data'))
DB_PATH = DATA_DIR / 'collectra.db'
UPLOADS = DATA_DIR / 'uploads'
REFERENCES = DATA_DIR / 'references'


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def connect():
    DATA_DIR.mkdir(exist_ok=True)
    UPLOADS.mkdir(exist_ok=True)
    REFERENCES.mkdir(exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys = ON')
    return con


def _ensure_column(cur, table: str, column: str, definition: str):
    cols = {r[1] for r in cur.execute(f'PRAGMA table_info({table})').fetchall()}
    if column not in cols:
        cur.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')
        return True
    return False


def init_db():
    con = connect()
    cur = con.cursor()
    cur.executescript('''
    CREATE TABLE IF NOT EXISTS stores (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        description TEXT DEFAULT '',
        logo_url TEXT DEFAULT '',
        verified INTEGER DEFAULT 0,
        category TEXT DEFAULT 'Mixed',
        token TEXT UNIQUE NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        store_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        category TEXT NOT NULL,
        price REAL NOT NULL,
        stock INTEGER NOT NULL DEFAULT 1,
        condition TEXT DEFAULT 'Like New',
        image_url TEXT DEFAULT '',
        verified INTEGER DEFAULT 0,
        description TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        FOREIGN KEY(store_id) REFERENCES stores(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        store_id INTEGER NOT NULL,
        device_id TEXT NOT NULL,
        rating INTEGER NOT NULL,
        text TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        FOREIGN KEY(store_id) REFERENCES stores(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS favorites (
        device_id TEXT NOT NULL,
        product_id INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(device_id, product_id),
        FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS seller_applications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT NOT NULL,
        shop_name TEXT DEFAULT '',
        social_link TEXT DEFAULT '',
        status TEXT DEFAULT 'pending',
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS sellers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT NOT NULL UNIQUE COLLATE NOCASE,
        password_hash TEXT NOT NULL,
        password_salt TEXT NOT NULL,
        full_name TEXT NOT NULL,
        shop_name TEXT NOT NULL,
        phone TEXT DEFAULT '',
        id_last4 TEXT DEFAULT '',
        id_hash TEXT DEFAULT '',
        id_front_path TEXT DEFAULT '',
        id_selfie_path TEXT DEFAULT '',
        status TEXT DEFAULT 'pending',
        reject_reason TEXT DEFAULT '',
        store_id INTEGER,
        created_at TEXT NOT NULL,
        reviewed_at TEXT DEFAULT '',
        id_challenge TEXT DEFAULT '',
        id_source TEXT DEFAULT '',
        selfie_source TEXT DEFAULT '',
        id_ai_json TEXT DEFAULT '',
        reset_requested_at TEXT DEFAULT '',
        email_verified INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS seller_resets (
        token_hash TEXT PRIMARY KEY,
        seller_id INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        used INTEGER DEFAULT 0,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS seller_sessions (
        token_hash TEXT PRIMARY KEY,
        seller_id INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS buyers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT NOT NULL UNIQUE COLLATE NOCASE,
        password_hash TEXT NOT NULL,
        password_salt TEXT NOT NULL,
        full_name TEXT NOT NULL,
        phone TEXT DEFAULT '',
        reset_requested_at TEXT DEFAULT '',
        email_verified INTEGER DEFAULT 0,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS buyer_sessions (
        token_hash TEXT PRIMARY KEY,
        buyer_id INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS buyer_resets (
        token_hash TEXT PRIMARY KEY,
        buyer_id INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        used INTEGER DEFAULT 0,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS email_verifications (
        token_hash TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        account_id INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        used INTEGER DEFAULT 0,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS email_codes (
        kind TEXT NOT NULL,
        account_id INTEGER NOT NULL,
        code_hash TEXT NOT NULL,
        expires_at INTEGER NOT NULL,
        attempts INTEGER DEFAULT 0,
        PRIMARY KEY(kind, account_id)
    );
    CREATE TABLE IF NOT EXISTS addresses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id TEXT NOT NULL,
        label TEXT NOT NULL,
        name TEXT NOT NULL,
        phone TEXT NOT NULL,
        line1 TEXT NOT NULL,
        district TEXT DEFAULT '',
        city TEXT NOT NULL,
        province TEXT NOT NULL,
        postal TEXT NOT NULL,
        is_default INTEGER DEFAULT 0,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id TEXT NOT NULL,
        total REAL NOT NULL,
        address_id INTEGER,
        status TEXT DEFAULT 'Awaiting Payment',
        payment_status TEXT DEFAULT 'pending',
        slip_url TEXT DEFAULT '',
        tracking_number TEXT DEFAULT '',
        packing_video_url TEXT DEFAULT '',
        unboxing_video_url TEXT DEFAULT '',
        packing_steps_json TEXT DEFAULT '[]',
        unboxing_steps_json TEXT DEFAULT '[]',
        packing_evidence_status TEXT DEFAULT 'not_uploaded',
        unboxing_evidence_status TEXT DEFAULT 'not_uploaded',
        created_at TEXT NOT NULL,
        FOREIGN KEY(address_id) REFERENCES addresses(id)
    );
    CREATE TABLE IF NOT EXISTS order_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        qty INTEGER NOT NULL,
        unit_price REAL NOT NULL,
        FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS support_tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id TEXT NOT NULL,
        email TEXT DEFAULT '',
        message TEXT NOT NULL,
        status TEXT DEFAULT 'open',
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS verification_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id TEXT NOT NULL,
        category TEXT DEFAULT '',
        card_name TEXT DEFAULT '',
        verdict TEXT NOT NULL,
        confidence INTEGER NOT NULL,
        summary TEXT DEFAULT '',
        details_json TEXT DEFAULT '{}',
        front_url TEXT DEFAULT '',
        back_url TEXT DEFAULT '',
        edges_url TEXT DEFAULT '',
        surface_url TEXT DEFAULT '',
        provider TEXT DEFAULT 'local',
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS reference_cards (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        category TEXT NOT NULL,
        card_name TEXT DEFAULT '',
        set_name TEXT DEFAULT '',
        image_url TEXT NOT NULL,
        notes TEXT DEFAULT '',
        created_at TEXT NOT NULL
    );
    ''')

    # Lightweight migrations so this build can also replace an older prototype folder.
    for col, definition in [
        ('packing_steps_json', "TEXT DEFAULT '[]'"),
        ('unboxing_steps_json', "TEXT DEFAULT '[]'"),
        ('packing_evidence_status', "TEXT DEFAULT 'not_uploaded'"),
        ('unboxing_evidence_status', "TEXT DEFAULT 'not_uploaded'"),
        ('packing_ai_status', "TEXT DEFAULT ''"),
        ('packing_ai_confidence', "INTEGER DEFAULT 0"),
        ('packing_ai_summary', "TEXT DEFAULT ''"),
        ('packing_ai_json', "TEXT DEFAULT '{}'"),
        ('unboxing_ai_status', "TEXT DEFAULT ''"),
        ('unboxing_ai_confidence', "INTEGER DEFAULT 0"),
        ('unboxing_ai_summary', "TEXT DEFAULT ''"),
        ('unboxing_ai_json', "TEXT DEFAULT '{}'"),
        ('evidence_match_status', "TEXT DEFAULT ''"),
        ('slip_ai_status', "TEXT DEFAULT ''"),
        ('slip_ai_confidence', "INTEGER DEFAULT 0"),
        ('slip_ai_summary', "TEXT DEFAULT ''"),
        ('slip_ai_json', "TEXT DEFAULT '{}'"),
    ]:
        _ensure_column(cur, 'orders', col, definition)
    for col, definition in [
        ('edges_url', "TEXT DEFAULT ''"),
        ('surface_url', "TEXT DEFAULT ''"),
    ]:
        _ensure_column(cur, 'verification_results', col, definition)

    for col, definition in [
        ('id_challenge', "TEXT DEFAULT ''"), ('id_source', "TEXT DEFAULT ''"), ('selfie_source', "TEXT DEFAULT ''"),
        ('id_ai_json', "TEXT DEFAULT ''"), ('reset_requested_at', "TEXT DEFAULT ''"),
    ]:
        _ensure_column(cur, 'sellers', col, definition)

    for table in ('buyers', 'sellers'):
        if _ensure_column(cur, table, 'email_verified', 'INTEGER DEFAULT 0'):
            cur.execute(f'UPDATE {table} SET email_verified=1')   # accounts made before verification existed keep working
    purge_demo_data(cur)
    con.commit()
    con.close()


LEGACY_SAMPLE_STORES = ('Collectra T-pop Stock', 'Collectra K-pop Stock', 'Collectra Seller Demo Store')

def purge_demo_data(cur):
    """Older builds pre-loaded sample shops, products, reviews and a sandbox seller order.
    The real marketplace starts empty, so remove them. A sample store is kept only if a real buyer
    (not a demo device) has an order containing its products."""
    for st in cur.execute('SELECT id FROM stores WHERE name IN (%s)' % ','.join('?' * len(LEGACY_SAMPLE_STORES)), LEGACY_SAMPLE_STORES).fetchall():
        sid = st['id']
        real = cur.execute('''SELECT 1 FROM order_items oi JOIN products p ON p.id=oi.product_id
            JOIN orders o ON o.id=oi.order_id WHERE p.store_id=? AND o.device_id NOT LIKE 'demo%' LIMIT 1''', (sid,)).fetchone()
        if real: continue
        pids = [r['id'] for r in cur.execute('SELECT id FROM products WHERE store_id=?', (sid,)).fetchall()]
        for pid in pids:
            cur.execute('DELETE FROM favorites WHERE product_id=?', (pid,))
            cur.execute('DELETE FROM order_items WHERE product_id=?', (pid,))
        cur.execute('DELETE FROM products WHERE store_id=?', (sid,))
        cur.execute('DELETE FROM reviews WHERE store_id=?', (sid,))
        cur.execute('DELETE FROM sellers WHERE store_id=?', (sid,))
        cur.execute('DELETE FROM stores WHERE id=?', (sid,))
    cur.execute("DELETE FROM orders WHERE device_id LIKE 'demo%'")
    cur.execute("DELETE FROM addresses WHERE device_id LIKE 'demo%'")
    cur.execute("DELETE FROM seller_sessions WHERE seller_id NOT IN (SELECT id FROM sellers)")
    cur.execute('DROP TABLE IF EXISTS demo_feedback')

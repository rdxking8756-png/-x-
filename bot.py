"""
RDX SMS BOMBER — bot.py  v3.0
Telegram Bot (long-poll)
Developer: @itzrdxking 

New in v3:
  - Welcome media: admin sets photo/video + caption shown on /start to new/returning users
  - Media broadcast: broadcast photo/video + text to all users
  - All original features preserved
"""

import os, re, json, time, html, secrets, threading, sqlite3, copy, io
import signal, sys
from concurrent.futures import ThreadPoolExecutor
import requests

# ✅ FIX 1: SIGTERM handler for Render clean shutdown
def _shutdown(sig, frame):
    print(f"[Bot] signal {sig} — shutting down", flush=True)
    sys.exit(0)

signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT, _shutdown)

# ═══════════════════════════════════════════════════════════════
#  ENV
# ═══════════════════════════════════════════════════════════════
def _load_dotenv(path=".env"):
    if not os.path.exists(path): return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line: continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ: os.environ[k] = v

_load_dotenv()

# ═══════════════════════════════════════════════════════════════
#  FIREBASE
# ═══════════════════════════════════════════════════════════════
try:
    import firebase_admin
    from firebase_admin import credentials, firestore
    FIREBASE_AVAILABLE = True
except ImportError:
    FIREBASE_AVAILABLE = False

_db = None

def _init_firebase():
    global _db
    if not FIREBASE_AVAILABLE: return None
    try:
        if firebase_admin._apps:
            _db = firestore.client()
            return _db
        fb_key_json = os.environ.get("FIREBASE_KEY_JSON")
        fb_key_path = os.environ.get("FIREBASE_KEY_PATH", "firebase-key.json")
        if fb_key_json:
            cred = credentials.Certificate(json.loads(fb_key_json))
        elif os.path.exists(fb_key_path):
            cred = credentials.Certificate(fb_key_path)
        else:
            return None
        firebase_admin.initialize_app(cred)
        _db = firestore.client()
        return _db
    except Exception as e:
        print(f"❌ Firebase error: {e}")
        return None

_init_firebase()

# ═══════════════════════════════════════════════════════════════
#  CONSTANTS
# ═══════════════════════════════════════════════════════════════
DEV_TAG          = os.environ.get("DEVELOPER_TAG", "@itzrdxking")
DEV_URL          = os.environ.get("DEVELOPER_URL", "https://t.me/itzrdxking")
CONTACT_USERNAME = os.environ.get("CONTACT_USERNAME", "itzrdxking").lstrip("@")
CONTACT_URL      = f"https://t.me/{CONTACT_USERNAME}"
DEFAULT_DAILY    = int(os.environ.get("API_DAILY_LIMIT", "500"))
SQLITE_PATH      = os.environ.get("SQLITE_PATH", "rdx_bot.db")

PLANS = [(5, 7, "7 Days"), (10, 15, "15 Days"), (20, 30, "1 Month")]

CREDITS_PER_REFERRAL = int(os.environ.get("CREDITS_PER_REFERRAL", "10"))
CREDITS_SMS_BOMB     = int(os.environ.get("CREDITS_SMS_BOMB", "2"))
CREDITS_CUSTOM_BOMB  = int(os.environ.get("CREDITS_CUSTOM_BOMB", "3"))
CREDITS_CALL_BOMB    = int(os.environ.get("CREDITS_CALL_BOMB", "5"))
CREDITS_NUCLEAR_IND  = int(os.environ.get("CREDITS_NUCLEAR_IND", "5"))
CREDITS_NUCLEAR_BD   = int(os.environ.get("CREDITS_NUCLEAR_BD", "30"))
CREDITS_MIXED_BD     = int(os.environ.get("CREDITS_MIXED_BD", "7"))
PROXY_MULTIPLIER     = int(os.environ.get("PROXY_MULTIPLIER", "10"))

COLOR_THEMES = {
    "blue":  {"main": "primary", "good": "success", "bad": "danger",  "nav": "primary"},
    "red":   {"main": "danger",  "good": "success", "bad": "primary", "nav": "danger"},
    "green": {"main": "success", "good": "primary", "bad": "danger",  "nav": "success"},
}
_global_color = "blue"

BOT_TOKEN    = ""
BOT_ID       = 0
BOT_USERNAME = "Rdxmixbombot"
BASE_URL     = os.environ.get("BASE_URL", "http://localhost:5000").rstrip("/")
LOCAL_PORT   = 5000
ADMIN_IDS    = set()

E = html.escape
INTERNAL_TOKEN = secrets.token_hex(24)

# ═══════════════════════════════════════════════════════════════
#  PERMANENT CHANNELS
# ═══════════════════════════════════════════════════════════════
def _parse_permanent_channels() -> list:
    ids    = [x.strip() for x in os.environ.get("PERMANENT_CHANNELS",       "").split(",") if x.strip()]
    links  = [x.strip() for x in os.environ.get("PERMANENT_CHANNEL_LINKS",  "").split(",") if x.strip()]
    titles = [x.strip() for x in os.environ.get("PERMANENT_CHANNEL_TITLES", "").split(",") if x.strip()]
    result = []
    for i, cid in enumerate(ids):
        result.append({
            "chat_id": cid,
            "title":   titles[i] if i < len(titles) else cid,
            "link":    links[i]  if i < len(links)  else "",
        })
    return result

PERM_CHANNELS: list = _parse_permanent_channels()

# ═══════════════════════════════════════════════════════════════
#  FONT / CARD UTILITIES
# ═══════════════════════════════════════════════════════════════
_SMALL_CAPS = str.maketrans(dict(zip(
    "abcdefghijklmnopqrstuvwxyz",
    "ᴀʙᴄᴅᴇғɢʜɪᴊᴋʟᴍɴᴏᴘǫʀsᴛᴜᴠᴡxʏᴢ"
)))

_HTML_PARTS = re.compile(
    r'(<code\b[^>]*>.*?</code>'
    r'|<pre\b[^>]*>.*?</pre>'
    r'|<a\b[^>]*>.*?</a>'
    r'|<[^>]+>'
    r'|&(?:#\d+|#x[0-9a-f]+|\w+);'
    r'|https?://[^\s<]+'
    r'|(?<![\w])/[a-z][a-z0-9_]*(?:@\w+)?'
    r'|(?<![\w])@\w+)',
    re.I | re.S
)

def small_caps(text: str) -> str:
    return str(text).lower().translate(_SMALL_CAPS)

def style_markup(text: str) -> str:
    parts = _HTML_PARTS.split(text)
    for i in range(0, len(parts), 2):
        parts[i] = small_caps(parts[i])
    return "".join(parts)

def tree(lines) -> str:
    items = [
        re.sub(r'^\s*•\s*', '', str(line)).replace('\n\n', '\n')
        for line in lines if str(line).strip()
    ]
    if not items: return ""
    return '\n'.join(
        ('└ ' if i == len(items) - 1 else '├ ') + line.replace('\n', '\n  ')
        for i, line in enumerate(items)
    )

def card(title: str, lines=(), footer: str = None, *, mood: str = 'luxury') -> str:
    symbol = {'bold': '★', 'luxury': '✦', 'calm': '❖'}.get(mood, '✦')
    title_safe = small_caps(html.escape(str(title), quote=False))
    body = f"<b>{symbol} » {title_safe}</b>"
    if lines:
        content = tree(lines)
        sep = '━━━\n' if len(lines) > 8 else ''
        body += f'\n\n{sep}<blockquote>{content}</blockquote>'
    action = str(footer or '').strip().lstrip('➥').strip()
    if not action:
        action = small_caps('choose an option below')
    else:
        action = style_markup(re.sub(r'\n+', ' · ', action))
    body += f'\n\n➥ {action}'
    return style_markup(body)

FOOTER = f"\n<i>» <a href=\"{DEV_URL}\">{DEV_TAG}</a></i>"

# ═══════════════════════════════════════════════════════════════
#  SQLITE LAYER
# ═══════════════════════════════════════════════════════════════
_sq_lock = threading.Lock()

def _sq():
    conn = sqlite3.connect(SQLITE_PATH, check_same_thread=False, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn

def init_sqlite():
    with _sq_lock:
        conn = _sq()
        with conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT DEFAULT '',
                first_name TEXT DEFAULT '',
                joined_at INTEGER DEFAULT 0,
                referred_by INTEGER,
                ref_counted INTEGER DEFAULT 0,
                ref_points INTEGER DEFAULT 0,
                total_refs INTEGER DEFAULT 0,
                credits INTEGER DEFAULT 0,
                banned INTEGER DEFAULT 0,
                total_bombs INTEGER DEFAULT 0,
                is_premium INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS api_keys (
                key TEXT PRIMARY KEY,
                user_id TEXT DEFAULT '',
                created_at INTEGER DEFAULT 0,
                expires_at INTEGER DEFAULT 0,
                revoked INTEGER DEFAULT 0,
                daily_limit INTEGER DEFAULT 500,
                plan TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS channels (
                chat_id TEXT PRIMARY KEY,
                title TEXT DEFAULT '',
                link TEXT DEFAULT '',
                added_at INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS settings (
                k TEXT PRIMARY KEY,
                v TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS gift_codes (
                code TEXT PRIMARY KEY,
                credits INTEGER DEFAULT 0,
                max_users INTEGER DEFAULT 0,
                claimed_count INTEGER DEFAULT 0,
                expires_at INTEGER DEFAULT 0,
                created_at INTEGER DEFAULT 0,
                is_premium INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS gift_claims (
                id TEXT PRIMARY KEY,
                code TEXT DEFAULT '',
                user_id TEXT DEFAULT '',
                claimed_at INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS usage_log (
                id TEXT PRIMARY KEY,
                key_id TEXT DEFAULT '',
                day TEXT DEFAULT '',
                count INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS refer_subs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ref_cost INTEGER DEFAULT 5,
                days INTEGER DEFAULT 7,
                label TEXT DEFAULT '',
                created_at INTEGER DEFAULT 0,
                active INTEGER DEFAULT 1
            );
            """)
        conn.close()

# ── user helpers ──────────────────────────────────────────────
def sq_get_user(uid):
    with _sq_lock:
        conn = _sq()
        try:
            row = conn.execute("SELECT * FROM users WHERE id=?", (int(uid),)).fetchone()
            return dict(row) if row else None
        except: return None
        finally: conn.close()

def sq_upsert_user(uid, data: dict):
    with _sq_lock:
        conn = _sq()
        try:
            existing = conn.execute("SELECT id FROM users WHERE id=?", (int(uid),)).fetchone()
            filtered = {k: v for k, v in data.items() if k != "id"}
            if existing:
                sets = ", ".join(f"{k}=?" for k in filtered)
                vals = list(filtered.values()) + [int(uid)]
                conn.execute(f"UPDATE users SET {sets} WHERE id=?", vals)
            else:
                filtered["id"] = int(uid)
                cols = ", ".join(filtered.keys())
                placeholders = ", ".join("?" * len(filtered))
                conn.execute(f"INSERT OR REPLACE INTO users ({cols}) VALUES ({placeholders})", list(filtered.values()))
            conn.commit()
        except Exception as e:
            print(f"[SQLite] upsert_user error: {e}")
        finally: conn.close()

def sq_update_user(uid, **kwargs):
    with _sq_lock:
        conn = _sq()
        try:
            sets = ", ".join(f"{k}=?" for k in kwargs)
            vals = list(kwargs.values()) + [int(uid)]
            conn.execute(f"UPDATE users SET {sets} WHERE id=?", vals)
            conn.commit()
        except Exception as e:
            print(f"[SQLite] update_user error: {e}")
        finally: conn.close()

def sq_incr_user(uid, field, amount):
    with _sq_lock:
        conn = _sq()
        try:
            conn.execute(f"UPDATE users SET {field}={field}+? WHERE id=?", (amount, int(uid)))
            conn.commit()
        except: pass
        finally: conn.close()

def sq_all_users(limit=5000):
    with _sq_lock:
        conn = _sq()
        try:
            rows = conn.execute("SELECT * FROM users LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]
        except: return []
        finally: conn.close()

def sq_get_key(key):
    with _sq_lock:
        conn = _sq()
        try:
            row = conn.execute("SELECT * FROM api_keys WHERE key=?", (key,)).fetchone()
            return dict(row) if row else None
        except: return None
        finally: conn.close()

def sq_get_key_by_user(uid):
    with _sq_lock:
        conn = _sq()
        try:
            row = conn.execute(
                "SELECT * FROM api_keys WHERE user_id=? AND revoked=0 ORDER BY expires_at DESC LIMIT 1",
                (str(uid),)
            ).fetchone()
            return dict(row) if row else None
        except: return None
        finally: conn.close()

def sq_upsert_key(key, data: dict):
    with _sq_lock:
        conn = _sq()
        try:
            data["key"] = key
            cols = ", ".join(data.keys())
            placeholders = ", ".join("?" * len(data))
            conn.execute(f"INSERT OR REPLACE INTO api_keys ({cols}) VALUES ({placeholders})", list(data.values()))
            conn.commit()
        except Exception as e:
            print(f"[SQLite] upsert_key error: {e}")
        finally: conn.close()

def sq_incr_usage(key):
    today = time.strftime("%Y-%m-%d", time.gmtime())
    uid_key = f"{key}_{today}"
    with _sq_lock:
        conn = _sq()
        try:
            conn.execute(
                "INSERT INTO usage_log (id, key_id, day, count) VALUES (?,?,?,1) "
                "ON CONFLICT(id) DO UPDATE SET count=count+1",
                (uid_key, key, today)
            )
            conn.commit()
            row = conn.execute("SELECT count FROM usage_log WHERE id=?", (uid_key,)).fetchone()
            return row[0] if row else 1
        except: return 0
        finally: conn.close()

def sq_usage_today(key):
    today = time.strftime("%Y-%m-%d", time.gmtime())
    uid_key = f"{key}_{today}"
    with _sq_lock:
        conn = _sq()
        try:
            row = conn.execute("SELECT count FROM usage_log WHERE id=?", (uid_key,)).fetchone()
            return row[0] if row else 0
        except: return 0
        finally: conn.close()

def sq_get_setting(k, default=None):
    with _sq_lock:
        conn = _sq()
        try:
            row = conn.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
            return row[0] if row else default
        except: return default
        finally: conn.close()

def sq_set_setting(k, v):
    with _sq_lock:
        conn = _sq()
        try:
            conn.execute("INSERT OR REPLACE INTO settings (k,v) VALUES (?,?)", (k, str(v)))
            conn.commit()
        except: pass
        finally: conn.close()

def sq_all_channels():
    with _sq_lock:
        conn = _sq()
        try:
            rows = conn.execute("SELECT * FROM channels").fetchall()
            return [dict(r) for r in rows]
        except: return []
        finally: conn.close()

def sq_upsert_channel(chat_id, title, link):
    with _sq_lock:
        conn = _sq()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO channels (chat_id,title,link,added_at) VALUES (?,?,?,?)",
                (str(chat_id), title, link, int(time.time()))
            )
            conn.commit()
        except: pass
        finally: conn.close()

def sq_del_channel(chat_id):
    with _sq_lock:
        conn = _sq()
        try:
            conn.execute("DELETE FROM channels WHERE chat_id=?", (str(chat_id),))
            conn.commit()
        except: pass
        finally: conn.close()

def sq_get_gift(code):
    with _sq_lock:
        conn = _sq()
        try:
            row = conn.execute("SELECT * FROM gift_codes WHERE code=?", (code,)).fetchone()
            return dict(row) if row else None
        except: return None
        finally: conn.close()

def sq_upsert_gift(code, data: dict):
    with _sq_lock:
        conn = _sq()
        try:
            data["code"] = code
            cols = ", ".join(data.keys())
            placeholders = ", ".join("?" * len(data))
            conn.execute(f"INSERT OR REPLACE INTO gift_codes ({cols}) VALUES ({placeholders})", list(data.values()))
            conn.commit()
        except: pass
        finally: conn.close()

def sq_gift_claim_exists(code, uid):
    with _sq_lock:
        conn = _sq()
        try:
            row = conn.execute("SELECT id FROM gift_claims WHERE id=?", (f"{code}_{uid}",)).fetchone()
            return row is not None
        except: return False
        finally: conn.close()

def sq_gift_claim(code, uid):
    with _sq_lock:
        conn = _sq()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO gift_claims (id,code,user_id,claimed_at) VALUES (?,?,?,?)",
                (f"{code}_{uid}", code, str(uid), int(time.time()))
            )
            conn.commit()
        except: pass
        finally: conn.close()

def sq_import_users(users_list: list):
    with _sq_lock:
        conn = _sq()
        try:
            imported = 0
            for u in users_list:
                if "id" not in u: continue
                data = {k: v for k, v in u.items() if k in (
                    "id","username","first_name","joined_at","referred_by",
                    "ref_counted","ref_points","total_refs","credits","banned",
                    "total_bombs","is_premium"
                )}
                cols = ", ".join(data.keys())
                placeholders = ", ".join("?" * len(data))
                conn.execute(
                    f"INSERT OR IGNORE INTO users ({cols}) VALUES ({placeholders})",
                    list(data.values())
                )
                imported += 1
            conn.commit()
            return imported
        except Exception as e:
            print(f"[SQLite] import_users error: {e}")
            return 0
        finally: conn.close()

def sq_all_refer_subs():
    with _sq_lock:
        conn = _sq()
        try:
            rows = conn.execute("SELECT * FROM refer_subs WHERE active=1 ORDER BY ref_cost ASC").fetchall()
            return [dict(r) for r in rows]
        except: return []
        finally: conn.close()

def sq_add_refer_sub(ref_cost, days, label):
    with _sq_lock:
        conn = _sq()
        try:
            conn.execute(
                "INSERT INTO refer_subs (ref_cost,days,label,created_at,active) VALUES (?,?,?,?,1)",
                (int(ref_cost), int(days), label, int(time.time()))
            )
            conn.commit()
            return True
        except Exception as e:
            print(f"[SQLite] add_refer_sub error: {e}")
            return False
        finally: conn.close()

def sq_del_refer_sub(sub_id):
    with _sq_lock:
        conn = _sq()
        try:
            conn.execute("UPDATE refer_subs SET active=0 WHERE id=?", (int(sub_id),))
            conn.commit()
        except: pass
        finally: conn.close()

# ═══════════════════════════════════════════════════════════════
#  WELCOME MEDIA SETTINGS
# ═══════════════════════════════════════════════════════════════
def get_welcome_media() -> dict:
    raw = sq_get_setting("welcome_media")
    if not raw: return {}
    try: return json.loads(raw)
    except: return {}

def set_welcome_media(media_type: str, file_id: str, caption: str):
    data = {"type": media_type, "file_id": file_id, "caption": caption}
    sq_set_setting("welcome_media", json.dumps(data))
    if _db:
        try: _db.collection("settings").document("welcome_media").set({"v": json.dumps(data)})
        except: pass

def clear_welcome_media():
    sq_set_setting("welcome_media", "")
    if _db:
        try: _db.collection("settings").document("welcome_media").set({"v": ""})
        except: pass

def send_welcome_media(cid, markup=None):
    wm = get_welcome_media()
    if not wm or not wm.get("type") or wm["type"] == "text":
        return False
    cap = wm.get("caption", "")
    fid = wm.get("file_id", "")
    if not fid: return False
    try:
        p = dict(chat_id=cid, caption=cap, parse_mode="HTML")
        if markup: p["reply_markup"] = markup
        if wm["type"] == "photo":
            p["photo"] = fid
            tg("sendPhoto", **p)
        elif wm["type"] == "video":
            p["video"] = fid
            tg("sendVideo", **p)
        return True
    except TGError as e:
        print(f"[Bot] send_welcome_media error: {e.desc}")
        return False

# ═══════════════════════════════════════════════════════════════
#  FIREBASE HELPERS
# ═══════════════════════════════════════════════════════════════
def fb_user(uid):
    if not _db: return sq_get_user(uid)
    try:
        doc = _db.collection("users").document(str(uid)).get()
        u = doc.to_dict() if doc.exists else None
        return u or sq_get_user(uid)
    except: return sq_get_user(uid)

def fb_user_set(uid, data):
    sq_upsert_user(uid, data)
    if not _db: return False
    try:
        _db.collection("users").document(str(uid)).set(data, merge=True)
        return True
    except: return False

def fb_user_update(uid, **kwargs):
    sq_update_user(uid, **{k: (1 if v is True else (0 if v is False else v)) for k, v in kwargs.items()})
    if not _db: return False
    try:
        _db.collection("users").document(str(uid)).update(kwargs)
        return True
    except: return False

def fb_key_get(uid):
    if not _db: return sq_get_key_by_user(uid)
    try:
        docs = _db.collection("keys").where("user_id", "==", str(uid)) \
            .where("revoked", "==", False) \
            .order_by("expires_at", direction=firestore.Query.DESCENDING).limit(1).stream()
        for d in docs: return d.to_dict()
        return sq_get_key_by_user(uid)
    except: return sq_get_key_by_user(uid)

def fb_key_set(uid, key, exp, plan=""):
    kdata = {
        "key": key, "user_id": str(uid), "created_at": int(time.time()),
        "expires_at": exp, "revoked": False,
        "daily_limit": DEFAULT_DAILY, "plan": plan
    }
    sq_upsert_key(key, {**kdata, "revoked": 0})
    if not _db: return False
    try:
        _db.collection("keys").document(key).set(kdata)
        return True
    except: return False

def fb_key_update(key, **kwargs):
    with _sq_lock:
        conn = _sq()
        try:
            sq_kwargs = {k: (1 if v is True else (0 if v is False else v)) for k, v in kwargs.items()}
            sets = ", ".join(f"{k}=?" for k in sq_kwargs)
            vals = list(sq_kwargs.values()) + [key]
            conn.execute(f"UPDATE api_keys SET {sets} WHERE key=?", vals)
            conn.commit()
        except: pass
        finally: conn.close()
    if not _db: return False
    try:
        _db.collection("keys").document(key).update(kwargs)
        return True
    except: return False

def fb_channels():
    if not _db: return sq_all_channels()
    try:
        rows = [dict(d.to_dict(), id=d.id) for d in _db.collection("channels").stream()]
        return rows or sq_all_channels()
    except: return sq_all_channels()

def fb_channel_add(chat_id, title, link):
    sq_upsert_channel(chat_id, title, link)
    if not _db: return False
    try:
        _db.collection("channels").document(str(chat_id)).set({
            "chat_id": str(chat_id), "title": title, "link": link,
            "added_at": int(time.time())
        })
        return True
    except: return False

def fb_channel_del(chat_id):
    sq_del_channel(chat_id)
    if not _db: return False
    try:
        _db.collection("channels").document(str(chat_id)).delete()
        return True
    except: return False

def fb_setting_get(k, default=None):
    sq_v = sq_get_setting(k)
    if not _db: return sq_v if sq_v is not None else default
    try:
        doc = _db.collection("settings").document(k).get()
        v = doc.to_dict().get("v") if doc.exists else None
        return v if v is not None else (sq_v if sq_v is not None else default)
    except: return sq_v if sq_v is not None else default

def fb_setting_set(k, v):
    sq_set_setting(k, v)
    if not _db: return False
    try:
        _db.collection("settings").document(k).set({"v": str(v)})
        return True
    except: return False

def fb_gift_get(code):
    if not _db: return sq_get_gift(code)
    try:
        doc = _db.collection("gift_codes").document(code).get()
        return doc.to_dict() if doc.exists else sq_get_gift(code)
    except: return sq_get_gift(code)

def fb_gift_set(code, data):
    sq_upsert_gift(code, {k: (1 if v is True else (0 if v is False else v)) for k, v in data.items()})
    if not _db: return False
    try:
        _db.collection("gift_codes").document(code).set(data)
        return True
    except: return False

def fb_gift_claim_exists(code, uid):
    if sq_gift_claim_exists(code, uid): return True
    if not _db: return False
    try:
        doc = _db.collection("gift_claims").document(f"{code}_{uid}").get()
        return doc.exists
    except: return False

def fb_gift_claim(code, uid):
    sq_gift_claim(code, uid)
    if not _db: return False
    try:
        _db.collection("gift_claims").document(f"{code}_{uid}").set({
            "code": code, "user_id": str(uid), "claimed_at": int(time.time())
        })
        return True
    except: return False

def fb_usage_today(key):
    if not _db: return sq_usage_today(key)
    try:
        today = time.strftime("%Y-%m-%d", time.gmtime())
        doc = _db.collection("usage").document(f"{key}_{today}").get()
        return doc.to_dict().get("count", 0) if doc.exists else sq_usage_today(key)
    except: return sq_usage_today(key)

def fb_users_all():
    if not _db: return sq_all_users(5000)
    try:
        rows = [d.to_dict() for d in _db.collection("users").stream()]
        return rows or sq_all_users(5000)
    except: return sq_all_users(5000)

# ═══════════════════════════════════════════════════════════════
#  DB WRAPPER
# ═══════════════════════════════════════════════════════════════
def get_setting(k, default=None): return fb_setting_get(k, default)
def set_setting(k, v): fb_setting_set(k, v)
def daily_limit():
    try: return int(get_setting("daily_limit", DEFAULT_DAILY))
    except: return DEFAULT_DAILY
def force_join_on(): return get_setting("force_join", "1") == "1"
def get_button_color():
    global _global_color
    c = get_setting("button_color", "blue")
    if c in COLOR_THEMES: _global_color = c
    return _global_color
def set_button_color(color):
    global _global_color
    _global_color = color
    set_setting("button_color", color)

def get_user(uid): return fb_user(uid)

def upsert_user(frm, referred_by=None):
    uid = frm["id"]
    row = fb_user(uid)
    if row is None:
        rb = None
        if referred_by and referred_by != uid and fb_user(referred_by):
            rb = referred_by
        data = {
            "id": uid, "username": frm.get("username") or "",
            "first_name": frm.get("first_name") or "",
            "joined_at": int(time.time()), "referred_by": rb,
            "ref_counted": False, "ref_points": 0, "total_refs": 0,
            "credits": 0, "banned": False, "total_bombs": 0, "is_premium": False
        }
        fb_user_set(uid, data)
        return fb_user(uid), True
    fb_user_update(uid, username=frm.get("username") or "",
                   first_name=frm.get("first_name") or "")
    return row, False

def add_credits(uid, amount):
    sq_incr_user(uid, "credits", amount)
    if not _db: return
    try:
        _db.collection("users").document(str(uid)).set(
            {"credits": firestore.Increment(amount)}, merge=True)
    except: pass

def active_key(uid):
    k = fb_key_get(uid)
    if k and k.get("expires_at", 0) > int(time.time()):
        if isinstance(k.get("revoked"), int): k["revoked"] = bool(k["revoked"])
        if not k.get("revoked"): return k
    return None

def grant_key(uid, days, label=""):
    now = int(time.time())
    row = active_key(uid)
    if row:
        exp = max(now, row["expires_at"]) + days * 86400
        fb_key_update(row["key"], expires_at=exp)
        return row["key"], exp
    key = f"EXO-{secrets.token_hex(12).upper()}"
    exp = now + days * 86400
    fb_key_set(uid, key, exp, label)
    add_credits(uid, 500)
    return key, exp

def claim_plan(uid, idx, plans_list=None):
    plan_list = plans_list or get_refer_subs()
    if idx < 0 or idx >= len(plan_list): return None
    plan = plan_list[idx]
    cost = plan["ref_cost"]; days = plan["days"]; label = plan["label"]
    u = fb_user(uid)
    if not u or u.get("ref_points", 0) < cost: return None
    fb_user_update(uid, ref_points=u["ref_points"] - cost)
    sq_incr_user(uid, "ref_points", -cost)
    return grant_key(uid, days, label)

def get_refer_subs():
    db_subs = sq_all_refer_subs()
    if db_subs: return db_subs
    return [{"id": i, "ref_cost": c, "days": d, "label": l}
            for i, (c, d, l) in enumerate(PLANS)]

def list_channels():
    db_chans = fb_channels()
    perm_ids = {c["chat_id"] for c in PERM_CHANNELS}
    extra = [c for c in db_chans if c.get("chat_id") not in perm_ids]
    return PERM_CHANNELS + extra

# ═══════════════════════════════════════════════════════════════
#  TELEGRAM LAYER
# ═══════════════════════════════════════════════════════════════
class TGError(Exception):
    def __init__(self, desc, code=0, retry_after=0):
        super().__init__(desc)
        self.desc, self.code, self.retry_after = desc, code, retry_after

_tl = threading.local()

def tg(method, _t=30, **params):
    s = getattr(_tl, "s", None)
    if s is None: s = _tl.s = requests.Session()
    try:
        r = s.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=params, timeout=_t)
        data = r.json()
    except Exception as e:
        raise TGError(f"network: {e}")
    if not data.get("ok"):
        raise TGError(data.get("description", "error"), data.get("error_code", 0),
                      (data.get("parameters") or {}).get("retry_after", 0))
    return data["result"]

def tg_multipart(method, files, **params):
    s = getattr(_tl, "s", None)
    if s is None: s = _tl.s = requests.Session()
    try:
        r = s.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
                   data={k: v for k, v in params.items() if v is not None},
                   files=files, timeout=60)
        data = r.json()
    except Exception as e:
        raise TGError(f"network: {e}")
    if not data.get("ok"):
        raise TGError(data.get("description", "error"), data.get("error_code", 0))
    return data["result"]

def _strip_style(markup):
    m = copy.deepcopy(markup)
    for row in m.get("inline_keyboard", []):
        for b in row: b.pop("style", None)
    return m

def _call_markup(method, **params):
    try: return tg(method, **params)
    except TGError as e:
        if "message is not modified" in e.desc: return None
        if e.code == 400 and params.get("reply_markup"):
            low = e.desc.lower()
            if "style" in low or "button" in low or "unsupported" in low:
                params["reply_markup"] = _strip_style(params["reply_markup"])
                try: return tg(method, **params)
                except TGError as e2:
                    if "message is not modified" in e2.desc: return None
                    raise
        raise

def send(cid, text, markup=None):
    p = dict(chat_id=cid, text=text, parse_mode="HTML", link_preview_options={"is_disabled": True})
    if markup: p["reply_markup"] = markup
    return _call_markup("sendMessage", **p)

def edit(cid, mid, text, markup=None):
    p = dict(chat_id=cid, message_id=mid, text=text, parse_mode="HTML",
             link_preview_options={"is_disabled": True})
    if markup: p["reply_markup"] = markup
    return _call_markup("editMessageText", **p)

def show(cid, mid, text, markup=None):
    if mid:
        try: return edit(cid, mid, text, markup)
        except TGError: pass
    return send(cid, text, markup)

def B(text, cb=None, url=None, style=None, copy_text=None):
    b = {"text": small_caps(str(text))}
    if url: b["url"] = url
    elif copy_text: b["copy_text"] = {"text": copy_text}
    else: b["callback_data"] = cb or "noop"
    if style: b["style"] = style
    return b

def KB(*rows):
    return {"inline_keyboard": [list(r) for r in rows if r]}

def C():
    return COLOR_THEMES.get(get_button_color(), COLOR_THEMES["blue"])

# ═══════════════════════════════════════════════════════════════
#  FORCE JOIN
# ═══════════════════════════════════════════════════════════════
_member_cache = {}
_CACHE_TTL = 60

def _is_member(uid, chat_id):
    ck = (uid, chat_id)
    if time.time() - _member_cache.get(ck, 0) < _CACHE_TTL: return True
    try:
        m = tg("getChatMember", chat_id=chat_id, user_id=uid)
    except TGError: return True
    st = m.get("status")
    ok = st in ("creator", "administrator", "member") or (st == "restricted" and m.get("is_member", False))
    if ok: _member_cache[ck] = time.time()
    return ok

def missing_channels(uid):
    if uid in ADMIN_IDS: return []
    miss = []
    for c in PERM_CHANNELS:
        if not _is_member(uid, c["chat_id"]): miss.append(c)
    if force_join_on():
        perm_ids = {c["chat_id"] for c in PERM_CHANNELS}
        for c in fb_channels():
            if c.get("chat_id") not in perm_ids and not _is_member(uid, c["chat_id"]):
                miss.append(c)
    return miss

_pending_action = {}

def join_prompt(cid, mid, miss):
    ct = C()
    chan_lines = [f"📢 {E(c['title'] or 'Channel')}" for c in miss]
    text = card("🔒 join required", [
        "you must join our channels to continue",
        *chan_lines,
        "after joining all channels, tap ✅ verify below",
    ], "join · then verify", mood="calm") + FOOTER
    rows = []
    styles = [ct["main"], ct["good"]]
    for i, c in enumerate(miss):
        if c.get("link"):
            rows.append([B(f"📢 {c['title'] or 'Join Channel'}", url=c["link"], style=styles[i % 2])])
    rows.append([B("✅ Verify", cb="verify", style=ct["good"])])
    rows.append([B("💎 Buy Premium", url=CONTACT_URL, style=ct["bad"])])
    show(cid, mid, text, KB(*rows))

def gate(uid, cid, mid=None, pending=None):
    miss = missing_channels(uid)
    if not miss: return True
    if pending: _pending_action[uid] = pending
    join_prompt(cid, mid, miss)
    return False

def try_count_referral(uid):
    row = fb_user(uid)
    if not row or not row.get("referred_by") or row.get("ref_counted"): return
    if missing_channels(uid): return
    fb_user_update(uid, ref_counted=True)
    sq_update_user(uid, ref_counted=1)
    ref = row["referred_by"]
    sq_incr_user(ref, "ref_points", 1)
    sq_incr_user(ref, "total_refs", 1)
    sq_incr_user(ref, "credits", CREDITS_PER_REFERRAL)
    if _db:
        try:
            _db.collection("users").document(str(ref)).set({
                "ref_points": firestore.Increment(1),
                "total_refs": firestore.Increment(1),
                "credits": firestore.Increment(CREDITS_PER_REFERRAL)
            }, merge=True)
        except: pass
    r = fb_user(ref)
    if r:
        name = E(row.get("first_name") or "Someone")
        try:
            ct = C()
            send(ref, card("🎉 new referral!", [
                f"{name} joined using your link",
                f"🎁 +{CREDITS_PER_REFERRAL} credits added",
                f"⭐ points · <code>{(r.get('ref_points') or 0) + 1}</code>",
            ], "keep sharing to earn more") + FOOTER,
            KB([B("🎁 Refer & Earn", cb="refer", style=ct["good"])]))
        except TGError: pass

# ═══════════════════════════════════════════════════════════════
#  SCREENS
# ═══════════════════════════════════════════════════════════════
def fmt_exp(ts):
    left = ts - int(time.time())
    if left < 0: return "expired"
    d, h = left // 86400, (left % 86400) // 3600
    return f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(ts))} utc  ({d}d {h}h left)"

COLOR_NAMES = {"blue": "🔵 blue", "red": "🔴 red", "green": "🟢 green"}
COLOR_ORDER = ["blue", "red", "green"]

def _next_color(current):
    idx = COLOR_ORDER.index(current) if current in COLOR_ORDER else 0
    return COLOR_ORDER[(idx + 1) % len(COLOR_ORDER)]

def screen_start(uid, name):
    ct = C()
    row = fb_user(uid)
    credits = row.get("credits", 0) if row else 0
    is_premium = row and row.get("is_premium", False)
    prem = " 💎" if is_premium else ""
    text = card(f"Rdx sms bomber{prem}", [
        f"hey <b>{E(name or 'there')}</b> 👋",
        f"credits · <code>{credits}</code>",
        f"referral bonus · <code>{CREDITS_PER_REFERRAL}</code> credits per friend",
        "🌍 select target country",
    ], "choose your target below") + FOOTER
    rows = [
        [B("🇧🇩 Bangladesh", cb="country:bd",  style=ct["main"]),
         B("🇮🇳 India",      cb="country:ind", style=ct["main"]),
         B("🇵🇰 Pakistan",   cb="country:pak", style=ct["main"])],
        [B("💳 My Credits",  cb="credits",     style=ct["nav"]),
         B("🎁 Refer",       cb="refer",       style=ct["good"]),
         B("🔑 API Key",     cb="mykey",       style=ct["nav"])],
        [B("🎟️ Gift Code",   cb="gift",        style=ct["good"]),
         B("💎 Buy Premium", url=CONTACT_URL,  style=ct["bad"]),
         B("👨‍💻 Dev",        cb="dev",         style=ct["nav"])],
    ]
    if uid in ADMIN_IDS:
        rows.append([B("🛡️ Admin Panel", cb="adm:home", style=ct["bad"])])
    return text, KB(*rows)

def screen_india(uid):
    ct = C()
    row = fb_user(uid)
    is_premium = row and row.get("is_premium", False)
    credits = row.get("credits", 0) if row else 0
    nuclear_txt = f"☢️ Nuclear ({CREDITS_NUCLEAR_IND}c)" if is_premium else "🔒 Nuclear — Premium Only"
    nuclear_cb  = "bomb:ind:nuclear" if is_premium else "premium_only"
    text = card("🇮🇳 india — attack mode", [
        f"credits · <code>{credits}</code>",
        f"📲 sms bomb — <code>{CREDITS_SMS_BOMB}</code> credits\n60+ otp apis fired simultaneously",
        f"⚡ custom sms — <code>{CREDITS_CUSTOM_BOMB}</code> credits\ncarrybee merchant api (deep penetration)",
        f"☢️ nuclear — <code>{CREDITS_NUCLEAR_IND}</code> credits\nsms + custom + firebase (maximum destruction)",
    ], "choose attack mode") + FOOTER
    return text, KB(
        [B(f"📲 SMS ({CREDITS_SMS_BOMB}c)",      cb="bomb:ind:sms",    style=ct["main"]),
         B(f"⚡ Custom ({CREDITS_CUSTOM_BOMB}c)", cb="bomb:ind:custom", style=ct["good"])],
        [B(nuclear_txt,                           cb=nuclear_cb,        style=ct["bad"])],
        [B("🔙 Back",                             cb="back:start",      style=ct["nav"])],
    )

def screen_bd(uid):
    ct = C()
    row = fb_user(uid)
    is_premium = row and row.get("is_premium", False)
    credits = row.get("credits", 0) if row else 0
    nuclear_txt = f"☢️ Nuclear ({CREDITS_NUCLEAR_BD}c)" if is_premium else "🔒 Nuclear — Premium Only"
    nuclear_cb  = "bomb:bd:nuclear" if is_premium else "premium_only"
    sms_hits     = CREDITS_SMS_BOMB   * PROXY_MULTIPLIER
    call_hits    = CREDITS_CALL_BOMB  * PROXY_MULTIPLIER
    mixed_hits   = CREDITS_MIXED_BD   * PROXY_MULTIPLIER
    nuclear_hits = CREDITS_NUCLEAR_BD * PROXY_MULTIPLIER
    text = card("🇧🇩 bangladesh — attack mode", [
        f"credits · <code>{credits}</code>",
        f"⚡ multiplier · <code>{PROXY_MULTIPLIER}×</code> proxies per call",
        f"📲 sms — <code>{CREDITS_SMS_BOMB}</code>c → <code>{sms_hits}</code> total hits",
        f"📞 call — <code>{CREDITS_CALL_BOMB}</code>c → <code>{call_hits}</code> total hits",
        f"🔀 mixed — <code>{CREDITS_MIXED_BD}</code>c → sms + call each <code>{mixed_hits}</code> hits",
        f"☢️ nuclear — <code>{CREDITS_NUCLEAR_BD}</code>c → both type <code>{nuclear_hits}</code>+ hits",
    ], "choose attack mode") + FOOTER
    return text, KB(
        [B(f"📲 SMS ({CREDITS_SMS_BOMB}c)",     cb="bomb:bd:sms",   style=ct["main"]),
         B(f"📞 Call ({CREDITS_CALL_BOMB}c)",    cb="bomb:bd:call",  style=ct["good"])],
        [B(f"🔀 Mixed ({CREDITS_MIXED_BD}c)",    cb="bomb:bd:mixed", style=ct["main"]),
         B(nuclear_txt,                          cb=nuclear_cb,       style=ct["bad"])],
        [B("🔙 Back",                            cb="back:start",    style=ct["nav"])],
    )

def screen_pak(uid):
    ct = C()
    row = fb_user(uid)
    credits = row.get("credits", 0) if row else 0
    text = card("🇵🇰 pakistan — sms only", [
        f"credits · <code>{credits}</code>",
        f"📲 sms bomb — <code>{CREDITS_SMS_BOMB}</code> credits",
    ], "ready to fire") + FOOTER
    return text, KB(
        [B(f"📲 SMS ({CREDITS_SMS_BOMB}c)", cb="bomb:pak:sms", style=ct["main"])],
        [B("🔙 Back",                       cb="back:start",   style=ct["nav"])],
    )

def screen_credits(uid):
    ct = C()
    row = fb_user(uid)
    credits = row.get("credits", 0) if row else 0
    is_premium = row and row.get("is_premium", False)
    text = card("💳 my credits", [
        f"balance · <code>{credits}</code> credits",
        f"premium · {'✅ active' if is_premium else '❌ no'}",
        f"🇮🇳 sms {CREDITS_SMS_BOMB}c  |  custom {CREDITS_CUSTOM_BOMB}c  |  nuclear {CREDITS_NUCLEAR_IND}c",
        f"🇧🇩 sms {CREDITS_SMS_BOMB}c  |  call {CREDITS_CALL_BOMB}c  |  mixed {CREDITS_MIXED_BD}c  |  nuclear {CREDITS_NUCLEAR_BD}c",
        f"🇵🇰 sms {CREDITS_SMS_BOMB}c",
        f"refer friends · +{CREDITS_PER_REFERRAL} per referral",
    ], "earn or spend your credits") + FOOTER
    return text, KB(
        [B("🎁 Refer & Earn", cb="refer",      style=ct["good"]),
         B("🎟️ Redeem Gift",  cb="gift",       style=ct["main"])],
        [B("💎 Buy Credits",  url=CONTACT_URL, style=ct["bad"])],
        [B("🔙 Back",         cb="back:start", style=ct["nav"])],
    )

def screen_refer(uid):
    ct = C()
    u = fb_user(uid)
    link = f"https://t.me/{BOT_USERNAME}?start=ref_{uid}"
    pts = u.get("ref_points", 0) if u else 0
    plans = get_refer_subs()
    tiers = []
    for p in plans:
        mark = "✅" if pts >= p["ref_cost"] else "🔒"
        tiers.append(f"{mark} {p['ref_cost']} refs → {p.get('label') or str(p['days'])+' days'}")
    text = card("🎁 refer & earn", [
        f"invite friends · get <code>{CREDITS_PER_REFERRAL}</code> credits per referral",
        f"your link · <code>{E(link)}</code>",
        f"total refs · <code>{u.get('total_refs', 0) if u else 0}</code>",
        f"points · <code>{pts}</code>",
        "🏆 key rewards",
        *tiers,
    ], "share your link to earn") + FOOTER
    rows = []
    for i, p in enumerate(plans):
        if pts >= p["ref_cost"]:
            rows.append([B(
                f"🔑 Claim {p.get('label') or str(p['days'])+' Days'}  (−{p['ref_cost']} pts)",
                cb=f"claim:{i}", style=ct["good"]
            )])
    share = ("https://t.me/share/url?url=" + requests.utils.quote(link, safe="") +
             "&text=" + requests.utils.quote("💣 Bomb SMS free with Rdx Bomber!", safe=""))
    rows.append([B("📤 Share", url=share, style=ct["main"]),
                 B("📋 Copy",  copy_text=link, style=ct["main"])])
    rows.append([B("🔙 Back", cb="back:start", style=ct["nav"])])
    return text, KB(*rows)

def api_guide(key=None):
    k = key or "YOUR_KEY"
    return card("📖 api guide", [
        f"base · <code>{E(BASE_URL)}</code>",
        f"🇮🇳 india · <code>{E(BASE_URL)}/ind?key={k}&number=91XXXXXXXXXX&mode=sms</code>",
        f"🇧🇩 bd · <code>{E(BASE_URL)}/bd?key={k}&number=880XXXXXXXX&mode=sms</code>",
        f"🇵🇰 pak · <code>{E(BASE_URL)}/pak?key={k}&number=92XXXXXXXXXX</code>",
        "modes · sms  ·  custom (india)  ·  call / mixed / nuclear (bd)",
    ], "copy your key and start bombing") + FOOTER

def screen_key(uid):
    ct = C()
    row = active_key(uid)
    if not row:
        plans = get_refer_subs()
        tier_lines = [f"{p['ref_cost']} refs → {p.get('label') or str(p['days'])+' days'}" for p in plans]
        text = card("🔑 my api key", ["no active api key", *tier_lines], "earn refs or buy to get a key") + FOOTER
        return text, KB(
            [B("🎁 Refer & Earn", cb="refer",      style=ct["good"])],
            [B("💎 Buy Key",      url=CONTACT_URL, style=ct["bad"])],
            [B("🔙 Back",         cb="back:start", style=ct["nav"])],
        )
    lim = row.get("daily_limit") or daily_limit()
    text = card("🔑 my api key", [
        f"key · <code>{row['key']}</code>",
        f"expires · {fmt_exp(row['expires_at'])}",
        f"today · <code>{fb_usage_today(row['key'])}/{lim}</code>",
        f"🇮🇳 <code>{E(BASE_URL)}/ind?key={row['key']}&number=91XX&mode=sms</code>",
        f"🇧🇩 <code>{E(BASE_URL)}/bd?key={row['key']}&number=880XX&mode=sms</code>",
        f"🇵🇰 <code>{E(BASE_URL)}/pak?key={row['key']}&number=92XX</code>",
    ], "copy key and start bombing") + FOOTER
    return text, KB(
        [B("📋 Copy Key", copy_text=row["key"], style=ct["good"]),
         B("📖 Guide",    cb="docs",            style=ct["main"])],
        [B("💎 Upgrade",  url=CONTACT_URL,      style=ct["bad"]),
         B("🔙 Back",     cb="back:start",      style=ct["nav"])],
    )

def key_card(key, exp, label=""):
    ct = C()
    text = card("🎉 api key ready!", [
        f"key · <code>{key}</code>",
        f"plan · {E(label or 'custom')}",
        f"expires · {fmt_exp(exp)}",
        f"🇮🇳 <code>{E(BASE_URL)}/ind?key={key}&number=91XX&mode=sms</code>",
        f"🇧🇩 <code>{E(BASE_URL)}/bd?key={key}&number=880XX&mode=sms</code>",
        f"🇵🇰 <code>{E(BASE_URL)}/pak?key={key}&number=92XX</code>",
    ], "copy your key and start bombing", mood="bold") + FOOTER
    return text, KB(
        [B("📋 Copy Key", copy_text=key,   style=ct["good"])],
        [B("📖 Guide",    cb="docs",       style=ct["main"]),
         B("🔙 Back",     cb="back:start", style=ct["nav"])],
    )

def screen_docs(uid):
    ct = C()
    row = active_key(uid)
    return api_guide(row["key"] if row else None), KB(
        [B("🔑 My Key",  cb="mykey",      style=ct["main"]),
         B("🎁 Get Key", cb="refer",      style=ct["good"])],
        [B("🔙 Back",    cb="back:start", style=ct["nav"])],
    )

def screen_buy():
    ct = C()
    text = card("💎 buy premium", [
        "credits (custom amount)",
        "api key (instant activation)",
        "premium upgrade (nuclear bomb unlock)",
        f"contact @{E(CONTACT_USERNAME)} to purchase",
    ], "get in touch below") + FOOTER
    return text, KB(
        [B("💬 Contact to Buy", url=CONTACT_URL, style=ct["good"])],
        [B("🔙 Back",           cb="back:start", style=ct["nav"])],
    )

def screen_gift():
    ct = C()
    text = card("🎟️ redeem gift code", [
        "send a gift code to claim credits or premium access",
        "example · <code>EXO-GIFT-ABCD1234</code>",
    ], "type your code below") + FOOTER
    return text, KB([B("🔙 Back", cb="back:start", style=ct["nav"])])

def handle_gift_code(uid, cid, code):
    ct = C()
    code = code.strip().upper()
    gc = fb_gift_get(code)
    back = KB([B("🔙 Back", cb="back:start", style=ct["nav"])])
    if not gc:
        return send(cid, card("❌ invalid code", ["gift code not found"], "try again or contact dev", mood="calm") + FOOTER, back)
    if gc.get("expires_at") and gc["expires_at"] < int(time.time()):
        return send(cid, card("❌ expired", ["this gift code has expired"], "", mood="calm") + FOOTER, back)
    max_u = gc.get("max_users", 0)
    if max_u and gc.get("claimed_count", 0) >= max_u:
        return send(cid, card("❌ fully claimed", ["gift code fully redeemed"], "", mood="calm") + FOOTER, back)
    if fb_gift_claim_exists(code, uid):
        return send(cid, card("❌ already claimed", ["you already used this gift code"], "", mood="calm") + FOOTER, back)
    fb_gift_claim(code, uid)
    new_data = {**gc, "claimed_count": gc.get("claimed_count", 0) + 1}
    fb_gift_set(code, new_data)
    extra_lines = []
    if gc.get("is_premium"):
        fb_user_update(uid, is_premium=True)
        sq_update_user(uid, is_premium=1)
        extra_lines.append("💎 premium access unlocked!")
    add_credits(uid, gc["credits"])
    send(cid, card("🎉 gift claimed!", [
        f"+{gc['credits']} credits added", *extra_lines,
    ], "your credits are ready") + FOOTER,
    KB([B("💳 My Credits", cb="credits",    style=ct["good"]),
        B("🔙 Back",       cb="back:start", style=ct["nav"])]))

# ═══════════════════════════════════════════════════════════════
#  BOMB FLOW
# ═══════════════════════════════════════════════════════════════
_inflight      = set()
_inflight_lock = threading.Lock()

BOMB_COSTS = {
    ("ind", "sms"):     CREDITS_SMS_BOMB,
    ("ind", "custom"):  CREDITS_CUSTOM_BOMB,
    ("ind", "nuclear"): CREDITS_NUCLEAR_IND,
    ("bd",  "sms"):     CREDITS_SMS_BOMB,
    ("bd",  "call"):    CREDITS_CALL_BOMB,
    ("bd",  "mixed"):   CREDITS_MIXED_BD,
    ("bd",  "nuclear"): CREDITS_NUCLEAR_BD,
    ("pak", "sms"):     CREDITS_SMS_BOMB,
}

def call_bomb_api(country, number, mode="sms"):
    try:
        url    = f"http://127.0.0.1:{LOCAL_PORT}/{country}"
        params = {"key": "INTERNAL", "number": number, "mode": mode}
        r = requests.get(url, params=params,
                         headers={"X-Internal-Token": INTERNAL_TOKEN},
                         timeout=300)
        return r.json(), None
    except Exception as e:
        return None, f"API unreachable ({type(e).__name__})"

def handle_bomb(uid, cid, country, mode, number):
    ct = C()
    pending = {"action": "bomb", "country": country, "mode": mode, "number": number}
    if not gate(uid, cid, pending=pending): return
    with _inflight_lock:
        if uid in _inflight:
            send(cid, card("⏳ wait", ["your previous bomb is still running"], "please wait", mood="calm") + FOOTER)
            return
        _inflight.add(uid)
    try:
        flags = {"ind": "🇮🇳", "bd": "🇧🇩", "pak": "🇵🇰"}
        wait = send(cid, card(f"💣 bombing {flags.get(country,'')} {country.upper()}…", [
            f"target · <code>{E(number)}</code>",
            f"mode · {mode.upper()}",
            "firing all apis now…",
        ], "stand by", mood="bold") + FOOTER)
        mid = wait["message_id"] if wait else None
        data, err = call_bomb_api(country, number, mode)
        buy_row = [B("💎 Buy Credits", url=CONTACT_URL, style=ct["bad"]),
                   B("👨‍💻 Dev",        url=DEV_URL,     style=ct["nav"])]
        ok = bool(data) and data.get("status") is True
        if not ok:
            reason = err or (data or {}).get("message") or "all apis failed"
            show(cid, mid, card("❌ bomb failed", [E(str(reason))], "try again or contact dev", mood="calm") + FOOTER,
                 KB(buy_row))
            return
        sent  = data.get("total_sent") or data.get("sent", 0)
        total = data.get("total_apis", 0)
        rate  = (sent / total * 100) if total else 0
        count_line = "☢️ maximum payload delivered" if mode == "nuclear" else f"apis · <code>{sent}/{total}</code> ({rate:.1f}%)"
        show(cid, mid, card("✅ bomb complete!", [
            f"{flags.get(country,'')} country · {country.upper()}",
            f"target · <code>{E(number)}</code>",
            f"mode · {mode.upper()}",
            count_line,
            "💥 target destroyed!",
        ], "fire another round?", mood="bold") + FOOTER,
        KB([B("💣 Bomb Again", cb=f"country:{country}", style=ct["good"])], buy_row))
        cost = BOMB_COSTS.get((country, mode), CREDITS_SMS_BOMB)
        sq_incr_user(uid, "credits", -cost)
        sq_incr_user(uid, "total_bombs", 1)
        if _db:
            try:
                _db.collection("users").document(str(uid)).set(
                    {"credits": firestore.Increment(-cost),
                     "total_bombs": firestore.Increment(1)}, merge=True)
            except: pass
    finally:
        with _inflight_lock: _inflight.discard(uid)

def start_bomb_flow(uid, cid, mid, country, mode):
    ct = C()
    row = fb_user(uid)
    if not row: return
    is_premium = row.get("is_premium", False)
    if mode == "nuclear" and not is_premium:
        return show(cid, mid, card("🔒 premium only", [
            "nuclear bomb requires premium",
            f"contact @{E(CONTACT_USERNAME)} to upgrade",
        ], "get premium below", mood="calm") + FOOTER,
        KB([B("💎 Get Premium", url=CONTACT_URL, style=ct["bad"])],
           [B("🔙 Back",        cb="back:start", style=ct["nav"])]))
    cost = BOMB_COSTS.get((country, mode), CREDITS_SMS_BOMB)
    if row.get("credits", 0) < cost:
        return show(cid, mid, card("❌ insufficient credits", [
            f"need · <code>{cost}</code> credits",
            f"balance · <code>{row.get('credits', 0)}</code> credits",
        ], "top up below", mood="calm") + FOOTER,
        KB([B("🎁 Refer & Earn", cb="refer",      style=ct["good"])],
           [B("💎 Buy Credits",  url=CONTACT_URL, style=ct["bad"])],
           [B("🔙 Back",         cb="back:start", style=ct["nav"])]))
    flags = {"ind": "🇮🇳", "bd": "🇧🇩", "pak": "🇵🇰"}
    country_name = {"ind": "india", "bd": "bangladesh", "pak": "pakistan"}.get(country, country)
    _pending_action[uid] = {"action": "bomb", "country": country, "mode": mode}
    show(cid, mid, card(f"{flags.get(country,'')} {country_name} — {mode}", [
        f"cost · <code>{cost}</code> credits",
        f"balance · <code>{row.get('credits', 0)}</code> credits",
        "📱 enter the target phone number",
        "include country code · 91XXXXXXXXXX / 880XXXXXXXX / 92XXXXXXXXXX",
    ], "type the number below") + FOOTER,
    KB([B("❌ Cancel", cb="back:start", style=ct["bad"])]))

# ═══════════════════════════════════════════════════════════════
#  ADMIN PANEL
# ═══════════════════════════════════════════════════════════════
admin_state = {}

def _stats():
    users = fb_users_all()
    now   = int(time.time())
    day0  = now - 86400
    keys  = []
    if _db:
        try: keys = [d.to_dict() for d in _db.collection("keys").stream()]
        except: pass
    chans = list_channels()
    return {
        "users":   len(users),
        "new24":   sum(1 for u in users if u.get("joined_at", 0) > day0),
        "banned":  sum(1 for u in users if u.get("banned")),
        "keys":    sum(1 for k in keys if k.get("expires_at", 0) > now and not k.get("revoked")),
        "chans":   len(chans),
        "bombs":   sum(u.get("total_bombs", 0) for u in users),
        "refs":    sum(u.get("total_refs", 0) for u in users),
        "credits": sum(u.get("credits", 0) for u in users),
        "premium": sum(1 for u in users if u.get("is_premium")),
    }

def adm_home():
    ct = C()
    s = _stats()
    wm = get_welcome_media()
    wm_status = f"{'📷' if wm.get('type')=='photo' else '🎬' if wm.get('type')=='video' else '❌'} {wm.get('type','none')}"
    text = card("🛡️ admin control panel", [
        f"👥 users · <code>{s.get('users',0)}</code>  🆕 24h · <code>{s.get('new24',0)}</code>",
        f"🔑 keys · <code>{s.get('keys',0)}</code>  📢 channels · <code>{s.get('chans',0)}</code>",
        f"💣 bombs · <code>{s.get('bombs',0)}</code>  🚫 banned · <code>{s.get('banned',0)}</code>",
        f"💰 credits · <code>{s.get('credits',0)}</code>  💎 premium · <code>{s.get('premium',0)}</code>",
        f"force-join · {'on ✅' if force_join_on() else 'off ❌'}",
        f"welcome media · {wm_status}",
        f"color · {COLOR_NAMES.get(get_button_color(), 'blue')}",
    ], "manage your bot below", mood="bold") + FOOTER
    return text, KB(
        [B("📊 Statistics",    cb="adm:stats",   style=ct["main"]),
         B("👥 Users",         cb="adm:users",   style=ct["main"])],
        [B("📢 Channels",      cb="adm:ch",      style=ct["good"]),
         B("📣 Broadcast",     cb="adm:bc",      style=ct["good"])],
        [B("🔑 API Keys",      cb="adm:keys",    style=ct["main"]),
         B("⚙️ Settings",      cb="adm:set",     style=ct["main"])],
        [B("🎟️ Gift Codes",    cb="adm:gift",    style=ct["good"]),
         B("🎯 Refer Subs",    cb="adm:rsubs",   style=ct["good"])],
        [B("🖼 Welcome Media", cb="adm:wm",      style=ct["main"]),
         B("💾 Data",          cb="adm:data",    style=ct["main"])],
        [B("💎 Premium Users", cb="adm:prem",    style=ct["bad"])],
        [B("🌐 Web Panel", url=BASE_URL + "/admin", style=ct["good"]),
         B("🔙 Close",         cb="back:start",  style=ct["bad"])],
    )

def adm_stats():
    ct = C()
    s = _stats()
    text = card("📊 statistics", [
        f"👥 total users · <code>{s.get('users',0)}</code>",
        f"🆕 joined 24h · <code>{s.get('new24',0)}</code>",
        f"🚫 banned · <code>{s.get('banned',0)}</code>",
        f"💣 total bombs · <code>{s.get('bombs',0)}</code>",
        f"🎁 total refs · <code>{s.get('refs',0)}</code>",
        f"🔑 active keys · <code>{s.get('keys',0)}</code>",
        f"💰 total credits · <code>{s.get('credits',0)}</code>",
        f"📢 channels · <code>{s.get('chans',0)}</code>",
        f"💎 premium · <code>{s.get('premium',0)}</code>",
    ], "live stats", mood="bold")
    return text, KB(
        [B("🔄 Refresh", cb="adm:stats", style=ct["good"]),
         B("🔙 Back",    cb="adm:home",  style=ct["nav"])],
    )

def adm_channels():
    ct = C()
    chans = list_channels()
    chan_lines = [
        f"{E(c.get('title') or c.get('chat_id'))}  <code>{E(c.get('chat_id'))}</code>"
        for c in chans
    ]
    text = card("📢 force-join channels", [
        *(chan_lines if chan_lines else ["no channels yet"]),
        "bot must be admin in every channel",
    ], "manage channels below")
    rows = [[B(f"❌ Remove {c.get('title') or c.get('chat_id')}"[:55],
               cb=f"adm:chdel:{c.get('chat_id')}", style=ct["bad"])] for c in chans]
    rows.append([B("➕ Add Channel", cb="adm:chadd", style=ct["good"])])
    rows.append([B("🔙 Back",        cb="adm:home",  style=ct["nav"])])
    return text, KB(*rows)

def adm_users():
    ct = C()
    text = card("👥 user manager", ["find a user by telegram id to manage"], "search below")
    return text, KB(
        [B("🔍 Find User", cb="adm:ufind", style=ct["good"])],
        [B("🔙 Back",      cb="adm:home",  style=ct["nav"])],
    )

def adm_user_card(uid):
    ct = C()
    u = fb_user(uid)
    if not u:
        return (card("❌ user not found", [], "back to users", mood="calm"),
                KB([B("🔙 Back", cb="adm:users", style=ct["nav"])]))
    k = active_key(uid)
    text = card("👤 user card", [
        f"id · <code>{uid}</code>",
        f"name · {E(u.get('first_name') or '-')} {('@' + E(u.get('username'))) if u.get('username') else ''}",
        f"joined · {time.strftime('%Y-%m-%d', time.gmtime(u.get('joined_at', 0)))}",
        f"credits · <code>{u.get('credits',0)}</code>",
        f"premium · {'✅' if u.get('is_premium') else '❌'}",
        f"refs · <code>{u.get('total_refs',0)}</code>  pts · <code>{u.get('ref_points',0)}</code>",
        f"bombs · <code>{u.get('total_bombs',0)}</code>",
        f"banned · {'yes 🚫' if u.get('banned') else 'no ✅'}",
        f"key · {('<code>' + k['key'][:20] + '…</code> — ' + fmt_exp(k['expires_at'])) if k else 'none'}",
    ], "manage this user below", mood="bold")
    ban_btn = (B("✅ Unban", cb=f"adm:uunban:{uid}", style=ct["good"])
               if u.get("banned")
               else B("🚫 Ban", cb=f"adm:uban:{uid}", style=ct["bad"]))
    prem_btn = (B("🔓 Remove Premium", cb=f"adm:uunprem:{uid}", style=ct["bad"])
                if u.get("is_premium")
                else B("💎 Make Premium", cb=f"adm:uprem:{uid}", style=ct["good"]))
    return text, KB(
        [ban_btn, B("🗑 Revoke Key", cb=f"adm:urev:{uid}", style=ct["bad"])],
        [prem_btn],
        [B("💰 +10c",  cb=f"adm:ucred:{uid}:10",  style=ct["good"]),
         B("💰 +50c",  cb=f"adm:ucred:{uid}:50",  style=ct["good"]),
         B("💰 +100c", cb=f"adm:ucred:{uid}:100", style=ct["good"])],
        [B("🔑 +7d Key",  cb=f"adm:ukey:{uid}:7",  style=ct["main"]),
         B("🔑 +30d Key", cb=f"adm:ukey:{uid}:30", style=ct["main"])],
        [B("🔙 Back", cb="adm:users", style=ct["nav"])],
    )

def adm_keys():
    ct = C()
    keys = []
    if _db:
        try:
            keys = [d.to_dict() for d in _db.collection("keys")
                    .order_by("created_at", direction=firestore.Query.DESCENDING).limit(10).stream()]
        except: pass
    now = int(time.time())
    lines = []
    for k in keys:
        st = "🚫" if k.get("revoked") else ("✅" if k.get("expires_at",0) > now else "⌛")
        lines.append(f"{st} <code>{k['key'][:20]}…</code> · <code>{k['user_id']}</code>")
    text = card("🔑 api keys", [
        *(lines if lines else ["no keys yet"]),
        "showing 10 most recent",
    ], "generate or revoke below")
    return text, KB(
        [B("➕ Generate", cb="adm:kgen", style=ct["good"]),
         B("🗑 Revoke",   cb="adm:krev", style=ct["bad"])],
        [B("🔙 Back",     cb="adm:home", style=ct["nav"])],
    )

def adm_gift():
    ct = C()
    gifts = []
    if _db:
        try:
            gifts = [d.to_dict() for d in _db.collection("gift_codes")
                     .order_by("created_at", direction=firestore.Query.DESCENDING).limit(20).stream()]
        except: pass
    lines = []
    for g in gifts:
        st   = "✅" if not g.get("expires_at") or g["expires_at"] > int(time.time()) else "⌛"
        prem = " [prem]" if g.get("is_premium") else ""
        lines.append(f"{st} <code>{g['code']}</code>{prem} · {g['credits']}c · {g.get('claimed_count',0)}/{g.get('max_users') or '∞'}")
    text = card("🎟️ gift codes", [*(lines if lines else ["no gift codes"])], "create or manage gift codes")
    return text, KB(
        [B("➕ Create Gift", cb="adm:giftadd", style=ct["good"])],
        [B("🔙 Back",        cb="adm:home",    style=ct["nav"])],
    )

def adm_refer_subs():
    ct = C()
    subs = get_refer_subs()
    lines = []
    for i, s in enumerate(subs):
        label = s.get("label") or f"{s['days']}d"
        lines.append(f"{i+1}. <code>{s['ref_cost']}</code> refs → {label}")
    text = card("🎯 referral subscriptions", [
        "plans users can claim with referral points",
        *(lines if lines else ["no plans yet — using defaults"]),
        "format · <code>REF_COST DAYS LABEL</code>",
    ], "manage plans below")
    rows = []
    for i, s in enumerate(subs):
        if "id" in s:
            label = s.get("label") or f"{s['days']}d"
            rows.append([B(f"❌ Remove Plan {i+1} ({label})", cb=f"adm:rsubdel:{s['id']}", style=ct["bad"])])
    rows.append([B("➕ Add Plan", cb="adm:rsubadd", style=ct["good"])])
    rows.append([B("🔙 Back",    cb="adm:home",    style=ct["nav"])])
    return text, KB(*rows)

def adm_welcome_media():
    ct = C()
    wm = get_welcome_media()
    if wm and wm.get("type") and wm["type"] != "text":
        status_lines = [
            f"type · {wm['type']}",
            f"file id · <code>{wm.get('file_id','')[:30]}…</code>",
            f"caption · {E(wm.get('caption','(none)'))[:80]}",
        ]
        footer_txt = "update or clear below"
    else:
        status_lines = ["no welcome media set", "send a photo or video + caption to set it"]
        footer_txt = "set welcome media below"
    text = card("🖼 welcome media", [
        "shown to users when they open the bot",
        *status_lines,
        "to set: send me a photo or video",
        "you can add a caption with it",
    ], footer_txt, mood="calm") + FOOTER
    rows = []
    if wm and wm.get("type") and wm["type"] != "text":
        rows.append([B("🔍 Preview", cb="adm:wmpreview", style=ct["main"]),
                     B("🗑 Clear",   cb="adm:wmclear",   style=ct["bad"])])
    rows.append([B("📷 Set Photo/Video", cb="adm:wmset", style=ct["good"])])
    rows.append([B("🔙 Back",            cb="adm:home",  style=ct["nav"])])
    return text, KB(*rows)

def adm_data():
    ct = C()
    text = card("💾 data management", [
        "import · send json file or paste json text",
        "export · get all data as json",
    ], "manage data below", mood="calm") + FOOTER
    return text, KB(
        [B("📥 Import DB",  cb="adm:import", style=ct["main"]),
         B("📤 Export DB",  cb="adm:export", style=ct["main"])],
        [B("🔙 Back",       cb="adm:home",   style=ct["nav"])],
    )

def adm_prem():
    ct = C()
    users = []
    if _db:
        try:
            users = [d.to_dict() for d in _db.collection("users")
                     .where("is_premium", "==", True).limit(20).stream()]
        except:
            users = [u for u in sq_all_users(5000) if u.get("is_premium")]
    else:
        users = [u for u in sq_all_users(5000) if u.get("is_premium")]
    lines = [f"💎 <code>{u['id']}</code> — {E(u.get('first_name') or '-')}" for u in users]
    text = card("💎 premium users", [*(lines if lines else ["none yet"])], "these users have premium access")
    return text, KB([B("🔙 Back", cb="adm:home", style=ct["nav"])])

def adm_settings():
    ct = C()
    cur_color = get_button_color()
    text = card("⚙️ settings", [
        f"force-join · {'on ✅' if force_join_on() else 'off ❌'}",
        f"daily api limit/key · <code>{daily_limit()}</code>",
        f"credits per referral · <code>{CREDITS_PER_REFERRAL}</code>",
        f"button color · {COLOR_NAMES.get(cur_color, 'blue')}",
        f"contact · @{E(CONTACT_USERNAME)}",
    ], "configure bot settings")
    return text, KB(
        [B("🔒 Toggle Force-Join", cb="adm:settog", style=ct["main"]),
         B("📊 Set Daily Limit",   cb="adm:setlim", style=ct["good"])],
        [B("🔙 Back",              cb="adm:home",   style=ct["nav"])],
    )

# ─── Broadcast ───────────────────────────────────────────────────
def _broadcast_text(admin_cid, text_msg):
    users = fb_users_all()
    ok = bad = 0
    for u in users:
        if u.get("banned"): continue
        uid = u["id"]
        for _ in range(2):
            try:
                send(uid, text_msg + FOOTER)
                ok += 1
                break
            except TGError as e:
                if e.retry_after: time.sleep(e.retry_after + 1); continue
                bad += 1; break
        time.sleep(0.05)
    ct = C()
    send(admin_cid, card("📣 broadcast done", [
        f"✅ sent · <code>{ok}</code>",
        f"❌ failed · <code>{bad}</code>",
    ], "text broadcast complete", mood="bold"),
    KB([B("🔙 Admin Panel", cb="adm:home", style=ct["nav"])]))

def _broadcast_media(admin_cid, media_type: str, file_id: str, caption: str):
    users = fb_users_all()
    ok = bad = 0
    for u in users:
        if u.get("banned"): continue
        uid = u["id"]
        try:
            p = dict(chat_id=uid, caption=caption, parse_mode="HTML")
            if media_type == "photo": p["photo"] = file_id; tg("sendPhoto", **p)
            elif media_type == "video": p["video"] = file_id; tg("sendVideo", **p)
            elif media_type == "animation": p["animation"] = file_id; tg("sendAnimation", **p)
            ok += 1
        except TGError as e:
            if e.retry_after: time.sleep(e.retry_after + 1)
            bad += 1
        time.sleep(0.06)
    ct = C()
    send(admin_cid, card("📣 media broadcast done", [
        f"✅ sent · <code>{ok}</code>",
        f"❌ failed · <code>{bad}</code>",
        f"type · {media_type}",
    ], "broadcast complete", mood="bold"),
    KB([B("🔙 Admin Panel", cb="adm:home", style=ct["nav"])]))

# ═══════════════════════════════════════════════════════════════
#  ADMIN CALLBACK ROUTER
# ═══════════════════════════════════════════════════════════════
def on_admin_callback(uid, cid, mid, data, ack):
    ct = C()
    parts = data.split(":")
    act   = parts[1] if len(parts) > 1 else "home"
    prev  = admin_state.pop(uid, None)

    if act == "home":    return show(cid, mid, *adm_home())
    if act == "stats":   return show(cid, mid, *adm_stats())
    if act == "ch":      return show(cid, mid, *adm_channels())
    if act == "users":   return show(cid, mid, *adm_users())
    if act == "keys":    return show(cid, mid, *adm_keys())
    if act == "gift":    return show(cid, mid, *adm_gift())
    if act == "prem":    return show(cid, mid, *adm_prem())
    if act == "set":     return show(cid, mid, *adm_settings())
    if act == "rsubs":   return show(cid, mid, *adm_refer_subs())
    if act == "wm":      return show(cid, mid, *adm_welcome_media())
    if act == "data":    return show(cid, mid, *adm_data())

    if act == "wmset":
        admin_state[uid] = {"mode": "set_welcome_media"}
        return show(cid, mid, card("🖼 set welcome media", [
            "send a photo or video now",
            "you can include a caption in the message",
            "or send caption separately after uploading",
            "type /skip to set media without caption",
        ], "send photo or video below", mood="calm") + FOOTER,
        KB([B("❌ Cancel", cb="adm:wm", style=ct["bad"])]))

    if act == "wmclear":
        clear_welcome_media()
        ack("Welcome media cleared")
        return show(cid, mid, *adm_welcome_media())

    if act == "wmpreview":
        wm = get_welcome_media()
        if wm and wm.get("file_id"):
            sent = send_welcome_media(cid)
            if not sent: send(cid, "No welcome media set.")
        return ack("Preview sent")

    if act == "import":
        admin_state[uid] = {"mode": "import_db"}
        return show(cid, mid, card("💾 import database", [
            "send a json file or paste json text",
            "format · <code>{\"users\":[...]}</code>",
        ], "send json below", mood="calm") + FOOTER,
        KB([B("❌ Cancel", cb="adm:home", style=ct["bad"])]))

    if act == "export":
        if _db:
            try:
                users = [d.to_dict() for d in _db.collection("users").stream()]
                keys  = [d.to_dict() for d in _db.collection("keys").stream()]
                gifts = [d.to_dict() for d in _db.collection("gift_codes").stream()]
                chans = [d.to_dict() for d in _db.collection("channels").stream()]
            except:
                users = sq_all_users(5000); keys=[]; gifts=[]; chans=sq_all_channels()
        else:
            users = sq_all_users(5000); keys=[]; gifts=[]; chans=sq_all_channels()
        payload = json.dumps({"users": users, "keys": keys, "gifts": gifts, "channels": chans}, default=str)
        _send_document(cid, "rdx_export.json", payload.encode())
        return show(cid, mid,
                    card("✅ export sent!", ["json file sent to chat"], "download it now", mood="calm"),
                    KB([B("🔙 Admin Panel", cb="adm:home", style=ct["nav"])]))

    if act == "chadd":
        admin_state[uid] = {"mode": "add_channel"}
        return show(cid, mid, card("➕ add channel", [
            "send the channel @username or -100… id",
            "private channel · send <code>-100ID https://t.me/+invite</code>",
        ], "type channel info below", mood="calm"),
        KB([B("❌ Cancel", cb="adm:ch", style=ct["bad"])]))

    if act == "chdel":
        fb_channel_del(parts[2])
        ack("Channel removed")
        return show(cid, mid, *adm_channels())

    if act == "bc":
        admin_state[uid] = {"mode": "broadcast_choose"}
        return show(cid, mid, card("📣 broadcast", [
            "choose broadcast type",
            "text · send a text message to all users",
            "media · send a photo or video to all users",
        ], "choose below", mood="calm") + FOOTER,
        KB([B("📝 Text Only",   cb="adm:bc_text",  style=ct["main"]),
            B("📷 Photo/Video", cb="adm:bc_media", style=ct["good"])],
           [B("❌ Cancel", cb="adm:home", style=ct["bad"])]))

    if act == "bc_text":
        admin_state[uid] = {"mode": "broadcast_text"}
        return show(cid, mid, card("📝 text broadcast", [
            "send the message to broadcast to all users",
            "html supported · <b>bold</b>  <i>italic</i>  <code>code</code>",
        ], "type your message below", mood="calm") + FOOTER,
        KB([B("❌ Cancel", cb="adm:home", style=ct["bad"])]))

    if act == "bc_media":
        admin_state[uid] = {"mode": "broadcast_media"}
        return show(cid, mid, card("📷 media broadcast", [
            "send a photo or video to broadcast",
            "you can include a caption in the message",
            "caption supports html formatting",
        ], "send photo or video below", mood="calm") + FOOTER,
        KB([B("❌ Cancel", cb="adm:home", style=ct["bad"])]))

    if act == "bcgo":
        txt = (prev or {}).get("text")
        if not txt: return ack("Nothing to send", True)
        ack("Text broadcast started")
        threading.Thread(target=_broadcast_text, args=(cid, txt), daemon=True).start()
        return show(cid, mid, card("📣 broadcasting…", ["you will get a report when done"], "sit tight", mood="calm"))

    if act == "bcmediago":
        p = prev or {}
        media_type = p.get("media_type"); file_id = p.get("file_id"); caption = p.get("caption","")
        if not file_id: return ack("No media to broadcast", True)
        ack("Media broadcast started")
        threading.Thread(target=_broadcast_media, args=(cid, media_type, file_id, caption), daemon=True).start()
        return show(cid, mid, card("📣 media broadcasting…", ["you will get a report when done"], "sit tight", mood="calm"))

    if act == "ufind":
        admin_state[uid] = {"mode": "find_user"}
        return show(cid, mid,
                    card("🔍 find user", ["send the user's numeric telegram id"], "type id below", mood="calm"),
                    KB([B("❌ Cancel", cb="adm:users", style=ct["bad"])]))

    if act in ("uban", "uunban"):
        t = parts[2]
        fb_user_update(t, banned=(act == "uban"))
        sq_update_user(t, banned=1 if act == "uban" else 0)
        ack("Done")
        return show(cid, mid, *adm_user_card(t))

    if act in ("uprem", "uunprem"):
        t = parts[2]
        v = act == "uprem"
        fb_user_update(t, is_premium=v)
        sq_update_user(t, is_premium=1 if v else 0)
        ack("Updated")
        return show(cid, mid, *adm_user_card(t))

    if act == "urev":
        t = parts[2]
        if _db:
            try:
                for k in _db.collection("keys").where("user_id", "==", str(t)).stream():
                    k.reference.update({"revoked": True})
            except: pass
        with _sq_lock:
            conn = _sq()
            try:
                conn.execute("UPDATE api_keys SET revoked=1 WHERE user_id=?", (str(t),))
                conn.commit()
            except: pass
            finally: conn.close()
        ack("Keys revoked")
        return show(cid, mid, *adm_user_card(t))

    if act == "ukey":
        t, days = parts[2], int(parts[3])
        grant_key(t, days, f"Admin {days}d")
        ack("Key granted")
        return show(cid, mid, *adm_user_card(t))

    if act == "ucred":
        t, amt = parts[2], int(parts[3])
        add_credits(t, amt)
        ack(f"+{amt} credits")
        return show(cid, mid, *adm_user_card(t))

    if act == "kgen":
        admin_state[uid] = {"mode": "gen_key"}
        return show(cid, mid,
                    card("➕ generate key", ["send · <code>USER_ID DAYS</code>", "example · <code>123456 30</code>"], "type below", mood="calm"),
                    KB([B("❌ Cancel", cb="adm:keys", style=ct["bad"])]))

    if act == "krev":
        admin_state[uid] = {"mode": "revoke_key"}
        return show(cid, mid,
                    card("🗑 revoke key", ["send the full api key to revoke"], "type key below", mood="calm"),
                    KB([B("❌ Cancel", cb="adm:keys", style=ct["bad"])]))

    if act == "giftadd":
        admin_state[uid] = {"mode": "create_gift"}
        return show(cid, mid, card("🎟️ create gift code", [
            "send · <code>CREDITS MAX_USERS DAYS_VALID [PREMIUM]</code>",
            "example · <code>50 100 7</code>",
            "premium example · <code>50 100 7 premium</code>",
        ], "type below", mood="calm"),
        KB([B("❌ Cancel", cb="adm:gift", style=ct["bad"])]))

    if act == "settog":
        fb_setting_set("force_join", "0" if force_join_on() else "1")
        ack("Toggled")
        return show(cid, mid, *adm_settings())

    if act == "setlim":
        admin_state[uid] = {"mode": "set_limit"}
        return show(cid, mid,
                    card("📊 daily limit", ["send a number (requests/key/day)"], "type below", mood="calm"),
                    KB([B("❌ Cancel", cb="adm:set", style=ct["bad"])]))

    if act == "rsubadd":
        admin_state[uid] = {"mode": "add_refer_sub"}
        return show(cid, mid, card("🎯 add referral plan", [
            "send · <code>REF_COST DAYS LABEL</code>",
            "example · <code>5 7 One Week</code>",
        ], "type below", mood="calm"),
        KB([B("❌ Cancel", cb="adm:rsubs", style=ct["bad"])]))

    if act == "rsubdel":
        sq_del_refer_sub(int(parts[2]))
        ack("Plan removed")
        return show(cid, mid, *adm_refer_subs())

    return ack()

def on_admin_input(uid, cid, text_in, msg=None):
    ct = C()
    st   = admin_state.get(uid) or {}
    mode = st.get("mode")
    back = lambda cb: KB([B("🔙 Back", cb=cb, style=ct["nav"])])

    if msg and mode in ("set_welcome_media", "broadcast_media"):
        photo = msg.get("photo")
        video = msg.get("video")
        animation = msg.get("animation")
        caption = msg.get("caption") or ""

        if photo:
            file_id = photo[-1]["file_id"]
            media_type = "photo"
        elif video:
            file_id = video["file_id"]
            media_type = "video"
        elif animation:
            file_id = animation["file_id"]
            media_type = "animation"
        else:
            return send(cid, card("❌ unsupported", ["send a photo or video"], "try again", mood="calm"))

        if mode == "set_welcome_media":
            set_welcome_media(media_type, file_id, caption)
            admin_state.pop(uid, None)
            send(cid, card("✅ welcome media set!", [
                f"type · {media_type}",
                f"caption · {E(caption[:60]) if caption else '(none)'}",
                "users will see this when they open the bot",
            ], "done", mood="bold") + FOOTER,
            back("adm:wm"))

        elif mode == "broadcast_media":
            admin_state[uid] = {"mode": "confirm_media_broadcast",
                                 "media_type": media_type, "file_id": file_id, "caption": caption}
            send(cid, card("📣 confirm media broadcast", [
                f"type · {media_type}",
                f"caption · {E(caption[:80]) if caption else '(none)'}",
                "send this to ALL users?",
            ], "confirm below", mood="calm") + FOOTER,
            KB([B("✅ Send Now", cb="adm:bcmediago", style=ct["good"]),
                B("❌ Cancel",  cb="adm:home",       style=ct["bad"])]))
        return

    if not text_in: return

    try:
        if mode == "add_channel":
            parts = text_in.split()
            if not parts: raise ValueError("send @username or -100ID")
            ref  = parts[0]
            linkin = parts[1] if len(parts) > 1 else None
            m = re.match(r"https?://t\.me/([A-Za-z0-9_]{4,})/?$", ref)
            if m: ref = "@" + m.group(1)
            chat_ref = int(ref) if re.fullmatch(r"-?\d+", ref) else ref
            try: chat = tg("getChat", chat_id=chat_ref)
            except TGError as e: raise ValueError(f"Cannot access channel: {e.desc}")
            try: me = tg("getChatMember", chat_id=chat["id"], user_id=BOT_ID)
            except TGError as e: raise ValueError(f"Bot not in channel: {e.desc}")
            if me.get("status") not in ("administrator", "creator"):
                raise ValueError("make the bot an admin in the channel first")
            if chat.get("username"): link = f"https://t.me/{chat['username']}"
            elif linkin and linkin.startswith("http"): link = linkin
            else:
                try: link = tg("createChatInviteLink", chat_id=chat["id"], name="rdx")["invite_link"]
                except TGError: raise ValueError("Private channel: send -100ID https://t.me/+invite")
            title = chat.get("title") or chat.get("username") or str(chat["id"])
            fb_channel_add(str(chat["id"]), title, link)
            admin_state.pop(uid, None)
            send(cid, card("✅ channel added", [f"{E(title)}"], "done", mood="calm"), back("adm:ch"))

        elif mode == "broadcast_text":
            try: send(cid, "👀 preview:\n\n" + text_in + FOOTER)
            except TGError as e:
                return send(cid, card("❌ invalid html", [E(e.desc)], "fix and try again", mood="calm"))
            admin_state[uid] = {"mode": "confirm_text_broadcast", "text": text_in}
            send(cid, card("📣 confirm broadcast", ["send this text to all users?"], "confirm below", mood="calm") + FOOTER,
            KB([B("✅ Send Now", cb="adm:bcgo", style=ct["good"]),
                B("❌ Cancel",  cb="adm:home", style=ct["bad"])]))

        elif mode == "confirm_text_broadcast":
            pass

        elif mode == "find_user":
            if not text_in.strip().lstrip("-").isdigit():
                return send(cid, card("❌ numeric id only", [], "try again", mood="calm"))
            admin_state.pop(uid, None)
            send(cid, *adm_user_card(text_in.strip()))

        elif mode == "gen_key":
            p = text_in.split()
            if len(p) != 2 or not (p[0].isdigit() and p[1].isdigit()) or not (1 <= int(p[1]) <= 3650):
                return send(cid, card("❌ format error", ["send: USER_ID DAYS"], "try again", mood="calm"))
            if not fb_user(p[0]):
                return send(cid, card("❌ user not found", [], "check the id", mood="calm"))
            key, exp = grant_key(p[0], int(p[1]), f"Admin {p[1]}d")
            admin_state.pop(uid, None)
            send(cid, card("✅ key ready", [
                f"key · <code>{key}</code>",
                f"expires · {fmt_exp(exp)}",
            ], "sent to user", mood="bold"), back("adm:keys"))
            try:
                kt, km = key_card(key, exp, f"{p[1]} Days")
                send(p[0], kt, km)
            except TGError: pass

        elif mode == "revoke_key":
            if _db:
                try: _db.collection("keys").document(text_in.strip()).update({"revoked": True})
                except: pass
            with _sq_lock:
                conn = _sq()
                try:
                    conn.execute("UPDATE api_keys SET revoked=1 WHERE key=?", (text_in.strip(),))
                    conn.commit()
                except: pass
                finally: conn.close()
            admin_state.pop(uid, None)
            send(cid, card("✅ key revoked", [], "done", mood="calm"), back("adm:keys"))

        elif mode == "create_gift":
            p = text_in.split()
            if len(p) < 3 or not (p[0].isdigit() and p[1].isdigit() and p[2].isdigit()):
                return send(cid, card("❌ format error", ["send: CREDITS MAX_USERS DAYS_VALID [PREMIUM]"], "try again", mood="calm"))
            credits, max_users, days = int(p[0]), int(p[1]), int(p[2])
            is_premium = len(p) > 3 and p[3].lower() == "premium"
            if credits <= 0: return send(cid, card("❌ credits must be > 0", [], "", mood="calm"))
            code = "EXO-GIFT-" + secrets.token_hex(4).upper()
            exp  = int(time.time()) + days * 86400 if days > 0 else 0
            gdata = {"code": code, "credits": credits, "max_users": max_users,
                     "claimed_count": 0, "expires_at": exp,
                     "created_at": int(time.time()), "is_premium": is_premium}
            fb_gift_set(code, gdata)
            admin_state.pop(uid, None)
            send(cid, card("✅ gift code created", [
                f"code · <code>{code}</code>",
                f"credits · {credits}  users · {max_users or '∞'}",
                f"expires · {fmt_exp(exp) if exp else 'never'}",
                *(['💎 premium included'] if is_premium else []),
            ], "ready to share", mood="bold"), back("adm:gift"))

        elif mode == "set_limit":
            if not text_in.strip().isdigit() or int(text_in) < 1:
                return send(cid, card("❌ positive number only", [], "try again", mood="calm"))
            fb_setting_set("daily_limit", int(text_in))
            admin_state.pop(uid, None)
            send(cid, card("✅ daily limit updated", [
                f"new limit · <code>{int(text_in)}</code> per key per day",
            ], "done", mood="calm"), back("adm:set"))

        elif mode == "add_refer_sub":
            p = text_in.split(None, 2)
            if len(p) < 2 or not (p[0].isdigit() and p[1].isdigit()):
                return send(cid, card("❌ format error", ["send: REF_COST DAYS LABEL"], "try again", mood="calm"))
            ref_cost = int(p[0]); days = int(p[1])
            label = p[2] if len(p) > 2 else f"{days}d"
            if ref_cost < 1 or days < 1:
                return send(cid, card("❌ cost and days must be ≥ 1", [], "", mood="calm"))
            sq_add_refer_sub(ref_cost, days, label)
            admin_state.pop(uid, None)
            send(cid, card("✅ plan added", [f"{ref_cost} refs → {label} ({days}d)"], "done", mood="calm"), back("adm:rsubs"))

        elif mode == "import_db":
            try:
                data = json.loads(text_in)
                users = data.get("users", [])
                imported = sq_import_users(users)
                for u in users:
                    if "id" in u: fb_user_set(u["id"], u)
                admin_state.pop(uid, None)
                send(cid, card("✅ import complete", [
                    f"imported · <code>{imported}</code> users",
                ], "done", mood="calm"), back("adm:home"))
            except json.JSONDecodeError:
                send(cid, card("❌ invalid json", ["send the full export json"], "try again", mood="calm"))

    except ValueError as e:
        cb_map = {"add_channel": "adm:ch", "broadcast_text": "adm:home", "find_user": "adm:users"}
        send(cid, card("❌ error", [E(str(e))], "try again", mood="calm"),
             KB([B("🔙 Cancel", cb=cb_map.get(mode, "adm:home"), style=ct["bad"])]))

def _send_document(cid, filename, data_bytes):
    try:
        tg("sendDocument", _t=60,
           chat_id=cid,
           document=("document", (filename, io.BytesIO(data_bytes), "application/json")))
    except Exception as e:
        print(f"[Bot] sendDocument error: {e}", flush=True)
        if len(data_bytes) < 4000:
            try: send(cid, f"<pre>{html.escape(data_bytes.decode()[:3900])}</pre>")
            except: pass

# ═══════════════════════════════════════════════════════════════
#  UPDATE ROUTER
# ═══════════════════════════════════════════════════════════════
def on_message(m):
    chat = m.get("chat") or {}
    if chat.get("type") != "private" or not m.get("from"): return
    frm, cid = m["from"], chat["id"]
    uid = frm["id"]
    text = (m.get("text") or "").strip()

    has_photo = bool(m.get("photo"))
    has_video = bool(m.get("video"))
    has_animation = bool(m.get("animation"))
    is_media = has_photo or has_video or has_animation

    if not text and not is_media: return

    ref = None
    if text.startswith("/start"):
        p = text.split(maxsplit=1)
        if len(p) > 1 and p[1].startswith("ref_") and p[1][4:].isdigit():
            ref = int(p[1][4:])

    row, is_new = upsert_user(frm, ref)
    if row and row.get("banned") and uid not in ADMIN_IDS:
        return send(cid, card("🚫 banned", [
            "you are banned from using this bot",
        ], "contact dev to appeal", mood="calm") + FOOTER)

    if uid in ADMIN_IDS and uid in admin_state and is_media:
        return on_admin_input(uid, cid, text, msg=m)

    if text.startswith("/"):
        cmd = text.split()[0].split("@")[0].lower()

        if cmd == "/cancel":
            admin_state.pop(uid, None)
            _pending_action.pop(uid, None)
            return send(cid, card("✅ cancelled", ["current input cleared"], "back to menu", mood="calm") + FOOTER)

        if cmd == "/color" and uid in ADMIN_IDS:
            cur = get_button_color()
            nxt = _next_color(cur)
            set_button_color(nxt)
            return send(cid, card("🎨 color changed", [
                f"{COLOR_NAMES.get(cur)} → {COLOR_NAMES.get(nxt)}",
            ], "done") + FOOTER)

        if cmd == "/admin" and uid in ADMIN_IDS:
            admin_state.pop(uid, None)
            return send(cid, *adm_home())

        if cmd == "/buy": return send(cid, *screen_buy())

        if not gate(uid, cid): return

        if cmd in ("/start", "/menu", "/help"):
            try_count_referral(uid)
            wm = get_welcome_media()
            if wm and wm.get("file_id"):
                try: send_welcome_media(cid)
                except: pass
            return send(cid, *screen_start(uid, frm.get("first_name")))

        if cmd == "/refer":   return send(cid, *screen_refer(uid))
        if cmd in ("/key", "/mykey"): return send(cid, *screen_key(uid))
        if cmd in ("/api", "/docs"):  return send(cid, *screen_docs(uid))
        if cmd == "/credits": return send(cid, *screen_credits(uid))
        if cmd == "/gift":    return send(cid, *screen_gift())

        return send(cid, *screen_start(uid, frm.get("first_name")))

    if uid in ADMIN_IDS and uid in admin_state:
        return on_admin_input(uid, cid, text, msg=m)

    pend = _pending_action.get(uid)
    if pend and pend.get("action") == "bomb" and re.fullmatch(r"\d{7,15}", text):
        country = pend["country"]
        mode    = pend.get("mode", "sms")
        _pending_action.pop(uid, None)
        threading.Thread(target=handle_bomb, args=(uid, cid, country, mode, text), daemon=True).start()
        return

    if text.upper().startswith("EXO-GIFT-"):
        return handle_gift_code(uid, cid, text)

    if not gate(uid, cid): return
    send(cid, card("💣 main menu", [], "choose an option below") + FOOTER,
         screen_start(uid, frm.get("first_name"))[1])


def on_callback(cq):
    ct = C()
    uid  = cq["from"]["id"]
    data = cq.get("data") or ""
    msg  = cq.get("message") or {}
    cid  = (msg.get("chat") or {}).get("id")
    mid  = msg.get("message_id")
    done = {"v": False}

    def ack(text_msg=None, alert=False):
        if done["v"]: return
        done["v"] = True
        p = {"callback_query_id": cq["id"]}
        if text_msg: p.update(text=text_msg, show_alert=alert)
        try: tg("answerCallbackQuery", **p)
        except TGError: pass

    if not cid: return ack()
    row, _ = upsert_user(cq["from"])
    if row and row.get("banned") and uid not in ADMIN_IDS:
        return ack("🚫 Banned.", True)

    if data.startswith("adm:"):
        if uid not in ADMIN_IDS: return ack("Admins only", True)
        on_admin_callback(uid, cid, mid, data, ack)
        return ack()

    if data == "verify":
        _member_cache.clear()
        miss = missing_channels(uid)
        if miss:
            ack("❌ Join all channels first!", True)
            return join_prompt(cid, mid, miss)
        ack("✅ Verified!")
        try_count_referral(uid)
        pend = _pending_action.pop(uid, None)
        if pend and pend.get("action") == "bomb":
            return start_bomb_flow(uid, cid, mid, pend["country"], pend.get("mode", "sms"))
        return show(cid, mid, *screen_start(uid, cq["from"].get("first_name")))

    if data == "noop": return ack()
    if not gate(uid, cid, mid): return ack()

    if data == "back:start":
        return show(cid, mid, *screen_start(uid, cq["from"].get("first_name")))
    if data == "credits":  return show(cid, mid, *screen_credits(uid))
    if data == "refer":    return show(cid, mid, *screen_refer(uid))
    if data == "mykey":    return show(cid, mid, *screen_key(uid))
    if data == "docs":     return show(cid, mid, *screen_docs(uid))
    if data == "buy":      return show(cid, mid, *screen_buy())
    if data == "gift":     return show(cid, mid, *screen_gift())
    if data == "dev":      return ack(f"Dev: {DEV_TAG}", True)

    if data == "premium_only":
        ack("💎 Premium only", True)
        return show(cid, mid, card("🔒 premium only", [
            "nuclear bomb requires premium",
            f"contact @{E(CONTACT_USERNAME)} to upgrade",
        ], "get premium below", mood="calm") + FOOTER,
        KB([B("💎 Get Premium", url=CONTACT_URL, style=ct["bad"])],
           [B("🔙 Back",        cb="back:start", style=ct["nav"])]))

    if data.startswith("country:"):
        country = data.split(":")[1]
        if country == "ind": return show(cid, mid, *screen_india(uid))
        if country == "bd":  return show(cid, mid, *screen_bd(uid))
        if country == "pak": return show(cid, mid, *screen_pak(uid))

    if data.startswith("bomb:"):
        parts = data.split(":")
        if len(parts) >= 3:
            return start_bomb_flow(uid, cid, mid, parts[1], parts[2])

    if data.startswith("claim:"):
        try:
            idx   = int(data.split(":")[1])
            plans = get_refer_subs()
            res   = claim_plan(uid, idx, plans)
        except (ValueError, IndexError): return ack()
        if not res:
            ack("Not enough referral points", True)
            return show(cid, mid, *screen_refer(uid))
        ack("🎉 Key unlocked!")
        key, exp = res
        plans = get_refer_subs()
        label = plans[idx]["label"] if idx < len(plans) else "Custom"
        return show(cid, mid, *key_card(key, exp, label))

    ack()


def _safe(fn, *args):
    try: fn(*args)
    except Exception as e:
        print(f"[Bot] handler error: {type(e).__name__}: {e}", flush=True)


# ✅ FIX 3: deleteWebhook(drop_pending_updates=True)
def _poll_loop():
    pool   = ThreadPoolExecutor(max_workers=24, thread_name_prefix="tgbot")
    offset = None
    try: tg("deleteWebhook", drop_pending_updates=True)
    except TGError: pass
    print(f"[Bot] @{BOT_USERNAME} polling…", flush=True)
    while True:
        try:
            params = dict(timeout=50, allowed_updates=["message", "callback_query"])
            if offset is not None: params["offset"] = offset
            for u in tg("getUpdates", _t=70, **params):
                offset = u["update_id"] + 1
                if "message" in u:
                    pool.submit(_safe, on_message, u["message"])
                elif "callback_query" in u:
                    pool.submit(_safe, on_callback, u["callback_query"])
        except TGError as e:
            print(f"[Bot] poll error: {e.desc}", flush=True)
            time.sleep(max(3, e.retry_after))
        except Exception as e:
            print(f"[Bot] poll crash: {e}", flush=True)
            time.sleep(5)


# ═══════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════
def start_bot(port):
    global BOT_TOKEN, BOT_ID, BOT_USERNAME, BASE_URL, LOCAL_PORT, ADMIN_IDS, FOOTER

    LOCAL_PORT = int(os.environ.get("PORT", os.environ.get("SERVER_PORT", port)))
    BASE_URL   = os.environ.get("BASE_URL", f"http://localhost:{LOCAL_PORT}").strip().rstrip("/")
    ADMIN_IDS  = {int(x) for x in re.findall(r"-?\d+", os.environ.get("ADMIN_IDS", ""))}
    BOT_TOKEN  = os.environ.get("BOT_TOKEN", "").strip()
    FOOTER     = f"\n<i>» <a href=\"{DEV_URL}\">{DEV_TAG}</a></i>"

    if not BOT_TOKEN:
        print("⚠️  BOT_TOKEN not set — Telegram bot disabled.", flush=True)
        return False

    init_sqlite()
    get_button_color()

    me = None
    for attempt in range(1, 6):
        try:
            me = tg("getMe", _t=60)
            break
        except TGError as e:
            err_l = (e.desc or "").lower()
            is_net_or_limit = (
                "network:" in err_l
                or "timed out" in err_l
                or "timeout" in err_l
                or e.code == 429
            )
            if not is_net_or_limit:
                print(f"⚠️  Bot token rejected: {e.desc}", flush=True)
                return False
            wait_s = max(3, e.retry_after or (attempt * 3))
            print(f"⚠️  getMe attempt {attempt}/5 failed ({e.desc}), retrying in {wait_s}s…", flush=True)
            time.sleep(wait_s)

    if me:
        BOT_ID, BOT_USERNAME = me["id"], me.get("username", "")
    else:
        # ✅ FIX 2: getMe fail pe return False (polling start mat karo)
        print("❌ getMe failed after 5 tries — NOT starting polling.", flush=True)
        print("   Cause: duplicate instance OR webhook active OR bad token.", flush=True)
        return False

    if not ADMIN_IDS:
        print("⚠️  ADMIN_IDS empty — nobody can open the admin panel.", flush=True)

    perm_list = "\n".join(f"    {i+1}. {c['title']} ({c['chat_id']})" for i, c in enumerate(PERM_CHANNELS))
    wm = get_welcome_media()
    print(f"""
[Bot] ✅ Started v3.0
  Username:    @{BOT_USERNAME}
  Admins:      {ADMIN_IDS or 'NONE SET'}
  Color:       {get_button_color()}
  SQLite:      {SQLITE_PATH}
  Firebase:    {'✅' if _db else '❌'}
  Base URL:    {BASE_URL}
  Perm chans:  {len(PERM_CHANNELS)} loaded
  Welcome media: {wm.get('type','none') if wm else 'none'}
{perm_list if PERM_CHANNELS else '    (none — set PERMANENT_CHANNELS in env)'}
""", flush=True)

    threading.Thread(target=_poll_loop, daemon=True, name="tg-poll").start()
    return True
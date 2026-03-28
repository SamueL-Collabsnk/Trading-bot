"""
backend/server.py
─────────────────────────────────────────────────────────────
Flask backend for Trading Bot UI
Handles: Registration, Login, OTP (SMS + Email), JWT Auth
SMS:   Africa's Talking Sandbox
Email: Google Gmail API (OAuth2)
─────────────────────────────────────────────────────────────
"""

import os
import random
import string
import sqlite3
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, static_folder='../frontend', static_url_path='')
CORS(app)

# ─── Config ───────────────────────────────────────────────
SECRET_KEY       = os.getenv('APP_SECRET_KEY', 'dev-secret')
JWT_SECRET       = os.getenv('JWT_SECRET', 'jwt-secret')
AT_USERNAME      = os.getenv('AT_USERNAME', 'sandbox')
AT_API_KEY       = os.getenv('AT_API_KEY', '')
AT_ENVIRONMENT   = os.getenv('AT_ENVIRONMENT', 'sandbox')
GOOGLE_CLIENT_ID = os.getenv('GOOGLE_CLIENT_ID', '')
GOOGLE_CLIENT_SECRET = os.getenv('GOOGLE_CLIENT_SECRET', '')
GOOGLE_REFRESH_TOKEN = os.getenv('GOOGLE_REFRESH_TOKEN', '')
GMAIL_SENDER     = os.getenv('GMAIL_SENDER', '')
OTP_EXPIRY_MINS  = int(os.getenv('OTP_EXPIRY_MINUTES', '10'))
OTP_LENGTH       = int(os.getenv('OTP_LENGTH', '6'))
DB_PATH          = 'trading_bot.db'


# ─── Database ─────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL,
                phone TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                email_verified INTEGER DEFAULT 0,
                phone_verified INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now')),
                last_login TEXT
            );

            CREATE TABLE IF NOT EXISTS otp_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                contact TEXT NOT NULL,
                otp_type TEXT NOT NULL,
                code TEXT NOT NULL,
                attempts INTEGER DEFAULT 0,
                expires_at TEXT NOT NULL,
                used INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                token_hash TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                expires_at TEXT NOT NULL
            );
        """)
    print("✓ Database initialised")

init_db()


# ─── Utilities ────────────────────────────────────────────
def hash_password(password: str) -> str:
    salt = os.urandom(32)
    key = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 100_000)
    return salt.hex() + ':' + key.hex()

def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, key_hex = stored.split(':')
        salt = bytes.fromhex(salt_hex)
        key = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 100_000)
        return hmac.compare_digest(key.hex(), key_hex)
    except Exception:
        return False

def generate_otp() -> str:
    return ''.join(random.choices(string.digits, k=OTP_LENGTH))

def generate_jwt(user_id: int, email: str) -> str:
    """Simple JWT-like token (base64 encoded JSON + HMAC signature)."""
    import base64
    payload = json.dumps({
        'sub': user_id,
        'email': email,
        'iat': int(time.time()),
        'exp': int(time.time()) + 86400,  # 24 hours
    })
    encoded = base64.b64encode(payload.encode()).decode()
    sig = hmac.new(JWT_SECRET.encode(), encoded.encode(), hashlib.sha256).hexdigest()
    return f"{encoded}.{sig}"

def verify_jwt(token: str) -> dict | None:
    import base64
    try:
        encoded, sig = token.rsplit('.', 1)
        expected_sig = hmac.new(JWT_SECRET.encode(), encoded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return None
        payload = json.loads(base64.b64decode(encoded).decode())
        if payload['exp'] < int(time.time()):
            return None
        return payload
    except Exception:
        return None

def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        payload = verify_jwt(token)
        if not payload:
            return jsonify({'error': 'Unauthorised'}), 401
        request.user = payload
        return f(*args, **kwargs)
    return decorated


# ─── SMS via Africa's Talking ─────────────────────────────
def send_sms_africastalking(phone: str, message: str) -> dict:
    """
    Sends SMS via Africa's Talking Sandbox.
    Sandbox API: https://api.sandbox.africastalking.com/version1/messaging
    In sandbox mode, messages are NOT delivered — check dashboard at:
    https://account.africastalking.com/apps/sandbox/messaging
    """
    try:
        import africastalking
        africastalking.initialize(
            username=AT_USERNAME,
            api_key=AT_API_KEY,
        )
        sms = africastalking.SMS
        response = sms.send(message, [phone])
        return {'success': True, 'response': response}
    except ImportError:
        # Fallback: direct HTTP request if SDK not installed
        import urllib.request
        import urllib.parse

        base_url = (
            'https://api.sandbox.africastalking.com/version1/messaging'
            if AT_ENVIRONMENT == 'sandbox'
            else 'https://api.africastalking.com/version1/messaging'
        )
        data = urllib.parse.urlencode({
            'username': AT_USERNAME,
            'to': phone,
            'message': message,
        }).encode()

        req = urllib.request.Request(
            base_url,
            data=data,
            headers={
                'apiKey': AT_API_KEY,
                'Content-Type': 'application/x-www-form-urlencoded',
                'Accept': 'application/json',
            },
            method='POST',
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return {'success': True, 'response': json.loads(resp.read())}
        except Exception as e:
            print(f"[AT SMS] Error: {e}")
            return {'success': False, 'error': str(e)}
    except Exception as e:
        print(f"[AT SMS] SDK Error: {e}")
        return {'success': False, 'error': str(e)}


# ─── Email via Google Gmail API ───────────────────────────
def send_email_gmail(to_email: str, subject: str, html_body: str) -> dict:
    """
    Sends email via Gmail API using OAuth2 refresh token.
    Setup:
      1. Enable Gmail API in Google Cloud Console
      2. Create OAuth2 credentials (Desktop app)
      3. Use OAuth Playground to get refresh token with
         scope: https://www.googleapis.com/auth/gmail.send
    """
    try:
        import base64
        import urllib.request
        import urllib.parse

        # Step 1: Refresh access token
        token_data = urllib.parse.urlencode({
            'client_id': GOOGLE_CLIENT_ID,
            'client_secret': GOOGLE_CLIENT_SECRET,
            'refresh_token': GOOGLE_REFRESH_TOKEN,
            'grant_type': 'refresh_token',
        }).encode()

        token_req = urllib.request.Request(
            'https://oauth2.googleapis.com/token',
            data=token_data,
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            method='POST',
        )

        with urllib.request.urlopen(token_req, timeout=10) as resp:
            token_resp = json.loads(resp.read())
            access_token = token_resp['access_token']

        # Step 2: Build RFC 2822 email message
        email_lines = [
            f'From: Trading Bot <{GMAIL_SENDER}>',
            f'To: {to_email}',
            f'Subject: {subject}',
            'MIME-Version: 1.0',
            'Content-Type: text/html; charset=utf-8',
            '',
            html_body,
        ]
        raw_message = '\r\n'.join(email_lines)
        encoded = base64.urlsafe_b64encode(raw_message.encode()).decode()

        # Step 3: Send via Gmail API
        send_data = json.dumps({'raw': encoded}).encode()
        send_req = urllib.request.Request(
            'https://gmail.googleapis.com/gmail/v1/users/me/messages/send',
            data=send_data,
            headers={
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json',
            },
            method='POST',
        )
        with urllib.request.urlopen(send_req, timeout=10) as resp:
            return {'success': True, 'response': json.loads(resp.read())}

    except Exception as e:
        print(f"[Gmail] Error: {e}")
        # In development, log the OTP to console
        print(f"[Gmail DEV] Would send to {to_email}: {subject}")
        return {'success': False, 'error': str(e), 'dev_mode': True}


# ─── OTP Helpers ──────────────────────────────────────────
def store_otp(user_id, contact, otp_type, code):
    expires_at = (datetime.utcnow() + timedelta(minutes=OTP_EXPIRY_MINS)).isoformat()
    with get_db() as db:
        # Invalidate existing OTPs for this contact+type
        db.execute(
            "UPDATE otp_tokens SET used=1 WHERE contact=? AND otp_type=? AND used=0",
            (contact, otp_type)
        )
        db.execute(
            "INSERT INTO otp_tokens (user_id, contact, otp_type, code, expires_at) VALUES (?,?,?,?,?)",
            (user_id, contact, otp_type, code, expires_at)
        )

def verify_otp_code(contact, otp_type, code) -> tuple[bool, str]:
    with get_db() as db:
        row = db.execute(
            """SELECT * FROM otp_tokens
               WHERE contact=? AND otp_type=? AND used=0
               ORDER BY created_at DESC LIMIT 1""",
            (contact, otp_type)
        ).fetchone()

        if not row:
            return False, "No OTP found. Please request a new one."

        if datetime.fromisoformat(row['expires_at']) < datetime.utcnow():
            return False, "OTP has expired. Please request a new one."

        if row['attempts'] >= 3:
            return False, "Too many attempts. Please request a new OTP."

        db.execute(
            "UPDATE otp_tokens SET attempts=attempts+1 WHERE id=?",
            (row['id'],)
        )

        if row['code'] != code:
            return False, "Invalid OTP code."

        db.execute("UPDATE otp_tokens SET used=1 WHERE id=?", (row['id'],))
        return True, "OTP verified."


# ─── Email OTP HTML template ──────────────────────────────
def otp_email_html(otp: str, full_name: str, purpose: str) -> str:
    return f"""
<!DOCTYPE html><html>
<head><meta charset="utf-8">
<style>
  body{{font-family:'Segoe UI',Arial,sans-serif;background:#0a0e1a;margin:0;padding:40px 20px}}
  .card{{background:#111827;border:1px solid #1f2937;border-radius:16px;max-width:480px;
         margin:0 auto;padding:40px;text-align:center}}
  h1{{color:#f0f4ff;font-size:24px;margin:0 0 8px}}
  p{{color:#8892a4;font-size:14px;line-height:1.6;margin:0 0 24px}}
  .otp{{background:#0f172a;border:2px solid #22d3ee;border-radius:12px;
        color:#22d3ee;font-size:36px;font-weight:700;letter-spacing:12px;
        padding:20px 32px;margin:24px 0;display:inline-block}}
  .warn{{color:#64748b;font-size:12px;margin-top:24px}}
  .brand{{color:#22d3ee;font-size:18px;font-weight:700;margin-bottom:24px}}
</style></head>
<body><div class="card">
  <div class="brand">⚡ TRADEBOT</div>
  <h1>Verification Code</h1>
  <p>Hi {full_name}, here is your {purpose} verification code:</p>
  <div class="otp">{otp}</div>
  <p>This code expires in <strong style="color:#f0f4ff">{OTP_EXPIRY_MINS} minutes</strong>.</p>
  <p class="warn">If you didn't request this, ignore this email.</p>
</div></body></html>
"""


# ═══════════════════════════════════════════════
# API ROUTES
# ═══════════════════════════════════════════════

# ─── Registration ──────────────────────────────
@app.route('/api/auth/register', methods=['POST'])
def register():
    data = request.get_json()
    full_name = (data.get('fullName') or '').strip()
    email     = (data.get('email') or '').strip().lower()
    phone     = (data.get('phone') or '').strip()
    password  = data.get('password') or ''

    if not all([full_name, email, phone, password]):
        return jsonify({'error': 'All fields are required'}), 400
    if len(password) < 8:
        return jsonify({'error': 'Password must be at least 8 characters'}), 400

    # Normalise phone: ensure +254 or similar international format
    if not phone.startswith('+'):
        phone = '+' + phone

    with get_db() as db:
        existing = db.execute(
            "SELECT id FROM users WHERE email=? OR phone=?", (email, phone)
        ).fetchone()
        if existing:
            return jsonify({'error': 'Email or phone already registered'}), 409

        db.execute(
            "INSERT INTO users (full_name, email, phone, password_hash) VALUES (?,?,?,?)",
            (full_name, email, phone, hash_password(password))
        )
        user_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]

    # Send email OTP
    email_otp = generate_otp()
    store_otp(user_id, email, 'email_verify', email_otp)
    email_result = send_email_gmail(
        email,
        "Verify your TradeBot email",
        otp_email_html(email_otp, full_name, "email")
    )

    # Send SMS OTP via Africa's Talking
    sms_otp = generate_otp()
    store_otp(user_id, phone, 'phone_verify', sms_otp)
    sms_result = send_sms_africastalking(
        phone,
        f"TradeBot: Your verification code is {sms_otp}. Valid for {OTP_EXPIRY_MINS} mins."
    )

    # DEV: echo OTPs in response when APIs aren't configured
    dev_info = {}
    if not email_result.get('success'):
        dev_info['email_otp_dev'] = email_otp
    if not sms_result.get('success'):
        dev_info['sms_otp_dev'] = sms_otp

    return jsonify({
        'message': 'Registration successful. Verification codes sent.',
        'userId': user_id,
        'emailSent': email_result.get('success', False),
        'smsSent': sms_result.get('success', False),
        **dev_info,
    }), 201


# ─── Verify Email OTP ──────────────────────────
@app.route('/api/auth/verify-email', methods=['POST'])
def verify_email():
    data = request.get_json()
    email = (data.get('email') or '').strip().lower()
    code  = (data.get('code') or '').strip()

    ok, msg = verify_otp_code(email, 'email_verify', code)
    if not ok:
        return jsonify({'error': msg}), 400

    with get_db() as db:
        db.execute("UPDATE users SET email_verified=1 WHERE email=?", (email,))

    return jsonify({'message': 'Email verified successfully'})


# ─── Verify Phone OTP ──────────────────────────
@app.route('/api/auth/verify-phone', methods=['POST'])
def verify_phone():
    data = request.get_json()
    phone = (data.get('phone') or '').strip()
    code  = (data.get('code') or '').strip()

    if not phone.startswith('+'):
        phone = '+' + phone

    ok, msg = verify_otp_code(phone, 'phone_verify', code)
    if not ok:
        return jsonify({'error': msg}), 400

    with get_db() as db:
        db.execute("UPDATE users SET phone_verified=1 WHERE phone=?", (phone,))

    return jsonify({'message': 'Phone verified successfully'})


# ─── Resend OTP ────────────────────────────────
@app.route('/api/auth/resend-otp', methods=['POST'])
def resend_otp():
    data    = request.get_json()
    contact = (data.get('contact') or '').strip()
    channel = data.get('channel', 'email')  # "email" | "sms"

    with get_db() as db:
        if channel == 'email':
            user = db.execute(
                "SELECT * FROM users WHERE email=?", (contact.lower(),)
            ).fetchone()
        else:
            phone = contact if contact.startswith('+') else '+' + contact
            user = db.execute(
                "SELECT * FROM users WHERE phone=?", (phone,)
            ).fetchone()

    if not user:
        return jsonify({'error': 'Contact not found'}), 404

    otp = generate_otp()
    dev_info = {}

    if channel == 'email':
        store_otp(user['id'], user['email'], 'email_verify', otp)
        result = send_email_gmail(
            user['email'],
            "TradeBot — New Verification Code",
            otp_email_html(otp, user['full_name'], "email")
        )
        if not result.get('success'):
            dev_info['email_otp_dev'] = otp
    else:
        phone = user['phone']
        store_otp(user['id'], phone, 'phone_verify', otp)
        result = send_sms_africastalking(
            phone,
            f"TradeBot: Your new code is {otp}. Valid for {OTP_EXPIRY_MINS} mins."
        )
        if not result.get('success'):
            dev_info['sms_otp_dev'] = otp

    return jsonify({'message': 'New OTP sent', 'sent': result.get('success'), **dev_info})


# ─── Login ─────────────────────────────────────
@app.route('/api/auth/login', methods=['POST'])
def login():
    data     = request.get_json()
    email    = (data.get('email') or '').strip().lower()
    password = data.get('password') or ''

    with get_db() as db:
        user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()

    if not user or not verify_password(password, user['password_hash']):
        return jsonify({'error': 'Invalid email or password'}), 401

    if not user['email_verified']:
        return jsonify({
            'error': 'Email not verified. Please verify your email first.',
            'requiresVerification': True,
            'verificationType': 'email',
        }), 403

    # Send login OTP (2FA)
    otp = generate_otp()
    store_otp(user['id'], email, 'login_2fa', otp)
    result = send_email_gmail(
        email,
        "TradeBot — Login Verification Code",
        otp_email_html(otp, user['full_name'], "login")
    )

    dev_info = {}
    if not result.get('success'):
        dev_info['login_otp_dev'] = otp

    return jsonify({
        'message': '2FA code sent to your email',
        'userId': user['id'],
        'requires2FA': True,
        **dev_info,
    })


# ─── Login 2FA Verify ──────────────────────────
@app.route('/api/auth/login-verify', methods=['POST'])
def login_verify():
    data  = request.get_json()
    email = (data.get('email') or '').strip().lower()
    code  = (data.get('code') or '').strip()

    ok, msg = verify_otp_code(email, 'login_2fa', code)
    if not ok:
        return jsonify({'error': msg}), 400

    with get_db() as db:
        user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        db.execute(
            "UPDATE users SET last_login=datetime('now') WHERE email=?", (email,)
        )

    token = generate_jwt(user['id'], email)

    return jsonify({
        'token': token,
        'user': {
            'id': user['id'],
            'fullName': user['full_name'],
            'email': user['email'],
            'phone': user['phone'],
            'emailVerified': bool(user['email_verified']),
            'phoneVerified': bool(user['phone_verified']),
        }
    })


# ─── Get Current User ──────────────────────────
@app.route('/api/auth/me', methods=['GET'])
@require_auth
def get_me():
    with get_db() as db:
        user = db.execute(
            "SELECT * FROM users WHERE id=?", (request.user['sub'],)
        ).fetchone()
    if not user:
        return jsonify({'error': 'User not found'}), 404
    return jsonify({
        'id': user['id'],
        'fullName': user['full_name'],
        'email': user['email'],
        'phone': user['phone'],
        'emailVerified': bool(user['email_verified']),
        'phoneVerified': bool(user['phone_verified']),
        'createdAt': user['created_at'],
        'lastLogin': user['last_login'],
    })


# ─── Mock Market Data (for chart) ──────────────
@app.route('/api/market/ohlcv', methods=['GET'])
@require_auth
def get_ohlcv():
    """Returns mock OHLCV + indicators for chart display."""
    import random, math

    symbol = request.args.get('symbol', 'BTCUSDT')
    bars   = int(request.args.get('bars', '100'))

    random.seed(42)
    base  = 43500 if 'BTC' in symbol else (1.0850 if 'EUR' in symbol else 1.2650)
    price = base
    data  = []
    now   = int(time.time())

    for i in range(bars):
        ts     = now - (bars - i) * 3600
        change = random.gauss(0, base * 0.008)
        open_  = price
        close  = price + change
        high   = max(open_, close) + abs(random.gauss(0, base * 0.003))
        low    = min(open_, close) - abs(random.gauss(0, base * 0.003))
        vol    = random.uniform(500, 3000)
        data.append({
            'time': ts, 'open': round(open_, 4),
            'high': round(high, 4), 'low': round(low, 4),
            'close': round(close, 4), 'volume': round(vol, 2)
        })
        price = close

    return jsonify({'symbol': symbol, 'timeframe': 'H1', 'data': data})


# ─── Serve frontend ────────────────────────────
@app.route('/')
def index():
    return send_from_directory('../frontend', 'index.html')

@app.route('/<path:path>')
def serve_static(path):
    return send_from_directory('../frontend', path)


if __name__ == '__main__':
    port = int(os.getenv('APP_PORT', '5000'))
    print(f"🚀 Trading Bot Server running on http://localhost:{port}")
    app.run(debug=True, port=port, host='0.0.0.0')

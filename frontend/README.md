# TradeBot UI — Setup Guide

## Project Structure
```
trading_ui/
├── .env                    ← API keys (never commit)
├── frontend/
│   └── index.html          ← Full SPA (auth + chart)
└── backend/
    ├── server.py           ← Flask API server
    └── requirements.txt
```

## Quick Start

### 1. Install Python dependencies
```bash
cd trading_ui/backend
pip install -r requirements.txt
```

### 2. Configure .env (root of trading_ui/)
Edit `.env` with your actual keys — see sections below.

### 3. Run the server
```bash
cd trading_ui/backend
python server.py
```

### 4. Open the UI
Visit: http://localhost:5000

---

## Africa's Talking SMS Setup (Sandbox)

1. Register at https://account.africastalking.com/auth/register
2. Go to **Sandbox** → **SMS** in dashboard
3. Copy your sandbox API key (starts with `atsk_uat_...`)
4. Set in `.env`:
   ```
   AT_USERNAME=sandbox
   AT_API_KEY=atsk_uat_your_key_here
   AT_ENVIRONMENT=sandbox
   ```
5. In **sandbox mode**, SMS messages appear in the AT dashboard simulator — not real phones
6. To test, check: https://account.africastalking.com/apps/sandbox/messaging

Install SDK:
```bash
pip install africastalking
```

---

## Google Gmail API Setup (OAuth2)

1. Go to https://console.cloud.google.com/
2. Create a new project (or use existing)
3. Enable **Gmail API**
4. Go to **Credentials** → Create **OAuth 2.0 Client ID** (Desktop app)
5. Download the credentials JSON

6. Get a refresh token via OAuth Playground:
   - Open: https://developers.google.com/oauthplayground
   - In ⚙️ settings, check "Use your own OAuth credentials"
   - Enter your Client ID and Secret
   - In scope box, enter: `https://www.googleapis.com/auth/gmail.send`
   - Click **Authorize APIs** → sign in with your Gmail
   - Click **Exchange authorization code for tokens**
   - Copy the **Refresh token**

7. Set in `.env`:
   ```
   GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
   GOOGLE_CLIENT_SECRET=GOCSPX-your-secret
   GOOGLE_REFRESH_TOKEN=1//your-refresh-token
   GMAIL_SENDER=your-gmail@gmail.com
   ```

---

## Development Mode (No API Keys)

The app runs **without real API keys** — OTP codes are shown directly in:
- The API JSON response (visible in browser DevTools → Network)
- The yellow ⚡ DEV MODE hint box on the OTP screen

This lets you test the full auth flow offline.

---

## Auth Flow

```
Register:
  [Fill form] → POST /api/auth/register
    → Email OTP (Gmail) + SMS OTP (Africa's Talking)
  [Enter Email OTP] → POST /api/auth/verify-email
  [Enter SMS OTP]   → POST /api/auth/verify-phone
  → Redirect to Login

Login:
  [Email + Password] → POST /api/auth/login
    → 2FA OTP sent to email
  [Enter 2FA OTP]    → POST /api/auth/login-verify
    → JWT token stored in localStorage
  → Enter Dashboard
```

---

## Production Deployment

```bash
# Use gunicorn
gunicorn -w 4 -b 0.0.0.0:5000 server:app

# Change in .env:
AT_ENVIRONMENT=production
AT_API_KEY=your_live_key

# Use a real PostgreSQL database:
DATABASE_URL=postgresql://user:pass@localhost/tradingbot
```

---

## Chart Features

- **Candlestick chart** with EMA 20/50, Bollinger Bands
- **Volume sub-chart** (color-coded bull/bear)
- **RSI sub-chart** with OB/OS levels
- **Live price ticker** (2s simulated tick updates)
- **Crosshair tooltip** with OHLCV data
- **Mouse wheel zoom** (scroll to zoom in/out)
- **Symbol switcher**: BTC/USDT, EUR/USD, GBP/USD, ETH/USD
- **Timeframe switcher**: 15M, 30M, 1H, 4H, 1D
- **Signal panel** with entry/SL/TP and confluence score
- **Market structure tags**: BOS, CHoCH, FVG, Liquidity zones
- **Sentiment gauge** driven by RSI proxy

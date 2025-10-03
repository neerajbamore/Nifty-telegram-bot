import os, time, threading, sqlite3, datetime
from flask import Flask, jsonify
import requests
import pytz

# ================== CONFIG ==================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
IST = pytz.timezone("Asia/Kolkata")

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json,text/javascript,*/*;q=0.01",
    "Referer": "https://www.nseindia.com/option-chain",
    "Connection": "keep-alive"
}

NSE_BASE = "https://www.nseindia.com"
URL_OC = f"{NSE_BASE}/api/option-chain-indices?symbol=NIFTY"
URL_FUT = f"{NSE_BASE}/api/quote-derivative?symbol=NIFTY"

DB_PATH = "data.sqlite"

# ================== APP/DB ==================
app = Flask(__name__)

def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("""CREATE TABLE IF NOT EXISTS snapshots(
        ts INTEGER, expiry TEXT, side TEXT, strike INTEGER,
        oi INTEGER, iv REAL, vol INTEGER,
        PRIMARY KEY(ts, side, strike)
    )""")
    return conn

def ist_now():
    return datetime.datetime.now(IST)

def in_trading_window():
    now = ist_now()
    if now.weekday() >= 5:  # Sat/Sun
        return False
    start = now.replace(hour=9, minute=14, second=0, microsecond=0)
    end = now.replace(hour=15, minute=14, second=0, microsecond=0)
    return start <= now <= end

def get_json(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print("Fetch error:", e)
    return None

def nearest_n_strikes(all_strikes, spot, n=6):
    all_strikes = sorted(all_strikes)
    closest = min(all_strikes, key=lambda x: abs(x - spot))
    idx = all_strikes.index(closest)
    half = n // 2
    return all_strikes[max(0, idx-half): idx+half]

def color_val(val):
    if val > 0:
        return f"🟢{val}"
    elif val < 0:
        return f"🔴{val}"
    return str(val)

# ================== SNAPSHOT STORE ==================
def store_snapshot(ts, expiry, side, strike, oi, iv, vol):
    conn = db()
    conn.execute("""
        INSERT OR REPLACE INTO snapshots(ts, expiry, side, strike, oi, iv, vol)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (ts, expiry, side, strike, oi, iv, vol))
    conn.commit()
    conn.close()

def fetch_last_snapshot(min_ago=5):
    cutoff = int(time.time()) - (min_ago * 60)
    conn = db()
    rows = conn.execute("SELECT * FROM snapshots WHERE ts>=? ORDER BY ts ASC", (cutoff,)).fetchall()
    conn.close()
    return rows

# ================== FETCH NSE DATA ==================
def fetch_data():
    data = get_json(URL_OC)
    if not data:
        return None, None, None, None
    try:
        ce_data = data["records"]["data"]
        expiry = data["records"]["expiryDates"][0]
        spot = float(data["records"]["underlyingValue"])
    except Exception:
        return None, None, None, None
    return ce_data, expiry, spot, data

# ================== TELEGRAM ALERT ==================
def send_telegram(msg):
    if not BOT_TOKEN or not CHAT_ID:
        print("❌ BOT_TOKEN/CHAT_ID missing")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}
    try:
        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        print("Telegram error:", e)

# ================== PROCESS DATA ==================
def process_and_alert():
    if not in_trading_window():
        print("⏸ Outside trading hours")
        return

    ce_data, expiry, spot, full_data = fetch_data()
    if not ce_data:
        send_telegram("⚠️ NSE Option Chain not available.")
        return

    strikes = []
    ce_map, pe_map = {}, {}
    for row in ce_data:
        if "CE" in row and "PE" in row:
            strike = row["strikePrice"]
            strikes.append(strike)
            ce_map[strike] = row["CE"]
            pe_map[strike] = row["PE"]

    nearest = nearest_n_strikes(strikes, spot, 6)
    ts = int(time.time())
    msg = f"📊 NIFTY 50 — Expiry {expiry}\nSpot: {spot}\n\n"

    # Calls
    msg += "📌 <b>Calls (CE)</b>\nStrike   OI   ΔOI   IV   ΔIV   ΔVol\n"
    for s in nearest:
        if s in ce_map:
            oi = ce_map[s].get("openInterest", 0)
            iv = ce_map[s].get("impliedVolatility", 0.0)
            vol = ce_map[s].get("totalTradedVolume", 0)
            prev = fetch_last_snapshot(5)
            delta_oi, delta_iv, delta_vol = 0, 0, 0
            if prev:
                for p in prev:
                    _, _, side, strike, poi, piv, pvol = p
                    if side == "CE" and strike == s:
                        delta_oi = oi - poi
                        delta_iv = round(iv - piv, 2)
                        delta_vol = vol - pvol
            msg += f"{s:<7} {oi:<6} {color_val(delta_oi):<5} {iv:<5} {color_val(delta_iv):<5} {color_val(delta_vol)}\n"
            store_snapshot(ts, expiry, "CE", s, oi, iv, vol)

    # Puts
    msg += "\n📌 <b>Puts (PE)</b>\nStrike   OI   ΔOI   IV   ΔIV   ΔVol\n"
    for s in nearest:
        if s in pe_map:
            oi = pe_map[s].get("openInterest", 0)
            iv = pe_map[s].get("impliedVolatility", 0.0)
            vol = pe_map[s].get("totalTradedVolume", 0)
            prev = fetch_last_snapshot(5)
            delta_oi, delta_iv, delta_vol = 0, 0, 0
            if prev:
                for p in prev:
                    _, _, side, strike, poi, piv, pvol = p
                    if side == "PE" and strike == s:
                        delta_oi = oi - poi
                        delta_iv = round(iv - piv, 2)
                        delta_vol = vol - pvol
            msg += f"{s:<7} {oi:<6} {color_val(delta_oi):<5} {iv:<5} {color_val(delta_iv):<5} {color_val(delta_vol)}\n"
            store_snapshot(ts, expiry, "PE", s, oi, iv, vol)

    # Futures
    fut = get_json(URL_FUT)
    if fut and "marketDeptOrderBook" in fut:
        try:
            vol = fut["marketDeptOrderBook"]["tradeInfo"]["totalTradedVolume"]
            prev = fetch_last_snapshot(5)
            delta_vol = 0
            if prev:
                for p in prev:
                    _, _, side, _, _, _, pvol = p
                    if side == "FUT":
                        delta_vol = vol - pvol
            msg += f"\n📌 <b>Futures Volume</b>\n{vol} ({color_val(delta_vol)})"
            store_snapshot(ts, expiry, "FUT", 0, 0, 0, vol)
        except:
            msg += "\n⚠️ Futures data error."

    send_telegram(msg)

# ================== BACKGROUND LOOP ==================
def background_loop():
    while True:
        try:
            process_and_alert()
        except Exception as e:
            print("Error in loop:", e)
        time.sleep(300)  # every 5 min

# ================== ROUTES ==================
@app.route("/")
def home():
    return jsonify({"status": "running", "time": str(ist_now())})

# ================== MAIN ==================
if __name__ == "__main__":
    t = threading.Thread(target=background_loop, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
# =====================
# Test Message Section
# =====================

import os, requests

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

def send_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": text}
    response = requests.post(url, data=data)
    print(response.json())

# Deploy hone ke turant baad test message bhejna
if __name__ == "__main__":
    send_message("🚀 Bot test successful! Ye message Render se aaya hai ✅")

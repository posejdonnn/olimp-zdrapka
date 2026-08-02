import os
import random
import string
import sqlite3
import time

import requests
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__, static_folder="static", static_url_path="")

DB_PATH = os.environ.get("DB_PATH", "codes.db")
BOT_API_KEY = os.environ.get("BOT_API_KEY", "zmien-mnie-w-zmiennych-srodowiskowych")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

# =========================================================
#  PULA NAGRÓD (ta sama logika co w prototypie na froncie)
# =========================================================
TIERS = {
    "t20": {
        "price": 20,
        "prizes": [
            {"amount": "2 000 LF", "weight": 40, "icon": "🪙", "jackpot": False},
            {"amount": "4 000 LF", "weight": 28, "icon": "🪙", "jackpot": False},
            {"amount": "7 000 LF", "weight": 18, "icon": "💰", "jackpot": False},
            {"amount": "12 000 LF", "weight": 9, "icon": "💰", "jackpot": False},
            {"amount": "25 000 LF", "weight": 4.5, "icon": "👑", "jackpot": False},
            {"amount": "100 000 LF", "weight": 0.5, "icon": "🔱", "jackpot": True},
        ],
    },
    "t30": {
        "price": 30,
        "prizes": [
            {"amount": "3 000 LF", "weight": 35, "icon": "🪙", "jackpot": False},
            {"amount": "6 000 LF", "weight": 27, "icon": "🪙", "jackpot": False},
            {"amount": "10 000 LF", "weight": 20, "icon": "💰", "jackpot": False},
            {"amount": "18 000 LF", "weight": 12, "icon": "💰", "jackpot": False},
            {"amount": "35 000 LF", "weight": 5.5, "icon": "👑", "jackpot": False},
            {"amount": "150 000 LF", "weight": 0.5, "icon": "🔱", "jackpot": True},
        ],
    },
}


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS codes (
            code TEXT PRIMARY KEY,
            tier TEXT NOT NULL,
            prize_index INTEGER NOT NULL,
            prize_amount TEXT NOT NULL,
            buyer_discord_id TEXT,
            channel_id TEXT,
            created_at REAL NOT NULL,
            used INTEGER NOT NULL DEFAULT 0,
            used_at REAL
        )
        """
    )
    conn.commit()
    conn.close()


init_db()


def weighted_choice(prizes):
    total = sum(p["weight"] for p in prizes)
    r = random.uniform(0, total)
    upto = 0
    for i, p in enumerate(prizes):
        upto += p["weight"]
        if upto >= r:
            return i
    return len(prizes) - 1


def generate_code_string():
    chars = string.ascii_uppercase + string.digits
    return "-".join("".join(random.choices(chars, k=4)) for _ in range(2))


# =========================================================
#  API — WYWOŁYWANE PRZEZ BOTA DISCORD
# =========================================================

@app.route("/api/generate-code", methods=["POST"])
def generate_code():
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {BOT_API_KEY}":
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(force=True, silent=True) or {}
    tier = data.get("tier")
    if tier not in TIERS:
        return jsonify({"error": "invalid tier - use 't20' or 't30'"}), 400

    prizes = TIERS[tier]["prizes"]
    prize_index = weighted_choice(prizes)
    prize = prizes[prize_index]

    # unikamy (bardzo mało prawdopodobnej) kolizji kodów
    conn = sqlite3.connect(DB_PATH)
    while True:
        code = generate_code_string()
        exists = conn.execute("SELECT 1 FROM codes WHERE code = ?", (code,)).fetchone()
        if not exists:
            break

    conn.execute(
        """INSERT INTO codes
           (code, tier, prize_index, prize_amount, buyer_discord_id, channel_id, created_at, used)
           VALUES (?, ?, ?, ?, ?, ?, ?, 0)""",
        (
            code,
            tier,
            prize_index,
            prize["amount"],
            data.get("buyer_discord_id"),
            data.get("channel_id"),
            time.time(),
        ),
    )
    conn.commit()
    conn.close()

    return jsonify({"code": code, "tier": tier})


# =========================================================
#  API — WYWOŁYWANE PRZEZ STRONĘ (ZDRAPKĘ)
# =========================================================

@app.route("/api/validate-code", methods=["POST"])
def validate_code():
    data = request.get_json(force=True, silent=True) or {}
    code = (data.get("code") or "").strip().upper()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM codes WHERE code = ?", (code,)).fetchone()
    conn.close()

    if not row:
        return jsonify({"valid": False, "reason": "not_found"})
    if row["used"]:
        return jsonify({"valid": False, "reason": "already_used"})

    return jsonify(
        {
            "valid": True,
            "tier": row["tier"],
            "prize_index": row["prize_index"],
            "prize_amount": row["prize_amount"],
        }
    )


@app.route("/api/redeem", methods=["POST"])
def redeem_code():
    data = request.get_json(force=True, silent=True) or {}
    code = (data.get("code") or "").strip().upper()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM codes WHERE code = ?", (code,)).fetchone()

    if not row:
        conn.close()
        return jsonify({"success": False, "reason": "not_found"}), 404
    if row["used"]:
        conn.close()
        return jsonify({"success": False, "reason": "already_used"}), 409

    conn.execute("UPDATE codes SET used = 1, used_at = ? WHERE code = ?", (time.time(), code))
    conn.commit()
    conn.close()

    if DISCORD_WEBHOOK_URL:
        try:
            requests.post(
                DISCORD_WEBHOOK_URL,
                json={
                    "content": (
                        f"🎉 Ktoś zdrapał zdrapkę **{row['tier']}** i wygrał: "
                        f"**{row['prize_amount']}**! (kod: `{code}`)"
                    )
                },
                timeout=5,
            )
        except requests.RequestException:
            pass

    return jsonify({"success": True, "prize_amount": row["prize_amount"]})


# =========================================================
#  SERWOWANIE STRONY
# =========================================================

@app.route("/")
def index():
    return send_from_directory("static", "zdrapka.html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

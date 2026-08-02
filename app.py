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
            {"amount": "10 000 LF", "weight": 40, "icon": "🪙", "jackpot": False},
            {"amount": "20 000 LF", "weight": 28, "icon": "🪙", "jackpot": False},
            {"amount": "40 000 LF", "weight": 18, "icon": "💰", "jackpot": False},
            {"amount": "75 000 LF", "weight": 9, "icon": "💰", "jackpot": False},
            {"amount": "150 000 LF", "weight": 5, "icon": "👑", "jackpot": False},
            {"amount": "333 000 LF", "weight": 0, "icon": "🔱", "jackpot": True},
        ],
    },
    "t30": {
        "price": 30,
        "prizes": [
            {"amount": "15 000 LF", "weight": 35, "icon": "🪙", "jackpot": False},
            {"amount": "30 000 LF", "weight": 27, "icon": "🪙", "jackpot": False},
            {"amount": "60 000 LF", "weight": 20, "icon": "💰", "jackpot": False},
            {"amount": "110 000 LF", "weight": 12, "icon": "💰", "jackpot": False},
            {"amount": "220 000 LF", "weight": 6, "icon": "👑", "jackpot": False},
            {"amount": "500 000 LF", "weight": 0, "icon": "🔱", "jackpot": True},
        ],
    },
}

# domyślna szansa na jackpot (%) - edytowalna na żywo przez panel na Discordzie,
# przechowywana w bazie (tabela jackpot_settings), to tylko wartość startowa
DEFAULT_JACKPOT_PCT = {"t20": 1.0, "t30": 1.0}


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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jackpot_settings (
            tier TEXT PRIMARY KEY,
            percent REAL NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


init_db()


def get_jackpot_percent(tier):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT percent FROM jackpot_settings WHERE tier = ?", (tier,)).fetchone()
    if row:
        conn.close()
        return row[0]
    default = DEFAULT_JACKPOT_PCT.get(tier, 1.0)
    conn.execute("INSERT INTO jackpot_settings (tier, percent) VALUES (?, ?)", (tier, default))
    conn.commit()
    conn.close()
    return default


def set_jackpot_percent(tier, percent):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """INSERT INTO jackpot_settings (tier, percent) VALUES (?, ?)
           ON CONFLICT(tier) DO UPDATE SET percent = excluded.percent""",
        (tier, percent),
    )
    conn.commit()
    conn.close()


def weighted_choice(prizes, jackpot_pct):
    """Jackpot losowany NIEZALEŻNIE, wg aktualnego (edytowalnego) procentu. Pozostałe nagrody
    dzielą między siebie resztę szansy, proporcjonalnie do swoich wag - ich wzajemne proporcje
    się nie zmieniają niezależnie od tego, jaki procent ma teraz jackpot."""
    jackpot_idx = next((i for i, p in enumerate(prizes) if p["jackpot"]), None)
    non_jackpot = [(i, p) for i, p in enumerate(prizes) if not p["jackpot"]]

    if jackpot_idx is not None and random.uniform(0, 100) < jackpot_pct:
        return jackpot_idx

    total_weight = sum(p["weight"] for _, p in non_jackpot)
    r = random.uniform(0, total_weight)
    upto = 0
    for i, p in non_jackpot:
        upto += p["weight"]
        if upto >= r:
            return i
    return non_jackpot[-1][0]


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
    jackpot_pct = get_jackpot_percent(tier)
    prize_index = weighted_choice(prizes, jackpot_pct)
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


@app.route("/api/settings/jackpot-chance", methods=["GET"])
def get_jackpot_chance_endpoint():
    tier = request.args.get("tier")
    if tier not in TIERS:
        return jsonify({"error": "invalid tier - use 't20' or 't30'"}), 400
    percent = get_jackpot_percent(tier)
    return jsonify({"tier": tier, "percent": percent})


@app.route("/api/settings/jackpot-chance", methods=["POST"])
def set_jackpot_chance_endpoint():
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {BOT_API_KEY}":
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(force=True, silent=True) or {}
    tier = data.get("tier")
    if tier not in TIERS:
        return jsonify({"error": "invalid tier - use 't20' or 't30'"}), 400

    try:
        percent = float(data.get("percent"))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid percent"}), 400
    if not (0 <= percent <= 100):
        return jsonify({"error": "percent must be between 0 and 100"}), 400

    set_jackpot_percent(tier, percent)
    return jsonify({"success": True, "tier": tier, "percent": percent})


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

import json
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

TIERS = {
    "t20": {"price": 20},
    "t30": {"price": 30},
}

CHEST_TIERS = {
    "c50": {"price": 50},
    "c100": {"price": 100},
}

NUM_FIELDS = 9
DEFAULT_JACKPOT_PCT = {
    "t20": 1.0, "t30": 1.0, "trial": 1.0,
    "c50": 1.0, "c100": 1.0, "chest_trial": 3.0,
}
DEFAULT_PRICES = {
    "buy": 60.0, "sell": 111.0,
    "trial_min": 200000.0, "trial_max": 300000.0,
    "chest_trial_min": 450000.0, "chest_trial_max": 1200000.0,
}


# =========================================================
#  BAZA DANYCH
# =========================================================

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS codes (
            code TEXT PRIMARY KEY,
            tier TEXT NOT NULL,
            fields_json TEXT NOT NULL,
            total_lf INTEGER NOT NULL,
            is_jackpot INTEGER NOT NULL DEFAULT 0,
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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS price_settings (
            key TEXT PRIMARY KEY,
            value REAL NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


init_db()


# =========================================================
#  USTAWIENIA (edytowalne przez panel na Discordzie)
# =========================================================

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


def get_price_settings():
    conn = sqlite3.connect(DB_PATH)
    result = {}
    for key in ("buy", "sell", "trial_min", "trial_max", "chest_trial_min", "chest_trial_max"):
        row = conn.execute("SELECT value FROM price_settings WHERE key = ?", (key,)).fetchone()
        if row:
            result[key] = row[0]
        else:
            default = DEFAULT_PRICES[key]
            conn.execute("INSERT INTO price_settings (key, value) VALUES (?, ?)", (key, default))
            result[key] = default
    conn.commit()
    conn.close()
    return result


def set_price_setting(key, value):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """INSERT INTO price_settings (key, value) VALUES (?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
        (key, value),
    )
    conn.commit()
    conn.close()


# =========================================================
#  GENEROWANIE 9 PÓL - ZAWSZE SUMUJĄ SIĘ DO ZAŁOŻONEJ WARTOŚCI
# =========================================================

def generate_prize_fields(total_lf: int, num_fields: int = NUM_FIELDS, min_field: int = 1000):
    """Dzieli total_lf na num_fields różnych, atrakcyjnie wyglądających kwot (zaokrąglonych
    do pełnych 500 LF), które ZAWSZE sumują się dokładnie do total_lf - co do jednostki."""
    reserve = min_field * num_fields
    remaining = max(total_lf - reserve, 0)

    weights = [random.uniform(0.4, 2.4) for _ in range(num_fields)]
    weight_sum = sum(weights)

    raw_amounts = [min_field + remaining * (w / weight_sum) for w in weights]
    amounts = [max(min_field, int(round(a / 500)) * 500) for a in raw_amounts]

    diff = total_lf - sum(amounts)
    idx_max = amounts.index(max(amounts))
    amounts[idx_max] += diff

    return amounts


def build_scratch_card(tier: str):
    """Losuje czy to jackpot (wg aktualnego, edytowalnego %), liczy docelową sumę na
    podstawie AKTUALNYCH cen skupu/sprzedaży, i generuje 9 pól sumujących się do niej."""
    prices = get_price_settings()
    price = TIERS[tier]["price"]
    jackpot_pct = get_jackpot_percent(tier)

    is_jackpot = random.uniform(0, 100) < jackpot_pct

    if is_jackpot:
        total_lf = round(price * 1_000_000 / prices["buy"])
    else:
        total_lf = round(price * 1_000_000 / prices["sell"])

    amounts = generate_prize_fields(total_lf, NUM_FIELDS)

    jackpot_field_index = random.randrange(NUM_FIELDS) if is_jackpot else None
    fields = [
        {"amount": amt, "is_jackpot": (i == jackpot_field_index)}
        for i, amt in enumerate(amounts)
    ]

    return fields, is_jackpot, total_lf


def build_chest_prize(tier: str):
    """Skrzynia = JEDNO losowanie (nie suma pól jak w zdrapce). Zwykła (nie-jackpotowa)
    wygrana jest zawsze BLISKO gwarantowanego minimum (cena / kurs SPRZEDAŻY) - dokładnie
    tyle, ile klient dostałby kupując tę samą kwotę normalnie na tickecie, plus drobny
    kosmetyczny szum (żeby liczba nie była identyczna za każdym razem). Jackpot (rzadki,
    edytowalny %) = cena / kurs SKUPU, czyli zero zysku dla sklepu - to JEDYNY sposób na
    dużo wyższą wygraną. Dokładnie ta sama logika co w zdrapce."""
    prices = get_price_settings()
    price = CHEST_TIERS[tier]["price"]
    jackpot_pct = get_jackpot_percent(tier)

    min_lf = round(price * 1_000_000 / prices["sell"])
    max_lf = round(price * 1_000_000 / prices["buy"])

    is_jackpot = random.uniform(0, 100) < jackpot_pct
    if is_jackpot:
        return max_lf, True

    jitter = random.randint(0, 3000)  # kosmetyczny szum - zeby nie bylo identycznej liczby za kazdym razem
    lf = min_lf + jitter
    lf = min(lf, max_lf - 500)  # NIGDY nie dotyka/przekracza jackpota
    lf = max(lf, min_lf)
    return lf, False


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

    if tier in TIERS:
        fields, is_jackpot, total_lf = build_scratch_card(tier)
    elif tier in CHEST_TIERS:
        lf, is_jackpot = build_chest_prize(tier)
        fields = [{"amount": lf, "is_jackpot": is_jackpot}]
        total_lf = lf
    else:
        return jsonify({"error": "invalid tier - use 't20', 't30', 'c50' or 'c100'"}), 400

    conn = sqlite3.connect(DB_PATH)
    while True:
        code = generate_code_string()
        exists = conn.execute("SELECT 1 FROM codes WHERE code = ?", (code,)).fetchone()
        if not exists:
            break

    conn.execute(
        """INSERT INTO codes
           (code, tier, fields_json, total_lf, is_jackpot, buyer_discord_id, channel_id, created_at, used)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)""",
        (
            code,
            tier,
            json.dumps(fields),
            total_lf,
            int(is_jackpot),
            data.get("buyer_discord_id"),
            data.get("channel_id"),
            time.time(),
        ),
    )
    conn.commit()
    conn.close()

    return jsonify({"code": code, "tier": tier, "total_lf": total_lf, "is_jackpot": is_jackpot})


VALID_JACKPOT_TIERS = ("t20", "t30", "trial", "c50", "c100", "chest_trial")


@app.route("/api/settings/jackpot-chance", methods=["GET"])
def get_jackpot_chance_endpoint():
    tier = request.args.get("tier")
    if tier not in VALID_JACKPOT_TIERS:
        return jsonify({"error": "invalid tier - use 't20', 't30' or 'trial'"}), 400
    return jsonify({"tier": tier, "percent": get_jackpot_percent(tier)})


@app.route("/api/settings/jackpot-chance", methods=["POST"])
def set_jackpot_chance_endpoint():
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {BOT_API_KEY}":
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(force=True, silent=True) or {}
    tier = data.get("tier")
    if tier not in VALID_JACKPOT_TIERS:
        return jsonify({"error": "invalid tier - use 't20', 't30' or 'trial'"}), 400
    try:
        percent = float(data.get("percent"))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid percent"}), 400
    if not (0 <= percent <= 100):
        return jsonify({"error": "percent must be between 0 and 100"}), 400

    set_jackpot_percent(tier, percent)
    return jsonify({"success": True, "tier": tier, "percent": percent})


@app.route("/api/settings/prices", methods=["GET"])
def get_prices_endpoint():
    return jsonify(get_price_settings())


@app.route("/api/settings/prices", methods=["POST"])
def set_prices_endpoint():
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {BOT_API_KEY}":
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(force=True, silent=True) or {}
    try:
        if "buy" in data:
            buy = float(data["buy"])
            if buy <= 0:
                return jsonify({"error": "buy must be > 0"}), 400
            set_price_setting("buy", buy)
        if "sell" in data:
            sell = float(data["sell"])
            if sell <= 0:
                return jsonify({"error": "sell must be > 0"}), 400
            set_price_setting("sell", sell)
        if "trial_min" in data:
            trial_min = float(data["trial_min"])
            if trial_min <= 0:
                return jsonify({"error": "trial_min must be > 0"}), 400
            set_price_setting("trial_min", trial_min)
        if "trial_max" in data:
            trial_max = float(data["trial_max"])
            if trial_max <= 0:
                return jsonify({"error": "trial_max must be > 0"}), 400
            set_price_setting("trial_max", trial_max)
        if "chest_trial_min" in data:
            v = float(data["chest_trial_min"])
            if v <= 0:
                return jsonify({"error": "chest_trial_min must be > 0"}), 400
            set_price_setting("chest_trial_min", v)
        if "chest_trial_max" in data:
            v = float(data["chest_trial_max"])
            if v <= 0:
                return jsonify({"error": "chest_trial_max must be > 0"}), 400
            set_price_setting("chest_trial_max", v)
    except (TypeError, ValueError):
        return jsonify({"error": "invalid price value"}), 400

    return jsonify({"success": True, **get_price_settings()})


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
            "fields": json.loads(row["fields_json"]),
            "total_lf": row["total_lf"],
            "is_jackpot": bool(row["is_jackpot"]),
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

    total_lf_str = f"{row['total_lf']:,}".replace(",", " ")

    game_labels = {
        "t20": ("20 zł", "zdrapkę", "zdrapał"),
        "t30": ("30 zł", "zdrapkę", "zdrapał"),
        "c50": ("50 zł", "skrzynię", "otworzył"),
        "c100": ("100 zł", "skrzynię", "otworzył"),
    }
    price_label, game_noun, game_verb = game_labels.get(row["tier"], ("? zł", "produkt", "wykorzystał"))

    if DISCORD_WEBHOOK_URL:
        mention = f"<@{row['buyer_discord_id']}>" if row["buyer_discord_id"] else "Nieznany gracz"

        if row["is_jackpot"]:
            embed = {
                "title": "🔱 TRAFIONY JACKPOT! 🔱",
                "description": f"{mention} {game_verb} {game_noun} **{price_label}** i trafił jackpota!",
                "color": 0xFFD700,
                "fields": [
                    {"name": "💰 Łączna wygrana", "value": f"**{total_lf_str} LF**", "inline": True},
                    {"name": "🎫 Poziom", "value": price_label, "inline": True},
                    {"name": "🔑 Kod", "value": f"`{code}`", "inline": False},
                ],
                "footer": {"text": "🍀 Olimp Shop — Skarb Posejdona"},
            }
        else:
            embed = {
                "title": "🍀 Ktoś wygrał!",
                "description": f"{mention} {game_verb} {game_noun} **{price_label}** i wygrał łącznie.",
                "color": 0xD4AF37,
                "fields": [
                    {"name": "💰 Wygrana", "value": f"**{total_lf_str} LF**", "inline": True},
                    {"name": "🎫 Poziom", "value": price_label, "inline": True},
                    {"name": "🔑 Kod", "value": f"`{code}`", "inline": False},
                ],
                "footer": {"text": "🍀 Olimp Shop — Skarb Posejdona"},
            }

        try:
            requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=5)
        except requests.RequestException:
            pass

    return jsonify(
        {
            "success": True,
            "fields": json.loads(row["fields_json"]),
            "total_lf": row["total_lf"],
            "is_jackpot": bool(row["is_jackpot"]),
        }
    )


# =========================================================
#  API — STATYSTYKI (zarobki z wygenerowanych kodów)
# =========================================================

@app.route("/api/stats/earnings", methods=["GET"])
def get_earnings_endpoint():
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {BOT_API_KEY}":
        return jsonify({"error": "unauthorized"}), 401

    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT tier, COUNT(*) FROM codes GROUP BY tier").fetchall()
    conn.close()

    counts = {"t20": 0, "t30": 0, "c50": 0, "c100": 0}
    for tier, cnt in rows:
        if tier in counts:
            counts[tier] = cnt

    total = (
        counts["t20"] * TIERS["t20"]["price"]
        + counts["t30"] * TIERS["t30"]["price"]
        + counts["c50"] * CHEST_TIERS["c50"]["price"]
        + counts["c100"] * CHEST_TIERS["c100"]["price"]
    )

    return jsonify(
        {
            "total_earnings": total,
            "count_t20": counts["t20"],
            "count_t30": counts["t30"],
            "count_c50": counts["c50"],
            "count_c100": counts["c100"],
        }
    )


# =========================================================
#  SERWOWANIE STRONY
# =========================================================

@app.route("/")
def index():
    return send_from_directory("static", "zdrapka.html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

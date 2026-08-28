"""
check.py - Versión "un solo disparo" del bot, pensada para GitHub Actions.

A diferencia de bot.py (que corre sin parar en un servidor), este script:
  1. Se despierta, lee su "memoria" desde alerts.json
  2. Revisa si llegaron comandos nuevos por Telegram (/alerta, /alertas, /quitar, etc.)
  3. Revisa las cuotas actuales contra tus alertas activas
  4. Manda mensajes de Telegram si algo se activó
  5. Guarda la memoria actualizada en alerts.json
  6. Se apaga

GitHub Actions lo vuelve a correr cada N minutos (definido en el workflow),
y como guarda todo en alerts.json (que se sube de vuelta al repo), no pierde
el historial de alertas ni los mensajes ya procesados entre corridas.

Variables de entorno necesarias (se configuran como GitHub Secrets):
  TELEGRAM_BOT_TOKEN
  APIFOOTBALL_KEY
"""

import os
import json
from datetime import date
import requests

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
APIFOOTBALL_KEY = os.environ.get("APIFOOTBALL_KEY", "")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
APIFOOTBALL_BASE_URL = "https://v3.football.api-sports.io"
STATE_FILE = os.path.join(os.path.dirname(__file__), "alerts.json")


# ---------------------------------------------------------------------------
# Manejo del estado (memoria persistente entre corridas)
# ---------------------------------------------------------------------------
def load_state():
    if not os.path.exists(STATE_FILE):
        return {"last_update_id": 0, "next_alert_id": 1, "alerts": []}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Telegram: leer mensajes nuevos y responder
# ---------------------------------------------------------------------------
def get_updates(offset):
    resp = requests.get(
        f"{TELEGRAM_API}/getUpdates",
        params={"offset": offset, "timeout": 0},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("result", [])


def send_message(chat_id, text):
    requests.post(
        f"{TELEGRAM_API}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=15,
    )


def process_commands(state):
    updates = get_updates(state["last_update_id"] + 1)

    for update in updates:
        state["last_update_id"] = update["update_id"]
        message = update.get("message")
        if not message or "text" not in message:
            continue

        chat_id = message["chat"]["id"]
        text = message["text"].strip()

        if text.startswith("/start"):
            send_message(
                chat_id,
                "👋 Hola. Comandos:\n"
                "/partidos - ver partidos de hoy con cuotas\n"
                "/alerta EQUIPO_LOCAL EQUIPO_VISITA CUOTA\n"
                "/alertas\n"
                "/quitar ID\n\n"
                "Nota: reviso cada 30 min (versión GitHub Actions), "
                "no en tiempo real.",
            )

        elif text.startswith("/partidos"):
            events = fetch_soccer_events()
            if not events:
                send_message(
                    chat_id,
                    "No encontré partidos con cuotas para hoy. Puede que "
                    "no haya partidos importantes hoy, o que la key no "
                    "esté bien configurada.",
                )
            else:
                lines = ["📅 Partidos de hoy:"]
                for ev in events[:15]:
                    odds_txt = (
                        f"cuota local: {ev['home_odds']}"
                        if ev.get("home_odds")
                        else "cuota no disponible"
                    )
                    lines.append(f"• {ev['home']} vs {ev['away']} ({odds_txt})")
                send_message(chat_id, "\n".join(lines))

        elif text.startswith("/alerta "):
            parts = text.split()[1:]
            if len(parts) < 3:
                send_message(chat_id, "Formato: /alerta LOCAL VISITA CUOTA")
                continue
            *team_parts, odds_str = parts
            try:
                threshold = float(odds_str)
            except ValueError:
                send_message(chat_id, "La cuota debe ser un número, ej: 1.85")
                continue
            mid = len(team_parts) // 2
            team_home = " ".join(team_parts[:mid]) or team_parts[0]
            team_away = " ".join(team_parts[mid:]) or team_parts[-1]

            alert_id = state["next_alert_id"]
            state["next_alert_id"] += 1
            state["alerts"].append(
                {
                    "id": alert_id,
                    "chat_id": chat_id,
                    "team_home": team_home,
                    "team_away": team_away,
                    "threshold_odds": threshold,
                    "triggered": False,
                }
            )
            send_message(
                chat_id,
                f"✅ Alerta #{alert_id} creada: {team_home} vs {team_away} "
                f"≤ {threshold}",
            )

        elif text.startswith("/alertas"):
            mine = [a for a in state["alerts"] if a["chat_id"] == chat_id]
            if not mine:
                send_message(chat_id, "No tienes alertas activas.")
                continue
            lines = ["🔔 Tus alertas:"]
            for a in mine:
                estado = "✅ activada" if a["triggered"] else "⏳ esperando"
                lines.append(
                    f"#{a['id']} — {a['team_home']} vs {a['team_away']} "
                    f"≤ {a['threshold_odds']} ({estado})"
                )
            send_message(chat_id, "\n".join(lines))

        elif text.startswith("/quitar "):
            try:
                alert_id = int(text.split()[1])
            except (IndexError, ValueError):
                send_message(chat_id, "Formato: /quitar ID")
                continue
            before = len(state["alerts"])
            state["alerts"] = [
                a
                for a in state["alerts"]
                if not (a["id"] == alert_id and a["chat_id"] == chat_id)
            ]
            if len(state["alerts"]) < before:
                send_message(chat_id, f"🗑️ Alerta #{alert_id} eliminada.")
            else:
                send_message(chat_id, "No encontré esa alerta.")

    return state


# ---------------------------------------------------------------------------
# API-Football (api-football.com)
# ---------------------------------------------------------------------------
def _apifootball_get(endpoint, params):
    if not APIFOOTBALL_KEY:
        print("APIFOOTBALL_KEY no configurada.")
        return []
    url = f"{APIFOOTBALL_BASE_URL}/{endpoint}"
    headers = {"x-apisports-key": APIFOOTBALL_KEY}
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json().get("response", [])
    except requests.RequestException as e:
        print(f"Error consultando API-Football ({endpoint}): {e}")
        return []


def fetch_soccer_events():
    """
    Combina dos llamadas (cuentan 2 de tus 100 solicitudes/día):
      1. /fixtures?date=hoy  -> para saber qué equipos juegan (nombres)
      2. /odds?date=hoy      -> para las cuotas de esos partidos
    y devuelve una lista de partidos con la forma:
      {"fixture_id": int, "home": str, "away": str, "home_odds": float|None}

    Nota: revisa /partidos en Telegram para confirmar que esto trae datos
    reales antes de crear alertas en serio. La estructura de la API puede
    variar según cobertura de liga/temporada.
    """
    today = date.today().isoformat()

    fixtures = _apifootball_get("fixtures", {"date": today})
    fixtures_by_id = {}
    for fx in fixtures:
        try:
            fid = fx["fixture"]["id"]
            home = fx["teams"]["home"]["name"]
            away = fx["teams"]["away"]["name"]
            fixtures_by_id[fid] = (home, away)
        except (KeyError, TypeError):
            continue

    odds_data = _apifootball_get("odds", {"date": today})
    events = []
    for od in odds_data:
        try:
            fid = od["fixture"]["id"]
        except (KeyError, TypeError):
            continue

        home, away = fixtures_by_id.get(fid, (None, None))
        if home is None:
            continue

        home_odds = None
        for bookmaker in od.get("bookmakers", []):
            for bet in bookmaker.get("bets", []):
                if bet.get("name") == "Match Winner":
                    for val in bet.get("values", []):
                        if val.get("value") == "Home":
                            try:
                                home_odds = float(val["odd"])
                            except (KeyError, ValueError, TypeError):
                                pass
                    break
            if home_odds is not None:
                break

        events.append(
            {"fixture_id": fid, "home": home, "away": away, "home_odds": home_odds}
        )

    return events


def extract_home_win_odds(event):
    return event.get("home_odds")


# Mismo gancho para tu modelo Poisson/xG que en bot.py.
# Reemplaza el "return False" por tu lógica real cuando la tengas lista.
def value_bet_hook(event, market_probability):
    return False


def check_alerts(state):
    pending = [a for a in state["alerts"] if not a["triggered"]]
    if not pending:
        return state

    events = fetch_soccer_events()
    if not events:
        return state

    for alert in pending:
        for ev in events:
            home = ev.get("home")
            away = ev.get("away")
            if not home or not away:
                continue

            if alert["team_home"].lower() not in home.lower():
                continue
            if alert["team_away"].lower() not in away.lower():
                continue

            current_odds = extract_home_win_odds(ev)
            if current_odds is None:
                continue

            if current_odds <= alert["threshold_odds"]:
                send_message(
                    alert["chat_id"],
                    f"🚨 ¡Alerta activada!\n{home} vs {away}\n"
                    f"Cuota local ahora: {current_odds} "
                    f"(tu umbral: {alert['threshold_odds']})",
                )
                alert["triggered"] = True

    return state


def main():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("Falta TELEGRAM_BOT_TOKEN")

    state = load_state()
    state = process_commands(state)
    state = check_alerts(state)
    save_state(state)
    print("Corrida completa. Alertas guardadas en alerts.json")


if __name__ == "__main__":
    main()

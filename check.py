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

from poisson_model import find_value_bets

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
        return {
            "last_update_id": 0,
            "next_alert_id": 1,
            "alerts": [],
            "value_subscribers": [],
            "value_alerts_sent": [],
            "last_value_scan_date": "",
            "value_log": [],
            "last_resolve_date": "",
        }
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)
    # Compatibilidad con estados guardados antes de agregar value bets
    state.setdefault("value_subscribers", [])
    state.setdefault("value_alerts_sent", [])
    state.setdefault("last_value_scan_date", "")
    # Compatibilidad con estados guardados antes de agregar el registro de aciertos
    state.setdefault("value_log", [])
    state.setdefault("last_resolve_date", "")
    return state


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
                "/quitar ID\n"
                "/valor on - activar alertas automáticas de value bets\n"
                "/valor off - desactivar alertas de value bets\n"
                "/rendimiento - ver aciertos de tus value bets pasadas\n\n"
                "Nota: reviso cada 30 min (versión GitHub Actions), "
                "no en tiempo real.",
            )

        elif text.startswith("/rendimiento"):
            send_message(chat_id, build_rendimiento_message(state))

        elif text.startswith("/valor"):
            args = text.split()[1:]
            subs = state["value_subscribers"]
            if args and args[0].lower() == "off":
                if chat_id in subs:
                    subs.remove(chat_id)
                send_message(chat_id, "🔕 Alertas de value bets desactivadas.")
            elif args and args[0].lower() == "on":
                if chat_id not in subs:
                    subs.append(chat_id)
                send_message(
                    chat_id,
                    "🎯 Alertas de value bets activadas. Te aviso cuando mi "
                    "modelo Poisson detecte una cuota con valor en los "
                    "partidos de hoy que cubre tu Excel (11 ligas cargadas). "
                    "Umbral mínimo: "
                    f"{VALUE_THRESHOLD_DISPLAY}.",
                )
            else:
                estado = "activadas ✅" if chat_id in subs else "desactivadas ⏸️"
                send_message(
                    chat_id,
                    f"Tus alertas de value bets están {estado}.\n"
                    "Usa /valor on o /valor off para cambiarlo.",
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
      {
        "fixture_id": int, "home": str, "away": str,
        "home_odds": float|None, "draw_odds": float|None, "away_odds": float|None,
        "over25_odds": float|None, "under25_odds": float|None,
        "btts_yes_odds": float|None, "btts_no_odds": float|None,
      }

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

        parsed = {
            "fixture_id": fid,
            "home": home,
            "away": away,
            "home_odds": None,
            "draw_odds": None,
            "away_odds": None,
            "over25_odds": None,
            "under25_odds": None,
            "btts_yes_odds": None,
            "btts_no_odds": None,
        }

        # En vez de quedarnos con la primera casa que responde, juntamos
        # las cuotas de TODAS las casas disponibles y promediamos. Esto
        # evita que una sola casa mal calibrada genere una falsa señal
        # de valor (mejora de calidad #1).
        odds_lists = {
            "home_odds": [], "draw_odds": [], "away_odds": [],
            "over25_odds": [], "under25_odds": [],
            "btts_yes_odds": [], "btts_no_odds": [],
        }

        for bookmaker in od.get("bookmakers", []):
            for bet in bookmaker.get("bets", []):
                bet_name = bet.get("name", "")
                values = bet.get("values", [])

                if bet_name == "Match Winner":
                    for val in values:
                        try:
                            odd = float(val["odd"])
                        except (KeyError, ValueError, TypeError):
                            continue
                        if val.get("value") == "Home":
                            odds_lists["home_odds"].append(odd)
                        elif val.get("value") == "Draw":
                            odds_lists["draw_odds"].append(odd)
                        elif val.get("value") == "Away":
                            odds_lists["away_odds"].append(odd)

                elif bet_name == "Goals Over/Under":
                    for val in values:
                        try:
                            odd = float(val["odd"])
                        except (KeyError, ValueError, TypeError):
                            continue
                        label = str(val.get("value", ""))
                        if label == "Over 2.5":
                            odds_lists["over25_odds"].append(odd)
                        elif label == "Under 2.5":
                            odds_lists["under25_odds"].append(odd)

                elif bet_name == "Both Teams Score":
                    for val in values:
                        try:
                            odd = float(val["odd"])
                        except (KeyError, ValueError, TypeError):
                            continue
                        label = str(val.get("value", "")).lower()
                        if label == "yes":
                            odds_lists["btts_yes_odds"].append(odd)
                        elif label == "no":
                            odds_lists["btts_no_odds"].append(odd)

        # Promedio simple de cada mercado (solo si al menos una casa lo trae)
        for key, values in odds_lists.items():
            if values:
                parsed[key] = round(sum(values) / len(values), 3)

        events.append(parsed)

    return events


def extract_home_win_odds(event):
    return event.get("home_odds")


VALUE_THRESHOLD_DISPLAY = "5%"  # se muestra en /valor; ajusta si cambias teams_stats.json


def scan_value_bets(events):
    """
    Recorre los partidos del día, corre el modelo Poisson (poisson_model.py)
    sobre cada uno, y arma un mensaje por partido donde se detectó valor.
    Solo incluye partidos de equipos que existen en teams_stats.json (las
    11 ligas que cargaste en tu Excel).
    Devuelve una lista de dicts:
      {"fixture_id", "home", "away", "message", "value_bets"}
    donde "value_bets" trae los datos crudos de cada mercado con valor
    (para poder guardarlos luego en el registro de rendimiento).
    """
    results = []
    for ev in events:
        value_bets = find_value_bets(
            ev["home"],
            ev["away"],
            odds_1x2=(ev.get("home_odds"), ev.get("draw_odds"), ev.get("away_odds")),
            odds_over25=ev.get("over25_odds"),
            odds_under25=ev.get("under25_odds"),
            odds_btts_yes=ev.get("btts_yes_odds"),
            odds_btts_no=ev.get("btts_no_odds"),
        )
        if not value_bets:
            continue  # None (equipo no está en tu Excel) o [] (sin valor)

        lines = [f"🎯 Value bet detectada: {ev['home']} vs {ev['away']}"]
        for vb in value_bets:
            lines.append(
                f"  • {vb['mercado']}: cuota {vb['cuota']} | "
                f"prob. modelo {vb['prob_modelo']*100:.1f}% vs "
                f"implícita {vb['prob_implicita']*100:.1f}% "
                f"(+{vb['diferencia']*100:.1f} pts)"
            )
        results.append(
            {
                "fixture_id": ev["fixture_id"],
                "home": ev["home"],
                "away": ev["away"],
                "message": "\n".join(lines),
                "value_bets": value_bets,
            }
        )

    return results


def check_value_bets(state):
    """
    Revisa partidos de hoy para todos los suscriptores de /valor on.
    Evita re-alertar el mismo partido el mismo día usando
    state["value_alerts_sent"].
    """
    if not state["value_subscribers"]:
        return state

    today = date.today().isoformat()
    if state.get("last_value_scan_date") != today:
        state["value_alerts_sent"] = []
        state["last_value_scan_date"] = today

    events = fetch_soccer_events()
    if not events:
        return state

    found = scan_value_bets(events)
    for item in found:
        fixture_id = item["fixture_id"]
        if fixture_id in state["value_alerts_sent"]:
            continue
        for chat_id in state["value_subscribers"]:
            send_message(chat_id, item["message"])
        state["value_alerts_sent"].append(fixture_id)

        # Registrar cada mercado detectado en el log de rendimiento, para
        # poder revisar más adelante si el modelo acertó o no (/rendimiento).
        for vb in item["value_bets"]:
            state["value_log"].append(
                {
                    "fixture_id": fixture_id,
                    "fecha": today,
                    "home": item["home"],
                    "away": item["away"],
                    "mercado": vb["mercado"],
                    "cuota": vb["cuota"],
                    "prob_modelo": vb["prob_modelo"],
                    "prob_implicita": vb["prob_implicita"],
                    "diferencia": vb["diferencia"],
                    "resuelto": False,
                    "acierto": None,
                    "goles_local": None,
                    "goles_visita": None,
                }
            )

    return state


def _acierto_por_mercado(mercado, goles_local, goles_visita):
    """Dado el resultado final, dice si esa apuesta específica habría ganado."""
    total = goles_local + goles_visita
    if mercado == "Local (1)":
        return goles_local > goles_visita
    if mercado == "Empate (X)":
        return goles_local == goles_visita
    if mercado == "Visitante (2)":
        return goles_visita > goles_local
    if mercado == "Over 2.5":
        return total > 2.5
    if mercado == "Under 2.5":
        return total < 2.5
    if mercado == "BTTS - Sí":
        return goles_local > 0 and goles_visita > 0
    if mercado == "BTTS - No":
        return not (goles_local > 0 and goles_visita > 0)
    return None


def resolve_pending_results(state):
    """
    Una vez al día (no en cada corrida, para cuidar el cupo de 100
    solicitudes/día de API-Football), busca el resultado final de los
    partidos de días anteriores que quedaron pendientes en value_log y
    calcula si cada apuesta detectada habría acertado o no.
    """
    today = date.today().isoformat()
    if state.get("last_resolve_date") == today:
        return state  # ya se resolvió hoy, no gastar más solicitudes

    pending_ids = sorted(
        {
            entry["fixture_id"]
            for entry in state["value_log"]
            if not entry["resuelto"] and entry["fecha"] < today
        }
    )
    if not pending_ids:
        state["last_resolve_date"] = today
        return state

    # API-Football permite pedir varios fixtures a la vez separados por "-",
    # hasta 20 por solicitud, así que se resuelven todos en pocas llamadas.
    results_by_id = {}
    for i in range(0, len(pending_ids), 20):
        chunk = pending_ids[i : i + 20]
        ids_param = "-".join(str(x) for x in chunk)
        fixtures = _apifootball_get("fixtures", {"ids": ids_param})
        for fx in fixtures:
            try:
                fid = fx["fixture"]["id"]
                status = fx["fixture"]["status"]["short"]
                if status not in ("FT", "AET", "PEN"):
                    continue  # partido aún no terminado, se reintenta después
                goles_local = fx["goals"]["home"]
                goles_visita = fx["goals"]["away"]
                if goles_local is None or goles_visita is None:
                    continue
                results_by_id[fid] = (goles_local, goles_visita)
            except (KeyError, TypeError):
                continue

    for entry in state["value_log"]:
        if entry["resuelto"] or entry["fixture_id"] not in results_by_id:
            continue
        goles_local, goles_visita = results_by_id[entry["fixture_id"]]
        acierto = _acierto_por_mercado(entry["mercado"], goles_local, goles_visita)
        entry["goles_local"] = goles_local
        entry["goles_visita"] = goles_visita
        entry["acierto"] = acierto
        entry["resuelto"] = True

    state["last_resolve_date"] = today
    return state


def build_rendimiento_message(state):
    """Arma el resumen de aciertos para el comando /rendimiento."""
    log = state.get("value_log", [])
    if not log:
        return (
            "📊 Todavía no hay value bets registradas.\n"
            "Se van guardando automáticamente cada vez que el bot detecta "
            "una con /valor on activo."
        )

    resueltas = [e for e in log if e["resuelto"]]
    pendientes = len(log) - len(resueltas)
    aciertos = [e for e in resueltas if e["acierto"]]

    lines = [
        "📊 Rendimiento de tus value bets:",
        f"Total detectadas: {len(log)}",
        f"Resueltas (con resultado ya conocido): {len(resueltas)}",
        f"Pendientes (partido aún no jugado/procesado): {pendientes}",
    ]

    if resueltas:
        pct = len(aciertos) / len(resueltas) * 100
        lines.append(f"✅ Aciertos: {len(aciertos)}/{len(resueltas)} ({pct:.1f}%)")

        # Ganancia simulada apostando 1 unidad plana por cada value bet
        ganancia = 0.0
        for e in resueltas:
            ganancia += (e["cuota"] - 1) if e["acierto"] else -1
        lines.append(f"💰 Resultado simulado (1 unidad c/u): {ganancia:+.2f} unidades")
    else:
        lines.append("Aún no hay ninguna resuelta — vuelve a preguntar en unos días.")

    lines.append(
        "\nNota: esto es solo seguimiento estadístico de tu modelo, no "
        "reemplaza tu propio juicio antes de apostar dinero real."
    )
    return "\n".join(lines)


# Mismo gancho previo, ya no se usa directamente (ver poisson_model.py),
# se deja por compatibilidad si algo externo lo importa.
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
    state = check_value_bets(state)
    state = resolve_pending_results(state)
    save_state(state)
    print("Corrida completa. Alertas guardadas en alerts.json")


if __name__ == "__main__":
    main()

"""
poisson_model.py - Réplica exacta de la lógica de Calculadora_Partido.xlsx

Traduce a Python las mismas fórmulas que tienes en tu Excel:
  - Fuerza de ataque/defensa relativa a la liga
  - Goles esperados (lambda) por Poisson
  - Matriz de probabilidades Poisson (marcadores 0-6 goles)
  - Probabilidades de mercado: 1X2, Over/Under 2.5, BTTS
  - Comparación contra cuota de mercado para detectar VALOR

Los datos de equipos y ligas se cargan desde teams_stats.json, que se
genera una sola vez a partir de tu archivo Excel (Poisson_Trading.xlsx).
Cuando actualices tus estadísticas de equipos en el Excel, hay que volver
a generar este JSON (avísame en el chat y te lo regenero).
"""

import json
import math
import os

STATS_FILE = os.path.join(os.path.dirname(__file__), "teams_stats.json")

with open(STATS_FILE, "r", encoding="utf-8") as f:
    _DATA = json.load(f)

TEAMS = _DATA["teams"]
LEAGUES = _DATA["leagues"]
VALUE_THRESHOLD = _DATA.get("value_threshold", 0.05)

MAX_GOALS = 7  # igual que el Excel: marcadores 0 a 6


def _poisson_pmf(k, lam):
    return (math.exp(-lam) * (lam ** k)) / math.factorial(k)


def get_team_stats(team_name):
    """Busca un equipo por nombre exacto (case-insensitive, coincidencia parcial)."""
    if team_name in TEAMS:
        return TEAMS[team_name]
    # fallback: coincidencia parcial insensible a mayúsculas
    tn_lower = team_name.lower()
    for name, stats in TEAMS.items():
        if tn_lower in name.lower() or name.lower() in tn_lower:
            return stats
    return None


def compute_match_probabilities(home_team, away_team):
    """
    Replica B18:B44 del Excel. Devuelve un dict con las probabilidades del
    modelo, o None si no se encuentra alguno de los dos equipos.
    """
    home_stats = get_team_stats(home_team)
    away_stats = get_team_stats(away_team)
    if not home_stats or not away_stats:
        return None

    liga = home_stats["liga"]
    liga_stats = LEAGUES.get(liga)
    if not liga_stats:
        return None

    liga_gf_local = liga_stats["prom_gf_local"]
    liga_gf_visitante = liga_stats["prom_gf_visitante"]

    prom_gf_local = home_stats["prom_gf_local"]
    prom_gc_local = home_stats["prom_gc_local"]
    prom_gf_visitante = away_stats["prom_gf_visitante"]
    prom_gc_visitante = away_stats["prom_gc_visitante"]

    # Fuerza de ataque/defensa (B18:B21 del Excel)
    ataque_local = prom_gf_local / liga_gf_local
    defensa_local = prom_gc_local / liga_gf_visitante
    ataque_visitante = prom_gf_visitante / liga_gf_visitante
    defensa_visitante = prom_gc_visitante / liga_gf_local

    # Goles esperados (B23:B24 del Excel)
    lambda_local = ataque_local * defensa_visitante * liga_gf_local
    lambda_visitante = ataque_visitante * defensa_local * liga_gf_visitante

    # Matriz de Poisson (B29:H35 del Excel)
    matrix = [
        [_poisson_pmf(i, lambda_local) * _poisson_pmf(j, lambda_visitante) for j in range(MAX_GOALS)]
        for i in range(MAX_GOALS)
    ]

    prob_home_win = sum(
        matrix[i][j] for i in range(MAX_GOALS) for j in range(MAX_GOALS) if i > j
    )
    prob_draw = sum(
        matrix[i][j] for i in range(MAX_GOALS) for j in range(MAX_GOALS) if i == j
    )
    prob_away_win = sum(
        matrix[i][j] for i in range(MAX_GOALS) for j in range(MAX_GOALS) if i < j
    )
    prob_over25 = sum(
        matrix[i][j] for i in range(MAX_GOALS) for j in range(MAX_GOALS) if (i + j) > 2
    )
    prob_under25 = 1 - prob_over25

    prob_home_scores_0 = sum(matrix[0][j] for j in range(MAX_GOALS))
    prob_away_scores_0 = sum(matrix[i][0] for i in range(MAX_GOALS))
    prob_btts_no = prob_home_scores_0 + prob_away_scores_0 - matrix[0][0]
    prob_btts_yes = 1 - prob_btts_no

    return {
        "lambda_local": round(lambda_local, 4),
        "lambda_visitante": round(lambda_visitante, 4),
        "home_win": prob_home_win,
        "draw": prob_draw,
        "away_win": prob_away_win,
        "over_2_5": prob_over25,
        "under_2_5": prob_under25,
        "btts_yes": prob_btts_yes,
        "btts_no": prob_btts_no,
    }


def check_value(model_prob, market_odds, threshold=None):
    """
    Replica la columna F ('¿Entrar?') del Excel:
      prob_implicita = 1 / cuota
      diferencia = prob_modelo - prob_implicita
      VALOR si diferencia >= umbral
    Devuelve (es_valor: bool, diferencia: float, prob_implicita: float)
    """
    if threshold is None:
        threshold = VALUE_THRESHOLD
    if not market_odds or market_odds <= 0:
        return False, None, None
    implied = 1 / market_odds
    diff = model_prob - implied
    return diff >= threshold, diff, implied


def find_value_bets(home_team, away_team, odds_1x2=None, odds_over25=None,
                     odds_under25=None, odds_btts_yes=None, odds_btts_no=None):
    """
    Punto de entrada principal: calcula probabilidades del modelo para el
    partido y las compara contra las cuotas de mercado que le pases.
    odds_1x2 es una tupla (cuota_local, cuota_empate, cuota_visitante).

    Devuelve una lista de dicts, uno por cada mercado donde se detectó
    VALOR, cada uno con: mercado, prob_modelo, cuota, prob_implicita, diferencia.
    """
    probs = compute_match_probabilities(home_team, away_team)
    if probs is None:
        return None  # equipo(s) no encontrado(s) en teams_stats.json

    value_bets = []

    def _add_if_value(market_label, model_prob, market_odds):
        is_value, diff, implied = check_value(model_prob, market_odds)
        if is_value:
            value_bets.append({
                "mercado": market_label,
                "prob_modelo": round(model_prob, 4),
                "cuota": market_odds,
                "prob_implicita": round(implied, 4),
                "diferencia": round(diff, 4),
            })

    if odds_1x2:
        cuota_local, cuota_empate, cuota_visitante = odds_1x2
        if cuota_local:
            _add_if_value("Local (1)", probs["home_win"], cuota_local)
        if cuota_empate:
            _add_if_value("Empate (X)", probs["draw"], cuota_empate)
        if cuota_visitante:
            _add_if_value("Visitante (2)", probs["away_win"], cuota_visitante)

    if odds_over25:
        _add_if_value("Over 2.5", probs["over_2_5"], odds_over25)
    if odds_under25:
        _add_if_value("Under 2.5", probs["under_2_5"], odds_under25)
    if odds_btts_yes:
        _add_if_value("BTTS - Sí", probs["btts_yes"], odds_btts_yes)
    if odds_btts_no:
        _add_if_value("BTTS - No", probs["btts_no"], odds_btts_no)

    return value_bets

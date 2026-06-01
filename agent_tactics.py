# agent_tactics.py
"""
Agente autónomo de análisis táctico.

Analiza:
  - Plantilla propia (stats promedio, formación actual)
  - Último resultado propio (forma reciente)
  - Posición del rival en la clasificación
  - Stats promedio del rival (si disponibles)
  - Historial de tácticas propias exitosas

Recomienda:
  - Formación, plan de juego, tackling, sliders, tácticas de línea
  - Con justificación legible en Discord

Invocación:
  - Manual vía /agenttactics
  - Automática pre-partido (futuro)
"""
from __future__ import annotations

import json
from llm_client import call_llm_json

_SYSTEM = """You are a tactical advisor for an Online Soccer Manager team.
You analyze opponent data and squad capabilities to recommend optimal tactics.
You always respond with valid JSON only. No text outside the JSON."""

_GAMEPLAN_OPTIONS  = ["Shoot on sight", "Long ball", "Counter-attack", "Wing play", "Passing game"]
_TACKLING_OPTIONS  = ["Careful", "Normal", "Reckless", "Aggressive"]
_MARKING_OPTIONS   = ["Zonal marking", "Man marking"]
_FWD_OPTIONS       = ["Attack only", "Support midfield", "Drop deep"]
_MID_OPTIONS       = ["Protect the defence", "Push forward", "Stay in position"]
_DEF_OPTIONS       = ["Defend deep", "Attacking full-backs", "Support midfield"]
_FORMATION_OPTIONS = [
    "4-3-3 A", "4-3-3 B", "4-5-1", "4-2-3-1", "4-4-2 A", "4-4-2 B",
    "3-5-2", "3-4-3 A", "3-4-3 B", "5-3-2", "5-4-1 A", "5-4-1 B",
]


def _squad_summary(squad: list[dict]) -> dict:
    """Computes average stats and position distribution from squad."""
    if not squad:
        return {}
    starters = [p for p in squad if p.get("in_lineup")]
    pool     = starters or squad

    avg_att = int(sum(p.get("stat_att", 0) for p in pool) / len(pool))
    avg_def = int(sum(p.get("stat_def", 0) for p in pool) / len(pool))
    avg_ovr = int(sum(p.get("stat_ovr", 0) for p in pool) / len(pool))
    pos_dist = {}
    for p in pool:
        pos = p.get("position", "?")
        pos_dist[pos] = pos_dist.get(pos, 0) + 1

    return {"avg_att": avg_att, "avg_def": avg_def, "avg_ovr": avg_ovr,
            "position_dist": pos_dist, "starter_count": len(starters)}


def _fmt_standings(standings: list[dict], my_team: str, opponent: str) -> str:
    if not standings:
        return "  (standings not available)"
    lines = []
    for i, team in enumerate(standings[:12], 1):
        club  = team.get("Club", "?")
        pts   = team.get("Points", team.get("Pts", "?"))
        played = team.get("Played", team.get("P", "?"))
        marker = " ← MY TEAM" if my_team.lower() in club.lower() else \
                 " ← OPPONENT" if opponent.lower() in club.lower() else ""
        lines.append(f"  {i:2}. {club:<20} {pts}pts ({played}P){marker}")
    return "\n".join(lines)


def analyze_tactics(
    my_team: str,
    my_squad: list[dict],
    my_current_tactics: dict,
    standings: list[dict],
    opponent_name: str,
    opponent_squad_stats: dict | None = None,
    matchday: int | None = None,
    is_home: bool | None = None,
) -> dict:
    """
    Runs the LLM agent to recommend tactics against the given opponent.

    opponent_squad_stats: { "avg_att": int, "avg_def": int, "avg_ovr": int } if available

    Returns:
        {
            "formation":         "4-3-3 A",
            "game_plan":         "Counter-attack",
            "tackling":          "Normal",
            "pressure":          60,
            "mentality":         50,
            "tempo":             55,
            "marking":           "Zonal marking",
            "forwards_tactic":   "Attack only",
            "midfielders_tactic": "Protect the defence",
            "defenders_tactic":  "Defend deep",
            "offside_trap":      false,
            "reasoning":         "Explanation..."
        }
    """
    my_stats = _squad_summary(my_squad)

    opp_stats_str = "Not available"
    if opponent_squad_stats:
        opp_stats_str = (
            f"ATT avg: {opponent_squad_stats.get('avg_att','?')}, "
            f"DEF avg: {opponent_squad_stats.get('avg_def','?')}, "
            f"OVR avg: {opponent_squad_stats.get('avg_ovr','?')}"
        )

    home_str = "Home" if is_home is True else "Away" if is_home is False else "Unknown"
    md_str   = f"Matchday {matchday}" if matchday else "Unknown matchday"

    current_str = json.dumps({
        k: v for k, v in my_current_tactics.items()
        if k in ("game_plan", "tackling", "pressure", "mentality", "tempo",
                 "marking", "forwards_tactic", "midfielders_tactic",
                 "defenders_tactic", "offside_trap")
    }, indent=2) if my_current_tactics else "Not set"

    prompt = f"""
MATCH CONTEXT:
  My team:    {my_team}
  Opponent:   {opponent_name}
  Venue:      {home_str}
  {md_str}

MY SQUAD STATS (starters):
  ATT avg: {my_stats.get('avg_att','?')}
  DEF avg: {my_stats.get('avg_def','?')}
  OVR avg: {my_stats.get('avg_ovr','?')}
  Position distribution: {json.dumps(my_stats.get('position_dist', {}))}

OPPONENT STATS: {opp_stats_str}

CURRENT STANDINGS:
{_fmt_standings(standings, my_team, opponent_name)}

MY CURRENT TACTICS:
{current_str}

AVAILABLE OPTIONS:
  Formations:          {_FORMATION_OPTIONS}
  Game plan:           {_GAMEPLAN_OPTIONS}
  Tackling:            {_TACKLING_OPTIONS}
  Marking:             {_MARKING_OPTIONS}
  Forwards tactic:     {_FWD_OPTIONS}
  Midfielders tactic:  {_MID_OPTIONS}
  Defenders tactic:    {_DEF_OPTIONS}
  Pressure/Mentality/Tempo: 0-100 integer

TASK:
Recommend the best tactics for this match considering:
- If opponent is stronger (higher OVR avg): more defensive/counter
- If opponent is weaker: more attacking
- Home game: can be slightly more aggressive
- Away game: prioritize defensive stability
- High in standings: conservative to protect position
- Low in standings: need points, more offensive risk

Return ONLY this JSON:
{{
  "formation":          "...",
  "game_plan":          "...",
  "tackling":           "...",
  "pressure":           50,
  "mentality":          50,
  "tempo":              50,
  "marking":            "...",
  "forwards_tactic":    "...",
  "midfielders_tactic": "...",
  "defenders_tactic":   "...",
  "offside_trap":       false,
  "reasoning":          "One or two sentences explaining the main tactical choice"
}}
"""

    result = call_llm_json(prompt, system=_SYSTEM)

    # Validate options
    def pick(value, options, default):
        if value in options:
            return value
        # Case-insensitive match
        for opt in options:
            if opt.lower() == str(value).lower():
                return opt
        return default

    def clamp(value, lo=0, hi=100, default=50):
        try:
            return max(lo, min(hi, int(value)))
        except (TypeError, ValueError):
            return default

    return {
        "formation":          pick(result.get("formation"),          _FORMATION_OPTIONS, "4-3-3 A"),
        "game_plan":          pick(result.get("game_plan"),          _GAMEPLAN_OPTIONS,  "Passing game"),
        "tackling":           pick(result.get("tackling"),           _TACKLING_OPTIONS,  "Normal"),
        "pressure":           clamp(result.get("pressure"),          default=50),
        "mentality":          clamp(result.get("mentality"),         default=50),
        "tempo":              clamp(result.get("tempo"),             default=50),
        "marking":            pick(result.get("marking"),            _MARKING_OPTIONS,   "Zonal marking"),
        "forwards_tactic":    pick(result.get("forwards_tactic"),    _FWD_OPTIONS,       "Attack only"),
        "midfielders_tactic": pick(result.get("midfielders_tactic"), _MID_OPTIONS,       "Stay in position"),
        "defenders_tactic":   pick(result.get("defenders_tactic"),   _DEF_OPTIONS,       "Defend deep"),
        "offside_trap":       bool(result.get("offside_trap", False)),
        "reasoning":          result.get("reasoning", ""),
    }

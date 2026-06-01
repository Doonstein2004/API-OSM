# agent_transfer.py
"""
Agente autónomo de gestión de transferibles.

Analiza:
  - La plantilla actual (stats, edad, estado, valor)
  - El historial de ventas de la BD (qué posiciones se venden, a qué precio)
  - Los jugadores ya en lista de transferibles

Decide:
  - Qué 4-6 jugadores conviene tener como candidatos de venta permanentes
  - Actualiza transfer_queue.json con su decisión
  - Devuelve un resumen del razonamiento para notificar en Discord

Invocación:
  - Autónoma (loop diario en discord_bot.py)
  - Manual vía comando /agentransfer
"""
from __future__ import annotations

import json
from llm_client import call_llm_json

_SYSTEM = """You are an autonomous transfer manager for an Online Soccer Manager team.
You analyze squad data and sales history to decide which players to keep as permanent
transfer list candidates (players the bot will automatically put up for sale).

You ALWAYS respond with valid JSON only, no explanation outside the JSON.
Never include markdown, code blocks, or text outside the JSON structure."""

_POSITION_NAMES = {"A": "Forward", "M": "Midfielder", "D": "Defender", "G": "Goalkeeper"}


def _fmt_squad(squad: list[dict]) -> str:
    lines = []
    for p in squad:
        pos   = _POSITION_NAMES.get(p.get("position", ""), p.get("position", "?"))
        sp    = p.get("specific_position", "")
        name  = p.get("name", "?")
        age   = p.get("age", 0)
        att   = p.get("stat_att", 0)
        def_  = p.get("stat_def", 0)
        ovr   = p.get("stat_ovr", 0)
        val   = p.get("value", 0)
        val_m = f"{val/1_000_000:.1f}M" if isinstance(val, (int, float)) and val >= 1e6 else str(val)
        fit   = p.get("fitness", 0)
        flags = []
        if p.get("in_lineup"):    flags.append("STARTER")
        if p.get("in_training"):  flags.append("TRAINING")
        if p.get("is_injured"):   flags.append("INJURED")
        if p.get("in_selection"): flags.append("BENCH")
        status = ",".join(flags) if flags else "UNASSIGNED"
        lines.append(
            f"  - {name} | {pos}({sp}) | Age:{age} | ATT:{att} DEF:{def_} OVR:{ovr} | Val:{val_m} | {status}"
        )
    return "\n".join(lines)


def _fmt_sales(sales: list[dict]) -> str:
    if not sales:
        return "  (no recent sales)"
    lines = []
    for s in sales[:20]:
        name   = s.get("player_name", "?")
        pos    = s.get("position", "?")
        price  = s.get("final_price", 0)
        rnd    = s.get("round", "?")
        txtype = s.get("transaction_type", "?")
        lines.append(f"  - {name} ({pos}) | {txtype} | Price:{price}M | Round:{rnd}")
    return "\n".join(lines)


def analyze_squad_for_transfers(
    team_name: str,
    squad: list[dict],
    recent_sales: list[dict],
    current_listed: list[str],
    current_candidates: list[str],
    max_candidates: int = 6,
) -> dict:
    """
    Runs the LLM agent to decide which players to keep as transfer candidates.

    Returns:
        {
            "candidates": ["PlayerA", "PlayerB", ...],  # ordered list, max max_candidates
            "reasoning":  "Short explanation of the decision",
        }
    """
    prompt = f"""
TEAM: {team_name}

CURRENT SQUAD:
{_fmt_squad(squad)}

PLAYERS CURRENTLY ON TRANSFER LIST: {json.dumps(current_listed)}

CURRENT CONFIGURED CANDIDATES (what the bot uses now): {json.dumps(current_candidates)}

RECENT SALES HISTORY:
{_fmt_sales(recent_sales)}

YOUR TASK:
Select the best {max_candidates} players to keep as PERMANENT transfer list candidates.
These are players the bot will automatically put for sale whenever a slot opens.

SELECTION RULES (in order of priority):
1. NEVER include STARTER players (status=STARTER) - they are essential
2. NEVER include players currently in TRAINING - do not interrupt their session
3. NEVER include INJURED players - poor sale value and optics
4. PREFER players with UNASSIGNED status (not in any selection)
5. PREFER older players (age > 28) with lower stats - they won't develop much more
6. PREFER players whose position is already well covered (many players at same position)
7. Keep at least 2 non-listed backup players for each key position (GK, CB, CM, ST)

Return ONLY this JSON, nothing else:
{{
  "candidates": ["Player1", "Player2", "Player3", "Player4"],
  "reasoning": "One sentence explaining the main criteria used"
}}
"""

    result = call_llm_json(prompt, system=_SYSTEM)

    # Validate and sanitize
    candidates = result.get("candidates", [])
    if not isinstance(candidates, list):
        candidates = []

    # Only keep names that actually exist in the squad
    squad_names_lower = {p.get("name", "").lower(): p.get("name", "") for p in squad}
    valid_candidates = []
    for name in candidates:
        lower = name.lower()
        # Exact match or fuzzy
        if lower in squad_names_lower:
            valid_candidates.append(squad_names_lower[lower])
        else:
            # Try partial match
            for sq_lower, sq_name in squad_names_lower.items():
                if lower in sq_lower or sq_lower in lower:
                    valid_candidates.append(sq_name)
                    break

    # Remove duplicates, keep order
    seen = set()
    deduped = []
    for name in valid_candidates:
        if name.lower() not in seen:
            seen.add(name.lower())
            deduped.append(name)

    return {
        "candidates": deduped[:max_candidates],
        "reasoning":  result.get("reasoning", ""),
    }

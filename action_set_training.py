# action_set_training.py
"""
Renueva automáticamente los entrenamientos en OSM via Playwright.

Flujo por slot de entrenamiento:
  1. Leer el jugador actual (antes de reclamar) desde la tarjeta del slot
  2. Si está Finished → clic en "Completado" (btn-show-result / claim())
  3. Esperar (polling) a que el slot pase de panel-player a panel-trainer
  4. Clic en "Empezar" → abre #modal-dialog-trainplayer
  5. Buscar el mismo jugador en el modal por nombre y seleccionarlo via KO setPlayer()
"""
import json
import time
from playwright.sync_api import Page
from utils import handle_popups

TRAINING_URL = "https://en.onlinesoccermanager.com/Training"


def _training_loaded(page: Page, timeout: int = 12000) -> bool:
    for sel in [".training-slot-container", ".training-slot",
                "[data-bind*='trainingSessionsPartial']"]:
        try:
            page.wait_for_selector(sel, timeout=timeout, state="attached")
            return True
        except Exception:
            pass
    return False


def _navigate_to_training(page: Page) -> bool:
    for sel in ["a[href='/Training']", "a[href*='/Training']",
                "a:has-text('Training')", "a:has-text('Entrenamiento')",
                ".nav-training a"]:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=800):
                loc.click()
                time.sleep(2)
                if _training_loaded(page):
                    print(f"  ✓ Training cargado vía SPA ({sel})")
                    return True
        except Exception:
            pass

    print("  → Fallback: page.goto(/Training)...")
    try:
        page.goto(TRAINING_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)
        handle_popups(page)
        if _training_loaded(page):
            return True
    except Exception as e:
        print(f"  ⚠️ page.goto() falló: {e}")
    return False


def _get_slot_states(page: Page) -> list[dict]:
    """
    Lee el estado de cada slot vía KO + DOM.
    Devuelve lista de dicts:
      { index, title, state, player_name, player_id }
    state: 'finished' | 'in_progress' | 'needs_player' | 'universal_locked' | 'unknown'
    """
    try:
        return page.evaluate("""
            () => {
                const slots = [];
                const containers = document.querySelectorAll('.training-slot-container');
                containers.forEach((container, i) => {
                    const slotEl = container.querySelector('.training-slot');
                    if (!slotEl) return;

                    const isPanelTrainer = slotEl.classList.contains('panel-trainer');
                    const isPanelPlayer  = slotEl.classList.contains('panel-player');
                    const isUnivLocked   = slotEl.classList.contains('universal-trainer-locked');

                    // Título del entrenador (tipo de posición)
                    const titleEl = slotEl.querySelector('.staff-title');
                    const title   = titleEl ? titleEl.innerText.trim() : '';

                    // Nombre del jugador actual (visible en la tarjeta)
                    const nameEl  = slotEl.querySelector('h2.player-mini-card-name');
                    let playerName = nameEl ? nameEl.innerText.trim() : '';
                    // Quitar el número de dorsal si viene incluido (ej. "7 Kylian Mbappé")
                    if (playerName) {
                        playerName = playerName.replace(/^\\d+\\s+/, '').trim();
                    }

                    // ID del jugador via KO
                    let playerId = null;
                    try {
                        const claimBtn = slotEl.querySelector('button.btn-show-result') ||
                                         slotEl.querySelector('[data-bind*="currentTrainingSession"]');
                        if (claimBtn) {
                            const ctx = ko.contextFor(claimBtn);
                            if (ctx) {
                                // btn-show-result tiene $data = currentTrainingSession
                                const session = ctx.$data;
                                if (session && typeof session.playerPartial === 'function') {
                                    const p = session.playerPartial();
                                    if (p) {
                                        playerId = p.id || null;
                                        if (!playerName) {
                                            const n = p.name;
                                            playerName = typeof n === 'function' ? n() : (n || '');
                                        }
                                    }
                                }
                            }
                        }
                    } catch(e) {}

                    // Detectar botón Completado
                    const claimBtn   = slotEl.querySelector('button.btn-show-result');
                    const hasFinished = !!claimBtn && claimBtn.offsetParent !== null;

                    // Detectar botón Empezar
                    const startBtn    = slotEl.querySelector('button[data-bind*="selectPlayer"]');
                    const needsPlayer = !!(startBtn && startBtn.offsetParent !== null && !hasFinished && isPanelTrainer);

                    const inProgress  = isPanelPlayer && !hasFinished;

                    const state = isUnivLocked  ? 'universal_locked'
                                : hasFinished   ? 'finished'
                                : needsPlayer   ? 'needs_player'
                                : inProgress    ? 'in_progress'
                                : 'unknown';

                    slots.push({ index: i, title, state, playerName, playerId });
                });
                return slots;
            }
        """)
    except Exception as e:
        print(f"  ⚠️ get_slot_states error: {e}")
        return []


def _wait_slot_reset(page: Page, slot_index: int, timeout: int = 15) -> bool:
    """
    Espera (polling) a que el slot pase de panel-player → panel-trainer.
    Necesario porque OSM muestra una animación tras el claim antes de resetear.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            result = page.evaluate(f"""
                () => {{
                    const containers = document.querySelectorAll('.training-slot-container');
                    const container = containers[{slot_index}];
                    if (!container) return 'not_found';
                    const slot = container.querySelector('.training-slot');
                    if (!slot) return 'no_slot';
                    if (slot.classList.contains('panel-trainer')) return 'needs_player';
                    if (slot.classList.contains('panel-player')) return 'in_progress';
                    return 'unknown';
                }}
            """)
            if result == 'needs_player':
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def _claim_slot(page: Page, slot_index: int) -> bool:
    """Reclama el entrenamiento terminado. Intenta KO claim() primero, luego click DOM."""
    try:
        ok = page.evaluate(f"""
            (function() {{
                const containers = document.querySelectorAll('.training-slot-container');
                const container = containers[{slot_index}];
                if (!container) return false;
                const claimBtn = container.querySelector('button.btn-show-result');
                if (!claimBtn || !claimBtn.offsetParent) return false;
                try {{
                    const ctx = ko.contextFor(claimBtn);
                    if (ctx && ctx.$data && typeof ctx.$data.claim === 'function') {{
                        ctx.$data.claim();
                        return 'ko';
                    }}
                }} catch(e) {{}}
                claimBtn.click();
                return 'click';
            }})()
        """)
        if ok:
            print(f"  ✓ Slot {slot_index}: claim ({ok})")
            time.sleep(1)
            handle_popups(page)
            return True
    except Exception as e:
        print(f"  ⚠️ claim KO slot {slot_index}: {e}")

    # Fallback Playwright
    try:
        btn = page.locator(".training-slot-container").nth(slot_index).locator("button.btn-show-result").first
        if btn.is_visible(timeout=1500):
            btn.click()
            time.sleep(1)
            handle_popups(page)
            print(f"  ✓ Slot {slot_index}: Completado (PW click)")
            return True
    except Exception as e:
        print(f"  ⚠️ claim PW slot {slot_index}: {e}")
    return False


def _open_player_modal(page: Page, slot_index: int) -> bool:
    """Clica 'Empezar' en el slot vacío para abrir el modal de selección de jugador."""
    try:
        ok = page.evaluate(f"""
            (function() {{
                const containers = document.querySelectorAll('.training-slot-container');
                const container = containers[{slot_index}];
                if (!container) return false;
                const startBtn = container.querySelector('button[data-bind*="selectPlayer"]');
                if (!startBtn || !startBtn.offsetParent) return false;
                try {{
                    const ctx = ko.contextFor(startBtn);
                    if (ctx && ctx.$data && typeof ctx.$data.selectPlayer === 'function') {{
                        ctx.$data.selectPlayer(ctx.$root.ongoingTrainingSessionsPartial());
                        return 'ko';
                    }}
                }} catch(e) {{}}
                startBtn.click();
                return 'click';
            }})()
        """)
        if ok:
            print(f"  → Slot {slot_index}: modal abierto ({ok})")
            time.sleep(1.5)
            return True
    except Exception as e:
        print(f"  ⚠️ open modal slot {slot_index}: {e}")

    try:
        btn = page.locator(".training-slot-container").nth(slot_index).locator("button[data-bind*='selectPlayer']").first
        if btn.is_visible(timeout=1500):
            btn.click()
            time.sleep(1.5)
            return True
    except Exception:
        pass
    return False


_COACH_STAT_KEY = {
    "attacking coach":   "statAtt",
    "midfielder coach":  "statOvr",
    "defending coach":   "statDef",
    "goalkeeping coach": "statDef",
}


def _select_player_in_modal(page: Page, player_name: str, coach_type: str = "") -> str | None:
    """
    Espera a que #modal-dialog-trainplayer esté visible y selecciona el jugador.
    Si player_name está vacío, elige el jugador con mayor stat para el coach_type dado.
    Usa KO setPlayer() directamente.
    Devuelve el nombre del jugador seleccionado, o None si no se encontró.
    """
    try:
        page.wait_for_selector("#modal-dialog-trainplayer", timeout=5000, state="visible")
    except Exception:
        try:
            page.wait_for_selector("#squad-table", timeout=3000, state="visible")
        except Exception:
            print("  ⚠️ Modal de selección de jugador no apareció")
            return None

    # Intentar selección via KO setPlayer() con el nombre guardado
    if player_name:
        try:
            selected = page.evaluate(f"""
                (function() {{
                    const target = {json.dumps(player_name)}.toLowerCase();
                    const modal  = document.querySelector('#modal-dialog-trainplayer') ||
                                   document.querySelector('#squad-table');
                    if (!modal) return null;

                    // Intentar KO directo: $root.setPlayer(playerData, msg1, msg2)
                    try {{
                        const ctx = ko.contextFor(modal);
                        const root = ctx && (ctx.$root || ctx.$data);
                        if (root && root.playersGroupablePartial) {{
                            const groupable = typeof root.playersGroupablePartial === 'function'
                                ? root.playersGroupablePartial() : root.playersGroupablePartial;
                            if (groupable && typeof groupable.getPlayers === 'function') {{
                                for (const group of groupable.getPlayers()) {{
                                    const items = group.players.getItems();
                                    for (const player of items) {{
                                        const n = typeof player.name === 'function' ? player.name() : (player.name || '');
                                        const full = typeof player.fullNameWithSquadNumber === 'function'
                                            ? player.fullNameWithSquadNumber() : '';
                                        if (n.toLowerCase() === target ||
                                            full.toLowerCase().includes(target) ||
                                            target.includes(n.toLowerCase())) {{
                                            if (!player.isInjured || !player.isInjured()) {{
                                                root.setPlayer(player,
                                                    '{{Player}} está empezando',
                                                    'Los jugadores no pueden estar en la alineación y entrenar a la vez. ¿Quieres quitar a {{Player}} del once inicial?'
                                                );
                                                return n;
                                            }}
                                        }}
                                    }}
                                }}
                            }}
                        }}
                    }} catch(e) {{}}

                    // Fallback: buscar la fila por nombre en el DOM y clicar
                    const rows = document.querySelectorAll('#squad-table tr.player-table-row:not(.disabled)');
                    for (const row of rows) {{
                        const nameEl = row.querySelector('span[data-bind="text: name"]');
                        if (nameEl) {{
                            const n = nameEl.innerText.trim().toLowerCase();
                            if (n === target || target.includes(n) || n.includes(target)) {{
                                row.click();
                                return nameEl.innerText.trim();
                            }}
                        }}
                    }}
                    return null;
                }})()
            """)
            if selected:
                print(f"  ✓ Modal: jugador seleccionado = {selected!r} (buscado: {player_name!r})")
                time.sleep(1)
                handle_popups(page)
                return selected
        except Exception as e:
            print(f"  ⚠️ setPlayer KO error: {e}")

    # Fallback: seleccionar el jugador con mayor stat relevante para el coach_type
    stat_key = _COACH_STAT_KEY.get(coach_type.lower(), "statOvr")
    reason   = f"mejor {stat_key}" if coach_type else "primero disponible"
    print(f"  ⚠️ Jugador '{player_name}' no encontrado — seleccionando por {reason}")
    try:
        selected = page.evaluate(f"""
            (function() {{
                const statKey = {json.dumps(stat_key)};
                function v(o) {{ return typeof o === 'function' ? o() : o; }}
                const modal = document.querySelector('#modal-dialog-trainplayer') ||
                              document.querySelector('#squad-table');
                if (!modal) return null;

                // Intentar via KO: buscar el jugador con mayor stat
                try {{
                    const ctx = ko.contextFor(modal);
                    const root = ctx && (ctx.$root || ctx.$data);
                    if (root && root.playersGroupablePartial) {{
                        const groupable = typeof root.playersGroupablePartial === 'function'
                            ? root.playersGroupablePartial() : root.playersGroupablePartial;
                        if (groupable && typeof groupable.getPlayers === 'function') {{
                            let best = null, bestStat = -1;
                            for (const group of groupable.getPlayers()) {{
                                for (const player of group.players.getItems()) {{
                                    if (player.isInjured && player.isInjured()) continue;
                                    const stat = v(player[statKey]) || v(player.statOvr) || 0;
                                    if (stat > bestStat) {{ bestStat = stat; best = player; }}
                                }}
                            }}
                            if (best) {{
                                root.setPlayer(best,
                                    '{{Player}} está empezando',
                                    'Los jugadores no pueden estar en la alineación y entrenar a la vez. ¿Quieres quitar a {{Player}} del once inicial?'
                                );
                                return v(best.name) || '';
                            }}
                        }}
                    }}
                }} catch(e) {{}}

                // Fallback DOM: primer no lesionado
                const first = document.querySelector('#squad-table tr.player-table-row:not(.disabled)');
                if (first) {{
                    const nameEl = first.querySelector('span[data-bind="text: name"]');
                    first.click();
                    return nameEl ? nameEl.innerText.trim() : 'desconocido';
                }}
                return null;
            }})()
        """)
        if selected:
            print(f"  ✓ Modal: seleccionado por {reason} = {selected!r}")
            time.sleep(1)
            handle_popups(page)
            return selected
    except Exception as e:
        print(f"  ⚠️ fallback por stat: {e}")

    return None


def _close_modal_if_open(page: Page):
    """Cierra el modal si quedó abierto por algún motivo."""
    try:
        close_btn = page.locator("#modal-dialog-trainplayer button.close").first
        if close_btn.is_visible(timeout=500):
            close_btn.click()
            time.sleep(0.5)
    except Exception:
        pass


def renew_training(page: Page, queued_players: dict | None = None) -> dict:
    """
    Reclama todos los entrenamientos terminados e inicia nuevos.
    queued_players: { "Attacking Coach": "PlayerName", ... } — si se provee, usa ese jugador
                    en lugar del anterior. Las claves son los títulos de los slots.
    Returns: {
        "claimed":  [ { slot, title, player } ],
        "started":  [ { slot, title, player } ],
        "errors":   list[str]
    }
    """
    claimed = []
    started = []
    errors  = []

    if not _navigate_to_training(page):
        return {"claimed": claimed, "started": started, "errors": ["page_not_loaded"]}

    time.sleep(1)
    handle_popups(page)

    # ── Paso 1: leer estados y guardar jugadores antes de reclamar ────────────
    states = _get_slot_states(page)
    print(f"  [training] {len(states)} slots:")
    for s in states:
        print(f"    Slot {s['index']} ({s['title'] or '?'}): {s['state']}"
              + (f"  jugador={s['playerName']!r}" if s['playerName'] else ""))

    # ── Paso 2: reclamar los terminados ───────────────────────────────────────
    for s in states:
        if s["state"] != "finished":
            continue
        if _claim_slot(page, s["index"]):
            # Esperar a que el slot se resetee antes de continuar con el siguiente
            if _wait_slot_reset(page, s["index"], timeout=15):
                print(f"  ✓ Slot {s['index']}: reseteado correctamente")
            else:
                print(f"  ⚠️ Slot {s['index']}: timeout esperando reset, continuando...")
            claimed.append({"slot": s["index"], "title": s["title"], "player": s["playerName"]})
        else:
            errors.append(f"claim_failed:slot{s['index']}")

    # Releer estados tras los claims
    if claimed:
        time.sleep(1)
        states = _get_slot_states(page)

    # ── Paso 3: iniciar entrenamiento en slots vacíos ─────────────────────────
    for s in states:
        if s["state"] != "needs_player":
            continue

        # Jugador programado en la cola tiene prioridad sobre el anterior
        prev_player = next(
            (c["player"] for c in claimed if c["slot"] == s["index"]),
            s["playerName"] or ""
        )
        queued_name = (queued_players or {}).get(s["title"], "")
        player_to_use = queued_name or prev_player
        if queued_name:
            print(f"  → Slot {s['index']} ({s['title']}): usando jugador programado {queued_name!r}")

        if not _open_player_modal(page, s["index"]):
            errors.append(f"modal_failed:slot{s['index']}")
            continue

        selected = _select_player_in_modal(page, player_to_use, coach_type=s["title"])
        if selected:
            started.append({"slot": s["index"], "title": s["title"], "player": selected})
        else:
            errors.append(f"select_failed:slot{s['index']}")
            _close_modal_if_open(page)

        time.sleep(1)

    print(f"  [training] Reclamados: {[c['player'] for c in claimed]}")
    print(f"  [training] Iniciados:  {[s['player'] for s in started]}")
    if errors:
        print(f"  [training] Errores: {errors}")

    return {"claimed": claimed, "started": started, "errors": errors}


def renew_training_for_slot(
    page: Page,
    league_name: str,
    career_url: str = "https://en.onlinesoccermanager.com/Career",
    queued_players: dict | None = None,
) -> dict:
    """Activa el slot de liga indicado y renueva los entrenamientos.
    queued_players se pasa directamente a renew_training() para priorizar jugadores programados."""
    from utils import click_slot_and_wait_for_dashboard, wait_for_visible_slots, get_slot_info

    page.goto(career_url, wait_until="domcontentloaded", timeout=30000)
    time.sleep(2)  # dar tiempo a que el SPA inicie antes de buscar slots
    wait_for_visible_slots(page, timeout=20000)
    time.sleep(1)
    handle_popups(page)

    slots = page.locator(".career-teamslot")
    count = slots.count()
    target_idx = None
    for i in range(count):
        _, slot_league, _ = get_slot_info(slots.nth(i))
        if slot_league and league_name.lower() in slot_league.lower():
            target_idx = i
            print(f"  ✓ Slot encontrado: DOM {i} → '{slot_league}'")
            break

    if target_idx is None:
        return {"claimed": [], "started": [], "errors": [f"slot_not_found:{league_name}"]}

    if not click_slot_and_wait_for_dashboard(page, target_idx):
        return {"claimed": [], "started": [], "errors": ["slot_activation_failed"]}

    return renew_training(page, queued_players=queued_players)

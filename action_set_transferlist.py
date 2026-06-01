# action_set_transferlist.py
"""
Gestiona la lista de transferibles en OSM via Playwright.

Flujo:
  1. Navegar a /TransferList → pestaña "Sell players"
  2. Leer estado via KO: max_slots, available_slots, jugadores listados
  3. Para cada slot vacío: abrir modal → seleccionar primer candidato no listado
"""
import json
import time
from playwright.sync_api import Page
from utils import handle_popups, click_slot_and_wait_for_dashboard, wait_for_visible_slots, get_slot_info

TRANSFER_LIST_URL = "https://en.onlinesoccermanager.com/TransferList"


def _transferlist_loaded(page: Page, timeout: int = 12000) -> bool:
    for sel in ["#sell-players-tab", "[data-bind*='sellPlayerSlots']", "#sell-players"]:
        try:
            page.wait_for_selector(sel, timeout=timeout, state="attached")
            return True
        except Exception:
            pass
    return False


def _navigate_to_transferlist(page: Page) -> bool:
    for sel in ["a[href='/TransferList']", "a[href*='/TransferList']",
                "a:has-text('Transfer')"]:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=800):
                loc.click()
                time.sleep(2)
                if _transferlist_loaded(page):
                    print(f"  ✓ TransferList cargado vía SPA ({sel})")
                    return True
        except Exception:
            pass

    print("  → Fallback: page.goto(/TransferList)...")
    try:
        page.goto(TRANSFER_LIST_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)
        handle_popups(page)
        if _transferlist_loaded(page):
            return True
    except Exception as e:
        print(f"  ⚠️ page.goto() falló: {e}")
    return False


def _activate_sell_tab(page: Page):
    """Asegura que la pestaña 'Sell players' esté activa."""
    try:
        tab = page.locator("#sell-players-tab a").first
        if tab.is_visible(timeout=3000):
            tab.click()
            time.sleep(1)
    except Exception:
        pass


def get_transferlist_state(page: Page) -> dict:
    """
    Lee el estado de la lista de transferibles via KO.js.
    Returns: { max_slots, filled_slots, available_slots, listed_players: [{name, value}] }
    """
    try:
        state = page.evaluate("""
            () => {
                // Intentar obtener el root desde el contenedor de sell-players
                const el = document.querySelector('#sell-players, [data-bind*="sellPlayerSlots"]') ||
                           document.querySelector('#sell-players-tab');
                if (!el) return null;
                const ctx = ko.contextFor(el);
                if (!ctx) return null;
                const root = ctx.$root || ctx.$data;
                if (!root) return null;

                function v(obs) { return typeof obs === 'function' ? obs() : obs; }

                const maxSlots  = v(root.maxPlayersOnTransferlist)  ?? 4;
                const available = v(root.availableSellPlayerSlotsAmount) ?? 0;
                const filled    = maxSlots - available;

                const listed = [];
                const slots = v(root.sellPlayerSlots) || [];
                for (const slot of slots) {
                    if (!slot || !slot.playerPartial) continue;
                    const p = v(slot.playerPartial);
                    if (!p) continue;
                    listed.push({
                        name:  v(p.name)  || '',
                        value: v(p.value) || 0,
                        price: v(slot.price) || 0,
                    });
                }

                return { max_slots: maxSlots, available_slots: available,
                         filled_slots: filled, listed_players: listed };
            }
        """)
        if state:
            return state
    except Exception as e:
        print(f"  ⚠️ get_transferlist_state KO: {e}")

    # Fallback DOM: leer del texto del tab "Sell players X/Y"
    try:
        tab_text = page.locator("#sell-players-tab span").first.inner_text(timeout=3000)
        import re
        m = re.search(r'(\d+)/(\d+)', tab_text)
        if m:
            filled   = int(m.group(1))
            max_s    = int(m.group(2))
            return {"max_slots": max_s, "filled_slots": filled,
                    "available_slots": max_s - filled, "listed_players": []}
    except Exception:
        pass

    return {"max_slots": 4, "filled_slots": 0, "available_slots": 4, "listed_players": []}


def _open_add_player_modal(page: Page) -> bool:
    """Abre el modal de selección para el siguiente slot vacío."""
    try:
        opened = page.evaluate("""
            () => {
                const root = ko.contextFor(document.body)?.$root;
                if (root && typeof root.showSelectSellPlayerModal === 'function') {
                    root.showSelectSellPlayerModal();
                    return 'ko';
                }
                // DOM fallback: clic en el botón "Choose" del primer slot vacío
                const empty = Array.from(document.querySelectorAll('.sell-player-slot-slide'))
                    .find(li => li.querySelector('.empty-info-container'));
                if (empty) {
                    const btn = empty.querySelector('button.btn-new');
                    if (btn) { btn.click(); return 'dom'; }
                }
                return null;
            }
        """)
        if opened:
            print(f"  → Modal de selección abierto ({opened})")
            time.sleep(2)
            return True
        print("  ⚠️ No hay slots vacíos o no se pudo abrir el modal")
        return False
    except Exception as e:
        print(f"  ⚠️ _open_add_player_modal: {e}")
        return False


def _select_candidate_in_modal(page: Page, candidates: list[str],
                                already_listed: set[str]) -> str | None:
    """
    En el modal abierto, selecciona el primer candidato de la lista que no esté
    ya en la lista de transferibles y no tenga clase 'disabled'.
    Retorna el nombre del jugador seleccionado o None si ninguno pudo ser añadido.
    """
    # Esperar a que el modal esté visible
    modal_sel = "#modal-selectlineupplayer-body, .modal.in .modal-body"
    try:
        page.wait_for_selector(modal_sel, timeout=6000, state="visible")
    except Exception:
        print("  ⚠️ Modal de selección no apareció")
        return None

    for candidate in candidates:
        if candidate.lower() in already_listed:
            continue
        try:
            selected = page.evaluate(f"""
                (function() {{
                    const target = {json.dumps(candidate.lower())};
                    const modal  = document.querySelector('#modal-selectlineupplayer-body') ||
                                   document.querySelector('.modal.in .modal-body');
                    if (!modal) return null;

                    // Intentar via KO root.selectPlayer
                    try {{
                        const ctx  = ko.contextFor(modal);
                        const root = ctx && (ctx.$root || ctx.$data);
                        if (root && root.playersGroupablePartial) {{
                            const groupable = typeof root.playersGroupablePartial === 'function'
                                ? root.playersGroupablePartial() : root.playersGroupablePartial;
                            const getGroups = groupable.sortedPlayersListByLine || groupable.getPlayers;
                            const groups = typeof getGroups === 'function' ? getGroups.call(groupable) : [];
                            for (const group of groups) {{
                                const items = typeof group.players.getItems === 'function'
                                    ? group.players.getItems() : [];
                                for (const p of items) {{
                                    const n = (typeof p.name === 'function' ? p.name() : (p.name || '')).toLowerCase();
                                    const onList = typeof p.isOnTransferList === 'function' ? p.isOnTransferList() : false;
                                    if (!onList && (n === target || n.includes(target) || target.includes(n))) {{
                                        if (typeof root.selectPlayer === 'function') {{
                                            root.selectPlayer(p);
                                            return typeof p.name === 'function' ? p.name() : n;
                                        }}
                                    }}
                                }}
                            }}
                        }}
                    }} catch(e) {{}}

                    // DOM fallback: clic en la fila
                    const rows = modal.querySelectorAll('tr.player-table-row:not(.disabled)');
                    for (const row of rows) {{
                        const nameEl = row.querySelector('.select-player-name, span[data-bind*="text: name"]');
                        if (!nameEl) continue;
                        const n = nameEl.innerText.trim().toLowerCase();
                        if (n === target || n.includes(target) || target.includes(n)) {{
                            row.click();
                            return nameEl.innerText.trim();
                        }}
                    }}
                    return null;
                }})()
            """)
            if selected:
                print(f"  ✓ Jugador añadido a transferibles: {selected!r}")
                time.sleep(1.5)
                handle_popups(page)
                return selected
        except Exception as e:
            print(f"  ⚠️ _select_candidate_in_modal ({candidate}): {e}")

    # Ningún candidato pudo ser colocado
    print(f"  ⚠️ Ningún candidato disponible de la lista: {candidates}")
    try:
        page.keyboard.press("Escape")
        time.sleep(0.5)
    except Exception:
        pass
    return None


def fill_transferlist(page: Page, candidates: list[str]) -> dict:
    """
    Rellena los slots vacíos de la lista de transferibles con jugadores de 'candidates'.
    Respeta el máximo dinámico (4 normal, 6 en Transfer Madness) leído del KO.
    candidates: lista ordenada de nombres — se intenta en ese orden.

    Returns: { max_slots, filled_before, added: [str], skipped: [str], errors: [str] }
    """
    added   = []
    skipped = []
    errors  = []

    if not _navigate_to_transferlist(page):
        return {"max_slots": 4, "filled_before": 0, "added": added,
                "skipped": skipped, "errors": ["page_not_loaded"]}

    time.sleep(1)
    handle_popups(page)
    _activate_sell_tab(page)

    state         = get_transferlist_state(page)
    max_slots     = state["max_slots"]
    filled_before = state["filled_slots"]
    slots_to_fill = state["available_slots"]

    print(f"  [transferlist] Estado: {filled_before}/{max_slots}, {slots_to_fill} slot(s) vacío(s)")

    if slots_to_fill <= 0:
        print("  [transferlist] Lista completa — nada que hacer")
        return {"max_slots": max_slots, "filled_before": filled_before,
                "added": added, "skipped": skipped, "errors": errors}

    listed_names = {p["name"].lower() for p in state["listed_players"]}

    for _ in range(slots_to_fill):
        if not _open_add_player_modal(page):
            errors.append("modal_failed")
            break
        selected = _select_candidate_in_modal(page, candidates, listed_names)
        if selected:
            added.append(selected)
            listed_names.add(selected.lower())
        else:
            skipped_candidates = [c for c in candidates if c.lower() not in listed_names]
            skipped.extend(skipped_candidates)
            errors.append("pool_exhausted")
            break
        time.sleep(1)

    print(f"  [transferlist] Añadidos: {added}")
    return {"max_slots": max_slots, "filled_before": filled_before,
            "added": added, "skipped": skipped, "errors": errors}


def fill_transferlist_for_slot(
    page: Page,
    league_name: str,
    candidates: list[str],
    career_url: str = "https://en.onlinesoccermanager.com/Career",
) -> dict:
    """Activa el slot de la liga indicada y rellena la lista de transferibles."""
    page.goto(career_url, wait_until="domcontentloaded", timeout=30000)
    time.sleep(2)
    wait_for_visible_slots(page, timeout=20000)
    time.sleep(1)
    handle_popups(page)

    slots = page.locator(".career-teamslot")
    target_idx = None
    for i in range(slots.count()):
        _, t_league, _ = get_slot_info(slots.nth(i))
        if t_league and league_name.lower() in t_league.lower():
            target_idx = i
            print(f"  ✓ Slot encontrado: DOM {i} → '{t_league}'")
            break

    if target_idx is None:
        return {"max_slots": 4, "filled_before": 0, "added": [], "skipped": [],
                "errors": [f"slot_not_found:{league_name}"]}

    if not click_slot_and_wait_for_dashboard(page, target_idx):
        return {"max_slots": 4, "filled_before": 0, "added": [], "skipped": [],
                "errors": ["slot_activation_failed"]}

    return fill_transferlist(page, candidates)

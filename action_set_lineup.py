# action_set_lineup.py
"""
Cambia la formación de un equipo en OSM via Playwright.
Navega a /Lineup, abre el modal de formaciones y selecciona la deseada.

Flujo OSM:
  1. Clic en .lineup-view-switch-container  →  abre #modal-dialog-formations
  2. El modal contiene .formation-cell.clickable por cada formación
  3. Cada celda llama a $parents[1].setFormation($data) via KO
"""
import json
import time
from playwright.sync_api import Page
from utils import handle_popups

LINEUP_URL = "https://en.onlinesoccermanager.com/Lineup"

VALID_FORMATIONS = [
    "4-3-3 A", "4-3-3 B", "4-5-1", "4-2-3-1",
    "4-4-2 A", "4-4-2 B", "3-2-5", "3-2-3-2",
    "3-3-4 A", "3-3-4 B", "3-4-3 A", "3-4-3 B",
    "3-3-2-2", "3-5-2", "4-2-4 A", "4-2-4 B",
    "5-2-3 A", "5-2-3 B", "5-3-2", "5-3-1-1",
    "5-4-1 A", "5-4-1 B", "6-3-1 A", "6-3-1 B",
]


def _lineup_loaded(page: Page, timeout: int = 12000) -> bool:
    """Devuelve True cuando el botón de cambio de formación es visible."""
    try:
        page.wait_for_selector(".lineup-view-switch-container", timeout=timeout, state="visible")
        return True
    except Exception:
        pass
    # Fallback: cualquier elemento de la página de lineup
    for sel in [".lineup-container", "#page-lineup", "[data-bind*='selectedFormation']",
                "[data-bind*='formationsPartial']"]:
        try:
            page.wait_for_selector(sel, timeout=2000, state="attached")
            return True
        except Exception:
            pass
    return False


def _navigate_to_lineup(page: Page) -> bool:
    for sel in ["a[href='/Lineup']", "a[href*='/Lineup']", "a:has-text('Lineup')",
                "a:has-text('Line-up')", ".nav-lineup a"]:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=800):
                loc.click()
                time.sleep(2)
                if _lineup_loaded(page):
                    print(f"  ✓ Lineup cargado vía SPA ({sel})")
                    return True
        except Exception:
            pass

    print("  → Fallback: page.goto(/Lineup)...")
    try:
        page.goto(LINEUP_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)
        handle_popups(page)
        if _lineup_loaded(page):
            return True
    except Exception as e:
        print(f"  ⚠️ page.goto() falló: {e}")
    return False


def _set_formation_via_ko(page: Page, formation: str) -> bool:
    """
    Llama a vm.setFormation(item) directamente vía KO, sin abrir el modal.
    Encuentra el item en formationsPartial().getItems() donde description === formation.
    """
    try:
        result = page.evaluate(f"""
            (function() {{
                const target = {json.dumps(formation)};

                // El botón de cambio de formación es el mejor punto de entrada al VM
                const sourceEls = [
                    document.querySelector('.lineup-view-switch-container'),
                    document.querySelector('[data-bind*="selectFormation"]'),
                    document.querySelector('[data-bind*="selectedFormation"]'),
                    document.querySelector('[data-bind*="formationsPartial"]'),
                    document.body,
                ].filter(Boolean);

                function getRoots(el) {{
                    try {{
                        const ctx = ko.contextFor(el);
                        if (!ctx) return [];
                        return [ctx.$root, ctx.$data, ctx.$parent].filter(v => v && typeof v === 'object');
                    }} catch(e) {{ return []; }}
                }}

                const seen = new WeakSet();
                for (const el of sourceEls) {{
                    for (const vm of getRoots(el)) {{
                        if (seen.has(vm)) continue;
                        seen.add(vm);

                        // Buscar formationsPartial y su lista de items
                        try {{
                            const fp = typeof vm.formationsPartial === 'function'
                                ? vm.formationsPartial()
                                : vm.formationsPartial;
                            if (fp && typeof fp.getItems === 'function') {{
                                const items = fp.getItems();
                                const item = items.find(it =>
                                    (it.description || '').toLowerCase() === target.toLowerCase()
                                );
                                if (item && typeof vm.setFormation === 'function') {{
                                    vm.setFormation(item);
                                    return {{ method: 'setFormation', item: item.description }};
                                }}
                            }}
                        }} catch(e) {{}}

                        // Fallback: setFormation con objeto {{ description }}
                        if (typeof vm.setFormation === 'function') {{
                            vm.setFormation({{ description: target, id: null }});
                            return {{ method: 'setFormation_fallback' }};
                        }}
                    }}
                }}
                return null;
            }})()
        """)
        if result:
            print(f"  ✓ KO setFormation: {formation!r}  ({result})")
            return True
    except Exception as e:
        print(f"  ⚠️ KO setFormation error: {e}")
    return False


def _set_formation_via_modal(page: Page, formation: str) -> bool:
    """
    Abre el modal de formaciones haciendo clic en .lineup-view-switch-container,
    luego clica el .formation-cell cuyo span muestra el texto de la formación.
    """
    # 1. Abrir el modal
    try:
        btn = page.locator(".lineup-view-switch-container").first
        if not btn.is_visible(timeout=3000):
            print("  ⚠️ Botón .lineup-view-switch-container no visible")
            return False
        btn.click()
        time.sleep(1)
        print("  → Modal de formaciones abierto")
    except Exception as e:
        print(f"  ⚠️ No se pudo abrir modal de formaciones: {e}")
        return False

    # 2. Esperar a que el modal sea visible
    try:
        page.wait_for_selector("#modal-dialog-formations", timeout=5000, state="visible")
    except Exception:
        # Puede que el modal no tenga ese ID en todas las versiones
        try:
            page.wait_for_selector(".formation-cell", timeout=4000, state="visible")
        except Exception:
            print("  ⚠️ Modal de formaciones no apareció")
            return False

    # 3. Clicar la celda con el texto correcto
    try:
        # Buscar span dentro de .formation-cell que tenga el texto exacto
        cell_sel = f".formation-cell:has(span:text-is('{formation}'))"
        cell = page.locator(cell_sel).first
        if cell.count() == 0:
            # Fallback: text-matches parcial (insensible a mayúsculas)
            cells = page.locator(".formation-cell")
            n = cells.count()
            for i in range(n):
                txt = cells.nth(i).locator("span").first.inner_text(timeout=500).strip()
                if txt.lower() == formation.lower():
                    cells.nth(i).click()
                    time.sleep(0.5)
                    print(f"  ✓ Modal: formación seleccionada = {txt!r}")
                    return True
            print(f"  ⚠️ No se encontró celda para '{formation}' ({n} celdas)")
            return False

        cell.click()
        time.sleep(0.5)
        print(f"  ✓ Modal: formación seleccionada = {formation!r}")
        return True

    except Exception as e:
        print(f"  ⚠️ Error clicando celda de formación: {e}")
        return False


def set_lineup(page: Page, formation: str) -> dict:
    """
    Cambia la formación del equipo activo.
    Returns dict: { "success": bool, "formation": str, "errors": list[str] }
    """
    if formation not in VALID_FORMATIONS:
        return {"success": False, "formation": formation, "errors": [f"invalid_formation:{formation}"]}

    errors = []
    try:
        handle_popups(page)

        if not _navigate_to_lineup(page):
            return {"success": False, "formation": formation, "errors": ["page_not_loaded"]}

        time.sleep(1)
        handle_popups(page)

        # Estrategia 1: KO directo (no abre modal)
        if _set_formation_via_ko(page, formation):
            time.sleep(1.5)
            return {"success": True, "formation": formation, "errors": []}

        # Estrategia 2: interacción con el modal
        if _set_formation_via_modal(page, formation):
            time.sleep(1.5)
            return {"success": True, "formation": formation, "errors": []}

        errors.append("formation_not_set")
        print(f"  ❌ No se pudo establecer la formación '{formation}'")

    except Exception as e:
        print(f"  ❌ Error en set_lineup: {e}")
        errors.append(str(e))

    return {"success": False, "formation": formation, "errors": errors}


def set_lineup_for_slot(
    page: Page,
    league_name: str,
    formation: str,
    career_url: str = "https://en.onlinesoccermanager.com/Career",
) -> dict:
    """
    Encuentra el slot correspondiente a league_name, lo activa y cambia la formación.
    """
    from utils import click_slot_and_wait_for_dashboard, wait_for_visible_slots, get_slot_info

    page.goto(career_url, wait_until="domcontentloaded", timeout=30000)
    wait_for_visible_slots(page, timeout=20000)
    time.sleep(1)
    handle_popups(page)

    slots = page.locator(".career-teamslot")
    count = slots.count()
    target_idx = None
    for i in range(count):
        _, slot_league = get_slot_info(slots.nth(i))
        if slot_league and league_name.lower() in slot_league.lower():
            target_idx = i
            print(f"  ✓ Slot encontrado: índice DOM {i} → '{slot_league}'")
            break

    if target_idx is None:
        print(f"  ❌ No se encontró slot para liga '{league_name}' (slots={count})")
        return {"success": False, "formation": formation, "errors": [f"slot_not_found:{league_name}"]}

    if not click_slot_and_wait_for_dashboard(page, target_idx):
        return {"success": False, "formation": formation, "errors": ["slot_activation_failed"]}

    return set_lineup(page, formation)

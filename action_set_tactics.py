# action_set_tactics.py
"""
Cambia las tácticas de un equipo en OSM via Playwright.
Navega a /Tactics, modifica los carousels y sliders, y guarda.
"""
import json
import time
from playwright.sync_api import Page
from utils import handle_popups

TACTICS_URL = "https://en.onlinesoccermanager.com/Tactics"

# Valores válidos para cada carousel (texto visible en OSM)
VALID_GAME_PLANS  = ["Normal", "Attacking", "Defensive", "Counter", "Long Ball", "Possession"]
VALID_TACKLING    = ["Easy", "Normal", "Hard", "Aggressive"]
VALID_MARKING     = ["Zonal", "Man-to-man"]
VALID_FORMATIONS  = [
    "4-4-2", "4-3-3", "4-5-1", "3-5-2", "5-3-2", "4-2-3-1",
    "4-1-4-1", "3-4-3", "5-4-1", "4-4-1-1", "3-6-1",
]
VALID_FWD_TACTICS = ["Normal", "Pressing", "Shadow", "Creative", "Target Man"]
VALID_MID_TACTICS = ["Normal", "Pressing", "Box to Box", "Wide", "Narrow"]
VALID_DEF_TACTICS = ["Normal", "Pressing", "Man Marking", "Offside", "Low Block"]


# ── HELPERS ───────────────────────────────────────────────────────────────────

def _read_carousel(page: Page, row_sel: str) -> str:
    """Lee el valor actualmente mostrado en una fila de carousel."""
    # OSM muestra el valor activo en diferentes estructuras según la versión
    for val_sel in [
        f"{row_sel} .carousel-item.active span",
        f"{row_sel} .carousel-value",
        f"{row_sel} span[data-bind*='text']",
        f"{row_sel} .selected-value",
        f"{row_sel} strong",
        f"{row_sel} h4",
    ]:
        try:
            loc = page.locator(val_sel).first
            if loc.count() > 0 and loc.is_visible(timeout=500):
                t = loc.inner_text().strip()
                if t:
                    return t
        except Exception:
            pass
    # Fallback: texto completo del contenedor limpio de timestamps/números de flechas
    try:
        raw = page.locator(row_sel).first.inner_text().strip()
        # Quitar símbolos de flecha y saltos de línea
        lines = [l.strip() for l in raw.split("\n") if l.strip() and l.strip() not in ("◄", "►", "<", ">", "‹", "›")]
        if lines:
            return lines[0]
    except Exception:
        pass
    return ""


def _set_carousel(page: Page, row_sel: str, target: str, max_steps: int = 25) -> bool:
    """Navega un carousel hasta mostrar 'target'. Prueba en ambas direcciones."""
    current = _read_carousel(page, row_sel)
    if current.lower() == target.lower():
        return True

    # Selectores de flechas (OSM usa varias convenciones)
    next_sels = [
        f"{row_sel} .carousel-next",
        f"{row_sel} .next",
        f"{row_sel} [data-dir='next']",
        f"{row_sel} a:has-text('›')",
        f"{row_sel} a:has-text('►')",
        f"{row_sel} .icon-chevron-right",
        f"{row_sel} button.next",
    ]
    prev_sels = [
        f"{row_sel} .carousel-prev",
        f"{row_sel} .prev",
        f"{row_sel} [data-dir='prev']",
        f"{row_sel} a:has-text('‹')",
        f"{row_sel} a:has-text('◄')",
        f"{row_sel} .icon-chevron-left",
        f"{row_sel} button.prev",
    ]

    def _first_visible(sels: list) -> str | None:
        for s in sels:
            try:
                if page.locator(s).first.is_visible(timeout=300):
                    return s
            except Exception:
                pass
        return None

    next_sel = _first_visible(next_sels)
    if not next_sel:
        print(f"  ⚠️ No se encontró flecha next en: {row_sel}")
        return False

    for _ in range(max_steps):
        current = _read_carousel(page, row_sel)
        if current.lower() == target.lower():
            return True
        page.locator(next_sel).first.click()
        time.sleep(0.25)

    # Intentar con prev si next no llegó
    prev_sel = _first_visible(prev_sels)
    if prev_sel:
        for _ in range(max_steps):
            current = _read_carousel(page, row_sel)
            if current.lower() == target.lower():
                return True
            page.locator(prev_sel).first.click()
            time.sleep(0.25)

    print(f"  ⚠️ No se encontró '{target}' en carousel {row_sel}. Valor actual: {_read_carousel(page, row_sel)!r}")
    return False


def _set_slider(page: Page, slider_sel: str, value: int) -> bool:
    """Fija un slider numérico (0-100) vía JS."""
    value = max(0, min(100, int(value)))
    try:
        result = page.evaluate(f"""
            (function() {{
                const el = document.querySelector('{slider_sel}');
                if (!el) return false;
                const nativeInputSetter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value'
                ).set;
                nativeInputSetter.call(el, {value});
                el.dispatchEvent(new Event('input',  {{ bubbles: true }}));
                el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                return true;
            }})()
        """)
        if result:
            return True
    except Exception as e:
        print(f"  ⚠️ JS slider falló ({slider_sel}): {e}")

    # Fallback: fill() para inputs range
    try:
        loc = page.locator(slider_sel).first
        if loc.is_visible(timeout=500):
            loc.fill(str(value))
            return True
    except Exception:
        pass
    return False


def _dump_tactics_structure(page: Page):
    """Debug: imprime los data-bind de todos los elementos de la página de tácticas."""
    try:
        binds = page.evaluate("""
            () => Array.from(document.querySelectorAll('[data-bind]'))
                       .map(el => ({ tag: el.tagName, cls: el.className, bind: el.getAttribute('data-bind') }))
                       .filter(x => x.bind.length < 200)
        """)
        print("  [dump] data-bind elements en /Tactics:")
        for b in binds[:40]:
            print(f"    {b['tag']}.{b['cls'][:30]} → {b['bind'][:100]}")
    except Exception as e:
        print(f"  [dump] error: {e}")


# ── FUNCIÓN PRINCIPAL ─────────────────────────────────────────────────────────

def set_tactics(page: Page, **kwargs) -> dict:
    """
    Establece las tácticas del equipo en el slot indicado.

    Parámetros opcionales (solo se aplican los que se pasen):
        game_plan       str   Normal / Attacking / Defensive / Counter / Long Ball / Possession
        tackling        str   Easy / Normal / Hard / Aggressive
        pressure        int   0-100
        mentality       int   0-100
        tempo           int   0-100
        formation       str   4-4-2, 4-3-3, etc.
        forwards_tactic str
        midfielders_tactic str
        defenders_tactic   str
        offside_trap    bool
        marking         str   Zonal / Man-to-man

    Returns dict: { "success": bool, "changed": list[str], "errors": list[str] }
    """
    changed = []
    errors  = []

    try:
        handle_popups(page)
        page.goto(TACTICS_URL, wait_until="domcontentloaded", timeout=30000)

        # Esperar a que la página cargue los controles de tácticas
        try:
            page.wait_for_selector(
                "[data-bind*='gamePlan'], [data-bind*='gameplan'], .tactics-container, #tactics",
                timeout=15000,
            )
        except Exception:
            print("  ⚠️ Selector de tácticas no encontrado — dumpeando estructura...")
            _dump_tactics_structure(page)
            errors.append("page_not_loaded")
            return {"success": False, "changed": changed, "errors": errors}

        time.sleep(1)
        handle_popups(page)

        # ── Intentar vía Knockout.js viewmodel (más confiable) ────────────────
        ko_result = _try_set_via_ko(page, **kwargs)
        if ko_result["applied"]:
            changed.extend(ko_result["applied"])
            # Si KO aplicó todos los cambios, solo necesitamos guardar
            if not ko_result["failed"]:
                _click_save(page)
                return {"success": True, "changed": changed, "errors": []}
            # Si KO falló en algunos, intentar esos por UI
            kwargs_remaining = {k: v for k, v in kwargs.items() if k in ko_result["failed"]}
        else:
            kwargs_remaining = kwargs

        # ── Fallback: navegación por UI (carousels + sliders) ─────────────────
        ui_changed, ui_errors = _set_via_ui(page, **kwargs_remaining)
        changed.extend(ui_changed)
        errors.extend(ui_errors)

        if changed:
            _click_save(page)

    except Exception as e:
        print(f"  ❌ Error en set_tactics: {e}")
        errors.append(str(e))

    return {
        "success": len(errors) == 0 and len(changed) > 0,
        "changed": changed,
        "errors":  errors,
    }


def _try_set_via_ko(page: Page, **kwargs) -> dict:
    """
    Intenta modificar las tácticas directamente via Knockout.js observables.
    Devuelve {"applied": [...], "failed": [...]}
    """
    # Mapa de nuestros kwargs → posibles nombres de observable en KO
    KO_MAP = {
        "game_plan":           ["gamePlan", "gameplan", "GamePlan"],
        "tackling":            ["tackling", "Tackling"],
        "pressure":            ["pressure", "Pressure"],
        "mentality":           ["mentality", "Mentality"],
        "tempo":               ["tempo", "Tempo"],
        "formation":           ["formation", "Formation"],
        "forwards_tactic":     ["forwardsTactic", "forwardTactic", "attackingTactic"],
        "midfielders_tactic":  ["midfieldersTactic", "midfielderTactic"],
        "defenders_tactic":    ["defendersTactic", "defenderTactic"],
        "offside_trap":        ["offsideTrap", "offside"],
        "marking":             ["marking", "Marking"],
    }

    applied = []
    failed  = []

    for key, value in kwargs.items():
        if value is None:
            continue
        ko_names = KO_MAP.get(key, [])
        if not ko_names:
            failed.append(key)
            continue

        js_names = json.dumps(ko_names)
        js_value = json.dumps(value)
        ok = page.evaluate(f"""
            (function() {{
                const names = {js_names};
                const val   = {js_value};
                const candidates = [
                    document.querySelector('[data-bind*="gamePlan"]'),
                    document.querySelector('[data-bind*="gameplan"]'),
                    document.querySelector('.tactics-container'),
                    document.querySelector('#tactics'),
                    document.body,
                ].filter(Boolean);

                for (const el of candidates) {{
                    try {{
                        const ctx = ko.contextFor(el);
                        if (!ctx) continue;
                        const vm = ctx.$root || ctx.$data;
                        if (!vm) continue;
                        for (const name of names) {{
                            if (typeof vm[name] === 'function') {{
                                vm[name](val);
                                return true;
                            }}
                            for (const prop of Object.keys(vm)) {{
                                if (vm[prop] && typeof vm[prop] === 'object' && typeof vm[prop][name] === 'function') {{
                                    vm[prop][name](val);
                                    return true;
                                }}
                            }}
                        }}
                    }} catch(e) {{}}
                }}
                return false;
            }})()
        """)

        if ok:
            applied.append(key)
            print(f"  ✓ KO: {key} = {value!r}")
        else:
            failed.append(key)

    return {"applied": applied, "failed": failed}


def _set_via_ui(page: Page, **kwargs) -> tuple[list, list]:
    """
    Configura tácticas navegando los carousels y sliders de la UI.
    Necesita conocer los selectores reales de la página.
    """
    changed = []
    errors  = []

    # Intentar detectar la estructura de la página automáticamente
    structure = _detect_tactics_structure(page)

    CAROUSEL_FIELDS = {
        "game_plan":          structure.get("game_plan_sel"),
        "formation":          structure.get("formation_sel"),
        "tackling":           structure.get("tackling_sel"),
        "forwards_tactic":    structure.get("fwd_tactic_sel"),
        "midfielders_tactic": structure.get("mid_tactic_sel"),
        "defenders_tactic":   structure.get("def_tactic_sel"),
        "marking":            structure.get("marking_sel"),
    }
    SLIDER_FIELDS = {
        "pressure":  structure.get("pressure_slider"),
        "mentality": structure.get("mentality_slider"),
        "tempo":     structure.get("tempo_slider"),
    }

    for field, value in kwargs.items():
        if value is None:
            continue

        if field in CAROUSEL_FIELDS and CAROUSEL_FIELDS[field]:
            if _set_carousel(page, CAROUSEL_FIELDS[field], str(value)):
                changed.append(field)
            else:
                errors.append(f"carousel:{field}")

        elif field in SLIDER_FIELDS and SLIDER_FIELDS[field]:
            if _set_slider(page, SLIDER_FIELDS[field], int(value)):
                changed.append(field)
            else:
                errors.append(f"slider:{field}")

        elif field == "offside_trap":
            sel = structure.get("offside_sel")
            if sel and _toggle_offside(page, sel, bool(value)):
                changed.append(field)
            else:
                errors.append("offside_trap")

    return changed, errors


def _detect_tactics_structure(page: Page) -> dict:
    """
    Detecta los selectores reales de la página de tácticas inspeccionando el DOM.
    Devuelve un dict con los selectores encontrados.
    """
    result = {}

    # Estrategia: buscar elementos con data-bind que contengan las palabras clave
    binds = page.evaluate("""
        () => {
            const out = {};
            document.querySelectorAll('[data-bind]').forEach(el => {
                const b = el.getAttribute('data-bind').toLowerCase();
                const id = el.id || el.className.split(' ')[0] || el.tagName;
                if (b.includes('gameplan') || b.includes('game_plan')) out.game_plan = '#' + (el.id || id);
                if (b.includes('formation'))  out.formation  = '#' + (el.id || id);
                if (b.includes('tackling'))   out.tackling   = '#' + (el.id || id);
                if (b.includes('pressure') && b.includes('range')) out.pressure_slider = '#' + (el.id || id);
                if (b.includes('mentality') && b.includes('range')) out.mentality_slider = '#' + (el.id || id);
                if (b.includes('tempo') && b.includes('range')) out.tempo_slider = '#' + (el.id || id);
            });
            // Sliders por tipo
            document.querySelectorAll('input[type="range"]').forEach((el, i) => {
                const label = el.closest('div, li, tr')?.querySelector('label, span, h4')?.innerText?.toLowerCase() || '';
                const key = label.includes('pressure') ? 'pressure_slider'
                          : label.includes('mental')   ? 'mentality_slider'
                          : label.includes('tempo')    ? 'tempo_slider'
                          : null;
                if (key && el.id) out[key] = '#' + el.id;
                else if (key)     out[key] = `input[type="range"]:nth-of-type(${i+1})`;
            });
            return out;
        }
    """)
    result.update(binds or {})

    # Si no encontramos sliders por data-bind, intentar por posición
    if not result.get("pressure_slider"):
        sliders = page.locator('input[type="range"]')
        if sliders.count() >= 1:
            result["pressure_slider"]  = 'input[type="range"]:nth-of-type(1)'
        if sliders.count() >= 2:
            result["mentality_slider"] = 'input[type="range"]:nth-of-type(2)'
        if sliders.count() >= 3:
            result["tempo_slider"]     = 'input[type="range"]:nth-of-type(3)'

    return result


def _toggle_offside(page: Page, sel: str, enable: bool) -> bool:
    """Activa o desactiva el offside trap."""
    try:
        el = page.locator(sel).first
        is_checked = el.is_checked() if el.get_attribute("type") == "checkbox" else \
                     "active" in (el.get_attribute("class") or "")
        if is_checked != enable:
            el.click()
        return True
    except Exception as e:
        print(f"  ⚠️ offside toggle: {e}")
        return False


def _click_save(page: Page) -> bool:
    """Hace click en el botón de guardar tácticas."""
    save_sels = [
        "button:has-text('Save')",
        "button:has-text('Guardar')",
        ".btn-primary:has-text('Save')",
        ".save-tactics",
        "#save-tactics",
        "button[data-bind*='save']",
        "button[data-bind*='Save']",
    ]
    for sel in save_sels:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=1000):
                loc.click()
                time.sleep(1)
                handle_popups(page)
                print("  ✓ Tácticas guardadas.")
                return True
        except Exception:
            pass
    print("  ⚠️ Botón de guardar no encontrado. Puede que las tácticas se guarden automáticamente.")
    return False


# ── WRAPPER PARA SER LLAMADO DESDE EL BOT ─────────────────────────────────────

def set_tactics_for_slot(
    page: Page,
    slot_index: int,
    career_url: str = "https://en.onlinesoccermanager.com/Career",
    **kwargs,
) -> dict:
    """
    Activa el slot indicado, navega a /Tactics y aplica los cambios.
    Llama a click_slot_and_wait_for_dashboard antes de set_tactics.
    """
    from utils import click_slot_and_wait_for_dashboard, wait_for_visible_slots

    # Navegar a Career y activar el slot
    page.goto(career_url, wait_until="domcontentloaded", timeout=30000)
    wait_for_visible_slots(page, timeout=20000)
    time.sleep(1)
    handle_popups(page)

    if not click_slot_and_wait_for_dashboard(page, slot_index):
        return {"success": False, "changed": [], "errors": ["slot_activation_failed"]}

    return set_tactics(page, **kwargs)

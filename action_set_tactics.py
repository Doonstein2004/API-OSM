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

# Valores válidos para cada campo (deben coincidir con el texto visible en OSM)
VALID_GAME_PLANS  = ["Shoot on sight", "Long ball", "Counter-attack", "Wing play", "Passing game"]
VALID_TACKLING    = ["Careful", "Normal", "Reckless", "Aggressive"]
VALID_MARKING     = ["Zonal marking", "Man marking"]
VALID_FWD_TACTICS = ["Attack only", "Support midfield", "Drop deep"]
VALID_MID_TACTICS = ["Protect the defence", "Push forward", "Stay in position"]
VALID_DEF_TACTICS = ["Defend deep", "Attacking full-backs", "Support midfield"]


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
    """Debug: muestra URL, sliders, clases tácticas y data-binds relevantes."""
    try:
        result = page.evaluate("""
            () => {
                const out = { url: window.location.href, binds: [], classes: [], rangeInputs: [] };
                document.querySelectorAll('[data-bind]').forEach(el => {
                    const b = el.getAttribute('data-bind');
                    if (b.length < 200)
                        out.binds.push({ tag: el.tagName, cls: el.className.substring(0,30), bind: b.substring(0,100) });
                });
                ['tactic','gameplan','game-plan','pressure','tackling','tempo','slider','offside','marking'].forEach(kw => {
                    document.querySelectorAll('[class*="'+kw+'"], [id*="'+kw+'"]').forEach(el => {
                        out.classes.push({ tag: el.tagName, id: el.id, cls: el.className.substring(0,60) });
                    });
                });
                document.querySelectorAll('input[type="range"]').forEach(el => {
                    out.rangeInputs.push({ id: el.id, cls: el.className, name: el.name,
                                           bind: (el.getAttribute('data-bind') || '').substring(0,80) });
                });
                return out;
            }
        """)
        print(f"  [dump] URL actual: {result['url']}")
        print(f"  [dump] input[range] encontrados: {result['rangeInputs']}")
        print(f"  [dump] Clases táctica/slider: {result['classes'][:10]}")
        print(f"  [dump] data-bind elements ({len(result['binds'])}):")
        for b in result['binds'][:50]:
            print(f"    {b['tag']}.{b['cls']} → {b['bind']}")
    except Exception as e:
        print(f"  [dump] error: {e}")


def _navigate_to_tactics(page: Page) -> bool:
    """
    Navega a la página de tácticas preservando el contexto de equipo.
    Intenta SPA (click en link de nav) primero; usa page.goto() como fallback.
    """
    # ── 1. SPA navigation via link ────────────────────────────────────────────
    nav_sels = [
        "a[href='/Tactics']",
        "a[href*='/Tactics']",
        "a[href*='tactics']",
        "a:has-text('Tactics')",
        "a:has-text('Tácticas')",
        ".nav-tactics a",
        "[data-target*='Tactics']",
    ]
    for sel in nav_sels:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=800):
                loc.click()
                time.sleep(2)
                if _tactics_loaded(page, timeout=10000):
                    print(f"  ✓ Tácticas cargadas vía SPA ({sel})")
                    return True
        except Exception:
            pass

    # ── 2. Fallback: page.goto() + espera extra ───────────────────────────────
    print("  → Fallback: page.goto(/Tactics)...")
    try:
        page.goto(TACTICS_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)
        handle_popups(page)
        if _tactics_loaded(page, timeout=15000):
            return True
    except Exception as e:
        print(f"  ⚠️ page.goto() falló: {e}")

    return False


def _tactics_loaded(page: Page, timeout: int = 10000) -> bool:
    """Devuelve True si el DOM contiene contenido específico de la página de tácticas."""
    selectors = [
        "[data-bind*='gamePlan']",
        "[data-bind*='gameplan']",
        "[data-bind*='tackling']",
        "[data-bind*='pressure']",
        "[data-bind*='offside']",
        ".tactics-container",
        ".tactic-selector",
        "#page-tactics",
        "input[type='range']",
    ]
    per = max(300, timeout // len(selectors))
    for sel in selectors:
        try:
            page.wait_for_selector(sel, timeout=per, state="visible")
            print(f"  ✓ Tácticas detectadas: {sel}")
            return True
        except Exception:
            pass
    return False


# ── FUNCIÓN PRINCIPAL ─────────────────────────────────────────────────────────

def set_tactics(page: Page, **kwargs) -> dict:
    """
    Establece las tácticas del equipo activo.

    Kwargs opcionales:
        game_plan, tackling, pressure (0-100), mentality (0-100), tempo (0-100),
        forwards_tactic, midfielders_tactic, defenders_tactic,
        offside_trap (bool), marking

    Returns dict: { "success": bool, "changed": list[str], "errors": list[str] }
    """
    changed = []
    errors  = []

    try:
        handle_popups(page)

        if not _navigate_to_tactics(page):
            print("  ⚠️ Página de tácticas no cargó — dumpeando DOM...")
            _dump_tactics_structure(page)
            errors.append("page_not_loaded")
            return {"success": False, "changed": changed, "errors": errors}

        time.sleep(1)
        handle_popups(page)
        _dump_ko_observables(page)

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


def _dump_ko_observables(page: Page):
    """Debug: imprime todos los observables KO del viewmodel de tácticas con sus valores actuales."""
    try:
        obs = page.evaluate("""
            () => {
                const candidates = [
                    document.querySelector('[data-bind*="offside"]'),
                    document.querySelector('[data-bind*="gamePlan"]'),
                    document.querySelector('[data-bind*="gameplan"]'),
                    document.querySelector('[data-bind*="tackling"]'),
                    document.querySelector('[data-bind*="pressure"]'),
                    document.body,
                ].filter(Boolean);
                for (const el of candidates) {
                    try {
                        const ctx = ko.contextFor(el);
                        if (!ctx) continue;
                        const vm = ctx.$root || ctx.$data;
                        if (!vm) continue;
                        const out = {};
                        for (const key of Object.keys(vm)) {
                            try {
                                const v = vm[key];
                                if (typeof v === 'function') out[key] = v();
                            } catch(e) {}
                        }
                        return out;
                    } catch(e) {}
                }
                return null;
            }
        """)
        if obs:
            print("  [KO observables en viewmodel de tácticas]:")
            for k, v in sorted(obs.items()):
                print(f"    {k} = {v!r}")
        else:
            print("  [KO] No se encontró viewmodel accesible")
    except Exception as e:
        print(f"  [KO dump] error: {e}")


def _try_set_via_ko(page: Page, **kwargs) -> dict:
    """
    Intenta modificar las tácticas directamente via Knockout.js observables.
    Devuelve {"applied": [...], "failed": [...]}
    """
    KO_MAP = {
        "game_plan":           ["gamePlan", "gameplan", "GamePlan", "game_plan",
                                 "gameplanId", "gamePlanId"],
        "tackling":            ["tackling", "Tackling", "tacklingStyle", "tacklingId"],
        "pressure":            ["pressure", "Pressure", "pressingRate", "pressPressure"],
        "mentality":           ["mentality", "Mentality", "teamMentality"],
        "tempo":               ["tempo", "Tempo", "playingTempo"],
        "forwards_tactic":     ["forwardsTactic", "forwardTactic", "attackingTactic",
                                 "forwardPlay", "forwardsPlay", "forwardsTacticId"],
        "midfielders_tactic":  ["midfieldersTactic", "midfielderTactic", "midfieldPlay",
                                 "midfielderPlay", "midfieldersTacticId"],
        "defenders_tactic":    ["defendersTactic", "defenderTactic", "defensivePlay",
                                 "defenderPlay", "defendersTacticId"],
        "offside_trap":        ["offsideTrap", "offside", "offsideEnabled",
                                 "offsideTrapEnabled", "isOffsideTrap"],
        "marking":             ["marking", "Marking", "markingStyle", "markingId"],
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
                const names   = {js_names};
                const val     = {js_value};
                // Todos los elementos con data-bind conocidos + fallbacks genéricos
                const candidates = [
                    document.querySelector('[data-bind*="offside"]'),
                    document.querySelector('[data-bind*="gamePlan"]'),
                    document.querySelector('[data-bind*="gameplan"]'),
                    document.querySelector('[data-bind*="tackling"]'),
                    document.querySelector('[data-bind*="pressure"]'),
                    document.querySelector('[data-bind*="tempo"]'),
                    document.querySelector('[data-bind*="mentality"]'),
                    document.querySelector('[data-bind*="marking"]'),
                    document.querySelector('[data-bind*="forwardsTactic"]'),
                    document.querySelector('[data-bind*="defendersTactic"]'),
                    document.querySelector('[data-bind*="midfieldersTactic"]'),
                    document.querySelector('.tactics-container'),
                    document.querySelector('[class*="tactic"]'),
                    document.querySelector('#tactics'),
                    document.body,
                ].filter(Boolean);

                // Extrae el viewmodel KO de un elemento
                function getVm(el) {{
                    try {{
                        const ctx = ko.contextFor(el);
                        if (!ctx) return null;
                        return ctx.$root || ctx.$data || null;
                    }} catch(e) {{ return null; }}
                }}

                // Intenta setear el valor en un viewmodel dado
                function trySet(vm) {{
                    if (!vm) return false;
                    // 1. Coincidencia exacta
                    for (const name of names) {{
                        if (typeof vm[name] === 'function') {{
                            vm[name](val);
                            return true;
                        }}
                    }}
                    // 2. Coincidencia case-insensitive
                    const lower = names.map(n => n.toLowerCase());
                    for (const prop of Object.keys(vm)) {{
                        if (lower.includes(prop.toLowerCase()) && typeof vm[prop] === 'function') {{
                            vm[prop](val);
                            return true;
                        }}
                    }}
                    // 3. Un nivel de profundidad (sub-objetos)
                    for (const prop of Object.keys(vm)) {{
                        const sub = vm[prop];
                        if (!sub || typeof sub !== 'object' || Array.isArray(sub)) continue;
                        for (const name of names) {{
                            if (typeof sub[name] === 'function') {{
                                sub[name](val);
                                return true;
                            }}
                        }}
                    }}
                    return false;
                }}

                const seen = new Set();
                for (const el of candidates) {{
                    const vm = getVm(el);
                    if (!vm || seen.has(vm)) continue;
                    seen.add(vm);
                    if (trySet(vm)) return true;
                    // Intentar también con $parent y $parents
                    try {{
                        const ctx = ko.contextFor(el);
                        if (ctx.$parent && !seen.has(ctx.$parent)) {{
                            seen.add(ctx.$parent);
                            if (trySet(ctx.$parent)) return true;
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
            print(f"  ⚠️ KO falló: {key}")

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
            if _toggle_offside_direct(page, bool(value)):
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
    """Activa o desactiva el offside trap dado un selector explícito."""
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


def _toggle_offside_direct(page: Page, enable: bool) -> bool:
    """
    Activa/desactiva el offside trap sin depender de _detect_tactics_structure.
    Busca [data-bind*='offside'] directamente y prueba KO → checkbox → click.
    """
    # 1. Via KO observable sobre el elemento offside
    try:
        ok = page.evaluate(f"""
            (function() {{
                const el = document.querySelector('[data-bind*="offside"]');
                if (!el) return false;
                try {{
                    const ctx = ko.contextFor(el);
                    if (ctx) {{
                        const vm = ctx.$root || ctx.$data;
                        if (vm) {{
                            const names = ['offsideTrap', 'offside', 'offsideEnabled', 'useOffside',
                                           'offsideTrapEnabled', 'isOffsideTrap'];
                            for (const n of names) {{
                                if (typeof vm[n] === 'function') {{
                                    vm[n]({json.dumps(enable)});
                                    return true;
                                }}
                            }}
                            // Buscar un nivel más profundo
                            for (const prop of Object.keys(vm)) {{
                                const sub = vm[prop];
                                if (sub && typeof sub === 'object') {{
                                    for (const n of names) {{
                                        if (typeof sub[n] === 'function') {{
                                            sub[n]({json.dumps(enable)});
                                            return true;
                                        }}
                                    }}
                                }}
                            }}
                        }}
                    }}
                }} catch(e) {{}}
                return false;
            }})()
        """)
        if ok:
            print(f"  ✓ KO: offside_trap = {enable}")
            return True
    except Exception as e:
        print(f"  ⚠️ KO offside: {e}")

    # 2. Interacción directa: checkbox o toggle/button
    offside_sels = [
        "input[type='checkbox'][data-bind*='offside']",
        "[data-bind*='offside']",
        "input[data-bind*='offside']",
        ".offside-trap",
        "#offside-trap",
        "[class*='offside']",
    ]
    for sel in offside_sels:
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            if not loc.is_visible(timeout=500):
                continue
            tag       = loc.evaluate("el => el.tagName.toLowerCase()")
            attr_type = loc.get_attribute("type") or ""
            if tag == "input" and attr_type == "checkbox":
                if loc.is_checked() != enable:
                    loc.click()
                print(f"  ✓ checkbox: offside_trap = {enable} ({sel})")
                return True
            else:
                cls       = loc.get_attribute("class") or ""
                is_active = any(w in cls for w in ("active", "checked", "on", "enabled"))
                if is_active != enable:
                    loc.click()
                    time.sleep(0.3)
                print(f"  ✓ toggle: offside_trap = {enable} ({sel})")
                return True
        except Exception as e:
            print(f"  ⚠️ offside ({sel}): {e}")

    print("  ❌ No se encontró elemento offside_trap en el DOM")
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
    print("  ✓ Tácticas guardadas automáticamente (OSM no requiere botón de guardar).")
    return True


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

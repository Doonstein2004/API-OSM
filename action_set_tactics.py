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

# Valores válidos para cada campo (texto visible en OSM)
VALID_GAME_PLANS  = ["Shoot on sight", "Long ball", "Counter-attack", "Wing play", "Passing game"]
VALID_TACKLING    = ["Careful", "Normal", "Reckless", "Aggressive"]
VALID_MARKING     = ["Zonal marking", "Man marking"]
VALID_FWD_TACTICS = ["Attack only", "Support midfield", "Drop deep"]
VALID_MID_TACTICS = ["Protect the defence", "Push forward", "Stay in position"]
VALID_DEF_TACTICS = ["Defend deep", "Attacking full-backs", "Support midfield"]

# Conversión de texto Discord → índice entero del carousel de OSM
# Índices confirmados vía data-enumtranslationN en el HTML de /Tactics
_TO_OSM_VALUE: dict[str, dict] = {
    "game_plan": {               # #carousel-tacticoverall
        "long ball":      0,
        "passing game":   1,
        "wing play":      2,
        "counter-attack": 3,
        "shoot on sight": 4,
    },
    "tackling": {                # #carousel-tacticstyleofplay
        "careful":    0,
        "normal":     1,
        "aggressive": 2,
        "reckless":   3,
    },
    "forwards_tactic": {         # #carousel-tacticlineatt
        "drop deep":        0,
        "support midfield": 1,
        "attack only":      2,
    },
    "midfielders_tactic": {      # #carousel-tacticlinemid
        "protect the defence": 0,
        "stay in position":    1,
        "push forward":        2,
    },
    "defenders_tactic": {        # #carousel-tacticlinedef
        "defend deep":          0,
        "attacking full-backs": 1,
        "support midfield":     2,
    },
    "marking": {                 # #carousel-tacticmarking
        "zonal marking": 0,
        "man marking":   1,
    },
    "offside_trap": {            # #carousel-tacticoffsidetrap
        False: 0, True: 1,
        "no": 0, "yes": 1, "false": 0, "true": 1,
    },
}

# Propiedad del viewmodel KO para cada campo — cada una es un objeto con .sliderValue()
_KO_TACTIC_PROP = {
    "game_plan":          "tacticOverall",
    "tackling":           "tacticStyleOfPlay",
    "pressure":           "tacticPressure",
    "mentality":          "tacticMentality",
    "tempo":              "tacticTempo",
    "forwards_tactic":    "tacticLineAtt",
    "midfielders_tactic": "tacticLineMid",
    "defenders_tactic":   "tacticLineDef",
    "offside_trap":       "tacticOffsideTrap",
    "marking":            "tacticMarking",
}

# IDs exactos de los carousels en el DOM de /Tactics
_CAROUSEL_ID = {
    "game_plan":          "carousel-tacticoverall",
    "tackling":           "carousel-tacticstyleofplay",
    "forwards_tactic":    "carousel-tacticlineatt",
    "midfielders_tactic": "carousel-tacticlinemid",
    "defenders_tactic":   "carousel-tacticlinedef",
    "offside_trap":       "carousel-tacticoffsidetrap",
    "marking":            "carousel-tacticmarking",
}

# Alias heredado — usado sólo en _try_set_via_ko capa 2 (objeto tactic de nextRoundPartial)
_KO_FIELD_MAP = {
    "game_plan":          "style",
    "tackling":           "overallMatchTactics",
    "pressure":           "pressing",
    "mentality":          "mentality",
    "tempo":              "tempo",
    "forwards_tactic":    "attack",
    "midfielders_tactic": "midfield",
    "defenders_tactic":   "defense",
    "offside_trap":       "offsideTrap",
    "marking":            "marking",
}


def _to_osm(field: str, value) -> tuple:
    """
    Convierte un valor de Discord al índice entero del carousel de OSM.
    Devuelve (osm_int_or_num, original_string) — el primer elemento es lo que
    se pasa al KO observable (.sliderValue) o al input de slider.
    """
    mapping = _TO_OSM_VALUE.get(field)
    if mapping is not None:
        # Para offside_trap que acepta bool directamente
        key = value if isinstance(value, bool) else str(value).lower().strip()
        converted = mapping.get(key)
        if converted is not None:
            return converted, value
    # Numérico (pressure/mentality/tempo) — devolver tal cual
    return value, value


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

            # Verificar que los valores quedaron escritos correctamente
            time.sleep(0.5)
            verify = _verify_ko_state(page, kwargs)
            save_methods = verify.get("save_methods", [])

            # Si KO aplicó todos los cambios, forzar save y retornar
            if not ko_result["failed"]:
                _force_save(page, vm_save_methods=save_methods)
                time.sleep(2)  # esperar que el auto-save debounce dispare
                return {"success": True, "changed": changed, "errors": []}

            # Si KO falló en algunos, intentar esos por UI
            kwargs_remaining = {k: v for k, v in kwargs.items() if k in ko_result["failed"]}
        else:
            kwargs_remaining = kwargs
            save_methods = []

        # ── Fallback: navegación por UI (carousels + sliders) ─────────────────
        ui_changed, ui_errors = _set_via_ui(page, **kwargs_remaining)
        changed.extend(ui_changed)
        errors.extend(ui_errors)

        if changed:
            _force_save(page, vm_save_methods=save_methods)
            time.sleep(2)

    except Exception as e:
        print(f"  ❌ Error en set_tactics: {e}")
        errors.append(str(e))

    return {
        "success": len(errors) == 0 and len(changed) > 0,
        "changed": changed,
        "errors":  errors,
    }


def _dump_ko_observables(page: Page):
    """Debug: muestra el objeto tactic del equipo propio + elementos interactivos de la página."""
    try:
        data = page.evaluate("""
            () => {
                const out = { tactic: null, rangeInputs: [], carouselBtns: [], tacticText: [] };

                // 1. Extraer objeto tactic del nextRoundPartial
                try {
                    const el = document.querySelector('[data-bind*="offside"]') || document.body;
                    const ctx = ko.contextFor(el);
                    const vm  = ctx && (ctx.$root || ctx.$data);
                    if (vm && typeof vm.nextRoundPartial === 'function') {
                        const nrp = vm.nextRoundPartial();
                        if (nrp && nrp.match) {
                            const teams = [nrp.match.homeTeam, nrp.match.awayTeam].filter(Boolean);
                            for (const team of teams) {
                                if (team.tactic) {
                                    // Serializar: distinguir observables de valores planos
                                    const t = team.tactic;
                                    const serialized = {};
                                    for (const k of Object.keys(t)) {
                                        try { serialized[k] = typeof t[k] === 'function' ? { obs: true, val: t[k]() } : { obs: false, val: t[k] }; }
                                        catch(e) { serialized[k] = { obs: false, val: '?' }; }
                                    }
                                    out.tactic = { teamId: team.id, name: team.name, fields: serialized };
                                    break;
                                }
                            }
                        }
                    }
                } catch(e) {}

                // 2. Range inputs (sliders)
                document.querySelectorAll('input[type="range"]').forEach(el => {
                    out.rangeInputs.push({
                        id: el.id, name: el.name, value: el.value,
                        bind: (el.getAttribute('data-bind') || '').substring(0, 80),
                        label: el.closest('div,li,tr')?.querySelector('label,span,h4,strong')?.innerText?.trim()?.substring(0, 30) || ''
                    });
                });

                // 3. Carousel prev/next buttons
                document.querySelectorAll('a, button, .clickable').forEach(el => {
                    if (!el.offsetParent) return;
                    const cls  = el.className || '';
                    const text = el.innerText?.trim() || '';
                    const bind = el.getAttribute('data-bind') || '';
                    if (/prev|next|left|right|carousel|◄|►|‹|›/.test(cls + text + bind)) {
                        out.carouselBtns.push({ tag: el.tagName, cls: cls.substring(0, 50), text: text.substring(0, 20), bind: bind.substring(0, 60) });
                    }
                });

                // 4. Visible text matching tactic keywords
                const words = ['passing', 'counter', 'long ball', 'wing', 'shoot', 'careful', 'normal', 'reckless', 'aggressive', 'attack only', 'drop deep', 'support', 'protect', 'push forward'];
                document.querySelectorAll('span, div, h4, strong, li').forEach(el => {
                    if (!el.offsetParent) return;
                    const text = el.innerText?.trim()?.toLowerCase();
                    if (text && words.some(w => text.includes(w)) && text.length < 60) {
                        out.tacticText.push({ tag: el.tagName, cls: el.className.substring(0, 40), text: el.innerText.trim().substring(0, 50), bind: (el.getAttribute('data-bind') || '').substring(0, 60) });
                    }
                });

                return out;
            }
        """)

        if data.get("tactic"):
            t = data["tactic"]
            print(f"  [KO tactic] equipo {t['name']} (id {t['teamId']}):")
            for k, v in t["fields"].items():
                marker = "obs" if v["obs"] else "   "
                print(f"    [{marker}] {k} = {v['val']!r}")
        else:
            print("  [KO] No se encontró objeto tactic en nextRoundPartial")

        if data.get("rangeInputs"):
            print(f"  [sliders] {len(data['rangeInputs'])} range input(s):")
            for inp in data["rangeInputs"]:
                print(f"    id={inp['id']!r} val={inp['value']} label={inp['label']!r} bind={inp['bind']}")

        if data.get("carouselBtns"):
            print(f"  [carousel btns] {len(data['carouselBtns'])}:")
            for btn in data["carouselBtns"][:8]:
                print(f"    {btn['tag']}.{btn['cls'][:30]} text={btn['text']!r}")

        if data.get("tacticText"):
            print(f"  [tactic text visible] {len(data['tacticText'])}:")
            for el in data["tacticText"][:8]:
                print(f"    {el['tag']}: {el['text']!r}  bind={el['bind']}")

    except Exception as e:
        print(f"  [KO dump] error: {e}")


def _try_set_via_ko(page: Page, **kwargs) -> dict:
    """
    Modifica tácticas vía KO. Estrategia:
      1. vm[tacticProp].sliderValue(val) — patrón real del viewmodel de /Tactics.
      2. Búsqueda en nextRoundPartial().match.*.tactic como fallback.
    Devuelve {"applied": [...], "failed": [...]}
    """
    applied = []
    failed  = []

    for key, value in kwargs.items():
        if value is None:
            continue

        ko_prop    = _KO_TACTIC_PROP.get(key)
        legacy_key = _KO_FIELD_MAP.get(key, key)
        osm_val, _ = _to_osm(key, value)

        if ko_prop is None:
            failed.append(key)
            continue

        js_prop    = json.dumps(ko_prop)
        js_legacy  = json.dumps(legacy_key)
        js_val     = json.dumps(osm_val)

        ok = page.evaluate(f"""
            (function() {{
                const prop    = {js_prop};
                const legacy  = {js_legacy};
                const val     = {js_val};

                // Fuentes del viewmodel — carousel wrappers son el mejor punto de entrada
                // para el viewmodel del editor de tácticas (no el de la preview del partido)
                const sourceEls = [
                    document.getElementById('carousel-tacticoverall'),
                    document.getElementById('carousel-tacticstyleofplay'),
                    document.getElementById('carousel-tacticlineatt'),
                    document.querySelector('.tactic-slider-input'),
                    document.querySelector('[data-bind*="tacticOverall"]'),
                    document.querySelector('[data-bind*="tacticStyleOfPlay"]'),
                    document.querySelector('[data-bind*="tacticPressure"]'),
                    document.querySelector('[data-bind*="tacticOffsideTrap"]'),
                    document.body,
                ].filter(Boolean);

                function getRoots(el) {{
                    try {{
                        const ctx = ko.contextFor(el);
                        if (!ctx) return [];
                        return [ctx.$root, ctx.$data, ctx.$parent].filter(
                            v => v && typeof v === 'object'
                        );
                    }} catch(e) {{ return []; }}
                }}

                const seen = new WeakSet();

                for (const el of sourceEls) {{
                    for (const vm of getRoots(el)) {{
                        if (seen.has(vm)) continue;
                        seen.add(vm);

                        // ── Capa 1: vm[tacticProp].sliderValue(val) ─────────
                        const tacticObj = vm[prop];
                        if (tacticObj && typeof tacticObj.sliderValue === 'function') {{
                            tacticObj.sliderValue(val);
                            return {{ layer: 1, prop }};
                        }}

                        // ── Capa 2: vm[legacyField](val) — campo plano ────────
                        if (typeof vm[legacy] === 'function') {{
                            vm[legacy](val);
                            return {{ layer: 2, prop: legacy }};
                        }}

                        // ── Capa 3: nextRoundPartial().match.*.tactic ─────────
                        try {{
                            const nrp = vm.nextRoundPartial;
                            if (typeof nrp === 'function') {{
                                const match = nrp()?.match;
                                for (const team of [match?.homeTeam, match?.awayTeam].filter(Boolean)) {{
                                    const t = team.tactic;
                                    if (t && typeof t[legacy] === 'function') {{
                                        t[legacy](val);
                                        return {{ layer: 3, prop: legacy }};
                                    }}
                                }}
                            }}
                        }} catch(e) {{}}
                    }}
                }}
                return null;
            }})()
        """)

        if ok:
            applied.append(key)
            print(f"  ✓ KO L{ok.get('layer','?')}: {key} = {osm_val!r}  ({ok.get('prop')})")
        else:
            failed.append(key)
            print(f"  ⚠️ KO falló: {key} ({ko_prop})")

    return {"applied": applied, "failed": failed}


def _set_via_ui(page: Page, **kwargs) -> tuple[list, list]:
    """
    Fallback UI: navega los carousels con prev/next (IDs exactos conocidos)
    y rellena los inputs numéricos de slider.
    Solo se ejecuta si _try_set_via_ko falla en algún campo.
    """
    changed = []
    errors  = []

    # Campos que usan carousel caroufredsel con IDs fijos
    CAROUSEL_FIELDS = {k for k in _CAROUSEL_ID}

    # Selectores para los inputs numéricos visibles de cada slider
    SLIDER_INPUTS = {
        "pressure":  "input.tactic-slider-input[data-bind*='tacticPressure']",
        "mentality": "input.tactic-slider-input[data-bind*='tacticMentality']",
        "tempo":     "input.tactic-slider-input[data-bind*='tacticTempo']",
    }

    for field, value in kwargs.items():
        if value is None:
            continue

        if field in CAROUSEL_FIELDS:
            osm_idx, str_val = _to_osm(field, value)
            # Para offside_trap y marking el "texto" de referencia viene del dict
            # Usar el carousel ID exacto + leer índice actual via KO + navegar
            carousel_id = _CAROUSEL_ID[field]
            ko_prop     = _KO_TACTIC_PROP.get(field)
            if _navigate_carousel(page, carousel_id, ko_prop, osm_idx, str(str_val)):
                changed.append(field)
            else:
                errors.append(f"carousel:{field}")

        elif field in SLIDER_INPUTS:
            sel = SLIDER_INPUTS[field]
            # Intentar rellenar el input numérico visible
            try:
                loc = page.locator(sel).first
                if loc.count() == 0:
                    # Fallback posicional
                    idx = list(SLIDER_INPUTS.keys()).index(field)
                    loc = page.locator("input.tactic-slider-input").nth(idx)
                loc.fill(str(int(value)))
                loc.dispatch_event("change")
                time.sleep(0.3)
                changed.append(field)
                print(f"  ✓ input: {field} = {value}")
            except Exception as e:
                print(f"  ⚠️ input slider {field}: {e}")
                errors.append(f"slider:{field}")

        elif field == "offside_trap":
            if _toggle_offside_direct(page, bool(value)):
                changed.append(field)
            else:
                errors.append("offside_trap")

    return changed, errors


def _navigate_carousel(page: Page, carousel_id: str, ko_prop: str | None,
                        target_idx: int, target_text: str) -> bool:
    """
    Navega el carousel con id=carousel_id hasta el índice target_idx.
    Lee el índice actual via KO si es posible; si no, lee el texto visible.
    """
    # Leer índice actual via KO
    current_idx = None
    if ko_prop:
        try:
            current_idx = page.evaluate(f"""
                () => {{
                    const el  = document.getElementById({json.dumps(carousel_id)});
                    if (!el) return null;
                    const ctx = ko.contextFor(el);
                    if (!ctx) return null;
                    for (const vm of [ctx.$root, ctx.$data, ctx.$parent].filter(Boolean)) {{
                        const obj = vm[{json.dumps(ko_prop)}];
                        if (obj && typeof obj.sliderValue === 'function')
                            return obj.sliderValue();
                    }}
                    return null;
                }}
            """)
        except Exception:
            pass

    # Si no pudimos leer el índice, intentar leer el texto visible para comparar
    if current_idx is None:
        current_text = _read_carousel(page, f"#{carousel_id}")
        if current_text.lower() == target_text.lower():
            return True   # ya está en el valor correcto

    if current_idx == target_idx:
        return True       # ya está en el valor correcto

    # Selectors de flechas dentro del carousel
    prev_sel = f"#{carousel_id} .carousel-prev"
    next_sel = f"#{carousel_id} .carousel-next"

    try:
        prev_loc = page.locator(prev_sel).first
        next_loc = page.locator(next_sel).first
        if not next_loc.is_visible(timeout=1000):
            print(f"  ⚠️ carousel arrows no visibles: #{carousel_id}")
            return False
    except Exception as e:
        print(f"  ⚠️ carousel #{carousel_id}: {e}")
        return False

    # Navegar haciendo clic
    max_steps = 10
    for _ in range(max_steps):
        # Releer índice actual
        cur = None
        if ko_prop:
            try:
                cur = page.evaluate(f"""
                    () => {{
                        const el = document.getElementById({json.dumps(carousel_id)});
                        const ctx = el && ko.contextFor(el);
                        if (!ctx) return null;
                        for (const vm of [ctx.$root, ctx.$data].filter(Boolean)) {{
                            const obj = vm[{json.dumps(ko_prop)}];
                            if (obj && typeof obj.sliderValue === 'function') return obj.sliderValue();
                        }}
                        return null;
                    }}
                """)
            except Exception:
                pass

        if cur == target_idx:
            print(f"  ✓ carousel: {carousel_id} → idx {target_idx} ({target_text!r})")
            return True

        # Decidir dirección
        if cur is not None:
            click_next = (target_idx > cur)
        else:
            # Sin índice, intentar leer texto
            cur_text = _read_carousel(page, f"#{carousel_id}")
            if cur_text.lower() == target_text.lower():
                print(f"  ✓ carousel: {carousel_id} → {target_text!r}")
                return True
            click_next = True  # avanzar por defecto

        try:
            if click_next:
                next_loc.click()
            else:
                prev_loc.click()
            time.sleep(0.25)
        except Exception as e:
            print(f"  ⚠️ click carousel #{carousel_id}: {e}")
            return False

    # Verificar una última vez
    cur_text = _read_carousel(page, f"#{carousel_id}")
    if cur_text.lower() == target_text.lower():
        print(f"  ✓ carousel: {carousel_id} → {target_text!r}")
        return True

    print(f"  ⚠️ carousel #{carousel_id}: no se llegó a {target_text!r} (actual: {cur_text!r})")
    return False


def _detect_tactics_structure(page: Page) -> dict:
    """
    Detecta los selectores reales de la página de tácticas inspeccionando el DOM.
    Devuelve un dict con los selectores encontrados.
    """
    result = {}

    # Carousels: buscar por data-bind, usar selector de atributo (más fiable que id/class)
    carousel_bind_map = {
        "game_plan_sel":  ["style", "gameStyle", "gamePlan", "gameplan"],
        "tackling_sel":   ["overallMatchTactics", "tackling"],
        "fwd_tactic_sel": ["attack", "forwardsTactic", "forwardTactic"],
        "mid_tactic_sel": ["midfield", "midfieldersTactic", "midfielderTactic"],
        "def_tactic_sel": ["defense", "defendersTactic", "defenderTactic"],
        "marking_sel":    ["marking"],
    }
    for key, words in carousel_bind_map.items():
        for word in words:
            sel = f"[data-bind*='{word}']"
            try:
                if page.locator(sel).count() > 0:
                    result[key] = sel
                    break
            except Exception:
                pass

    # Sliders: buscar por data-bind o por label de texto
    binds = page.evaluate("""
        () => {
            const out = {};
            document.querySelectorAll('[data-bind]').forEach(el => {
                const b = el.getAttribute('data-bind').toLowerCase();
                if (el.tagName === 'INPUT' && el.type === 'range') {
                    if (b.includes('pressing') || b.includes('pressure')) out.pressure_slider = el.id ? '#'+el.id : null;
                    if (b.includes('mentality')) out.mentality_slider = el.id ? '#'+el.id : null;
                    if (b.includes('tempo'))     out.tempo_slider     = el.id ? '#'+el.id : null;
                }
            });
            // Sliders por label de texto
            document.querySelectorAll('input[type="range"]').forEach((el, i) => {
                const label = el.closest('div, li, tr')?.querySelector('label, span, h4')?.innerText?.toLowerCase() || '';
                const key = label.includes('press') ? 'pressure_slider'
                          : label.includes('mental') ? 'mentality_slider'
                          : label.includes('tempo')  ? 'tempo_slider'
                          : null;
                if (key && !out[key]) out[key] = el.id ? '#'+el.id : `input[type="range"]:nth-of-type(${i+1})`;
            });
            return out;
        }
    """)
    result.update({k: v for k, v in (binds or {}).items() if v})

    # Fallback posicional para sliders si no se detectaron por data-bind/label
    if not result.get("pressure_slider"):
        sliders = page.locator('input[type="range"]')
        n = sliders.count()
        if n >= 1: result["pressure_slider"]  = 'input[type="range"]:nth-of-type(1)'
        if n >= 2: result["mentality_slider"] = 'input[type="range"]:nth-of-type(2)'
        if n >= 3: result["tempo_slider"]     = 'input[type="range"]:nth-of-type(3)'

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


def _verify_ko_state(page: Page, kwargs: dict) -> dict:
    """
    Lee los valores actuales de los observables KO del editor de tácticas y los imprime.
    Devuelve un dict field->current_value para comparar con lo solicitado.
    """
    prop_names = list(_KO_TACTIC_PROP.values())
    try:
        result = page.evaluate(f"""
            () => {{
                const propNames = {json.dumps(prop_names)};
                const sourceEls = [
                    document.getElementById('carousel-tacticoverall'),
                    document.getElementById('carousel-tacticstyleofplay'),
                    document.querySelector('[data-bind*="tacticOverall"]'),
                    document.body,
                ].filter(Boolean);

                function getVm(el) {{
                    try {{
                        const ctx = ko.contextFor(el);
                        if (!ctx) return null;
                        for (const vm of [ctx.$root, ctx.$data, ctx.$parent].filter(Boolean)) {{
                            if (vm && vm.tacticOverall && typeof vm.tacticOverall.sliderValue === 'function')
                                return vm;
                        }}
                    }} catch(e) {{}}
                    return null;
                }}

                let vm = null;
                for (const el of sourceEls) {{
                    vm = getVm(el);
                    if (vm) break;
                }}
                if (!vm) return {{ error: 'no_vm' }};

                const values = {{}};
                for (const p of propNames) {{
                    const obj = vm[p];
                    if (obj && typeof obj.sliderValue === 'function')
                        values[p] = obj.sliderValue();
                    else if (typeof obj === 'function')
                        try {{ values[p] = obj(); }} catch(e) {{ values[p] = null; }}
                    else
                        values[p] = obj;
                }}

                // Buscar métodos de save / isChanged en el VM
                const saveMethods = [];
                const changedFlags = {{}};
                for (const k of Object.keys(vm)) {{
                    const kl = k.toLowerCase();
                    if (kl.includes('save') || kl.includes('update') || kl.includes('confirm'))
                        saveMethods.push(k);
                }}
                // isChanged de cada propiedad tactic
                for (const p of propNames) {{
                    const obj = vm[p];
                    if (obj && typeof obj.isChanged === 'function')
                        try {{ changedFlags[p] = obj.isChanged(); }} catch(e) {{}}
                }}
                return {{ values, saveMethods, changedFlags }};
            }}
        """)
    except Exception as e:
        print(f"  [verify] error leyendo KO: {e}")
        return {}

    if result.get("error"):
        print(f"  [verify] no se encontró viewmodel KO ({result['error']})")
        return {}

    # Mapear prop→field para comparación
    prop_to_field = {v: k for k, v in _KO_TACTIC_PROP.items()}
    values = result.get("values", {})
    changed_flags = result.get("changedFlags", {})
    save_methods = result.get("saveMethods", [])

    print("  [verify] Estado KO actual del editor de tácticas:")
    field_values = {}
    for prop, current in values.items():
        field = prop_to_field.get(prop, prop)
        requested = kwargs.get(field)
        expected_osm, _ = _to_osm(field, requested) if requested is not None else (None, None)
        match_mark = "✓" if (requested is None or current == expected_osm) else "✗"
        changed_str = f" isChanged={changed_flags.get(prop)}" if prop in changed_flags else ""
        if requested is not None:
            print(f"    {match_mark} {field}: actual={current!r}  esperado={expected_osm!r}{changed_str}")
        field_values[field] = current

    if save_methods:
        print(f"  [verify] Métodos save encontrados en VM: {save_methods}")

    return {"field_values": field_values, "save_methods": save_methods, "raw_values": values}


def _force_save(page: Page, vm_save_methods: list | None = None) -> bool:
    """
    Intenta persistir los cambios de tácticas en OSM.
    Estrategia 1: llamar métodos save/update en el viewmodel KO.
    Estrategia 2: simular un clic de carousel (next + prev) en cualquier carousel
                  para forzar que isChanged() se active y el auto-save dispare.
    Estrategia 3: buscar y clicar botón Save visible.
    """
    # ── Estrategia 1: llamar método de save en el VM ──────────────────────────
    if vm_save_methods:
        try:
            saved = page.evaluate(f"""
                () => {{
                    const methods = {json.dumps(vm_save_methods)};
                    const sourceEls = [
                        document.getElementById('carousel-tacticoverall'),
                        document.body,
                    ].filter(Boolean);
                    function getVm(el) {{
                        try {{
                            const ctx = ko.contextFor(el);
                            if (!ctx) return null;
                            for (const vm of [ctx.$root, ctx.$data].filter(Boolean))
                                if (vm && vm.tacticOverall && typeof vm.tacticOverall.sliderValue === 'function')
                                    return vm;
                        }} catch(e) {{}}
                        return null;
                    }}
                    let vm = null;
                    for (const el of sourceEls) {{ vm = getVm(el); if (vm) break; }}
                    if (!vm) return null;
                    for (const m of methods) {{
                        if (typeof vm[m] === 'function') {{
                            try {{ vm[m](); return m; }} catch(e) {{}}
                        }}
                    }}
                    return null;
                }}
            """)
            if saved:
                print(f"  ✓ Save vía VM.{saved}()")
                time.sleep(1.5)
                return True
        except Exception as e:
            print(f"  ⚠️ VM save error: {e}")

    # ── Estrategia 2: nudge de carousel para activar isChanged() ─────────────
    # Hace clic en "next" y luego en "prev" para que OSM detecte interacción
    # sin cambiar el valor real; esto dispara el debounced auto-save de OSM.
    nudge_carousel_ids = [
        "carousel-tacticoverall",
        "carousel-tacticstyleofplay",
        "carousel-tacticlineatt",
    ]
    for cid in nudge_carousel_ids:
        try:
            next_loc = page.locator(f"#{cid} .carousel-next").first
            prev_loc = page.locator(f"#{cid} .carousel-prev").first
            if next_loc.is_visible(timeout=800) and prev_loc.is_visible(timeout=500):
                next_loc.click()
                time.sleep(0.3)
                prev_loc.click()
                time.sleep(0.3)
                print(f"  ✓ Nudge carousel #{cid} para activar isChanged()")
                time.sleep(2)
                return True
        except Exception:
            pass

    # ── Estrategia 3: botón Save visible ─────────────────────────────────────
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
            if loc.is_visible(timeout=500):
                loc.click()
                time.sleep(1)
                handle_popups(page)
                print(f"  ✓ Save vía botón ({sel})")
                return True
        except Exception:
            pass

    print("  ⚠️ No se pudo confirmar save (auto-save puede haber disparado igual)")
    return False


def _click_save(page: Page) -> bool:
    """Hace click en el botón de guardar tácticas (legacy, usa _force_save internamente)."""
    return _force_save(page)


# ── WRAPPER PARA SER LLAMADO DESDE EL BOT ─────────────────────────────────────

def set_tactics_for_slot(
    page: Page,
    league_name: str,
    career_url: str = "https://en.onlinesoccermanager.com/Career",
    **kwargs,
) -> dict:
    """
    Encuentra el slot de Career que corresponde a league_name y aplica los cambios.
    Usa get_slot_info para identificar el slot por nombre — nunca por índice DOM.
    """
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
            print(f"  ✓ Slot encontrado: índice DOM {i} → '{slot_league}'")
            break

    if target_idx is None:
        print(f"  ❌ No se encontró slot para liga '{league_name}' (slots={count})")
        return {"success": False, "changed": [], "errors": [f"slot_not_found:{league_name}"]}

    if not click_slot_and_wait_for_dashboard(page, target_idx):
        return {"success": False, "changed": [], "errors": ["slot_activation_failed"]}

    return set_tactics(page, **kwargs)

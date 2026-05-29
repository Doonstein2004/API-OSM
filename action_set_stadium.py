# action_set_stadium.py
"""
Automatiza ampliaciones del estadio en OSM via Playwright.
URL: /Stadium — 3 partes: Capacity, Pitch, TrainingFacility

Flujo:
  1. Leer estado de los 3 paneles (maxed / in_progress / finished / can_start) y costos
  2. Leer balances via KO (Club Funds y Savings)
  3. Reclamar paneles terminados (claimUpgrade)
  4. Para los paneles a ampliar:
       - Si CF >= costo → iniciar directo
       - Si CF < costo pero CF+Savings >= costo → abrir modal finanzas,
         transferir Savings→CF, cerrar modal, iniciar
       - Si no hay fondos → saltar al siguiente panel (pueden costar diferente)
  5. Al finalizar: si quedan fondos en CF → abrir modal, transferir CF→Savings

Nota OSM: solo se puede tener UNA ampliación activa a la vez.
"""
import time
from playwright.sync_api import Page
from utils import handle_popups

STADIUM_URL = "https://en.onlinesoccermanager.com/Stadium"

# Mapeo de clave interna → variantes de nombre mostrado por OSM
_PART_NAME_MAP = {
    "capacity": ["capacity", "aforo", "entradas"],
    "pitch":    ["pitch", "campo", "terreno", "terreno de juego"],
    "training": ["training", "entrenamiento", "training facility", "ciudad deportiva"],
}


def _identify_type(name: str) -> str:
    """Devuelve la clave interna ('capacity'/'pitch'/'training') del nombre OSM."""
    n = name.lower().strip()
    for key, variants in _PART_NAME_MAP.items():
        if any(v in n for v in variants):
            return key
    return "unknown"


def _parse_currency(text: str) -> float:
    """Convierte '384K', '1.5M', '2B' a float."""
    t = text.strip().replace(",", ".").replace("\xa0", "")
    mult = 1
    if t.endswith("K") or t.endswith("k"):
        mult, t = 1_000, t[:-1]
    elif t.endswith("M") or t.endswith("m"):
        mult, t = 1_000_000, t[:-1]
    elif t.endswith("B") or t.endswith("b"):
        mult, t = 1_000_000_000, t[:-1]
    try:
        return float(t) * mult
    except ValueError:
        return 0.0


# ── NAVEGACIÓN ────────────────────────────────────────────────────────────────

def _stadium_loaded(page: Page, timeout: int = 10000) -> bool:
    for sel in [".panel-stadium-part", "[data-bind*='stadiumPart']",
                "[data-bind*='startUpgrade']", "[data-bind*='claimUpgrade']"]:
        try:
            page.wait_for_selector(sel, timeout=timeout, state="attached")
            return True
        except Exception:
            pass
    return False


def _navigate_to_stadium(page: Page) -> bool:
    for sel in ["a[href='/Stadium']", "a[href*='/Stadium']",
                "a:has-text('Stadium')", "a:has-text('Estadio')", ".nav-stadium a"]:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=800):
                loc.click()
                time.sleep(2)
                if _stadium_loaded(page):
                    print(f"  ✓ Stadium cargado vía SPA ({sel})")
                    return True
        except Exception:
            pass

    print("  → Fallback: page.goto(/Stadium)...")
    try:
        page.goto(STADIUM_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)
        handle_popups(page)
        if _stadium_loaded(page):
            return True
    except Exception as e:
        print(f"  ⚠️ page.goto() falló: {e}")
    return False


# ── LECTURA DE ESTADO ─────────────────────────────────────────────────────────

def _get_stadium_parts(page: Page) -> list[dict]:
    """
    Lee el estado de los 3 paneles del estadio via DOM + KO.
    Devuelve lista de dicts:
      { index, name, type, is_maxed, is_building, is_finished, can_start, cost }
    """
    try:
        return page.evaluate("""
            () => {
                const parts = [];
                document.querySelectorAll('.panel.panel-stadium-part').forEach((panel, i) => {
                    // Nombre del panel
                    const nameEl = panel.querySelector('h3[data-bind*="name"]');
                    const name   = nameEl ? nameEl.innerText.trim() : '';

                    // Tipo via URL de imagen (más confiable que el nombre)
                    let partType = 'unknown';
                    const imgEl = panel.querySelector('[style*="background-image"]');
                    if (imgEl) {
                        const url = imgEl.style.backgroundImage || '';
                        if (url.includes('capacity'))        partType = 'capacity';
                        else if (url.includes('pitch'))      partType = 'pitch';
                        else if (url.includes('training'))   partType = 'training';
                    }

                    // Estado via visibilidad DOM
                    const isMaxed    = !!panel.querySelector('.panel-body.maxed-out');
                    const claimBtn   = panel.querySelector('button[data-bind*="claimUpgrade"]');
                    const startBtn   = panel.querySelector('button[data-bind*="startUpgrade"]');
                    const isFinished = !!(claimBtn && claimBtn.offsetParent !== null);
                    const canStart   = !!(startBtn && startBtn.offsetParent !== null) && !isMaxed;

                    // Determinar si hay construcción en progreso
                    // (está building pero el timer aún no terminó)
                    let isBuilding = false;
                    try {
                        const ctx = ko.contextFor(panel.querySelector('[data-bind]'));
                        if (ctx) {
                            const vm = ctx.$data;
                            if (vm && typeof vm.isBuilding === 'function')
                                isBuilding = vm.isBuilding();
                        }
                    } catch(e) {}
                    const isInProgress = isBuilding && !isFinished;

                    // Costo de ampliación (desde DOM)
                    let cost = 0;
                    if (startBtn) {
                        const costEl = startBtn.querySelector('.club-funds-amount');
                        if (costEl) {
                            const t = costEl.innerText.trim();
                            // Intentar leer vía KO para valor numérico exacto
                            try {
                                const ctx2 = ko.contextFor(startBtn);
                                if (ctx2 && ctx2.$data && typeof ctx2.$data.upgradeCost === 'function'
                                        && typeof ctx2.$data.stadiumPartType !== 'undefined') {
                                    cost = ctx2.$data.upgradeCost(ctx2.$data.stadiumPartType);
                                }
                            } catch(e) {}
                            if (!cost) {
                                // Parsear texto (e.g. "384K")
                                let txt = t.replace(/[^0-9.,KMBkmb]/g, '');
                                let mul = 1;
                                if (/[Kk]/.test(t)) mul = 1000;
                                else if (/[Mm]/.test(t)) mul = 1000000;
                                else if (/[Bb]/.test(t)) mul = 1000000000;
                                cost = (parseFloat(txt.replace(',', '.')) || 0) * mul;
                            }
                        }
                    }

                    parts.push({ index: i, name, type: partType, is_maxed: isMaxed,
                                  is_building: isBuilding, is_in_progress: isInProgress,
                                  is_finished: isFinished, can_start: canStart, cost });
                });
                return parts;
            }
        """)
    except Exception as e:
        print(f"  ⚠️ get_stadium_parts error: {e}")
        return []


def _read_balances(page: Page) -> dict:
    """
    Lee Club Funds y Savings.
    Estrategia 1: KO — recorre la cadena de contextos buscando savings/balanceProgress.
    Estrategia 2: DOM — lee el wallet y el modal de finanzas si está abierto.
    Devuelve { "cf": float, "savings": float }
    """
    try:
        result = page.evaluate("""
            () => {
                const walletEl = document.querySelector('.wallet-container.clubfunds-wallet');
                if (!walletEl) return null;
                const ctx = ko.contextFor(walletEl);
                if (!ctx) return null;

                // ctx.$data = clubFundsProgress (target del 'with: clubFundsProgress')
                const cfProgress = ctx.$data;
                let cf = 0;
                if (cfProgress && typeof cfProgress.animatedProgress === 'function')
                    cf = cfProgress.animatedProgress() || 0;

                // Buscar savings recorriendo la cadena de contextos KO hacia arriba
                let savings = 0;
                const candidates = [
                    ctx.$parent,
                    ctx.$parentContext && ctx.$parentContext.$data,
                    ctx.$root,
                ].filter(Boolean);

                for (const vm of candidates) {
                    if (!vm) continue;
                    // Camino 1: vm.savings() — observable directo
                    if (typeof vm.savings === 'function') {
                        const s = vm.savings();
                        if (s > 0) { savings = s; break; }
                    }
                    // Camino 2: vm.savingsProgress.animatedProgress() — igual que balanceProgress
                    if (vm.savingsProgress) {
                        const sp = typeof vm.savingsProgress === 'function'
                            ? vm.savingsProgress() : vm.savingsProgress;
                        if (sp && typeof sp.animatedProgress === 'function') {
                            const s = sp.animatedProgress();
                            if (s > 0) { savings = s; break; }
                        }
                    }
                }

                // Fallback: leer desde el DOM del modal de finanzas si está abierto
                if (savings === 0) {
                    const vals = document.querySelectorAll('.finance-modal-savings-value');
                    if (vals.length >= 2) {
                        // vals[0] = CF, vals[1] = Savings
                        const parseAmount = t => {
                            t = t.trim().replace(/[^0-9.,KMBkmb]/g, '');
                            let m = 1;
                            if (/[Kk]/.test(t)) { m = 1e3;  t = t.replace(/[Kk]/, ''); }
                            else if (/[Mm]/.test(t)) { m = 1e6; t = t.replace(/[Mm]/, ''); }
                            else if (/[Bb]/.test(t)) { m = 1e9; t = t.replace(/[Bb]/, ''); }
                            return (parseFloat(t.replace(',', '.')) || 0) * m;
                        };
                        if (!cf) cf = parseAmount(vals[0].innerText);
                        savings = parseAmount(vals[1].innerText);
                    }
                }

                // Fallback: leer desde el span del wallet
                if (cf === 0 && savings === 0) {
                    const walletSpan = walletEl.querySelector('span.pull-right');
                    if (walletSpan) {
                        const icon = walletEl.querySelector('.wallet-icon');
                        const isSavings = icon && icon.classList.contains('piggy-savings');
                        const t = walletSpan.innerText.trim();
                        let m = 1;
                        if (/[Kk]/.test(t)) m = 1e3;
                        else if (/[Mm]/.test(t)) m = 1e6;
                        else if (/[Bb]/.test(t)) m = 1e9;
                        const amount = (parseFloat(t.replace(/[^0-9.,]/g, '').replace(',', '.')) || 0) * m;
                        if (isSavings) savings = amount;
                        else cf = amount;
                    }
                }

                return { cf: cf || 0, savings: savings || 0 };
            }
        """)
        if result:
            return {"cf": float(result["cf"]), "savings": float(result["savings"])}
    except Exception as e:
        print(f"  ⚠️ read_balances error: {e}")
    return {"cf": 0.0, "savings": 0.0}


# ── OPERACIONES FINANCIERAS ───────────────────────────────────────────────────

def _open_finance_modal(page: Page) -> bool:
    """Abre el modal de finanzas clicando el wallet."""
    try:
        ok = page.evaluate("""
            () => {
                const walletEl = document.querySelector('.wallet-container.clubfunds-wallet');
                if (!walletEl) return false;
                const ctx = ko.contextFor(walletEl);
                const root = ctx && (ctx.$parent || ctx.$root);
                if (root && typeof root.showFinanceModal === 'function') {
                    root.showFinanceModal();
                    return 'ko';
                }
                walletEl.click();
                return 'click';
            }
        """)
        if ok:
            time.sleep(1.5)
            try:
                page.wait_for_selector("#finance-modal-transfer-arrow", timeout=4000, state="visible")
            except Exception:
                pass
            return True
    except Exception as e:
        print(f"  ⚠️ open_finance_modal: {e}")
    try:
        page.locator(".wallet-container.clubfunds-wallet").first.click()
        time.sleep(1.5)
        return True
    except Exception:
        return False


def _do_transfer(page: Page) -> bool:
    """
    Ejecuta la transferencia en el modal de finanzas.
    El sentido es el que OSM tenga activo (Savings→CF o CF→Savings).
    """
    try:
        ok = page.evaluate("""
            () => {
                const arrowEl = document.querySelector('#finance-modal-transfer-arrow');
                if (!arrowEl) return false;
                try {
                    const ctx = ko.contextFor(arrowEl);
                    if (ctx && ctx.$data && typeof ctx.$data.transferMoney === 'function') {
                        ctx.$data.transferMoney();
                        return 'ko';
                    }
                } catch(e) {}
                arrowEl.click();
                return 'click';
            }
        """)
        if ok:
            time.sleep(2)
            return True
    except Exception as e:
        print(f"  ⚠️ do_transfer: {e}")
    try:
        page.locator("#finance-modal-transfer-arrow").first.click()
        time.sleep(2)
        return True
    except Exception:
        return False


def _close_finance_modal(page: Page):
    """Cierra el modal de finanzas."""
    for sel in [".close-button-container button.close",
                ".modal.in button[data-bind*='closeButtonClicked']",
                ".modal.in button.close"]:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=800):
                loc.click()
                time.sleep(0.5)
                return
        except Exception:
            pass
    try:
        page.keyboard.press("Escape")
        time.sleep(0.5)
    except Exception:
        pass


def _transfer_savings_to_cf(page: Page) -> bool:
    """
    Abre el modal de finanzas y transfiere Savings → CF.
    Solo transfiere si hay dinero en Savings.
    """
    balances = _read_balances(page)
    if balances["savings"] <= 0:
        print("  ⚠️ Savings vacío, nada que transferir")
        return False

    print(f"  → Transfiriendo Savings({balances['savings']:,.0f}) → CF...")
    if not _open_finance_modal(page):
        return False

    # Verificar la dirección actual: si Savings tiene `active` → es modo Withdraw (Savings→CF)
    # Si CF tiene `active` → es modo Deposit (CF→Savings), necesitamos cambiar dirección
    try:
        direction = page.evaluate("""
            () => {
                // CF value: active cuando moneyTransferType === Deposit
                // Savings value: active cuando moneyTransferType === Withdraw
                const cfVal = document.querySelector(
                    '#finance-modal-savings .finance-modal-savings-cf-icon ~ div .finance-modal-savings-value, ' +
                    '.finance-modal-savings-cf-icon + div .finance-modal-savings-value'
                );
                // Buscar por posición: primer .finance-modal-savings-value = CF, segundo = Savings
                const vals = document.querySelectorAll('.finance-modal-savings-value');
                if (vals.length >= 2) {
                    const cfActive      = vals[0].classList.contains('active');
                    const savingsActive = vals[1].classList.contains('active');
                    return { cfActive, savingsActive };
                }
                return null;
            }
        """)
        if direction and direction.get("cfActive") and not direction.get("savingsActive"):
            # Modo Deposit (CF→Savings) pero queremos Withdraw (Savings→CF)
            # Necesitamos invertir la dirección antes de transferir
            # Intentar via KO
            flipped = page.evaluate("""
                () => {
                    const ctx = ko.contextFor(document.querySelector('#finance-modal-transfer-arrow'));
                    const vm = ctx && ctx.$data;
                    if (!vm) return false;
                    // Buscar moneyTransferType observable y cambiarlo
                    const root = ctx.$root || ctx.$parent;
                    if (root && typeof root.moneyTransferType === 'function') {
                        // MoneyTransferType.Withdraw es el que mueve Savings→CF
                        // Intentar con el valor numérico (típicamente 1 o 2)
                        const current = root.moneyTransferType();
                        // Cambiar al opuesto
                        root.moneyTransferType(current === 0 ? 1 : 0);
                        return true;
                    }
                    return false;
                }
            """)
            if not flipped:
                print("  ⚠️ No se pudo invertir dirección de transferencia")
                _close_finance_modal(page)
                return False
    except Exception as e:
        print(f"  ⚠️ direction check: {e}")

    transferred = _do_transfer(page)
    _close_finance_modal(page)
    if transferred:
        print("  ✓ Savings → CF transferido")
    return transferred


def _transfer_cf_to_savings(page: Page) -> bool:
    """Abre el modal de finanzas y transfiere CF → Savings."""
    balances = _read_balances(page)
    if balances["cf"] <= 0:
        return True  # Ya está en savings o no hay nada

    print(f"  → Devolviendo CF({balances['cf']:,.0f}) → Savings...")
    if not _open_finance_modal(page):
        return False

    transferred = _do_transfer(page)
    _close_finance_modal(page)
    if transferred:
        print("  ✓ CF → Savings transferido")
    return transferred


# ── ACCIONES DEL ESTADIO ──────────────────────────────────────────────────────

def _claim_stadium_part(page: Page, part_index: int) -> bool:
    """Reclama la ampliación terminada en el panel indicado."""
    try:
        ok = page.evaluate(f"""
            (function() {{
                const panels = document.querySelectorAll('.panel.panel-stadium-part');
                const panel = panels[{part_index}];
                if (!panel) return false;
                const claimBtn = panel.querySelector('button[data-bind*="claimUpgrade"]');
                if (!claimBtn || !claimBtn.offsetParent) return false;
                try {{
                    const ctx = ko.contextFor(claimBtn);
                    if (ctx && ctx.$data && typeof ctx.$data.claimUpgrade === 'function') {{
                        ctx.$data.claimUpgrade();
                        return 'ko';
                    }}
                }} catch(e) {{}}
                claimBtn.click();
                return 'click';
            }})()
        """)
        if ok:
            print(f"  ✓ Panel {part_index}: claimUpgrade ({ok})")
            time.sleep(2)
            handle_popups(page)
            return True
    except Exception as e:
        print(f"  ⚠️ claimUpgrade panel {part_index}: {e}")
    # Fallback Playwright
    try:
        btn = page.locator(".panel.panel-stadium-part").nth(part_index).locator(
            "button[data-bind*='claimUpgrade']").first
        if btn.is_visible(timeout=1000):
            btn.click()
            time.sleep(2)
            handle_popups(page)
            return True
    except Exception:
        pass
    return False


def _start_stadium_upgrade(page: Page, part_index: int) -> bool:
    """Inicia la ampliación en el panel indicado (startUpgrade)."""
    try:
        ok = page.evaluate(f"""
            (function() {{
                const panels = document.querySelectorAll('.panel.panel-stadium-part');
                const panel = panels[{part_index}];
                if (!panel) return false;
                const startBtn = panel.querySelector('button[data-bind*="startUpgrade"]');
                if (!startBtn || !startBtn.offsetParent) return false;
                try {{
                    const ctx = ko.contextFor(startBtn);
                    if (ctx && ctx.$data && typeof ctx.$data.startUpgrade === 'function') {{
                        ctx.$data.startUpgrade();
                        return 'ko';
                    }}
                }} catch(e) {{}}
                startBtn.click();
                return 'click';
            }})()
        """)
        if ok:
            print(f"  ✓ Panel {part_index}: startUpgrade ({ok})")
            time.sleep(2)
            handle_popups(page)
            return True
    except Exception as e:
        print(f"  ⚠️ startUpgrade panel {part_index}: {e}")
    try:
        btn = page.locator(".panel.panel-stadium-part").nth(part_index).locator(
            "button[data-bind*='startUpgrade']").first
        if btn.is_visible(timeout=1000):
            btn.click()
            time.sleep(2)
            handle_popups(page)
            return True
    except Exception:
        pass
    return False


# ── FUNCIÓN PRINCIPAL ─────────────────────────────────────────────────────────

def upgrade_stadium(page: Page, preferred_parts: list[str] | None = None) -> dict:
    """
    Reclama ampliaciones terminadas e inicia nuevas ampliaciones.

    preferred_parts: lista ordenada de tipos a intentar ('capacity'/'pitch'/'training').
                     Si None, intenta todos los disponibles en orden.

    Returns:
        { "claimed": list[str], "started": list[dict], "skipped": list[tuple],
          "errors": list[str], "cf": float, "savings": float }
    """
    result = {"claimed": [], "started": [], "skipped": [], "errors": [],
              "cf": 0.0, "savings": 0.0}
    money_moved_to_cf = False

    if not _navigate_to_stadium(page):
        result["errors"].append("page_not_loaded")
        return result

    time.sleep(1)
    handle_popups(page)

    # ── Leer estado inicial ───────────────────────────────────────────────────
    parts = _get_stadium_parts(page)
    bal   = _read_balances(page)
    print(f"  [stadium] {len(parts)} paneles | CF={bal['cf']:,.0f} Savings={bal['savings']:,.0f}")
    for p in parts:
        state = ("maxed" if p["is_maxed"] else
                 "finished" if p["is_finished"] else
                 "in_progress" if p["is_in_progress"] else
                 "can_start" if p["can_start"] else "idle")
        print(f"    {p['type']:12} ({p['name']:20}) {state:12} cost={p['cost']:,.0f}")

    # ── Paso 1: reclamar terminados ───────────────────────────────────────────
    for p in parts:
        if p["is_finished"]:
            if _claim_stadium_part(page, p["index"]):
                result["claimed"].append(p["type"])
            else:
                result["errors"].append(f"claim_failed:{p['type']}")

    if result["claimed"]:
        time.sleep(1)
        parts = _get_stadium_parts(page)
        bal   = _read_balances(page)

    # ── Paso 2: verificar si ya hay construcción en curso ────────────────────
    building = next((p for p in parts if p["is_in_progress"]), None)
    if building:
        print(f"  [stadium] Construcción en curso: {building['type']} — no se inicia nueva")
        result["skipped"].append((building["type"], "in_progress"))
        result["cf"]      = bal["cf"]
        result["savings"] = bal["savings"]
        return result

    # ── Paso 3: determinar qué intentar ampliar ───────────────────────────────
    if preferred_parts:
        ordered = preferred_parts
    else:
        ordered = [p["type"] for p in parts if p["can_start"]]

    total = bal["cf"] + bal["savings"]

    for type_key in ordered:
        part = next((p for p in parts if p["type"] == type_key and p["can_start"]), None)
        if not part:
            reason = "maxed" if next((p for p in parts if p["type"] == type_key and p["is_maxed"]), None) else "not_available"
            result["skipped"].append((type_key, reason))
            continue

        cost = part["cost"]
        if cost > 0 and total < cost:
            print(f"  [stadium] Fondos insuficientes para {type_key}: need={cost:,.0f} total={total:,.0f}")
            result["skipped"].append((type_key, f"no_funds:{cost:,.0f}"))
            continue

        # Mover Savings → CF si hace falta
        if cost > 0 and bal["cf"] < cost and bal["savings"] > 0 and not money_moved_to_cf:
            if _transfer_savings_to_cf(page):
                money_moved_to_cf = True
                time.sleep(0.5)
                bal = _read_balances(page)
            else:
                result["errors"].append(f"transfer_failed:{type_key}")
                continue

        if cost > 0 and bal["cf"] < cost:
            print(f"  [stadium] CF insuficiente para {type_key}: need={cost:,.0f} cf={bal['cf']:,.0f}")
            result["skipped"].append((type_key, f"cf_low:{cost:,.0f}"))
            continue

        # Iniciar ampliación
        if _start_stadium_upgrade(page, part["index"]):
            result["started"].append({"type": type_key, "cost": cost, "name": part["name"]})
            bal["cf"] = max(0, bal["cf"] - cost)
            # Solo UNA ampliación activa — parar después del primero exitoso
            break
        else:
            result["errors"].append(f"start_failed:{type_key}")

    # ── Paso 4: devolver CF restante a Savings ────────────────────────────────
    final_bal = _read_balances(page)
    if final_bal["cf"] > 0:
        _transfer_cf_to_savings(page)
        time.sleep(0.5)
        final_bal = _read_balances(page)

    result["cf"]      = final_bal["cf"]
    result["savings"] = final_bal["savings"]

    print(f"  [stadium] Reclamados: {result['claimed']} | Iniciados: {[s['type'] for s in result['started']]}")
    print(f"  [stadium] CF={result['cf']:,.0f} Savings={result['savings']:,.0f}")
    return result


def upgrade_stadium_for_slot(
    page: Page,
    league_name: str,
    preferred_parts: list[str] | None = None,
    career_url: str = "https://en.onlinesoccermanager.com/Career",
) -> dict:
    """Activa el slot de la liga indicada y ejecuta la lógica del estadio."""
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
        return {"claimed": [], "started": [], "skipped": [], "errors": [f"slot_not_found:{league_name}"],
                "cf": 0.0, "savings": 0.0}

    if not click_slot_and_wait_for_dashboard(page, target_idx):
        return {"claimed": [], "started": [], "skipped": [], "errors": ["slot_activation_failed"],
                "cf": 0.0, "savings": 0.0}

    return upgrade_stadium(page, preferred_parts=preferred_parts)

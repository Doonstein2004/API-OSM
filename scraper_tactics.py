# scraper_tactics.py
"""
Scraper de Tácticas para OSM
Extrae la configuración táctica actual del usuario para cada liga/equipo.
"""
import time
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError
from utils import handle_popups, safe_int, safe_navigate


def get_tactics_data(page: Page):
    """
    Extrae las tácticas configuradas para cada equipo gestionado.
    Navega por cada slot y obtiene la configuración táctica desde /Tactics
    
    NOTA: Esta función asume que ya estamos logueados y reutiliza el navegador.
    
    Returns:
        list: Lista de diccionarios con las tácticas por equipo/liga
    """
    print("\n--- Scraper de Tácticas V2.0 (Robusto) ---")
    MAIN_DASHBOARD_URL = "https://en.onlinesoccermanager.com/Career"
    TACTICS_URL = "https://en.onlinesoccermanager.com/Tactics"
    
    all_teams_tactics = []
    NUM_SLOTS = 4

    try:
        for i in range(NUM_SLOTS):
            print(f"\n--- Slot #{i + 1}: Extrayendo Tácticas ---")
            
            # === NAVEGACIÓN ROBUSTA AL CAREER ===
            max_nav_retries = 3
            for nav_attempt in range(max_nav_retries):
                try:
                    if not page.url.endswith("/Career"):
                        page.goto(MAIN_DASHBOARD_URL, wait_until="domcontentloaded", timeout=30000)
                    else:
                        page.reload(wait_until="domcontentloaded", timeout=30000)
                    
                    page.wait_for_selector(".career-teamslot", timeout=20000)
                    handle_popups(page)
                    time.sleep(1)
                    break
                    
                except Exception as nav_error:
                    print(f"  ⚠️ Error navegación (intento {nav_attempt + 1}): {nav_error}")
                    if nav_attempt < max_nav_retries - 1:
                        time.sleep(2)
                    else:
                        print(f"  ❌ No se pudo navegar a Career. Saltando slot {i+1}.")
                        continue

            # === VERIFICAR SLOT ===
            try:
                slots = page.locator(".career-teamslot")
                if slots.count() <= i:
                    print(f"Slot #{i + 1} no existe. Terminando.")
                    break
                    
                slot = slots.nth(i)
                
                title_locator = slot.locator("h2.clubslot-main-title")
                if title_locator.count() == 0:
                    print(f"Slot #{i + 1} está vacío. Saltando.")
                    continue
                
                team_name = title_locator.inner_text()
                league_name = slot.locator("h4.display-name").inner_text()
                print(f"Procesando: {team_name} en {league_name}")
                
            except Exception as slot_error:
                print(f"  ⚠️ Error verificando slot {i+1}: {slot_error}")
                continue

            # === HACER CLICK EN EL SLOT CON REINTENTOS ===
            click_success = False
            for click_attempt in range(3):
                try:
                    handle_popups(page)
                    slot = page.locator(".career-teamslot").nth(i)
                    slot.click(timeout=15000)
                    page.wait_for_selector("#timers", timeout=45000)
                    handle_popups(page)
                    click_success = True
                    break
                    
                except Exception as click_error:
                    print(f"  ⚠️ Error click (intento {click_attempt + 1}): {click_error}")
                    if click_attempt < 2:
                        try:
                            page.goto(MAIN_DASHBOARD_URL, wait_until="domcontentloaded", timeout=30000)
                            page.wait_for_selector(".career-teamslot", timeout=15000)
                            handle_popups(page)
                            time.sleep(1)
                        except:
                            pass
            
            if not click_success:
                print(f"  ❌ No se pudo activar el slot {i+1}. Saltando.")
                continue

            # === NAVEGAR A TÁCTICAS ===
            try:
                if not safe_navigate(page, TACTICS_URL, verify_selector="#tactics-overall"):
                    print(f"  ❌ No se pudo navegar a tácticas para {team_name}")
                    continue
                
                time.sleep(2)
                handle_popups(page)
                
                tactics_data = extract_tactics_from_page(page)
                tactics_data["team_name"] = team_name
                tactics_data["league_name"] = league_name
                
                all_teams_tactics.append(tactics_data)
                print(f"  ✓ Tácticas extraídas para {team_name}")
                
            except Exception as e:
                print(f"  ❌ Error extrayendo tácticas para {team_name}: {e}")
                continue

    except Exception as e:
        print(f"❌ Error general en scraper de tácticas: {e}")

    print(f"\n✅ Proceso completado. Tácticas extraídas de {len(all_teams_tactics)} equipos.")
    return all_teams_tactics


def extract_tactics_from_page(page: Page) -> dict:
    """
    Extrae todas las configuraciones tácticas de la página /Tactics
    usando JavaScript para acceder a los datos del DOM y los observables de Knockout.js
    """
    
    tactics = {}
    
    # === 1. GAME PLAN (Plan de Juego) ===
    try:
        tactics["game_plan"] = page.evaluate("""
            () => {
                const carousel = document.querySelector('#carousel-tacticoverall');
                if (!carousel) return 'Unknown';
                
                const wrapper = carousel.querySelector('.caroufredsel_wrapper');
                if (wrapper) {
                    const items = wrapper.querySelectorAll('.carousel-item h3');
                    for (const item of items) {
                        const rect = item.getBoundingClientRect();
                        const parentRect = wrapper.getBoundingClientRect();
                        if (rect.left >= parentRect.left && rect.right <= parentRect.right) {
                            return item.innerText.trim();
                        }
                    }
                    if (items.length > 0) return items[0].innerText.trim();
                }
                return 'Unknown';
            }
        """)
    except Exception as e:
        print(f"    ⚠️ Error extrayendo Game Plan: {e}")
        tactics["game_plan"] = "Unknown"
    
    # === 2. TACKLING (Agresividad) ===
    try:
        tactics["tackling"] = page.evaluate("""
            () => {
                const carousel = document.querySelector('#carousel-tacticstyleofplay');
                if (!carousel) return 'Unknown';
                
                const wrapper = carousel.querySelector('.caroufredsel_wrapper');
                if (wrapper) {
                    const items = wrapper.querySelectorAll('.carousel-item h3');
                    for (const item of items) {
                        const rect = item.getBoundingClientRect();
                        const parentRect = wrapper.getBoundingClientRect();
                        if (rect.left >= parentRect.left && rect.right <= parentRect.right) {
                            return item.innerText.trim();
                        }
                    }
                    if (items.length > 0) return items[0].innerText.trim();
                }
                return 'Unknown';
            }
        """)
    except Exception as e:
        print(f"    ⚠️ Error extrayendo Tackling: {e}")
        tactics["tackling"] = "Unknown"
    
    # === 3. SLIDERS AVANZADOS (Pressure, Style/Mentality, Tempo) ===
    try:
        tactics["pressure"] = page.evaluate("""
            () => {
                const input = document.querySelector('input[data-bind*="tacticPressure"]');
                if (input) return parseInt(input.value) || 50;
                
                const sliders = document.querySelectorAll('.tactic-slider-input');
                for (const slider of sliders) {
                    const label = slider.closest('.panel-body')?.querySelector('h6');
                    if (label && label.innerText.toLowerCase().includes('pressure')) {
                        return parseInt(slider.value) || 50;
                    }
                }
                return 50;
            }
        """)
    except:
        tactics["pressure"] = 50
    
    try:
        tactics["mentality"] = page.evaluate("""
            () => {
                const input = document.querySelector('input[data-bind*="tacticMentality"]');
                if (input) return parseInt(input.value) || 50;
                
                const sliders = document.querySelectorAll('.tactic-slider-input');
                for (const slider of sliders) {
                    const label = slider.closest('.panel-body')?.querySelector('h6');
                    if (label && label.innerText.toLowerCase().includes('style')) {
                        return parseInt(slider.value) || 50;
                    }
                }
                return 50;
            }
        """)
    except:
        tactics["mentality"] = 50
    
    try:
        tactics["tempo"] = page.evaluate("""
            () => {
                const input = document.querySelector('input[data-bind*="tacticTempo"]');
                if (input) return parseInt(input.value) || 50;
                
                const sliders = document.querySelectorAll('.tactic-slider-input');
                for (const slider of sliders) {
                    const label = slider.closest('.panel-body')?.querySelector('h6');
                    if (label && label.innerText.toLowerCase().includes('tempo')) {
                        return parseInt(slider.value) || 50;
                    }
                }
                return 50;
            }
        """)
    except:
        tactics["tempo"] = 50
    
    # === 4. LINE TACTICS (Tácticas de Línea) ===
    
    # Forwards (Delanteros)
    try:
        tactics["forwards_tactic"] = page.evaluate("""
            () => {
                const carousel = document.querySelector('#carousel-tacticlineatt');
                if (!carousel) return 'Unknown';
                
                const wrapper = carousel.querySelector('.caroufredsel_wrapper');
                if (wrapper) {
                    const items = wrapper.querySelectorAll('.carousel-item h3');
                    for (const item of items) {
                        const rect = item.getBoundingClientRect();
                        const parentRect = wrapper.getBoundingClientRect();
                        if (rect.left >= parentRect.left && rect.right <= parentRect.right) {
                            return item.innerText.trim();
                        }
                    }
                    if (items.length > 0) return items[0].innerText.trim();
                }
                return 'Unknown';
            }
        """)
    except:
        tactics["forwards_tactic"] = "Unknown"
    
    # Midfielders (Mediocampo)
    try:
        tactics["midfielders_tactic"] = page.evaluate("""
            () => {
                const carousel = document.querySelector('#carousel-tacticlinemid');
                if (!carousel) return 'Unknown';
                
                const wrapper = carousel.querySelector('.caroufredsel_wrapper');
                if (wrapper) {
                    const items = wrapper.querySelectorAll('.carousel-item h3');
                    for (const item of items) {
                        const rect = item.getBoundingClientRect();
                        const parentRect = wrapper.getBoundingClientRect();
                        if (rect.left >= parentRect.left && rect.right <= parentRect.right) {
                            return item.innerText.trim();
                        }
                    }
                    if (items.length > 0) return items[0].innerText.trim();
                }
                return 'Unknown';
            }
        """)
    except:
        tactics["midfielders_tactic"] = "Unknown"
    
    # Defenders (Defensas)
    try:
        tactics["defenders_tactic"] = page.evaluate("""
            () => {
                const carousel = document.querySelector('#carousel-tacticlinedef');
                if (!carousel) return 'Unknown';
                
                const wrapper = carousel.querySelector('.caroufredsel_wrapper');
                if (wrapper) {
                    const items = wrapper.querySelectorAll('.carousel-item h3');
                    for (const item of items) {
                        const rect = item.getBoundingClientRect();
                        const parentRect = wrapper.getBoundingClientRect();
                        if (rect.left >= parentRect.left && rect.right <= parentRect.right) {
                            return item.innerText.trim();
                        }
                    }
                    if (items.length > 0) return items[0].innerText.trim();
                }
                return 'Unknown';
            }
        """)
    except:
        tactics["defenders_tactic"] = "Unknown"
    
    # === 5. OFFSIDE TRAP ===
    try:
        tactics["offside_trap"] = page.evaluate("""
            () => {
                const carousel = document.querySelector('#carousel-tacticoffsidetrap');
                if (!carousel) return false;
                
                const wrapper = carousel.querySelector('.caroufredsel_wrapper');
                if (wrapper) {
                    const items = wrapper.querySelectorAll('.carousel-item h3');
                    for (const item of items) {
                        const rect = item.getBoundingClientRect();
                        const parentRect = wrapper.getBoundingClientRect();
                        if (rect.left >= parentRect.left && rect.right <= parentRect.right) {
                            return item.innerText.trim().toLowerCase() === 'yes';
                        }
                    }
                    if (items.length > 0) {
                        return items[0].innerText.trim().toLowerCase() === 'yes';
                    }
                }
                return false;
            }
        """)
    except:
        tactics["offside_trap"] = False
    
    # === 6. MARKING (Marcaje) ===
    try:
        tactics["marking"] = page.evaluate("""
            () => {
                const carousel = document.querySelector('#carousel-tacticmarking');
                if (!carousel) return 'Unknown';
                
                const wrapper = carousel.querySelector('.caroufredsel_wrapper');
                if (wrapper) {
                    const items = wrapper.querySelectorAll('.carousel-item h3');
                    for (const item of items) {
                        const rect = item.getBoundingClientRect();
                        const parentRect = wrapper.getBoundingClientRect();
                        if (rect.left >= parentRect.left && rect.right <= parentRect.right) {
                            return item.innerText.trim();
                        }
                    }
                    if (items.length > 0) return items[0].innerText.trim();
                }
                return 'Unknown';
            }
        """)
    except:
        tactics["marking"] = "Unknown"
    
    return tactics


if __name__ == "__main__":
    print("Este módulo está diseñado para ser importado y recibir el objeto 'page'.")

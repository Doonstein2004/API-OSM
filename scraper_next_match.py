# scraper_next_match.py
"""
Scraper de Próximo Partido para OSM
Obtiene información sobre el próximo partido y calcula cuándo ejecutar el scraping de tácticas.
"""
import re
import time
from datetime import datetime, timedelta
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError
from utils import handle_popups, safe_int


def parse_countdown(countdown_text: str) -> int:
    """
    Parsea el texto del countdown y devuelve los segundos restantes.
    Formato esperado: "02d 06h 05m 30s" o "06h 05m 30s" o "05m 30s" o "30s"
    
    Args:
        countdown_text: Texto del contador regresivo
        
    Returns:
        int: Segundos restantes hasta el partido
    """
    if not countdown_text:
        return 0
    
    countdown_text = countdown_text.strip().lower()
    
    total_seconds = 0
    
    # Expresiones regulares para cada unidad de tiempo
    days_match = re.search(r'(\d+)d', countdown_text)
    hours_match = re.search(r'(\d+)h', countdown_text)
    mins_match = re.search(r'(\d+)m', countdown_text)
    secs_match = re.search(r'(\d+)s', countdown_text)
    
    if days_match:
        total_seconds += int(days_match.group(1)) * 86400  # 24 * 60 * 60
    if hours_match:
        total_seconds += int(hours_match.group(1)) * 3600  # 60 * 60
    if mins_match:
        total_seconds += int(mins_match.group(1)) * 60
    if secs_match:
        total_seconds += int(secs_match.group(1))
    
    return total_seconds


def get_next_match_info(page: Page):
    """
    Extrae información sobre el próximo partido de cada liga gestionada.
    NOTA: Esta función asume que ya estamos logueados y reutiliza el navegador.
    
    Returns:
        list: Lista de diccionarios con información del próximo partido por equipo/liga
    """
    print("\n--- Scraper de Próximo Partido V2.0 (Robusto) ---")
    MAIN_DASHBOARD_URL = "https://en.onlinesoccermanager.com/Career"
    
    all_next_matches = []
    NUM_SLOTS = 4

    try:
        for i in range(NUM_SLOTS):
            print(f"\n--- Slot #{i + 1}: Extrayendo info de próximo partido ---")
            
            # === NAVEGACIÓN ROBUSTA AL CAREER ===
            max_nav_retries = 3
            for nav_attempt in range(max_nav_retries):
                try:
                    # Siempre navegar al Career para refrescar el DOM
                    if not page.url.endswith("/Career"):
                        page.goto(MAIN_DASHBOARD_URL, wait_until="domcontentloaded", timeout=30000)
                    else:
                        # Si ya estamos en Career, hacer reload para refrescar slots
                        page.reload(wait_until="domcontentloaded", timeout=30000)
                    
                    # Esperar a que aparezcan los slots
                    page.wait_for_selector(".career-teamslot", timeout=20000)
                    handle_popups(page)
                    
                    # Pequeña pausa para estabilizar el DOM
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
                
                # Verificar si el slot está vacío
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
                    
                    # Re-obtener el slot fresco para evitar stale element
                    slot = page.locator(".career-teamslot").nth(i)
                    slot.click(timeout=15000)
                    
                    # Esperar a que cargue algo (puede ser Dashboard o MatchExperience)
                    page.wait_for_selector("#timers", timeout=45000)
                    handle_popups(page)
                    
                    click_success = True
                    break
                    
                except Exception as click_error:
                    print(f"  ⚠️ Error click (intento {click_attempt + 1}): {click_error}")
                    if click_attempt < 2:
                        # Volver a navegar al Career
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
            
            # === NAVEGAR EXPLÍCITAMENTE A /Dashboard ===
            # Esto es necesario porque el click en slot puede llevarnos a /MatchExperience
            DASHBOARD_URL = "https://en.onlinesoccermanager.com/Dashboard"
            try:
                if "/Dashboard" not in page.url:
                    page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_selector("#timers", timeout=15000)
                    handle_popups(page)
                    time.sleep(0.5)
            except Exception as nav_error:
                print(f"  ⚠️ Error navegando a Dashboard: {nav_error}")
                # Continuamos de todas formas, intentaremos extraer lo que podamos
            
            # === EXTRAER INFO DEL PRÓXIMO PARTIDO ===
            try:
                match_info = extract_next_match_from_dashboard(page)
                match_info["team_name"] = team_name
                match_info["league_name"] = league_name
                match_info["slot_index"] = i
                
                # Calcular timestamp para scraping de tácticas
                if match_info["seconds_remaining"] > 0:
                    # El partido comienza en X segundos, añadimos 5 minutos (300s) de margen
                    tactics_scrape_delay = match_info["seconds_remaining"] + 300
                    match_info["tactics_scrape_at"] = datetime.now() + timedelta(seconds=tactics_scrape_delay)
                    match_info["tactics_scrape_delay_seconds"] = tactics_scrape_delay
                else:
                    # El partido ya empezó o no hay countdown
                    match_info["tactics_scrape_at"] = None
                    match_info["tactics_scrape_delay_seconds"] = 0
                
                all_next_matches.append(match_info)
                print(f"  ✓ Info extraída: Jornada {match_info['matchday']}, {match_info['seconds_remaining']}s restantes")
                
            except Exception as e:
                print(f"  ⚠️ Error extrayendo info del próximo partido: {e}")
                # Aún así continuamos con el siguiente slot
                continue

    except Exception as e:
        print(f"❌ Error general en scraper de próximo partido: {e}")

    print(f"\n✅ Proceso completado. Info extraída de {len(all_next_matches)} equipos.")
    return all_next_matches


def extract_next_match_from_dashboard(page: Page) -> dict:
    """
    Extrae la información del próximo partido desde el dashboard del equipo.
    Busca en .next-match-info-container para jornada y countdown.
    Si no encuentra, hace click en #timers para revelar el dropdown.
    
    Returns:
        dict: Información del próximo partido
    """
    match_info = {
        "matchday": 0,
        "countdown_text": "",
        "seconds_remaining": 0,
        "timer_state": "unknown",
        "referee_name": None,
        "referee_strictness": None,
        "is_cup_match": False
    }
    
    # Pequeña espera para que cargue todo el contenido dinámico
    time.sleep(0.5)
    
    # === CERRAR MODALES PRIMERO ===
    handle_popups(page)
    time.sleep(0.2)
    handle_popups(page)  # Segunda pasada por si quedó alguno
    
    # === MÉTODO 1: Buscar en .next-match-info-container (Dashboard principal) ===
    try:
        # Jornada: dentro de .next-match-info-container
        matchday_selectors = [
            ".next-match-info-container .matchday-title span.text-highlight",
            ".next-match-info-container a.matchday-title span",
            ".dashboard-header-vs .matchday-title span.text-highlight",
            "a.matchday-title span.text-highlight",
            ".matchday-title span.text-highlight"
        ]
        
        for selector in matchday_selectors:
            try:
                element = page.locator(selector)
                if element.count() > 0 and element.first.is_visible(timeout=1000):
                    matchday_text = element.first.inner_text()
                    match = re.search(r'(\d+)', matchday_text)
                    if match:
                        match_info["matchday"] = int(match.group(1))
                        print(f"    ℹ️ Jornada {match_info['matchday']} obtenida del header")
                        break
            except:
                continue
        
        # Countdown: dentro de .next-match-info-container
        countdown_selectors = [
            ".next-match-info-container .next-match-timer",
            ".dashboard-header-vs .next-match-timer",
            ".next-match-timer"
        ]
        
        for selector in countdown_selectors:
            try:
                element = page.locator(selector)
                if element.count() > 0 and element.first.is_visible(timeout=1000):
                    countdown_text = element.first.inner_text()
                    seconds = parse_countdown(countdown_text)
                    if seconds > 0:
                        match_info["countdown_text"] = countdown_text
                        match_info["seconds_remaining"] = seconds
                        match_info["timer_state"] = "in_progress"
                        print(f"    ℹ️ Countdown del header: {countdown_text}")
                        break
            except:
                continue
        
        # Árbitro
        referee_selectors = [
            ".next-match-info-container .next-match-referee-name",
            ".dashboard-header-vs .next-match-referee-name"
        ]
        for selector in referee_selectors:
            try:
                element = page.locator(selector)
                if element.count() > 0 and element.first.is_visible(timeout=500):
                    match_info["referee_name"] = element.first.inner_text()
                    break
            except:
                continue
        
    except Exception as e:
        print(f"    ⚠️ Error en Método 1: {e}")
    
    # === MÉTODO 2: Si no hay countdown, intentar con el dropdown #timers ===
    if match_info["seconds_remaining"] == 0:
        try:
            # Hacer click en el botón de timers para revelar el dropdown
            timers_button = page.locator("#timers")
            if timers_button.count() > 0 and timers_button.first.is_visible(timeout=3000):
                timers_button.first.click()
                time.sleep(1)  # Esperar a que se abra el dropdown
                
                # Buscar el countdown en el dropdown .next-match-container
                next_match_container = page.locator(".next-match-container span[data-bind*='secondsRemaining']")
                if next_match_container.count() > 0:
                    try:
                        countdown_text = next_match_container.first.inner_text()
                        match_info["countdown_text"] = countdown_text
                        match_info["seconds_remaining"] = parse_countdown(countdown_text)
                        match_info["timer_state"] = "in_progress"
                        print(f"    ℹ️ Countdown obtenido del dropdown: {countdown_text}")
                    except:
                        pass
                
                # Buscar matchday en el dropdown
                matchday_dropdown = page.locator(".next-match-container .matchday-title, .dropdown-menu .matchday-title")
                if matchday_dropdown.count() > 0 and match_info["matchday"] == 0:
                    try:
                        md_text = matchday_dropdown.first.inner_text()
                        md_match = re.search(r'(\d+)', md_text)
                        if md_match:
                            match_info["matchday"] = int(md_match.group(1))
                    except:
                        pass
                
                # Cerrar el dropdown
                try:
                    page.keyboard.press("Escape")
                    time.sleep(0.3)
                except:
                    pass
                    
        except Exception as e:
            print(f"    ⚠️ Error con dropdown timers: {e}")
    
    # === MÉTODO 3: Buscar en cualquier lugar visible de la página ===
    if match_info["seconds_remaining"] == 0:
        try:
            # Buscar cualquier countdown visible
            any_timer = page.locator("[data-bind*='time:']")
            for idx in range(min(any_timer.count(), 5)):  # Revisar los primeros 5
                try:
                    timer_el = any_timer.nth(idx)
                    if timer_el.is_visible(timeout=500):
                        timer_text = timer_el.inner_text()
                        # Verificar que parece un countdown (contiene d, h, m, s)
                        if any(x in timer_text.lower() for x in ['d ', 'h ', 'm ', 's']):
                            seconds = parse_countdown(timer_text)
                            if seconds > 0:
                                match_info["countdown_text"] = timer_text
                                match_info["seconds_remaining"] = seconds
                                match_info["timer_state"] = "in_progress"
                                print(f"    ℹ️ Countdown encontrado: {timer_text}")
                                break
                except:
                    continue
        except:
            pass
    
    # === Extraer árbitro (si no se obtuvo antes) ===
    if not match_info["referee_name"]:
        try:
            referee_icon = page.locator(".next-match-referee, .icon-referee")
            if referee_icon.count() > 0:
                classes = referee_icon.first.get_attribute("class") or ""
                strictness_map = {
                    "verylenient": "Very Lenient",
                    "lenient": "Lenient", 
                    "verystrict": "Very Strict",
                    "strict": "Strict",
                    "average": "Average"
                }
                for cls, name in strictness_map.items():
                    if cls in classes.lower():
                        match_info["referee_strictness"] = name
                        break
        except:
            pass
    
    # Si timer_state sigue unknown, marcarlo como finished
    if match_info["timer_state"] == "unknown":
        match_info["timer_state"] = "finished" if match_info["seconds_remaining"] == 0 else "in_progress"
    
    return match_info


def get_minimum_tactics_delay(next_matches_info: list) -> tuple:
    """
    Dado un listado de próximos partidos, encuentra el menor tiempo de espera
    necesario para capturar las tácticas de todos los equipos.
    
    Args:
        next_matches_info: Lista de diccionarios con info de próximos partidos
        
    Returns:
        tuple: (segundos_hasta_scraping, lista_de_matchdays_afectados)
    """
    if not next_matches_info:
        return 0, []
    
    # Filtrar solo los que tienen countdown activo
    active_timers = [m for m in next_matches_info if m.get("seconds_remaining", 0) > 0]
    
    if not active_timers:
        return 0, []
    
    # Encontrar el que tenga el menor tiempo restante
    min_match = min(active_timers, key=lambda m: m["seconds_remaining"])
    
    # Delay = tiempo hasta partido + 5 minutos de margen
    delay = min_match["seconds_remaining"] + 300
    
    # Todos los partidos que ocurrirán en ese mismo momento (o antes)
    affected = [
        m for m in active_timers 
        if m["seconds_remaining"] <= min_match["seconds_remaining"] + 60  # 1 minuto de tolerancia
    ]
    
    matchdays = [(m["league_name"], m["matchday"]) for m in affected]
    
    return delay, matchdays


if __name__ == "__main__":
    print("Este módulo está diseñado para ser importado y recibir el objeto 'page'.")
    
    # Ejemplo de uso del parser de countdown
    test_cases = [
        "02d 06h 05m 30s",
        "06h 05m 30s",
        "05m 30s",
        "30s",
        "1d 0h 0m 0s"
    ]
    
    print("\nPruebas de parse_countdown:")
    for test in test_cases:
        result = parse_countdown(test)
        print(f"  '{test}' -> {result} segundos ({result // 3600}h {(result % 3600) // 60}m {result % 60}s)")

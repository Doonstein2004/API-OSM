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
    V2.1: Incluye estrategia de RELOAD (F5) si se detecta bloqueo por modales.
    """
    
    # Intentamos 2 veces: 
    # Intento 0: Extracción normal
    # Intento 1: Si falla, hacemos F5 y reintentamos (Estrategia Anti-Modal)
    for attempt in range(2):
        match_info = {
            "matchday": 0,
            "countdown_text": "",
            "seconds_remaining": 0,
            "timer_state": "unknown",
            "referee_name": None,
            "referee_strictness": None,
            "is_cup_match": False
        }
        
        # Pequeña espera para renderizado
        time.sleep(1.5)
        
        # === CERRAR MODALES ===
        handle_popups(page)
        time.sleep(0.2)
        
        # === MÉTODO 1: Buscar en .next-match-info-container (Dashboard principal) ===
        try:
            page.wait_for_selector(".next-match-info-container .matchday-title", state="visible", timeout=3000)
        except:
            pass 

        try:
            # Texto Jornada (Header)
            matchday_selectors = [
                ".next-match-info-container .matchday-title span.text-highlight",
                ".next-match-info-container a.matchday-title span",
                ".dashboard-header-vs .matchday-title span.text-highlight",
                "a.matchday-title span.text-highlight",
                ".matchday-title span.text-highlight"
            ]
            for selector in matchday_selectors:
                if page.locator(selector).count() > 0 and page.locator(selector).first.is_visible(timeout=500):
                    txt = page.locator(selector).first.inner_text()
                    m = re.search(r'(\d+)', txt)
                    if m:
                        match_info["matchday"] = int(m.group(1))
                        print(f"    ℹ️ Jornada {match_info['matchday']} obtenida del header")
                        break
            
            # Countdown (Header)
            countdown_selectors = [
                ".next-match-info-container .next-match-timer",
                ".dashboard-header-vs .next-match-timer",
                ".next-match-timer"
            ]
            for selector in countdown_selectors:
                if page.locator(selector).count() > 0 and page.locator(selector).first.is_visible(timeout=500):
                    txt = page.locator(selector).first.inner_text()
                    secs = parse_countdown(txt)
                    if secs > 0:
                        match_info["countdown_text"] = txt
                        match_info["seconds_remaining"] = secs
                        match_info["timer_state"] = "in_progress"
                        print(f"    ℹ️ Countdown del header: {txt}")
                        break
            
            # Referee
            ref_sel = [".next-match-info-container .next-match-referee-name", ".dashboard-header-vs .next-match-referee-name"]
            for s in ref_sel:
                if page.locator(s).count() > 0 and page.locator(s).first.is_visible(timeout=500):
                    match_info["referee_name"] = page.locator(s).first.inner_text()
                    break

        except Exception as e:
            print(f"    ⚠️ Error en Método 1: {e}")
        
        # === MÉTODO 2: Dropdown #timers (Si no tenemos timer aún) ===
        if match_info["seconds_remaining"] == 0:
            try:
                timers_btn = page.locator("#timers")
                if timers_btn.count() > 0:
                    # Intento de click con timeout corto
                    try:
                        timers_btn.first.click(timeout=3000)
                    except Exception:
                        # Si falla el click, probablemente hay un MODAL
                        # Si estamos en el primer intento, lanzamos error para provocar el RELOAD
                        if attempt == 0:
                            print("    ⚠️ Bloqueo detectado al hacer click en timers (posible modal).")
                            raise Exception("ModalBlockingError")
                        else:
                            pass # Si ya recargamos y sigue fallando, desistimos
                    
                    # Si el click funcionó:
                    time.sleep(1)
                    
                    # Buscar en dropdown
                    nm_cont = page.locator(".next-match-container span[data-bind*='secondsRemaining']")
                    if nm_cont.count() > 0:
                        txt = nm_cont.first.inner_text()
                        match_info["countdown_text"] = txt
                        match_info["seconds_remaining"] = parse_countdown(txt)
                        match_info["timer_state"] = "in_progress"
                        print(f"    ℹ️ Countdown obtenido del dropdown: {txt}")
                    
                    # Matchday en dropdown
                    if match_info["matchday"] == 0:
                        md_drp = page.locator(".next-match-container .matchday-title, .dropdown-menu .matchday-title")
                        if md_drp.count() > 0:
                            txt = md_drp.first.inner_text()
                            m = re.search(r'(\d+)', txt)
                            if m: match_info["matchday"] = int(m.group(1))

                    # Cerrar dropdown
                    try: page.keyboard.press("Escape")
                    except: pass

            except Exception as e:
                # Si fue el error provocado "ModalBlockingError", el bloque except externo lo maneja (o el if attempt logic)
                # Como estamos dentro del `try` de timers, manejamos la logica de retry aqui
                if "ModalBlockingError" in str(e):
                    print("    🔄 Aplicando Reload (F5) para limpiar modales...")
                    page.reload(wait_until="domcontentloaded")
                    handle_popups(page)
                    continue # Siguiente intento del for
                print(f"    ⚠️ Error con dropdown timers: {e}")
        
        # === MÉTODO 3: Fallback cualquier texto timer (solo si seguimos sin info) ===
        if match_info["seconds_remaining"] == 0:
            try:
                any_timer = page.locator("[data-bind*='time:']")
                for idx in range(min(any_timer.count(), 3)):
                    t_el = any_timer.nth(idx)
                    if t_el.is_visible(timeout=500):
                        txt = t_el.inner_text()
                        if any(x in txt.lower() for x in ['d ', 'h ', 'm ', 's']):
                            match_info["seconds_remaining"] = parse_countdown(txt)
                            if match_info["seconds_remaining"] > 0:
                                match_info["timer_state"] = "in_progress"
                                print(f"    ℹ️ Countdown encontrado (fallback): {txt}")
                                break
            except: pass

        # Si llegamos aquí y tenemos datos o se acabaron los intentos, retornamos
        # Árbitro Strictness Check
        if not match_info["referee_strictness"]:
            try:
                icon = page.locator(".next-match-referee, .icon-referee").first
                if icon.count() > 0:
                    cls = icon.get_attribute("class") or ""
                    if "verylenient" in cls.lower(): match_info["referee_strictness"] = "Very Lenient"
                    elif "lenient" in cls.lower(): match_info["referee_strictness"] = "Lenient"
                    elif "verystrict" in cls.lower(): match_info["referee_strictness"] = "Very Strict"
                    elif "strict" in cls.lower(): match_info["referee_strictness"] = "Strict"
                    elif "average" in cls.lower(): match_info["referee_strictness"] = "Average"
            except: pass
        
        if match_info["timer_state"] == "unknown":
            match_info["timer_state"] = "finished" if match_info["seconds_remaining"] == 0 else "in_progress"
            
        return match_info
            
    return match_info # Should not reach here typically due to return inside loop



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

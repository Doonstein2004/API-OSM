# scraper_market_data.py
import time
from playwright.sync_api import Page, expect, TimeoutError, Error as PlaywrightError
from utils import handle_popups, safe_int

def parse_price(price_text):
    if not isinstance(price_text, str): return 0
    value_str = price_text.lower().strip().replace(',', '')
    if 'm' in value_str: return float(value_str.replace('m', ''))
    if 'k' in value_str: return float(value_str.replace('k', '')) / 1000
    try: return float(value_str)
    except (ValueError, TypeError): return 0

def get_market_data(page: Page):
    """
    Extrae TANTO la lista de transferencias en venta COMO el historial de fichajes
    en un solo pase por cada slot.
    """
    print("\n--- Iniciando scraper unificado de mercado ---")
    MAIN_DASHBOARD_URL = "https://en.onlinesoccermanager.com/Career"
    TRANSFERS_URL = "https://en.onlinesoccermanager.com/Transferlist"
    
    all_teams_transfer_list = []
    all_teams_transfer_history = []

    try:
        NUM_SLOTS = 4
        for i in range(NUM_SLOTS):
            print(f"\n--- Analizando Slot de Mercado #{i + 1} ---")
            
            if not page.url.endswith("/Career"):
                page.goto(MAIN_DASHBOARD_URL, wait_until="domcontentloaded")
            
            page.locator(".career-teamslot").first.wait_for(state="visible", timeout=60000)
            slot = page.locator(".career-teamslot").nth(i)

            if slot.locator("h2.clubslot-main-title").count() == 0:
                print(f"Slot #{i + 1} está vacío. Saltando.")
                continue

            team_name = slot.locator("h2.clubslot-main-title").inner_text()
            
            MAX_RETRIES = 3
            for attempt in range(MAX_RETRIES):
                try:
                    print(f"Procesando mercado para: {team_name} (Intento {attempt + 1})")
                    
                    with page.expect_navigation(wait_until="domcontentloaded", timeout=60000):
                        slot.click()
                    
                    print(f"  - Entrando a la sección de transferencias...")
                    page.goto(TRANSFERS_URL, wait_until="domcontentloaded", timeout=60000)
                    handle_popups(page)

                    # --- PARTE 1: EXTRAER LISTA DE JUGADORES EN VENTA ---
                    print("  - Extrayendo jugadores en venta...")
                    transfer_list_container = page.locator("#transfer-list-players")
                    expect(transfer_list_container).to_be_visible(timeout=60000)
                    
                    players_on_sale = []
                    player_tables = transfer_list_container.locator("table.table-sticky")
                    for table in player_tables.all():
                        rows = table.locator("tbody tr.clickable")
                        for row in rows.all():
                            try:
                                players_on_sale.append({
                                    "name": row.locator("td:nth-child(1) > span.semi-bold").inner_text(),
                                    "position": row.locator("td:nth-child(3)").inner_text(),
                                    "age": safe_int(row.locator("td:nth-child(4)").inner_text()),
                                    "seller_team": row.locator("td:nth-child(5) a.ellipsis, td:nth-child(5) span.ellipsis").first.inner_text(),
                                    "seller_manager": row.locator("td:nth-child(5) span.text-italic").inner_text() if row.locator("td:nth-child(5) span.text-italic").count() > 0 else "CPU",
                                    "attack": safe_int(row.locator("td:nth-child(6)").inner_text()),
                                    "defense": safe_int(row.locator("td:nth-child(7)").inner_text()),
                                    "overall": safe_int(row.locator("td:nth-child(8)").inner_text()),
                                    "price": parse_price(row.locator("td.td-price span[data-bind*='currency']").inner_text())
                                })
                            except Exception as e:
                                print(f"    - ADVERTENCIA: Saltando fila de jugador en venta. Error: {e}")
                    
                    all_teams_transfer_list.append({"team_name": team_name, "players_on_sale": players_on_sale})
                    print(f"  ✓ {len(players_on_sale)} jugadores en venta extraídos.")

                    # --- PARTE 2: EXTRAER HISTORIAL DE FICHAJES ---
                    print("  - Cambiando a la pestaña de historial...")
                    page.locator("a[href='#transfer-history']").click()
                    history_table = page.locator("#transfer-history table.table")
                    expect(history_table).to_be_visible(timeout=30000)
                    print("  - Contenido inicial del historial visible.")
                    
                    
                    # --- INICIO DE LA LÓGICA DE CARGA MEJORADA ---
                    print("  - Cargando todos los registros...")
                    more_button = page.locator('button:has-text("More transfers")')
                    
                    while more_button.is_visible(timeout=5000):
                        old_row_count = history_table.locator("tbody tr").count()
                        more_button.click()
                        
                        # Bucle de espera manual
                        # Intentaremos hasta 10 veces (20 segundos en total) a que las filas aumenten
                        wait_success = False
                        for _ in range(10): # 10 reintentos
                            time.sleep(2) # Espera 2 segundos entre cada comprobación
                            new_row_count = history_table.locator("tbody tr").count()
                            if new_row_count > old_row_count:
                                print(f"    - Cargadas {new_row_count - old_row_count} filas más (Total: {new_row_count})")
                                wait_success = True
                                break # Salir del bucle de espera si las filas aumentaron
                        
                        if not wait_success:
                            print("    - El botón 'More transfers' fue presionado pero no cargó más filas después de 20 segundos. Asumiendo que se ha cargado todo.")
                            break # Salir del bucle principal 'while'

                    print("  - Todos los registros cargados.")
                    # --- FIN DE LA LÓGICA DE CARGA MEJORADA ---
                    

                    print("  - Extrayendo datos del historial...")
                    transfers_list = page.evaluate("""
                        () => {
                            const rows = Array.from(document.querySelectorAll("#transfer-history table.table tbody tr"));
                            return rows.map(row => {
                                const tds = row.querySelectorAll("td");
                                // La estructura de columnas real es diferente a la de la otra tabla
                                return {
                                    Name: tds[0]?.innerText.trim() || "N/A", From: tds[1]?.innerText.trim() || "N/A",
                                    To: tds[2]?.innerText.trim() || "N/A", Position: tds[3]?.innerText.trim() || "N/A",
                                    Gameweek: tds[4]?.innerText.trim() || "N/A", Value: tds[5]?.innerText.trim() || "N/A",
                                    Price: tds[6]?.innerText.trim() || "N/A", Date: tds[7]?.innerText.trim() || "N/A"
                                };
                            });
                        }
                    """)

                    all_teams_transfer_history.append({"team_name": team_name, "transfers": transfers_list})
                    print(f"  - ¡ÉXITO! Se extrajeron {len(transfers_list)} fichajes del historial para {team_name}.")
                    break # Salir del bucle de reintento si todo fue exitoso

                except (TimeoutError, PlaywrightError) as e:
                    print(f"  - ERROR en el intento {attempt + 1}: {e}")
                    if attempt < MAX_RETRIES - 1:
                        print("    -> Volviendo al dashboard para reintentar...")
                        page.goto(MAIN_DASHBOARD_URL)
                    else:
                        print(f"  - Todos los reintentos para '{team_name}' han fallado.")
            
        return all_teams_transfer_list, all_teams_transfer_history

    except Exception as e:
        print(f"❌ Error crítico en get_market_data: {e}")
        return [{"error": str(e)}], [{"error": str(e)}]
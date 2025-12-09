# scraper_market_data.py
import time
from playwright.sync_api import Page, expect, TimeoutError
from utils import handle_popups, safe_int

def parse_price(price_text):
    if not isinstance(price_text, str): return 0
    value_str = price_text.lower().strip().replace(',', '.')
    if 'm' in value_str: return float(value_str.replace('m', ''))
    if 'k' in value_str: return float(value_str.replace('k', '')) / 1000
    try: return float(value_str)
    except (ValueError, TypeError): return 0

def get_market_data(page: Page):
    print("\n--- Iniciando scraper unificado de mercado (V3 - Fix Visibilidad) ---")
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
            
            try:
                page.locator(".career-teamslot").first.wait_for(state="visible", timeout=10000)
            except TimeoutError:
                print("No se encontraron slots de carrera visibles.")
                break

            slot = page.locator(".career-teamslot").nth(i)

            if slot.locator("h2.clubslot-main-title").count() == 0:
                print("Slot vacío. Saltando.")
                continue

            team_name = slot.locator("h2.clubslot-main-title").inner_text()
            league_name_on_dashboard = slot.locator("h4.display-name").inner_text()
            
            MAX_RETRIES = 2
            for attempt in range(MAX_RETRIES):
                try:
                    print(f"Procesando: {team_name}...")
                    
                    with page.expect_navigation(wait_until="domcontentloaded", timeout=60000):
                        slot.click()
                    
                    page.goto(TRANSFERS_URL, wait_until="load", timeout=90000)
                    handle_popups(page)

                    # --- PARTE 1: JUGADORES EN VENTA ---
                    print("  - Extrayendo lista de venta...")
                    try:
                        page.wait_for_selector("#transfer-list table.table-sticky", timeout=30000)
                    except TimeoutError:
                         print("  ⚠️ No se encontró la tabla. Posiblemente vacía.")
                    
                    players_on_sale = []
                    # Selector más específico para evitar cabeceras
                    rows = page.locator("#transfer-list table.table-sticky tbody tr.clickable")
                    count = rows.count()
                    print(f"  - Se encontraron {count} jugadores. Extrayendo valores...")

                    for k in range(count):
                        row = page.locator("#transfer-list table.table-sticky tbody tr.clickable").nth(k)
                        
                        try:
                            # Extracción de datos básicos (sin abrir modal aún)
                            name_el = row.locator("td").nth(0).locator("span.semi-bold")
                            name = name_el.inner_text()
                            pos = row.locator("td").nth(2).inner_text()
                            age = safe_int(row.locator("td").nth(3).inner_text())
                            
                            # Equipo y Manager
                            team_td = row.locator("td").nth(4)
                            seller_team = team_td.inner_text()
                            seller_manager = "CPU"
                            mgr_span = team_td.locator("span.text-italic")
                            if mgr_span.count() > 0:
                                seller_manager = mgr_span.inner_text()
                                seller_team = seller_team.replace(seller_manager, "").strip()

                            att = safe_int(row.locator("td").nth(5).inner_text())
                            def_ = safe_int(row.locator("td").nth(6).inner_text())
                            ovr = safe_int(row.locator("td").nth(7).inner_text())
                            price = parse_price(row.locator("td.td-price").inner_text())
                            
                            nat_loc = row.locator("td").nth(0).locator("span.flag-icon")
                            nationality = nat_loc.get_attribute("title") if nat_loc.count() > 0 else "N/A"

                            # --- CLICK PARA VALOR BASE ---
                            row.scroll_into_view_if_needed()
                            
                            # Intentamos abrir el modal
                            try:
                                name_el.click(force=True, timeout=2000)
                            except:
                                row.click(force=True)
                            
                            # --- CORRECCIÓN CRÍTICA AQUÍ ---
                            # Buscamos SOLO el contenedor que esté visible (:visible).
                            # Esto ignora los modales "fantasma" de jugadores anteriores.
                            val_locator = page.locator("div.player-profile-value:visible span[data-bind*='currency']").first
                            
                            try:
                                val_locator.wait_for(state="visible", timeout=4000)
                                value_text = val_locator.inner_text()
                                base_value = parse_price(value_text)
                            except TimeoutError:
                                # Último intento: a veces el texto está pero no se detecta 'visible' por opacidad
                                if val_locator.count() > 0:
                                     base_value = parse_price(val_locator.inner_text())
                                else:
                                    print(f"    ⚠️ No se pudo leer valor base para {name}")
                                    base_value = 0

                            # Cerrar modal
                            close_btn = page.locator("div.close-large[aria-label='Close']").first
                            if close_btn.is_visible():
                                close_btn.click()
                            else:
                                page.keyboard.press("Escape")
                            
                            # Esperar brevemente que la UI reaccione
                            page.wait_for_timeout(300) 

                            players_on_sale.append({
                                "name": name,
                                "nationality": nationality,
                                "position": pos,
                                "age": age,
                                "seller_team": seller_team,
                                "seller_manager": seller_manager,
                                "attack": att,
                                "defense": def_,
                                "overall": ovr,
                                "price": price,
                                "value": base_value 
                            })

                        except Exception as e:
                            # print(f"    ⚠️ Error fila {k}: {e}")
                            page.keyboard.press("Escape")
                            continue

                    all_teams_transfer_list.append({
                        "team_name": team_name, 
                        "league_name": league_name_on_dashboard, 
                        "players_on_sale": players_on_sale
                    })
                    print(f"  ✓ {len(players_on_sale)} jugadores procesados.")

                    # --- PARTE 2: HISTORIAL ---
                    print("  - Extrayendo historial...")
                    page.evaluate("window.scrollTo(0, 0)")
                    
                    history_tab = page.locator("a[href='#transfer-history']")
                    if history_tab.is_visible():
                        history_tab.click()
                        
                        try:
                            expect(page.locator("#transfer-history table.table")).to_be_visible(timeout=10000)
                        except:
                            print("  ⚠️ Tabla de historial no cargó.")
                        
                        # Cargar más
                        more_btn = page.locator('button:has-text("More transfers")')
                        clicks = 0
                        while more_btn.is_visible(timeout=2000) and clicks < 20:
                            try:
                                more_btn.click()
                                time.sleep(0.5) 
                                clicks += 1
                            except:
                                break
                        
                        transfers_list = page.evaluate("""
                            () => {
                                const rows = Array.from(document.querySelectorAll("#transfer-history table.table tbody tr"));
                                return rows.map(row => {
                                    const tds = row.querySelectorAll("td");
                                    if (tds.length < 8) return null;
                                    return {
                                        Name: tds[0].innerText.trim(), 
                                        From: tds[1].innerText.trim(),
                                        To: tds[2].innerText.trim(), 
                                        Position: tds[3].innerText.trim(),
                                        Gameweek: tds[4].innerText.trim(), 
                                        Value: tds[5].innerText.trim(),
                                        Price: tds[6].innerText.trim(), 
                                        Date: tds[7].innerText.trim()
                                    };
                                }).filter(t => t !== null);
                            }
                        """)
                        
                        all_teams_transfer_history.append({
                            "team_name": team_name, 
                            "league_name": league_name_on_dashboard, 
                            "transfers": transfers_list
                        })
                        print(f"  ✓ {len(transfers_list)} fichajes en historial.")
                    else:
                        print("  ⚠️ Pestaña historial no visible.")

                    break 

                except Exception as e:
                    print(f"  ❌ Error intento {attempt+1}: {e}")
                    page.goto(MAIN_DASHBOARD_URL, wait_until="domcontentloaded")
            
        return all_teams_transfer_list, all_teams_transfer_history

    except Exception as e:
        print(f"❌ Error crítico en get_market_data: {e}")
        return [], []
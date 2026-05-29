# scraper_market_data.py
import time
from playwright.sync_api import Page, expect, TimeoutError
from utils import handle_popups, safe_int, safe_navigate

def parse_price(price_text):
    if not isinstance(price_text, str): return 0
    
    value_str = price_text.lower().strip().replace(',', '.')
    
    # Buscamos el multiplicador
    if 'm' in value_str:
        multiplier = 1.0  # Ya está en millones
        value_str = value_str.replace('m', '')
    elif 'k' in value_str:
        multiplier = 0.001  # Mil a Millones (1/1000)
        value_str = value_str.replace('k', '')
    else:
        # Si no tiene letra, asumimos que es el valor entero y lo pasamos a millones
        multiplier = 0.000001 
    
    # Limpiar caracteres no numéricos
    value_str = ''.join(filter(lambda x: x.isdigit() or x == '.', value_str))
    
    try:
        # Devolvemos el valor en unidades de "Millón" con 2 decimales
        return round(float(value_str) * multiplier, 2)
    except:
        return 0

def get_market_data(page: Page):
    print("\n--- Scraper de Mercado V10 (Robust Extraction) ---")
    MAIN_DASHBOARD_URL = "https://en.onlinesoccermanager.com/Career"
    TRANSFERS_URL = "https://en.onlinesoccermanager.com/Transferlist"
    
    all_teams_transfer_list = []
    all_teams_transfer_history = []

    try:
        NUM_SLOTS = 4
        for i in range(NUM_SLOTS):
            print(f"\n--- Slot #{i + 1} ---")
            if not page.url.endswith("/Career"):
                page.goto(MAIN_DASHBOARD_URL, wait_until="domcontentloaded")
            
            from utils import wait_for_visible_slots
            if not wait_for_visible_slots(page, timeout=20000):
                break

            slot = page.locator(".career-teamslot").nth(i)
            
            from utils import get_slot_info
            team_name, league_name, _ = get_slot_info(slot)
            
            if not team_name:
                print(f"Slot #{i + 1} no es procesable (Searching/Unavailable/Empty). Saltando.")
                continue
                
            print(f"Procesando: {team_name} en {league_name}")

            # Hacer clic en el slot para activar ese equipo de forma robusta
            from utils import click_slot_and_wait_for_dashboard
            if not click_slot_and_wait_for_dashboard(page, i):
                print(f"  ❌ No se pudo activar el slot {i+1}. Saltando.")
                continue

            
            # --- EXTRACCIÓN CON FALLBACK ---
            print("  - Extrayendo datos de jugadores...")
            try:
                
                if safe_navigate(page, TRANSFERS_URL, verify_selector="#transfer-list"):
                    # B. ESPERA INTELIGENTE DE DATOS
                    print("  - Esperando renderizado de jugadores...")
                    try:
                        page.wait_for_selector("#transfer-list table.table-sticky tbody tr.clickable", state="visible", timeout=15000)
                        time.sleep(2) 
                    except TimeoutError:
                        print("    ⚠️ Tiempo de espera agotado: La tabla sigue vacía (¿Mercado vacío o fallo de carga?).")
                    
                    
                    # JS mejorado para encontrar los datos sin importar la estructura exacta
                    players_on_sale_raw = page.evaluate("""
                        () => {
                            const rows = Array.from(document.querySelectorAll("#transfer-list table.table-sticky tbody tr.clickable"));
                            return rows.map(row => {
                                const data = ko.dataFor(row);
                                if (!data) return null;
                                
                                const player = data.playerPartial ? data.playerPartial() : null;
                                if (!player) return null;
                                
                                const cols = row.querySelectorAll("td");
                                
                                // FALLBACK PARA EL PRECIO: 
                                let price = 0;
                                if (typeof data.price === 'function') price = data.price();
                                else if (data.price) price = data.price;
                                else {
                                    const priceText = cols[cols.length - 1].innerText;
                                    price = priceText.replace(/[^0-9.]/g, ''); // Limpieza básica
                                }

                                return {
                                    name: player.name || "N/A",
                                    nationality: (player.nationality && player.nationality.name) ? player.nationality.name : "N/A",
                                    position: cols[2] ? cols[2].innerText.trim() : "N/A",
                                    age: player.age || 0,
                                    seller_team: data.teamPartial ? data.teamPartial().name : "CPU",
                                    seller_manager: (data.teamPartial && data.teamPartial().managerPartial()) 
                                                    ? data.teamPartial().managerPartial().name 
                                                    : "CPU",
                                    attack: player.statAtt || 0,
                                    defense: player.statDef || 0,
                                    overall: player.statOvr || 0,
                                    price_val: price,
                                    value_val: player.value || 0
                                };
                            }).filter(p => p !== null);
                        }
                    """)
                    
                    # Procesamiento en Python (Millones)
                    players_on_sale = []
                    for p in players_on_sale_raw:
                        try:
                            raw_p = str(p.get('price_val', 0)).lower()
                            raw_v = str(p.get('value_val', 0)).lower()
                            
                            def to_million(val_str):
                                clean = ''.join(filter(lambda x: x.isdigit() or x == '.' or x == ',', val_str)).replace(',', '.')
                                if not clean: return 0.0
                                num = float(clean)
                                if num > 10000: return round(num / 1_000_000, 2)
                                return round(num, 2)

                            p['price'] = to_million(raw_p)
                            p['value'] = to_million(raw_v)
                            
                            del p['price_val']
                            del p['value_val']
                            players_on_sale.append(p)
                        except: continue

                    all_teams_transfer_list.append({
                        "team_name": team_name, 
                        "league_name": league_name, 
                        "players_on_sale": players_on_sale
                    })
                    print(f"  ✓ {len(players_on_sale)} jugadores extraídos.")

            except Exception as e:
                print(f"  ⚠️ Error extrayendo mercado: {e}")

            # --- HISTORIAL (Misma lógica de extracción rápida) ---
            print("  - Historial...")
            history_tab = page.locator("a[href='#transfer-history']")
            if history_tab.is_visible():
                history_tab.click()
                try:
                    page.wait_for_selector("#transfer-history table.table", timeout=10000)
                    
                    # Cargar historial exhaustivo con límite de seguridad e inicio de espera inteligente y dinámica
                    max_clicks = 150
                    clicks_done = 0
                    
                    while clicks_done < max_clicks:
                        btn = page.locator('button:has-text("More transfers")')
                        
                        if btn.is_visible(timeout=1000):
                            if btn.is_disabled():
                                print("    ℹ️ El botón 'More transfers' está deshabilitado. Historial completo.")
                                break
                            
                            try:
                                # Obtener el conteo de filas actual antes del click
                                old_count = page.locator("#transfer-history table.table tbody tr").count()
                                
                                # Hacer click con timeout bajo
                                btn.click(timeout=3000)
                                
                                # Esperar dinámicamente (hasta 5s) a que el conteo de filas aumente
                                page.wait_for_function(
                                    f"document.querySelectorAll('#transfer-history table.table tbody tr').length > {old_count}",
                                    timeout=5000
                                )
                                clicks_done += 1
                            except Exception as wait_err:
                                print(f"    ℹ️ Finalizada la carga de historial (no se detectaron más filas nuevas): {wait_err}")
                                break
                        else:
                            # El botón ya no es visible, se cargó todo el historial
                            break
                            
                    if clicks_done == max_clicks:
                        print("    ⚠️ Se alcanzó el límite máximo de clicks en el historial.")

                    hist_list = page.evaluate("""() => {
                        const rows = Array.from(document.querySelectorAll("#transfer-history table.table tbody tr"));
                        return rows.map(r => {
                            const c = r.querySelectorAll("td");
                            if(c.length < 8) return null;
                            return {
                                Name: c[0].innerText.trim(), From: c[1].innerText.trim(),
                                To: c[2].innerText.trim(), Position: c[3].innerText.trim(),
                                Gameweek: c[4].innerText.trim(), Value: c[5].innerText.trim(),
                                Price: c[6].innerText.trim(), Date: c[7].innerText.trim()
                            };
                        }).filter(x => x);
                    }""")
                    all_teams_transfer_history.append({
                        "team_name": team_name, "league_name": league_name, "transfers": hist_list
                    })
                except: pass

    except Exception as e:
        print(f"❌ Error crítico mercado: {e}")

    return all_teams_transfer_list, all_teams_transfer_history

# scraper_market_data.py
import time
from playwright.sync_api import Page, expect, TimeoutError
from utils import handle_popups, safe_int

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
            
            try:
                page.wait_for_selector(".career-teamslot", timeout=10000)
            except: break

            slot = page.locator(".career-teamslot").nth(i)
            if slot.locator("h2.clubslot-main-title").count() == 0: continue

            team_name = slot.locator("h2.clubslot-main-title").inner_text()
            league_name = slot.locator("h4.display-name").inner_text()
            print(f"Procesando: {team_name}")
            
            slot.click()
            page.wait_for_selector("#timers", timeout=60000)
            handle_popups(page)

            page.goto(TRANSFERS_URL, wait_until="load")
            handle_popups(page)

            # --- EXTRACCIÓN CON FALLBACK ---
            print("  - Extrayendo datos de jugadores...")
            try:
                table_selector = "#transfer-list table.table-sticky tbody tr.clickable"
                page.wait_for_selector(table_selector, timeout=20000)
                
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
                            // 1. Intentamos data.price (observable)
                            // 2. Intentamos leer el texto de la última columna
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
                        # Limpiamos y convertimos a millones
                        # Si price_val ya es un número grande (ej 78600000), dividimos
                        # Si es un string como "78.6", lo tratamos como tal
                        raw_p = str(p.get('price_val', 0)).lower()
                        raw_v = str(p.get('value_val', 0)).lower()
                        
                        def to_million(val_str):
                            clean = ''.join(filter(lambda x: x.isdigit() or x == '.' or x == ',', val_str)).replace(',', '.')
                            if not clean: return 0.0
                            num = float(clean)
                            # Si el número es mayor a 10000, asumimos que viene en unidades completas (ej. 15000000)
                            if num > 10000: return round(num / 1_000_000, 2)
                            return round(num, 2)

                        p['price'] = to_million(raw_p)
                        p['value'] = to_million(raw_v)
                        
                        # Debug preventivo
                        # print(f"    [CHECK] {p['name']}: {p['price']}M | {p['value']}M")
                        
                        # Limpiar campos temporales
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
                    # Cargar más un par de veces
                    for _ in range(3):
                        btn = page.locator('button:has-text("More transfers")')
                        if btn.is_visible(timeout=500): 
                            btn.click()
                            time.sleep(0.5)

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
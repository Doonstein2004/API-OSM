# run_update_from_user.py
import json
import sys
import os
import psycopg2
import psycopg2.extras
import time
from collections import defaultdict
from datetime import datetime
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

# --- Módulos Locales ---
from utils import login_to_osm, InvalidCredentialsError 
from notifications import init_firebase_admin, analyze_and_notify

# --- Importar las funciones de los scrapers ---
from scraper_league_details import get_league_data
from scraper_market_data import get_market_data
from scraper_match_results import get_match_results

# --- CONFIGURACIÓN ---
load_dotenv()
LEAGUES_TO_IGNORE = ["Africa 2024", "All Stars Battle League", "Americas Cup 2019", "Americas Cup 2024", "Asia 2024", "Boss Tournament", "Club History A", "Club History B", "Club Stars", "Community League M", "Community League S", "Europe 2024", "Knockout Royale", "World 2002"]

DB_CONFIG = {
    "host": os.getenv("DB_HOST"),
    "port": os.getenv("DB_PORT"),
    "dbname": os.getenv("DB_NAME"),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD")
}

# ==========================================
# 1. FUNCIONES AUXILIARES BÁSICAS
# ==========================================

def get_db_connection(max_retries=3):
    conn_args = {
        **DB_CONFIG,
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 5
    }
    for attempt in range(max_retries):
        try:
            conn = psycopg2.connect(**conn_args)
            conn.cursor_factory = psycopg2.extras.DictCursor
            return conn
        except psycopg2.OperationalError as e:
            print(f"⚠️ Error conectando a DB (Intento {attempt+1}/{max_retries}): {e}")
            time.sleep(5)
    print("❌ Error Fatal: No se pudo conectar a la base de datos.")
    return None

def parse_value_string(value_str):
    if not isinstance(value_str, str): return 0
    value_str = value_str.lower().strip().replace(',', '')
    if 'm' in value_str: return float(value_str.replace('m', ''))
    if 'k' in value_str: return float(value_str.replace('k', '')) / 1000
    try: return float(value_str) / 1_000_000
    except: return 0

def normalize_team_name(name):
    if not isinstance(name, str): return ""
    prefixes = ["fk ", "ca ", "fc ", "cd "]
    normalized = name.lower().strip()
    for prefix in prefixes:
        if normalized.startswith(prefix): normalized = normalized[len(prefix):]
    return normalized

def create_league_maps(all_leagues_data):
    team_to_leagues = defaultdict(list)
    league_to_teams = defaultdict(set)
    for league in all_leagues_data:
        league_name = league.get("league_name")
        for club in league.get("clubs", []):
            norm = normalize_team_name(club["name"])
            team_to_leagues[norm].append(league_name)
            league_to_teams[league_name].add(norm)
    return dict(team_to_leagues), dict(league_to_teams)

def get_leagues_for_mapping(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT ON (name) name AS league_name, teams FROM leagues;")
        results = []
        for row in cur.fetchall():
            league_dict = dict(row)
            teams_data = league_dict.get('teams')
            if isinstance(teams_data, str):
                try: league_dict['clubs'] = json.loads(teams_data)
                except json.JSONDecodeError: league_dict['clubs'] = []
            else:
                league_dict['clubs'] = teams_data or []
            if 'teams' in league_dict: del league_dict['teams']
            results.append(league_dict)
        return results

# ==========================================
# 2. LÓGICA DE RESOLUCIÓN Y SINCRONIZACIÓN
# ==========================================

def resolve_active_leagues(fichajes_data, all_leagues_data, league_details_data, leagues_to_ignore):
    print("\n[3.1] 🧠 Resolviendo ligas activas (Estrategia de Índices)...")
    
    active_leagues_list = []
    team_to_leagues_master, league_to_teams_master = create_league_maps(all_leagues_data)
    
    # Iteramos usando el índice original para mantener el rastreo exacto de cada slot
    for idx, league_info in enumerate(league_details_data):
        dashboard_name = league_info.get("league_name")
        managed_team = league_info.get("team_name")
        
        current_competitors = {normalize_team_name(t['Club']) for t in league_info.get("standings", [])}
        norm_my_team = normalize_team_name(managed_team)
        candidates = team_to_leagues_master.get(norm_my_team, [])
        candidates = [c for c in candidates if c not in leagues_to_ignore]
        
        best_match = dashboard_name 

        if len(candidates) == 1:
            best_match = candidates[0]
            print(f"  - [{idx}] '{dashboard_name}' -> '{best_match}' (Equipo único)")
        else:
            scores = {}
            search_space = candidates if candidates else league_to_teams_master.keys()
            for off_name in search_space:
                if off_name in leagues_to_ignore: continue
                off_teams = league_to_teams_master.get(off_name, set())
                scores[off_name] = len(current_competitors.intersection(off_teams))
            
            if scores:
                winner = max(scores, key=scores.get)
                if scores[winner] > len(current_competitors) * 0.3:
                    best_match = winner
                    print(f"  - [{idx}] '{dashboard_name}' -> '{best_match}' (Match competidores)")
        
        # Guardamos el índice original para mapear datos después
        active_leagues_list.append({
            "dashboard_name": dashboard_name,
            "managed_team": managed_team,
            "official_name": best_match,
            "data_index": idx 
        })

    return active_leagues_list

def find_matching_active_league(conn, user_id, official_name, current_managers_set, excluded_ids=None):
    if excluded_ids is None: excluded_ids = set()

    with conn.cursor() as cur:
        sql = """
            SELECT l.id, ul.managers_by_team 
            FROM user_leagues ul
            JOIN leagues l ON ul.league_id = l.id
            WHERE ul.user_id = %s AND l.name = %s AND ul.is_active = TRUE;
        """
        cur.execute(sql, (user_id, official_name))
        candidates = cur.fetchall()
        
        best_id = None
        best_ratio = 0.0
        
        for row in candidates:
            if row['id'] in excluded_ids: continue # Saltar IDs ya usados

            saved_mgrs = set((row['managers_by_team'] or {}).values())
            if not saved_mgrs:
                # Priorizar liga vacía si no hay match
                if best_id is None: best_id = row['id']
                continue
            
            common = current_managers_set.intersection(saved_mgrs)
            ratio = len(common) / len(saved_mgrs)
            
            if ratio > best_ratio:
                best_ratio = ratio
                best_id = row['id']
        
        if best_id:
            if best_ratio > 0.30: return best_id
            if best_ratio == 0 and len(candidates) == 1 and candidates[0]['id'] not in excluded_ids: return best_id

        return None

def sync_leagues_smart(conn, active_leagues_list, all_leagues_data, user_id, standings_data):
    print("\n🔄 Sincronizando IDs de ligas...")
    
    processed_leagues = []
    confirmed_ids = set()

    for item in active_leagues_list:
        dash_name = item["dashboard_name"]
        off_name = item["official_name"]
        idx = item["data_index"]
        
        # Recuperar datos usando el índice
        ls_data = standings_data[idx]
        
        curr_mgrs = set()
        for t in ls_data.get("standings", []):
            m = t.get("Manager", "N/A")
            if m and m != "N/A": curr_mgrs.add(m)
        
        matched_id = find_matching_active_league(conn, user_id, dash_name, curr_mgrs, confirmed_ids)
        
        final_id = None
        
        if matched_id:
            print(f"    ✅ [{idx}] Liga existente ID {matched_id}.")
            final_id = matched_id
            with conn.cursor() as cur:
                cur.execute("UPDATE leagues SET name = %s WHERE id = %s", (dash_name, final_id))
                cur.execute("UPDATE user_leagues SET last_scraped_at = NOW() WHERE user_id = %s AND league_id = %s", (user_id, final_id))
            conn.commit()
        else:
            print(f"    ✨ [{idx}] Creando NUEVA instancia para '{dash_name}'...")
            
            league_info_db = next((l for l in all_leagues_data if l.get('league_name') == off_name), None)
            raw_clubs = league_info_db.get("clubs", []) if league_info_db else []
            
            if not raw_clubs:
                raw_clubs = [{"name": t["Club"], "initialValue": 0} for t in ls_data.get("standings", [])]

            teams_db = []
            for c in raw_clubs:
                c_name = c.get("name") or c.get("Club")
                c_val = c.get("initialValue") or parse_value_string(c.get("squad_value", "0"))
                teams_db.append({"name": c_name, "initialValue": c_val, "fixedIncomePerRound": 0})

            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO leagues (name, teams) VALUES (%s, %s) RETURNING id;",
                    (dash_name, json.dumps(teams_db))
                )
                final_id = cur.fetchone()['id']
                cur.execute(
                    "INSERT INTO user_leagues (user_id, league_id, is_active, last_scraped_at) VALUES (%s, %s, TRUE, NOW())",
                    (user_id, final_id)
                )
            conn.commit()

        confirmed_ids.add(final_id)
        
        # Agregamos el ID al objeto y lo guardamos en la lista final
        item["league_id"] = final_id
        processed_leagues.append(item)

    # Limpieza
    with conn.cursor() as cur:
        cur.execute("SELECT league_id FROM user_leagues WHERE user_id = %s AND is_active = TRUE", (user_id,))
        active_db_ids = {row['league_id'] for row in cur.fetchall()}
        ids_to_deactivate = active_db_ids - confirmed_ids
        if ids_to_deactivate:
            print(f"    ❄️ Archivando ligas no detectadas: {ids_to_deactivate}")
            cur.execute("UPDATE user_leagues SET is_active = FALSE WHERE user_id = %s AND league_id IN %s", (user_id, tuple(ids_to_deactivate)))
            conn.commit()

    return processed_leagues

# ==========================================
# 3. PROCESAMIENTO Y CARGA DE DATOS (POR ÍNDICE)
# ==========================================

def sync_league_details(conn, standings_data, squad_values_data, processed_leagues, user_id):
    print("\n🔄 Sincronizando detalles...")
    with conn.cursor() as cur:
        for item in processed_leagues:
            idx = item["data_index"]
            league_id = item["league_id"]
            
            ls = standings_data[idx]
            lv = squad_values_data[idx] if idx < len(squad_values_data) else None

            standings = ls.get("standings", [])
            mgrs = {t["Club"]: t.get("Manager") for t in standings if t.get("Manager") and t.get("Manager") != "N/A"}
            
            squad_vals = lv.get("squad_values_ranking", []) if lv else []

            sql = "UPDATE user_leagues SET standings=%s, squad_values=%s, managers_by_team=%s WHERE user_id=%s AND league_id=%s"
            cur.execute(sql, (json.dumps(standings), json.dumps(squad_vals), json.dumps(mgrs), user_id, league_id))
    conn.commit()

def translate_and_group_transfers(fichajes_data, processed_leagues):
    grouped = defaultdict(list)
    processed_keys = set()

    for item in processed_leagues:
        idx = item["data_index"]
        league_id = item["league_id"]
        
        if idx >= len(fichajes_data): continue
        team_block = fichajes_data[idx] # Acceso directo por Slot

        for transfer in team_block.get("transfers", []):
            try:
                t_key = (transfer.get("Name"), transfer.get("From"), transfer.get("To"), transfer.get("Price"), league_id)
                if t_key in processed_keys: continue
                processed_keys.add(t_key)

                from_parts = transfer.get("From", "").split('\n')
                to_parts = transfer.get("To", "").split('\n')
                from_mgr = from_parts[1].strip() if len(from_parts) > 1 else None
                to_mgr = to_parts[1].strip() if len(to_parts) > 1 else None

                ttype = 'purchase' if to_mgr else 'sale'
                main_mgr = to_mgr if to_mgr else from_mgr

                grouped[league_id].append({
                    "playerName": transfer.get("Name"),
                    "managerName": main_mgr,
                    "seller_manager": from_mgr,
                    "buyer_manager": to_mgr,
                    "from_text": transfer.get("From", ""), 
                    "to_text": transfer.get("To", ""),
                    "transactionType": ttype,
                    "position": transfer.get("Position"),
                    "round": int(transfer.get("Gameweek", 0)),
                    "baseValue": parse_value_string(transfer.get("Value")),
                    "finalPrice": parse_value_string(transfer.get("Price")),
                    "createdAt": datetime.now()
                })
            except: continue
    return dict(grouped)

def sync_transfer_list(conn, transfer_list_data, processed_leagues, user_id, ts):
    print(f"  - Sincronizando mercado...")
    with conn.cursor() as cur:
        for item in processed_leagues:
            idx = item["data_index"]
            league_id = item["league_id"]
            
            if idx >= len(transfer_list_data): continue
            players = transfer_list_data[idx].get("players_on_sale", [])

            # Archivar viejos solo para ESTA liga
            cur.execute("UPDATE public.transfer_list_players SET is_active = FALSE WHERE user_id=%s AND league_id=%s AND is_active=TRUE", (user_id, league_id))
            
            if not players: continue

            unique = {}
            for p in players: unique[(p['name'], p['seller_manager'])] = p
            
            data = [
                (user_id, league_id, p['name'], p['seller_manager'], p.get('nationality', 'N/A'),
                 p['position'], p['age'], p['seller_team'], p['attack'], p['defense'], p['overall'], 
                 p['price'], p.get('value', 0), ts, ts, True) 
                for p in unique.values()
            ]

            sql = """
                INSERT INTO public.transfer_list_players (
                    user_id, league_id, name, seller_manager, nationality, position, age, 
                    seller_team, attack, defense, overall, price, base_value, 
                    scrape_id, scraped_at, is_active
                ) VALUES %s
                ON CONFLICT (user_id, league_id, name, seller_manager)
                DO UPDATE SET
                    price=EXCLUDED.price, base_value=EXCLUDED.base_value, 
                    scraped_at=EXCLUDED.scraped_at, is_active=TRUE;
            """
            psycopg2.extras.execute_values(cur, sql, data)
            print(f"    - Liga ID {league_id}: {len(data)} en venta.")
    conn.commit()

def sync_matches(conn, matches_data, processed_leagues, user_id):
    print("\n⚽ Sincronizando resultados de partidos...")
    with conn.cursor() as cur:
        for item in processed_leagues:
            idx = item["data_index"]
            league_id = item["league_id"]
            
            if idx >= len(matches_data): continue
            
            matches_info = matches_data[idx]
            if not matches_info or "matches" not in matches_info: continue

            data_tuples = []
            for m in matches_info["matches"]:
                data_tuples.append((
                    user_id, league_id, m['round'], m['home_team'], m['home_manager'], 
                    m['away_team'], m['away_manager'], m['home_goals'], m['away_goals'], 
                    json.dumps(m['events']), json.dumps(m['statistics']), json.dumps(m['ratings'])
                ))
            
            if data_tuples:
                sql = """
                    INSERT INTO public.matches (
                        user_id, league_id, round, home_team, home_manager, away_team, away_manager,
                        home_goals, away_goals, events, statistics, ratings
                    ) VALUES %s
                    ON CONFLICT (league_id, round, home_team, away_team) 
                    DO UPDATE SET
                        home_manager = EXCLUDED.home_manager, away_manager = EXCLUDED.away_manager,
                        home_goals = EXCLUDED.home_goals, away_goals = EXCLUDED.away_goals,
                        events = EXCLUDED.events, statistics = EXCLUDED.statistics, ratings = EXCLUDED.ratings;
                """
                psycopg2.extras.execute_values(cur, sql, data_tuples)
                print(f"  - Liga ID {league_id}: {len(data_tuples)} partidos.")
                
    conn.commit()

# ==========================================
# 4. SEGURIDAD Y ORQUESTACIÓN
# ==========================================

def get_osm_credentials(conn, user_id):
    print(f"  - Obteniendo credenciales para el usuario ID: {user_id}...")
    with conn.cursor() as cur:
        cur.execute("SELECT osm_username, osm_password, fcm_token FROM public.get_credentials_for_user(%s);", (user_id,))
        creds = cur.fetchone()
        if not creds or not creds['osm_username'] or not creds['osm_password']:
            return None, None, None
        return creds['osm_username'], creds['osm_password'], creds['fcm_token']

def invalidate_user_credentials(conn, user_id):
    print(f"⛔ CREDENCIALES INCORRECTAS DETECTADAS. Invalidando usuario {user_id}...")
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE users SET osm_username = NULL, last_scrape_triggered_at = NULL WHERE id = %s
            """, (user_id,))
        conn.commit()
        print("✅ Credenciales eliminadas.")
    except Exception as e:
        print(f"❌ Error al invalidar credenciales: {e}")

def upload_data_to_postgres(conn, grouped_transfers, user_id):
    # grouped_transfers ahora es un dict {league_id: [transfers]} gracias a translate_and_group_transfers
    print("\n📦 Sincronizando fichajes...")
    with conn.cursor() as cur:
        for league_id, transfers in grouped_transfers.items():
            if not transfers: continue
            
            unique_batch = {}
            for t in transfers:
                key = (user_id, league_id, t['round'], t['playerName'], t['managerName'], t['finalPrice'])
                unique_batch[key] = t
            
            data = [
                (user_id, league_id, t['playerName'], t['managerName'], t['transactionType'],
                 t['position'], t['round'], t['baseValue'], t['finalPrice'], t['createdAt'],
                 t['seller_manager'], t['buyer_manager'], t['from_text'], t['to_text']) 
                for t in unique_batch.values()
            ]
            
            sql = """
                INSERT INTO transfers (user_id, league_id, player_name, manager_name, transaction_type, position, round, base_value, final_price, created_at, seller_manager, buyer_manager, from_text, to_text) 
                VALUES %s ON CONFLICT (user_id, league_id, round, player_name, manager_name, final_price) 
                DO UPDATE SET seller_manager=EXCLUDED.seller_manager, buyer_manager=EXCLUDED.buyer_manager;
            """
            psycopg2.extras.execute_values(cur, sql, data, page_size=200)
            print(f"  - Liga ID {league_id}: {len(data)} fichajes.")
    conn.commit()

def run_update_for_user(user_id):
    print(f"🚀 Iniciando actualización para usuario: {user_id}")
    
    conn = get_db_connection()
    if not conn: return
    
    # 1. Credenciales
    try:
        osm_username, osm_password, user_fcm_token = get_osm_credentials(conn, user_id)
        if not osm_username:
            print("⚠️ Sin credenciales.")
            conn.close(); return
    except Exception as e:
        print(f"? Error DB: {e}")
        conn.close(); return

    # 2. Scraping
    try:
        scrape_timestamp = datetime.now() 
        with sync_playwright() as p:
            is_gha = os.getenv("GITHUB_ACTIONS") == "true"
            browser = p.chromium.launch(headless=True if is_gha else False, args=["--no-sandbox"])
            context = browser.new_context(viewport={'width': 1280, 'height': 720})
            page = context.new_page()
            
            try:
                if not login_to_osm(page, osm_username, osm_password): raise Exception("Login fallido")
            except InvalidCredentialsError:
                print("❌ ERROR FATAL: Credenciales incorrectas.")
                invalidate_user_credentials(conn, user_id)
                conn.close(); return

            print("\n[1/3] 📡 Scraping...")
            transfer_list_data, fichajes_data = get_market_data(page)
            standings_data, squad_values_data = get_league_data(page)
            matches_data = get_match_results(page)
            print("✅ Scraping OK.")
    except Exception as e:
        print(f"❌ Error scraping: {e}")
        return

    # 3. Sincronización
    print("\n[2/3] 💾 Sincronizando BD...")
    max_retries = 3
    
    for attempt in range(max_retries):
        conn = None
        try:
            conn = get_db_connection()
            if not conn: raise Exception("Error conexión")

            # A. Resolver Ligas
            all_leagues_db = get_leagues_for_mapping(conn)
            processed_leagues = resolve_active_leagues(fichajes_data, all_leagues_db, standings_data, LEAGUES_TO_IGNORE)
            
            if not processed_leagues:
                print("ℹ️ No hay ligas.")
                return

            # B. Sync IDs
            processed_leagues = sync_leagues_smart(conn, processed_leagues, all_leagues_db, user_id, standings_data)
            
            # C. Detalles
            sync_league_details(conn, standings_data, squad_values_data, processed_leagues, user_id)
            
            # D. Fichajes
            grouped = translate_and_group_transfers(fichajes_data, processed_leagues)
            upload_data_to_postgres(conn, grouped, user_id)
            
            # E. Mercado
            sync_transfer_list(conn, transfer_list_data, processed_leagues, user_id, scrape_timestamp)
            
            # F. Partidos
            if matches_data:
                sync_matches(conn, matches_data, processed_leagues, user_id)

            # 4. Notificaciones
            print("\n🔔 Notificaciones...")
            flat_transfers = [t for sublist in grouped.values() for t in sublist]
            analyze_and_notify(user_fcm_token, transfer_list_data, flat_transfers, osm_username)

            print("\n✨ FIN.")
            break 

        except Exception as e:
            print(f"❌ Error sync (Intento {attempt+1}): {e}")
            if attempt < max_retries - 1: time.sleep(10)
            else: 
                import traceback
                traceback.print_exc()
        finally:
            if conn: conn.close()

if __name__ == "__main__":
    init_firebase_admin()
    if len(sys.argv) > 1: run_update_for_user(sys.argv[1])
    else: print("ERROR: Falta user_id.")
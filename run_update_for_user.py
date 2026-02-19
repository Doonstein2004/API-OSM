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
from scraper_tactics import get_tactics_data
from scraper_next_match import get_next_match_info

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

def find_matching_active_league(conn, user_id, dashboard_name, current_managers_set, excluded_ids=None):
    """
    Busca una liga existente en TODA la BD (no solo del usuario) que coincida con el fingerprint de managers.
    Retorna (league_id, needs_new_link) donde needs_new_link indica si hay que vincular al usuario.
    """
    if excluded_ids is None: excluded_ids = set()

    with conn.cursor() as cur:
        # BÚSQUEDA GLOBAL: Busca en TODAS las ligas con ese nombre (de cualquier usuario)
        sql = """
            SELECT DISTINCT l.id, ul.managers_by_team 
            FROM leagues l
            JOIN user_leagues ul ON ul.league_id = l.id
            WHERE l.name = %s AND ul.is_active = TRUE;
        """
        cur.execute(sql, (dashboard_name,))
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
            match_found = best_ratio > 0.70 or (best_ratio == 0 and len(candidates) == 1 and candidates[0]['id'] not in excluded_ids)
            if match_found:
                # Verificar si el usuario ya está vinculado a esta liga
                cur.execute("SELECT 1 FROM user_leagues WHERE user_id = %s AND league_id = %s", (user_id, best_id))
                user_already_linked = cur.fetchone() is not None
                return best_id, not user_already_linked  # (id, needs_new_link)

        return None, False

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
        
        matched_id, needs_link = find_matching_active_league(conn, user_id, dash_name, curr_mgrs, confirmed_ids)
        
        final_id = None
        
        if matched_id:
            print(f"    ✅ [{idx}] Liga existente ID {matched_id}." + (" (Vinculando usuario)" if needs_link else ""))
            final_id = matched_id
            with conn.cursor() as cur:
                if needs_link:
                    # Usuario nuevo en esta liga global, crear vínculo
                    cur.execute("""
                        INSERT INTO user_leagues (user_id, league_id, is_active, last_scraped_at)
                        VALUES (%s, %s, TRUE, NOW())
                        ON CONFLICT (user_id, league_id) DO UPDATE SET is_active = TRUE, last_scraped_at = NOW()
                    """, (user_id, final_id))
                cur.execute("UPDATE leagues SET name = %s WHERE id = %s", (dash_name, final_id))
                # Actualizar last_scraped_at para TODOS los usuarios de esta liga
                cur.execute("UPDATE user_leagues SET last_scraped_at = NOW() WHERE league_id = %s", (final_id,))
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
    """Sincroniza detalles de liga para TODOS los usuarios vinculados."""
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

            # Actualizar para TODOS los usuarios vinculados a esta liga (no solo el actual)
            sql = "UPDATE user_leagues SET standings=%s, squad_values=%s, managers_by_team=%s WHERE league_id=%s"
            cur.execute(sql, (json.dumps(standings), json.dumps(squad_vals), json.dumps(mgrs), league_id))
    conn.commit()

def translate_and_group_transfers(fichajes_data, processed_leagues):
    grouped = defaultdict(list)
    processed_keys = set()

    for item in processed_leagues:
        idx = item["data_index"]
        league_id = item["league_id"]
        managed_team = item.get("managed_team", "")
        
        if idx >= len(fichajes_data): continue
        team_block = fichajes_data[idx] # Acceso directo por Slot
        
        # VALIDACIÓN CRÍTICA: Verificar que el bloque de fichajes corresponde al equipo correcto
        block_team_name = team_block.get("team_name", "")
        if block_team_name and managed_team:
            # Normalizar para comparación (minúsculas y sin espacios extra)
            if normalize_team_name(block_team_name) != normalize_team_name(managed_team):
                print(f"    ⚠️ Mismatch detectado: fichajes de '{block_team_name}' no coinciden con liga de '{managed_team}'. Saltando.")
                continue

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
    # Transfer list son datos compartidos de la liga
    print(f"  - Sincronizando mercado...")
    with conn.cursor() as cur:
        for item in processed_leagues:
            idx = item["data_index"]
            league_id = item["league_id"]
            
            if idx >= len(transfer_list_data): continue
            players = transfer_list_data[idx].get("players_on_sale", [])

            # Archivar viejos solo para ESTA liga
            cur.execute("UPDATE public.transfer_list_players SET is_active = FALSE WHERE league_id=%s AND is_active=TRUE", (league_id,))
            
            if not players: continue

            unique = {}
            for p in players: unique[(p['name'], p['seller_manager'])] = p
            
            data = [
                (league_id, p['name'], p['seller_manager'], p.get('nationality', 'N/A'),
                 p['position'], p['age'], p['seller_team'], p['attack'], p['defense'], p['overall'], 
                 p['price'], p.get('value', 0), ts, ts, True) 
                for p in unique.values()
            ]

            sql = """
                INSERT INTO public.transfer_list_players (
                    league_id, name, seller_manager, nationality, position, age, 
                    seller_team, attack, defense, overall, price, base_value, 
                    scrape_id, scraped_at, is_active
                ) VALUES %s
                ON CONFLICT (league_id, name, seller_manager)
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
                # FIX: Invertir orden de tarjetas visitantes (Away Cards) para corregir visualización en App
                # El usuario reporta que las amarillas salen como rojas. Invertimos "X Y" a "Y X".
                if 'statistics' in m and 'Cards' in m['statistics'] and 'away' in m['statistics']['Cards']:
                    val = str(m['statistics']['Cards']['away']).strip()
                    parts = val.split()
                    if len(parts) == 2:
                        m['statistics']['Cards']['away'] = f"{parts[1]} {parts[0]}"

                data_tuples.append((
                    user_id, league_id, m['round'], m['home_team'], m['home_manager'], 
                    m['away_team'], m['away_manager'], m['home_goals'], m['away_goals'], 
                    json.dumps(m['events']), json.dumps(m['statistics']), json.dumps(m['ratings']),
                    m.get('referee'), m.get('referee_strictness')
                ))
            
            if data_tuples:
                # Deduplicate to avoid CardinalityViolation (ON CONFLICT constraint)
                # Key: (league_id, round, home_team, away_team)
                seen = set()
                unique_tuples = []
                for dt in data_tuples:
                    # dt structure: (user_id, league_id, round, home, h_mgr, away, a_mgr, ...)
                    # Indices: league_id=1, round=2, home=3, away=5
                    key = (dt[1], dt[2], dt[3], dt[5])
                    if key not in seen:
                        seen.add(key)
                        unique_tuples.append(dt)

                sql = """
                    INSERT INTO public.matches (
                        user_id, league_id, round, home_team, home_manager, away_team, away_manager,
                        home_goals, away_goals, events, statistics, ratings, referee, referee_strictness
                    ) VALUES %s
                    ON CONFLICT (league_id, round, home_team, away_team) 
                    DO UPDATE SET
                        user_id = EXCLUDED.user_id,
                        home_manager = EXCLUDED.home_manager, away_manager = EXCLUDED.away_manager,
                        home_goals = EXCLUDED.home_goals, away_goals = EXCLUDED.away_goals,
                        events = EXCLUDED.events, statistics = EXCLUDED.statistics, ratings = EXCLUDED.ratings,
                        referee = EXCLUDED.referee, referee_strictness = EXCLUDED.referee_strictness;
                """
                psycopg2.extras.execute_values(cur, sql, unique_tuples)
                print(f"  - Liga ID {league_id}: {len(unique_tuples)} partidos.")
                
    conn.commit()

def ensure_tactics_table_exists(conn):
    """Auto-migration: Create match_tactics table if it doesn't exist."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT to_regclass('public.match_tactics');
        """)
        if cur.fetchone()[0] is None:
            print("🔧 Migrando BD: Creando tabla 'match_tactics'...")
            cur.execute("""
                CREATE TABLE public.match_tactics (
                    id SERIAL PRIMARY KEY,
                    user_id UUID NOT NULL,
                    league_id INTEGER NOT NULL REFERENCES leagues(id),
                    round INTEGER NOT NULL,
                    team_name VARCHAR(255) NOT NULL,
                    
                    -- Tácticas básicas
                    game_plan VARCHAR(50),
                    tackling VARCHAR(50),
                    
                    -- Sliders (0-100)
                    pressure INTEGER,
                    mentality INTEGER,
                    tempo INTEGER,
                    
                    -- Tácticas de línea
                    forwards_tactic VARCHAR(50),
                    midfielders_tactic VARCHAR(50),
                    defenders_tactic VARCHAR(50),
                    
                    -- Configuración adicional
                    offside_trap BOOLEAN DEFAULT FALSE,
                    marking VARCHAR(50),
                    
                    -- Metadatos
                    scraped_at TIMESTAMP DEFAULT NOW(),
                    
                    -- Constraint para evitar duplicados
                    CONSTRAINT unique_match_tactics UNIQUE (league_id, round, team_name)
                );
                
                CREATE INDEX idx_tactics_league_round ON match_tactics(league_id, round);
                CREATE INDEX idx_tactics_user ON match_tactics(user_id);
            """)
            conn.commit()
            print("✅ Tabla 'match_tactics' creada correctamente.")

def sync_tactics(conn, tactics_data, processed_leagues, user_id, current_round_map):
    """
    Sincroniza las tácticas extraídas en la base de datos.
    
    Args:
        conn: Conexión a la base de datos
        tactics_data: Lista de diccionarios con tácticas por equipo
        processed_leagues: Lista de ligas procesadas con sus IDs
        user_id: ID del usuario
        current_round_map: Diccionario {league_name: round} con la jornada actual de cada liga
    """
    print("\n🎯 Sincronizando tácticas...")
    ensure_tactics_table_exists(conn)
    
    with conn.cursor() as cur:
        for item in processed_leagues:
            idx = item["data_index"]
            league_id = item["league_id"]
            team_name = item.get("managed_team", "")
            league_name = item.get("dashboard_name", "")
            
            if idx >= len(tactics_data):
                continue
            
            tdata = tactics_data[idx]
            
            # Obtener la jornada actual para esta liga
            current_round = current_round_map.get(league_name, 0)
            if current_round == 0:
                print(f"  ⚠️ No se pudo determinar la jornada para {league_name}. Saltando.")
                continue
            
            # Preparar los datos para inserción
            data_tuple = (
                str(user_id),
                league_id,
                current_round,
                team_name,
                tdata.get("game_plan", "Unknown"),
                tdata.get("tackling", "Unknown"),
                tdata.get("pressure", 50),
                tdata.get("mentality", 50),
                tdata.get("tempo", 50),
                tdata.get("forwards_tactic", "Unknown"),
                tdata.get("midfielders_tactic", "Unknown"),
                tdata.get("defenders_tactic", "Unknown"),
                tdata.get("offside_trap", False),
                tdata.get("marking", "Unknown"),
            )
            
            sql = """
                    INSERT INTO public.match_tactics (
                    user_id, league_id, round, team_name,
                    game_plan, tackling, pressure, mentality, tempo,
                    forwards_tactic, midfielders_tactic, defenders_tactic,
                    offside_trap, marking, scraped_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (league_id, round, team_name)
                DO UPDATE SET
                    user_id = EXCLUDED.user_id,
                    game_plan = EXCLUDED.game_plan,
                    tackling = EXCLUDED.tackling,
                    pressure = EXCLUDED.pressure,
                    mentality = EXCLUDED.mentality,
                    tempo = EXCLUDED.tempo,
                    forwards_tactic = EXCLUDED.forwards_tactic,
                    midfielders_tactic = EXCLUDED.midfielders_tactic,
                    defenders_tactic = EXCLUDED.defenders_tactic,
                    offside_trap = EXCLUDED.offside_trap,
                    marking = EXCLUDED.marking,
                    scraped_at = NOW();
            """
            cur.execute(sql, data_tuple)
            print(f"  ✓ Tácticas guardadas: {team_name} (Jornada {current_round})")
    
    conn.commit()

def ensure_scheduled_tasks_table_exists(conn):
    """Auto-migration: Create scheduled_scrape_tasks table if it doesn't exist."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT to_regclass('public.scheduled_scrape_tasks');
        """)
        if cur.fetchone()[0] is None:
            print("🔧 Migrando BD: Creando tabla 'scheduled_scrape_tasks'...")
            cur.execute("""
                CREATE TABLE public.scheduled_scrape_tasks (
                    id SERIAL PRIMARY KEY,
                    user_id UUID NOT NULL,
                    task_type VARCHAR(50) NOT NULL,
                    scheduled_at TIMESTAMP NOT NULL,
                    status VARCHAR(20) DEFAULT 'pending',
                    metadata JSONB,
                    created_at TIMESTAMP DEFAULT NOW(),
                    executed_at TIMESTAMP,
                    
                    CONSTRAINT unique_pending_task UNIQUE (user_id, task_type, scheduled_at)
                );
                
                CREATE INDEX idx_scheduled_tasks_pending ON scheduled_scrape_tasks(scheduled_at) 
                    WHERE status = 'pending';
                CREATE INDEX idx_scheduled_tasks_user ON scheduled_scrape_tasks(user_id);
            """)
            conn.commit()
            print("✅ Tabla 'scheduled_scrape_tasks' creada correctamente.")

def schedule_tactics_scrape(conn, user_id, next_match_info, processed_leagues):
    """
    Programa tareas de scraping de tácticas basándose en los próximos partidos.
    
    Args:
        conn: Conexión a la base de datos
        user_id: ID del usuario
        next_match_info: Lista con info de próximos partidos (incluye countdown)
        processed_leagues: Lista de ligas procesadas
    """
    print("\n📅 Programando scraping de tácticas...")
    ensure_scheduled_tasks_table_exists(conn)
    
    # Crear un mapa de league_name -> league_id
    league_id_map = {item["dashboard_name"]: item["league_id"] for item in processed_leagues}
    
    scheduled_count = 0
    
    with conn.cursor() as cur:
        for match_info in next_match_info:
            # Solo programar si hay tiempo restante (partido no ha empezado)
            if match_info.get("seconds_remaining", 0) <= 0:
                continue
            
            league_name = match_info.get("league_name")
            league_id = league_id_map.get(league_name)
            
            if not league_id:
                continue
            
            # Calcular cuándo hacer el scraping (5 minutos después de que empiece el partido)
            scheduled_at = match_info.get("tactics_scrape_at")
            if not scheduled_at:
                continue
            
            # Preparar metadata
            metadata = json.dumps({
                "league_id": league_id,
                "league_name": league_name,
                "team_name": match_info.get("team_name"),
                "matchday": match_info.get("matchday"),
                "slot_index": match_info.get("slot_index")
            })
            
            # Insertar o actualizar la tarea programada
            sql = """
                INSERT INTO public.scheduled_scrape_tasks (
                    user_id, task_type, scheduled_at, status, metadata
                ) VALUES (%s, 'tactics_scrape', %s, 'pending', %s)
                ON CONFLICT (user_id, task_type, scheduled_at)
                DO NOTHING;
            """
            cur.execute(sql, (str(user_id), scheduled_at, metadata))
            
            if cur.rowcount > 0:
                scheduled_count += 1
                print(f"  📌 Programado: {league_name} Jornada {match_info.get('matchday')} -> {scheduled_at.strftime('%Y-%m-%d %H:%M:%S')}")
    
    conn.commit()
    
    if scheduled_count > 0:
        print(f"✅ {scheduled_count} tareas de tácticas programadas.")
    else:
        print("ℹ️ No hay nuevas tareas de tácticas para programar.")

def get_pending_tactics_tasks(conn, user_id):
    """
    Obtiene las tareas de scraping de tácticas pendientes que ya deberían ejecutarse.
    
    Returns:
        list: Lista de tareas pendientes con sus metadatos
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, metadata, scheduled_at
            FROM public.scheduled_scrape_tasks
            WHERE user_id = %s 
              AND task_type = 'tactics_scrape'
              AND status = 'pending'
              AND scheduled_at <= NOW()
            ORDER BY scheduled_at;
        """, (str(user_id),))
        
        tasks = []
        for row in cur.fetchall():
            task = {
                "id": row['id'],
                "metadata": row['metadata'] if isinstance(row['metadata'], dict) else json.loads(row['metadata'] or '{}'),
                "scheduled_at": row['scheduled_at']
            }
            tasks.append(task)
        
        return tasks

def mark_tactics_task_complete(conn, task_id):
    """Marca una tarea de scraping de tácticas como completada."""
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE public.scheduled_scrape_tasks
            SET status = 'completed', executed_at = NOW()
            WHERE id = %s;
        """, (task_id,))
    conn.commit()

# ==========================================
# 4. SEGURIDAD Y ORQUESTACIÓN
# ==========================================

def ensure_calendar_column_exists(conn):
    """Auto-migration: Ensure user_leagues table has calendar_scraped column."""
    with conn.cursor() as cur:
        # Check if column exists
        cur.execute("""
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name='user_leagues' AND column_name='calendar_scraped';
        """)
        if not cur.fetchone():
            print("🔧 Migrando BD: Agregando columna 'calendar_scraped'...")
            cur.execute("ALTER TABLE user_leagues ADD COLUMN calendar_scraped BOOLEAN DEFAULT FALSE;")
            conn.commit()

def ensure_matches_columns_exist(conn):
    """Auto-migration: Ensure matches table has referee columns."""
    with conn.cursor() as cur:
        # 1. Check referee
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='matches' AND column_name='referee';")
        if not cur.fetchone():
            print("🔧 Migrando BD: Agregando columna 'referee'...")
            cur.execute("ALTER TABLE matches ADD COLUMN referee VARCHAR(255);")
        
        # 2. Check strictness
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='matches' AND column_name='referee_strictness';")
        if not cur.fetchone():
            print("🔧 Migrando BD: Agregando columna 'referee_strictness'...")
            cur.execute("ALTER TABLE matches ADD COLUMN referee_strictness VARCHAR(50);")
        conn.commit()

def check_if_calendar_needed(conn, user_id):
    """
    Returns True if any active league for the user has not scraped the calendar yet.
    """
    ensure_calendar_column_exists(conn)
    ensure_matches_columns_exist(conn) # Ensure matches schema is ready too
    with conn.cursor() as cur:
        cur.execute("""
            SELECT 1 FROM user_leagues 
            WHERE user_id = %s AND is_active = TRUE AND calendar_scraped = FALSE 
            LIMIT 1;
        """, (user_id,))
        return cur.fetchone() is not None

def mark_calendar_as_scraped(conn, user_id, processed_leagues):
    """Mark synced leagues as calendar_scraped = True"""
    if not processed_leagues: return
    
    league_ids = [item['league_id'] for item in processed_leagues]
    if not league_ids: return

    with conn.cursor() as cur:
        cur.execute("UPDATE user_leagues SET calendar_scraped = TRUE WHERE user_id = %s AND league_id IN %s", (user_id, tuple(league_ids)))
    conn.commit()
    print("📅 Calendario marcado como sincronizado para estas ligas.")

def get_osm_credentials(conn, user_id):
    print(f"  - Obteniendo credenciales para el usuario ID: {user_id}...")
    with conn.cursor() as cur:
        cur.execute("SELECT osm_username, osm_password, fcm_token FROM public.get_credentials_for_user(%s);", (user_id,))
        creds = cur.fetchone()
        if not creds or not creds['osm_username'] or not creds['osm_password']:
            return None, None, None
        return creds['osm_username'].strip(), creds['osm_password'].strip(), creds['fcm_token']

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
    # Transfers son datos compartidos de la liga, no necesitan user_id
    print("\n📦 Sincronizando fichajes...")
    with conn.cursor() as cur:
        for league_id, transfers in grouped_transfers.items():
            if not transfers: continue
            
            unique_batch = {}
            for t in transfers:
                key = (league_id, t['round'], t['playerName'], t['managerName'], t['finalPrice'])
                unique_batch[key] = t
            
            data = [
                (league_id, t['playerName'], t['managerName'], t['transactionType'],
                 t['position'], t['round'], t['baseValue'], t['finalPrice'], t['createdAt'],
                 t['seller_manager'], t['buyer_manager'], t['from_text'], t['to_text']) 
                for t in unique_batch.values()
            ]
            
            sql = """
                INSERT INTO transfers (league_id, player_name, manager_name, transaction_type, position, round, base_value, final_price, created_at, seller_manager, buyer_manager, from_text, to_text) 
                VALUES %s ON CONFLICT (league_id, round, player_name, manager_name, final_price) 
                DO UPDATE SET seller_manager=EXCLUDED.seller_manager, buyer_manager=EXCLUDED.buyer_manager;
            """
            psycopg2.extras.execute_values(cur, sql, data, page_size=200)
            print(f"  - Liga ID {league_id}: {len(data)} fichajes.")
    conn.commit()

def run_update_for_user(user_id):
    print(f"🚀 Iniciando actualización para usuario: {user_id}")
    
    conn = get_db_connection()
    if not conn: return
    
    # 0. Determinar si necesitamos Calendario (Auto-detection)
    try:
        needs_calendar = check_if_calendar_needed(conn, user_id)
        if needs_calendar:
            print("📅 DETECTADO: Ligas nuevas/pendientes. Se activará el escaneo de calendario.")
        else:
            print("⏩ Calendario al día. Se escanearán solo resultados recientes.")
    except Exception as e:
        print(f"⚠️ Error verificando estado calendario: {e}")
        needs_calendar = False

    # 1. Credenciales
    try:
        osm_username, osm_password, user_fcm_token = get_osm_credentials(conn, user_id)
        if not osm_username:
            print("⚠️ Sin credenciales.")
            conn.close(); return
    except Exception as e:
        print(f"? Error DB: {e}")
        conn.close(); return

    # Variables para almacenar datos scrapeados
    transfer_list_data = []
    fichajes_data = []
    standings_data = []
    squad_values_data = []
    matches_data = []
    tactics_data = []
    next_match_info = []

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

            print("\n[1/4] 📡 Scraping datos principales...")
            transfer_list_data, fichajes_data = get_market_data(page)
            standings_data, squad_values_data = get_league_data(page)
            matches_data = get_match_results(page, scrape_future_fixtures=needs_calendar)
            
            print("\n[2/4] ⏱️ Obteniendo info de próximos partidos...")
            next_match_info = get_next_match_info(page)
            
            print("\n[3/4] 🎯 Scraping tácticas actuales...")
            tactics_data = get_tactics_data(page)
            
            print("✅ Scraping OK.")
            
    except Exception as e:
        print(f"❌ Error scraping: {e}")
        import traceback
        traceback.print_exc()
        return

    # 3. Sincronización
    print("\n[4/4] 💾 Sincronizando BD...")
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
                if needs_calendar:
                    mark_calendar_as_scraped(conn, user_id, processed_leagues)
            
            # G. Tácticas
            if tactics_data:
                # Crear mapa de jornadas actuales desde MÚLTIPLES FUENTES
                # Prioridad: 1) next_match_info, 2) standings (partidos jugados), 3) matches (max round)
                current_round_map = {}
                
                # Fuente 1: next_match_info (si tiene countdown activo, es la próxima jornada)
                for m in (next_match_info or []):
                    if m.get("matchday", 0) > 0:
                        current_round_map[m["league_name"]] = m["matchday"]
                
                # Fuente 2: standings_data (usar partidos jugados como jornada actual)
                for idx, item in enumerate(processed_leagues):
                    league_name = item.get("dashboard_name", "")
                    if league_name not in current_round_map or current_round_map[league_name] == 0:
                        if idx < len(standings_data):
                            standings = standings_data[idx].get("standings", [])
                            if standings:
                                # Jornada actual = max de partidos jugados en la clasificación
                                max_played = max((s.get("Played", 0) for s in standings), default=0)
                                if max_played > 0:
                                    current_round_map[league_name] = max_played
                                    print(f"    ℹ️ Jornada para '{league_name}' obtenida de standings: {max_played}")
                
                # Fuente 3: matches_data (max round de los partidos)
                for idx, item in enumerate(processed_leagues):
                    league_name = item.get("dashboard_name", "")
                    if league_name not in current_round_map or current_round_map[league_name] == 0:
                        if matches_data and idx < len(matches_data):
                            matches = matches_data[idx].get("matches", [])
                            if matches:
                                max_round = max((m.get("round", 0) for m in matches), default=0)
                                if max_round > 0:
                                    current_round_map[league_name] = max_round
                                    print(f"    ℹ️ Jornada para '{league_name}' obtenida de matches: {max_round}")
                
                if current_round_map:
                    sync_tactics(conn, tactics_data, processed_leagues, user_id, current_round_map)
                else:
                    print("⚠️ No se pudo determinar jornadas actuales. Saltando sync de tácticas.")
            
            # H. Programar scraping futuro de tácticas (solo si hay countdown activo)
            if next_match_info:
                active_countdowns = [m for m in next_match_info if m.get("seconds_remaining", 0) > 0]
                if active_countdowns:
                    schedule_tactics_scrape(conn, user_id, next_match_info, processed_leagues)
                else:
                    print("ℹ️ No hay partidos pendientes. No se programan tareas de tácticas.")
            
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
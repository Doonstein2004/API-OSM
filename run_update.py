# run_update.py
import requests
import json
import os
import psycopg2
import psycopg2.extras
from collections import defaultdict
from datetime import datetime
from dotenv import load_dotenv

# --- CONFIGURACIÓN ---
load_dotenv() # Cargar variables de .env
API_BASE_URL = "http://127.0.0.1:8000"
API_KEY = "$#N!7!T8sGkRmz8vD9Uhr9s&mq&xpc3NBKC2BpN*GX98bKMNDsf2!"
LEAGUES_TO_IGNORE = ["Champions Cup 25/26", "Greece"]

# --- NUEVO: Configuración de la conexión a PostgreSQL ---
DB_CONFIG = {
    "host": os.getenv("DB_HOST"),
    "port": os.getenv("DB_PORT"),
    "dbname": os.getenv("DB_NAME"),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD")
}

def get_db_connection():
    """Establece y devuelve una conexión a la base de datos."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        print("✅ Conexión con PostgreSQL establecida.")
        return conn
    except psycopg2.OperationalError as e:
        print(f"❌ ERROR: No se pudo conectar a PostgreSQL. Revisa tus credenciales en .env y que la BD esté en ejecución. Error: {e}")
        exit()
        
        
# --- MODIFICACIÓN CLAVE: Función de parseo robusta ---
def parse_value_string(value_str):
    if not isinstance(value_str, str): return 0
    # 1. Eliminar comas y convertir a minúsculas
    value_str = value_str.lower().strip().replace(',', '')
    
    # 2. El resto de la lógica permanece igual
    if 'm' in value_str: return float(value_str.replace('m', ''))
    if 'k' in value_str: return float(value_str.replace('k', '')) / 1000
    try: return float(value_str) / 1_000_000
    except (ValueError, TypeError): return 0

# --- RESTO DE FUNCIONES (Sin cambios) ---
def normalize_team_name(name):
    if not isinstance(name, str): return ""
    prefixes_to_remove = ["fk ", "ca ", "fc ", "cd "]
    normalized = name.lower().strip()
    for prefix in prefixes_to_remove:
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
    return normalized

def create_league_maps(all_leagues_data):
    team_to_leagues = defaultdict(list)
    league_to_teams = defaultdict(set)
    for league in all_leagues_data:
        league_name = league.get("league_name")
        for club in league.get("clubs", []):
            normalized_club = normalize_team_name(club["club"])
            team_to_leagues[normalized_club].append(league_name)
            league_to_teams[league_name].add(normalized_club)
    return dict(team_to_leagues), dict(league_to_teams)

def resolve_active_leagues(my_fichajes, all_leagues_data):
    my_managed_teams = {team['team_name']: team for team in my_fichajes}
    team_to_leagues, league_to_teams = create_league_maps(all_leagues_data)
    resolved_map = {}
    for original_name, team_data in my_managed_teams.items():
        normalized_name = normalize_team_name(original_name)
        candidate_leagues = team_to_leagues.get(normalized_name, [])
        if len(candidate_leagues) == 1:
            resolved_map[original_name] = candidate_leagues[0]
            print(f"  - Equipo '{original_name}' asignado unívocamente a la liga '{candidate_leagues[0]}'.")
        elif len(candidate_leagues) > 1:
            print(f"  - Ambigüedad detectada para '{original_name}'. Ligas candidatas: {candidate_leagues}. Iniciando análisis de fichajes...")
            league_scores = {league: 0 for league in candidate_leagues}
            witness_teams = set()
            for transfer in team_data.get("transfers", []):
                witness_teams.add(normalize_team_name(transfer.get("From", "").split('\n')[0]))
                witness_teams.add(normalize_team_name(transfer.get("To", "").split('\n')[0]))
            for witness in witness_teams:
                for league_name in candidate_leagues:
                    if witness in league_to_teams.get(league_name, set()):
                        league_scores[league_name] += 1
            if not any(league_scores.values()):
                 print(f"  - ADVERTENCIA: No se pudo resolver la ambigüedad para '{original_name}'. Saltando.")
                 continue
            winner_league = max(league_scores, key=league_scores.get)
            resolved_map[original_name] = winner_league
            print(f"  - Análisis completado. '{original_name}' asignado a '{winner_league}' (Puntuaciones: {league_scores}).")
        else:
            print(f"  - ADVERTENCIA: El equipo '{original_name}' no se encontró en ninguna liga de 'data.json'.")
    return resolved_map
    
def create_dashboard_to_official_league_map(standings_data, team_to_resolved_league):
    print("\n? Creando mapa de nombres de liga personalizados a oficiales...")
    dashboard_map = {}
    my_managed_teams = set(team_to_resolved_league.keys())
    for league_standings in standings_data:
        dashboard_name = league_standings.get("league_name")
        found_match = False
        for team in league_standings.get("standings", []):
            team_name_in_standings = team.get("Club")
            for my_team in my_managed_teams:
                if normalize_team_name(team_name_in_standings) == normalize_team_name(my_team):
                    official_name = team_to_resolved_league[my_team]
                    dashboard_map[dashboard_name] = official_name
                    print(f"  - Mapeado '{dashboard_name}' -> '{official_name}' (vía equipo '{my_team}')")
                    found_match = True
                    break
            if found_match:
                break
    return dashboard_map

# --- MODIFICADO: Sincronización con PostgreSQL ---
def sync_leagues_with_postgres(conn, active_league_names, all_leagues_data):
    print("\n🔄 Sincronizando ligas con PostgreSQL...")
    league_id_map = {}
    
    with conn.cursor() as cur:
        # 1. Obtener ligas existentes de Postgres
        cur.execute("SELECT id, name FROM leagues;")
        existing_leagues = {name: league_id for league_id, name in cur.fetchall()}
        print(f"  - Encontradas {len(existing_leagues)} ligas en PostgreSQL.")
        
        # 2. Iterar y crear las que falten (UPSERT)
        for league_name in active_league_names:
            league_info = next((l for l in all_leagues_data if l.get('league_name') == league_name), None)
            
            # Preparar datos para JSON
            teams_for_db = []
            if league_info:
                teams_for_db = [
                    {
                        "name": c["club"], "alias": c["club"], 
                        "initialValue": parse_value_string(c["squad_value"]),
                        "fixedIncomePerRound": parse_value_string(c["fixed_income"]),
                        "initialCash": 0, "currentValue": 0 # Valor inicial por defecto
                    } for c in league_info.get("clubs", [])
                ]

            # Convertir a string JSON para la base de datos
            teams_json = json.dumps(teams_for_db)
            
            # Sentencia SQL de UPSERT: inserta si no existe, actualiza si existe
            sql = """
                INSERT INTO leagues (name, teams, managers_by_team, standings)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (name) DO UPDATE SET
                    teams = EXCLUDED.teams,
                    updated_at = NOW()
                RETURNING id;
            """
            # Ejecutar y obtener el ID de la liga (sea nueva o existente)
            cur.execute(sql, (league_name, teams_json, '{}', '[]'))
            league_id = cur.fetchone()[0]
            league_id_map[league_name] = league_id
            
            if league_name in existing_leagues:
                print(f"  - Liga '{league_name}' actualizada. ID: {league_id}")
            else:
                print(f"  - Liga '{league_name}' creada. ID: {league_id}")

    conn.commit()
    return league_id_map

def translate_and_group_transfers(fichajes_data, team_to_resolved_league):
    grouped_transfers = defaultdict(list)
    for team_block in fichajes_data:
        my_team_name = team_block.get("team_name")
        league_name = team_to_resolved_league.get(my_team_name)
        if not league_name:
            continue
        for transfer in team_block.get("transfers", []):
            from_parts = transfer.get("From", "").split('\n')
            to_parts = transfer.get("To", "").split('\n')
            from_manager = from_parts[1] if len(from_parts) > 1 else None
            to_manager = to_parts[1] if len(to_parts) > 1 else None
            managerName, transaction_type = (None, None)
            if to_manager:
                managerName = to_manager
                transaction_type = 'purchase'
            elif from_manager:
                managerName = from_manager
                transaction_type = 'sale'
            if managerName and transaction_type:
                grouped_transfers[league_name].append({
                    "playerName": transfer.get("Name"), "managerName": managerName,
                    "transactionType": transaction_type, "position": transfer.get("Position"),
                    "round": int(transfer.get("Gameweek", 0)), "baseValue": parse_value_string(transfer.get("Value")),
                    "finalPrice": parse_value_string(transfer.get("Price")), "createdAt": datetime.now()
                })
    return dict(grouped_transfers)

# --- MODIFICADO: Subida de datos a PostgreSQL ---
def upload_data_to_postgres(conn, grouped_transfers, league_id_map):
    with conn.cursor() as cur:
        for league_name, transfers in grouped_transfers.items():
            if league_name in LEAGUES_TO_IGNORE:
                print(f"\n🚫 Omitiendo subida para la liga ignorada: '{league_name}'")
                continue
                
            league_id = league_id_map.get(league_name)
            if not league_id: continue

            print(f"\n🔄 Actualizando fichajes para '{league_name}' (ID: {league_id})...")
            
            # 1. Borrar fichajes antiguos para esta liga
            cur.execute("DELETE FROM transfers WHERE league_id = %s;", (league_id,))
            print(f"  - {cur.rowcount} fichajes antiguos eliminados.")
            
            # 2. Insertar los nuevos fichajes en un batch (muy eficiente)
            if transfers:
                sql = """
                    INSERT INTO transfers (league_id, player_name, manager_name, transaction_type, position, round, base_value, final_price, created_at)
                    VALUES %s;
                """
                # Preparar los datos para el batch
                data_tuples = [
                    (
                        league_id, t['playerName'], t['managerName'], t['transactionType'],
                        t['position'], t['round'], t['baseValue'], t['finalPrice'], t['createdAt']
                    ) for t in transfers
                ]
                
                psycopg2.extras.execute_values(cur, sql, data_tuples)
                print(f"  - {len(transfers)} nuevos fichajes subidos.")
    conn.commit()

# --- MODIFICADO: Sincronización de detalles de la liga ---
def sync_league_details(conn, standings_data, squad_values_data, league_id_map, dashboard_to_official_map):
    print("\n🔄 Sincronizando Mánagers, Valores y Clasificaciones...")
    
    with conn.cursor() as cur:
        for league_id, league_doc_data in get_all_leagues_from_db(cur).items():
            # Construir payload de actualización
            official_name = league_doc_data['name']
            managersByTeam = {}
            current_values = {}
            standings = []

            # Buscar el nombre del dashboard que corresponde a esta liga oficial
            dashboard_name = next((dn for dn, on in dashboard_to_official_map.items() if on == official_name), None)
            
            if dashboard_name:
                # Extraer datos de mánagers y clasificación
                league_standings_data = next((ls for ls in standings_data if ls.get("league_name") == dashboard_name), None)
                if league_standings_data:
                    standings = league_standings_data.get("standings", [])
                    for team in standings:
                        if team.get("Manager") and team.get("Manager") != "N/A":
                            managersByTeam[team["Club"]] = team["Manager"]
                
                # Extraer valores de equipo
                league_values_data = next((lv for lv in squad_values_data if lv.get("league_name") == dashboard_name), None)
                if league_values_data:
                    for team in league_values_data.get("squad_values_ranking", []):
                        current_values[team["Club"]] = parse_value_string(team["Value"])
            
            # Actualizar el campo 'teams' con los nuevos valores
            updated_teams = league_doc_data.get("teams", [])
            for team_obj in updated_teams:
                team_name = team_obj.get("name")
                if team_name in current_values:
                    team_obj["currentValue"] = current_values[team_name]
            
            # Ejecutar la actualización en la base de datos
            sql = """
                UPDATE leagues
                SET managers_by_team = %s, teams = %s, standings = %s
                WHERE id = %s;
            """
            cur.execute(sql, (json.dumps(managersByTeam), json.dumps(updated_teams), json.dumps(standings), league_id))
            print(f"  - Datos actualizados para '{official_name}' (Mánagers: {len(managersByTeam)}, Valores: {len(current_values)}, Clasificación: {len(standings)}).")

    conn.commit()
    

def get_all_leagues_from_db(cursor):
    """Función auxiliar para obtener los datos de las ligas y reconstruir el objeto."""
    cursor.execute("SELECT id, name, type, teams, managers_by_team, standings FROM leagues;")
    leagues = {}
    for row in cursor.fetchall():
        leagues[row[0]] = {
            "id": row[0], "name": row[1], "type": row[2],
            "teams": row[3], "managersByTeam": row[4], "standings": row[5]
        }
    return leagues

    

def run_full_automation():
    print("- Leyendo archivos JSON de los scrapers...")
    try:
        with open("data.json", "r", encoding='utf-8') as f:
            all_leagues_data = json.load(f).get('data', [])
        with open("fichajes_data.json", "r", encoding='utf-8') as f:
            fichajes_data = json.load(f)
        with open("standings_output.json", "r", encoding='utf-8') as f:
            standings_data = json.load(f)
        with open("squad_values_data.json", "r", encoding='utf-8') as f:
            squad_values_data = json.load(f)
    except FileNotFoundError as e:
        print(f"❌ ERROR: Archivo no encontrado. Asegúrate de haber ejecutado todos los scrapers primero. {e}")
        return

    conn = get_db_connection()
    if not conn: return
    
    try:
        print("\n🔎 Descubriendo y resolviendo ligas activas (basado en fichajes)...")
        team_to_resolved_league = resolve_active_leagues(fichajes_data, all_leagues_data)
        
        dashboard_to_official_map = create_dashboard_to_official_league_map(standings_data, team_to_resolved_league)
        
        official_leagues_from_transfers = set(team_to_resolved_league.values())
        filtered_leagues_to_process = {name for name in official_leagues_from_transfers if name not in LEAGUES_TO_IGNORE}
        
        if not filtered_leagues_to_process:
            print("ℹ️ No se encontraron ligas con fichajes para procesar. Finalizando.")
            return
        print(f"\n leagues a procesar: {list(filtered_leagues_to_process)}")

        league_id_map = sync_leagues_with_postgres(conn, filtered_leagues_to_process, all_leagues_data)
        
        sync_league_details(conn, standings_data, squad_values_data, league_id_map, dashboard_to_official_map)

        print("\n- Traduciendo datos de fichajes al formato de la aplicación...")
        grouped_transfers = translate_and_group_transfers(fichajes_data, team_to_resolved_league)
        
        upload_data_to_postgres(conn, grouped_transfers, league_id_map)
        
        print("\n✨ Proceso de sincronización completado.")
    finally:
        if conn:
            conn.close()
            print("🔌 Conexión con PostgreSQL cerrada.")

if __name__ == "__main__":
    run_full_automation()

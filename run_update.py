# run_update.py
import requests
import json
import os
import firebase_admin
from firebase_admin import credentials, firestore
from collections import defaultdict
from datetime import datetime

# --- CONFIGURACIÓN ---
API_BASE_URL = "http://127.0.0.1:8000"
API_KEY = "$#N!7!T8sGkRmz8vD9Uhr9s&mq&xpc3NBKC2BpN*GX98bKMNDsf2!"
FIREBASE_KEY_PATH = "serviceAccountKey.json"
LEAGUES_TO_IGNORE = ["Champions Cup 25/26", "Greece"] 

# --- INICIALIZAR FIREBASE ADMIN ---
try:
    cred = credentials.Certificate(FIREBASE_KEY_PATH)
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    print("? Conexión con Firebase establecida.")
except Exception as e:
    print(f"? ERROR: No se pudo conectar a Firebase. Revisa '{FIREBASE_KEY_PATH}'. Error: {e}")
    exit()

# --- FUNCIONES AUXILIARES ---

def parse_value_string(value_str):
    if not isinstance(value_str, str): return 0
    value_str = value_str.lower().strip()
    if 'm' in value_str: return float(value_str.replace('m', ''))
    if 'k' in value_str: return float(value_str.replace('k', '')) / 1000
    try: return float(value_str) / 1_000_000
    except (ValueError, TypeError): return 0

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

def sync_leagues_with_firebase(active_league_names, all_leagues_data, db):
    print("? Sincronizando ligas con Firebase...")
    leagues_ref = db.collection('leagues')
    firebase_leagues = {doc.to_dict().get('name'): doc.id for doc in leagues_ref.stream() if doc.to_dict().get('name')}
    print(f"? Encontradas {len(firebase_leagues)} ligas en Firebase.")
    league_id_map = {}
    for league_name in active_league_names:
        if league_name in firebase_leagues:
            league_id = firebase_leagues[league_name]
            print(f"  - La liga '{league_name}' ya existe con ID: {league_id}")
            league_id_map[league_name] = league_id
        else:
            print(f"  - La liga '{league_name}' no existe en Firebase. Creándola...")
            league_info = next((l for l in all_leagues_data if l.get('league_name') == league_name), None)
            if league_info:
                teams_for_firebase = [{"name": c["club"], "alias": c["club"], "initialValue": parse_value_string(c["squad_value"]), "fixedIncomePerRound": parse_value_string(c["fixed_income"]), "initialCash": 0} for c in league_info.get("clubs", [])]
                new_league_payload = {"name": league_name, "type": "standard", "teams": teams_for_firebase, "managersByTeam": {}}
                new_doc_ref = leagues_ref.document()
                new_doc_ref.set(new_league_payload)
                league_id_map[league_name] = new_doc_ref.id
                print(f"  - Liga '{league_name}' creada con ID: {new_doc_ref.id}")
    return league_id_map

# --- FUNCIÓN CON LA LÓGICA FINAL Y CORRECTA ---
def translate_and_group_transfers(fichajes_data, team_to_resolved_league):
    """Traduce TODOS los fichajes y los agrupa por el nombre de la liga resuelta."""
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

            # Lógica para identificar al mánager y el tipo de transacción en CUALQUIER fichaje
            if to_manager:
                # Si hay un manager en el destino, es una COMPRA para ese manager
                managerName = to_manager
                transaction_type = 'purchase'
            elif from_manager:
                # Si no, y hay un manager en el origen, es una VENTA de ese manager (a la CPU)
                managerName = from_manager
                transaction_type = 'sale'

            # Si el fichaje involucra al menos a un mánager, lo procesamos
            if managerName and transaction_type:
                grouped_transfers[league_name].append({
                    "playerName": transfer.get("Name"), "managerName": managerName,
                    "transactionType": transaction_type, "position": transfer.get("Position"),
                    "round": int(transfer.get("Gameweek", 0)), "baseValue": parse_value_string(transfer.get("Value")),
                    "finalPrice": parse_value_string(transfer.get("Price")), "createdAt": datetime.now()
                })
    return dict(grouped_transfers)

def upload_data_to_firebase(grouped_transfers, league_id_map, db):
    for league_name, transfers in grouped_transfers.items():
        if league_name in LEAGUES_TO_IGNORE:
            print(f"\n? Omitiendo subida para la liga ignorada: '{league_name}'")
            continue
        
        league_id = league_id_map.get(league_name)
        if not league_id: continue
            
        print(f"\n? Actualizando fichajes para '{league_name}' (ID: {league_id})...")
        transfers_ref = db.collection('leagues').document(league_id).collection('transfers')
        
        old_docs = list(transfers_ref.stream())
        if old_docs:
            batch = db.batch()
            for doc in old_docs: batch.delete(doc.reference)
            batch.commit()
            print(f"  - {len(old_docs)} fichajes antiguos eliminados.")
        
        if transfers:
            batch = db.batch()
            for t in transfers:
                new_doc_ref = transfers_ref.document()
                batch.set(new_doc_ref, t)
            batch.commit()
            print(f"  - {len(transfers)} nuevos fichajes subidos.")

def run_full_automation():
    print("? Leyendo archivos JSON de los scrapers...")
    try:
        with open("data.json", "r", encoding='utf-8') as f:
            all_leagues_data = json.load(f).get('data', [])
        with open("fichajes_data.json", "r", encoding='utf-8') as f:
            fichajes_data = json.load(f)
    except FileNotFoundError as e:
        print(f"? ERROR: Archivo no encontrado. Asegúrate de haber ejecutado los scrapers primero. {e}")
        return

    print("\n? Descubriendo y resolviendo ligas activas...")
    team_to_resolved_league = resolve_active_leagues(fichajes_data, all_leagues_data)
    
    active_league_names = set(team_to_resolved_league.values())
    filtered_active_leagues = {name for name in active_league_names if name not in LEAGUES_TO_IGNORE}
    
    if not filtered_active_leagues:
        print("? No se encontraron ligas activas (o todas fueron ignoradas). Finalizando.")
        return
    print(f"? Ligas activas a procesar: {list(filtered_active_leagues)}")

    league_id_map = sync_leagues_with_firebase(filtered_active_leagues, all_leagues_data, db)

    print("\n? Traduciendo datos de fichajes al formato de la aplicación...")
    # La variable my_manager_names ya no es necesaria aquí
    grouped_transfers = translate_and_group_transfers(fichajes_data, team_to_resolved_league)
    
    upload_data_to_firebase(grouped_transfers, league_id_map, db)
    
    print("\n? Proceso de sincronización completado.")


if __name__ == "__main__":
    run_full_automation()
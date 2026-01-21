# run_scheduled_tactics.py
"""
Script para ejecutar tareas de scraping de tácticas programadas.
Se debe ejecutar periódicamente (ej: cada 5 minutos mediante cron/scheduler).

Busca tareas pendientes cuyo scheduled_at ya ha pasado y ejecuta el scraping
de tácticas para esos usuarios/ligas específicos.
"""
import sys
import os
import json
import time
from datetime import datetime
from dotenv import load_dotenv
import psycopg2
import psycopg2.extras
from playwright.sync_api import sync_playwright

# --- Módulos Locales ---
from utils import login_to_osm, InvalidCredentialsError, handle_popups, safe_navigate
from scraper_tactics import get_tactics_data, extract_tactics_from_page

# --- CONFIGURACIÓN ---
load_dotenv()

DB_CONFIG = {
    "host": os.getenv("DB_HOST"),
    "port": os.getenv("DB_PORT"),
    "dbname": os.getenv("DB_NAME"),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD")
}


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


def get_pending_tactics_tasks(conn):
    """
    Obtiene TODAS las tareas de scraping de tácticas pendientes que ya deberían ejecutarse.
    Agrupa por usuario para procesar eficientemente.
    
    Returns:
        dict: {user_id: [lista de tareas]}
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, user_id, metadata, scheduled_at
            FROM public.scheduled_scrape_tasks
            WHERE task_type = 'tactics_scrape'
              AND status = 'pending'
              AND scheduled_at <= NOW()
            ORDER BY user_id, scheduled_at;
        """)
        
        tasks_by_user = {}
        for row in cur.fetchall():
            user_id = str(row['user_id'])
            task = {
                "id": row['id'],
                "metadata": row['metadata'] if isinstance(row['metadata'], dict) else json.loads(row['metadata'] or '{}'),
                "scheduled_at": row['scheduled_at']
            }
            
            if user_id not in tasks_by_user:
                tasks_by_user[user_id] = []
            tasks_by_user[user_id].append(task)
        
        return tasks_by_user


def mark_task_complete(conn, task_id):
    """Marca una tarea como completada."""
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE public.scheduled_scrape_tasks
            SET status = 'completed', executed_at = NOW()
            WHERE id = %s;
        """, (task_id,))
    conn.commit()


def mark_task_failed(conn, task_id, error_message):
    """Marca una tarea como fallida."""
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE public.scheduled_scrape_tasks
            SET status = 'failed', executed_at = NOW(), 
                metadata = metadata || %s::jsonb
            WHERE id = %s;
        """, (json.dumps({"error": error_message[:500]}), task_id))
    conn.commit()


def get_user_credentials(conn, user_id):
    """Obtiene las credenciales OSM del usuario."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT osm_username, osm_password 
            FROM public.get_credentials_for_user(%s);
        """, (user_id,))
        creds = cur.fetchone()
        if not creds or not creds['osm_username'] or not creds['osm_password']:
            return None, None
        return creds['osm_username'], creds['osm_password']


def save_tactics_to_db(conn, user_id, league_id, matchday, team_name, tactics):
    """Guarda las tácticas en la base de datos."""
    with conn.cursor() as cur:
        sql = """
            INSERT INTO public.match_tactics (
                user_id, league_id, round, team_name,
                game_plan, tackling, pressure, mentality, tempo,
                forwards_tactic, midfielders_tactic, defenders_tactic,
                offside_trap, marking, scraped_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (league_id, round, team_name)
            DO UPDATE SET
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
        cur.execute(sql, (
            str(user_id),
            league_id,
            matchday,
            team_name,
            tactics.get("game_plan", "Unknown"),
            tactics.get("tackling", "Unknown"),
            tactics.get("pressure", 50),
            tactics.get("mentality", 50),
            tactics.get("tempo", 50),
            tactics.get("forwards_tactic", "Unknown"),
            tactics.get("midfielders_tactic", "Unknown"),
            tactics.get("defenders_tactic", "Unknown"),
            tactics.get("offside_trap", False),
            tactics.get("marking", "Unknown"),
        ))
    conn.commit()


def scrape_tactics_for_slot(page, slot_index):
    """
    Navega a un slot específico y extrae sus tácticas.
    
    Args:
        page: Página de Playwright
        slot_index: Índice del slot (0-3)
    
    Returns:
        dict: Tácticas extraídas o None si falla
    """
    MAIN_DASHBOARD_URL = "https://en.onlinesoccermanager.com/Career"
    TACTICS_URL = "https://en.onlinesoccermanager.com/Tactics"
    
    try:
        # Ir al dashboard principal
        if not page.url.endswith("/Career"):
            page.goto(MAIN_DASHBOARD_URL, wait_until="domcontentloaded")
        
        page.wait_for_selector(".career-teamslot", timeout=15000)
        handle_popups(page)
        
        slot = page.locator(".career-teamslot").nth(slot_index)
        
        if slot.locator("h2.clubslot-main-title").count() == 0:
            return None
        
        team_name = slot.locator("h2.clubslot-main-title").inner_text()
        
        # Activar el slot
        slot.click()
        page.wait_for_selector("#timers", timeout=45000)
        handle_popups(page)
        
        # Ir a tácticas
        if not safe_navigate(page, TACTICS_URL, verify_selector="#tactics-overall"):
            return None
        
        time.sleep(2)
        handle_popups(page)
        
        # Extraer tácticas
        tactics = extract_tactics_from_page(page)
        tactics["team_name"] = team_name
        
        return tactics
        
    except Exception as e:
        print(f"    ⚠️ Error en slot {slot_index}: {e}")
        return None


def process_user_tasks(conn, user_id, tasks):
    """
    Procesa todas las tareas pendientes de un usuario.
    Usa una sola sesión de navegador para eficiencia.
    """
    print(f"\n🎯 Procesando {len(tasks)} tareas para usuario {user_id}")
    
    # Obtener credenciales
    osm_username, osm_password = get_user_credentials(conn, user_id)
    if not osm_username:
        print(f"  ⚠️ Sin credenciales para usuario {user_id}")
        for task in tasks:
            mark_task_failed(conn, task['id'], "No credentials found")
        return
    
    try:
        with sync_playwright() as p:
            is_gha = os.getenv("GITHUB_ACTIONS") == "true"
            browser = p.chromium.launch(headless=True if is_gha else False, args=["--no-sandbox"])
            context = browser.new_context(viewport={'width': 1280, 'height': 720})
            page = context.new_page()
            
            # Login
            try:
                if not login_to_osm(page, osm_username, osm_password):
                    raise Exception("Login fallido")
            except InvalidCredentialsError:
                print(f"  ❌ Credenciales inválidas para {user_id}")
                for task in tasks:
                    mark_task_failed(conn, task['id'], "Invalid credentials")
                return
            except Exception as e:
                print(f"  ❌ Error login: {e}")
                for task in tasks:
                    mark_task_failed(conn, task['id'], str(e))
                return
            
            # Procesar cada tarea
            for task in tasks:
                meta = task['metadata']
                slot_index = meta.get('slot_index', 0)
                league_id = meta.get('league_id')
                matchday = meta.get('matchday')
                team_name = meta.get('team_name')
                league_name = meta.get('league_name')
                
                print(f"  📋 Procesando: {league_name} Jornada {matchday} (Slot {slot_index})")
                
                try:
                    tactics = scrape_tactics_for_slot(page, slot_index)
                    
                    if tactics:
                        save_tactics_to_db(conn, user_id, league_id, matchday, team_name, tactics)
                        mark_task_complete(conn, task['id'])
                        print(f"    ✅ Tácticas guardadas")
                    else:
                        mark_task_failed(conn, task['id'], "Could not extract tactics")
                        print(f"    ⚠️ No se pudieron extraer tácticas")
                        
                except Exception as e:
                    print(f"    ❌ Error: {e}")
                    mark_task_failed(conn, task['id'], str(e))
            
            browser.close()
            
    except Exception as e:
        print(f"  ❌ Error general: {e}")
        for task in tasks:
            if task.get('_processed'):
                continue
            mark_task_failed(conn, task['id'], str(e))


def run_scheduled_tactics():
    """
    Función principal que ejecuta las tareas de scraping de tácticas programadas.
    """
    print(f"\n{'='*60}")
    print(f"🕐 Ejecutando tareas programadas de tácticas - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")
    
    conn = get_db_connection()
    if not conn:
        print("❌ No se pudo conectar a la base de datos")
        return
    
    try:
        # Obtener todas las tareas pendientes agrupadas por usuario
        tasks_by_user = get_pending_tactics_tasks(conn)
        
        if not tasks_by_user:
            print("ℹ️ No hay tareas de tácticas pendientes.")
            return
        
        total_tasks = sum(len(tasks) for tasks in tasks_by_user.values())
        print(f"📊 Encontradas {total_tasks} tareas pendientes para {len(tasks_by_user)} usuarios")
        
        # Procesar cada usuario
        for user_id, tasks in tasks_by_user.items():
            process_user_tasks(conn, user_id, tasks)
        
        print(f"\n✨ Ejecución completada.")
        
    except Exception as e:
        print(f"❌ Error general: {e}")
        import traceback
        traceback.print_exc()
    finally:
        conn.close()


if __name__ == "__main__":
    run_scheduled_tactics()

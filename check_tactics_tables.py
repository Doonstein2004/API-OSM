# check_tactics_tables.py
"""Script de verificación de tablas de tácticas"""
import os
from dotenv import load_dotenv
import psycopg2
import psycopg2.extras

load_dotenv()

DB_CONFIG = {
    "host": os.getenv("DB_HOST"),
    "port": os.getenv("DB_PORT"),
    "dbname": os.getenv("DB_NAME"),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD")
}

try:
    conn = psycopg2.connect(**DB_CONFIG)
    conn.cursor_factory = psycopg2.extras.DictCursor
    cur = conn.cursor()
    
    # Verificar tablas
    cur.execute("SELECT to_regclass('public.scheduled_scrape_tasks')")
    result = cur.fetchone()[0]
    print(f"✓ Tabla scheduled_scrape_tasks: {'EXISTE' if result else 'NO EXISTE'}")
    
    cur.execute("SELECT to_regclass('public.match_tactics')")
    result = cur.fetchone()[0]
    print(f"✓ Tabla match_tactics: {'EXISTE' if result else 'NO EXISTE'}")
    
    # Si existen, mostrar conteos
    try:
        cur.execute("SELECT COUNT(*) FROM scheduled_scrape_tasks")
        print(f"  - Tareas programadas: {cur.fetchone()[0]}")
        cur.execute("SELECT status, COUNT(*) FROM scheduled_scrape_tasks GROUP BY status")
        for row in cur.fetchall():
            print(f"    - {row[0]}: {row[1]}")
    except:
        print("  - (tabla no existe)")
    
    try:
        cur.execute("SELECT COUNT(*) FROM match_tactics")
        print(f"  - Tácticas guardadas: {cur.fetchone()[0]}")
    except:
        print("  - (tabla no existe)")
    
    conn.close()
    print("\n✅ Verificación completada")
    
except Exception as e:
    print(f"❌ Error: {e}")

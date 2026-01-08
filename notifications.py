# notifications.py
import firebase_admin
from firebase_admin import credentials, messaging
import os
import json
from datetime import datetime

# Inicializar Firebase (Solo una vez)
def init_firebase_admin():
    if not firebase_admin._apps:
        cert_content = os.getenv('FIREBASE_ADMIN_JSON')
        if cert_content:
            try:
                cred = credentials.Certificate(json.loads(cert_content))
                firebase_admin.initialize_app(cred)
                print("✅ Firebase Admin inicializado correctamente.")
            except Exception as e:
                print(f"⚠️ Error inicializando Firebase con el JSON provisto: {e}")
        else:
            print("⚠️ No se encontró la variable de entorno FIREBASE_ADMIN_JSON.")

def send_push(token, title, body):
    if not token: 
        print("⚠️ No hay token FCM para enviar notificación.")
        return
    try:
        android_config = messaging.AndroidConfig(
            priority='high',
            notification=messaging.AndroidNotification(
                icon='ic_notification',  # El nombre del archivo en res/drawable (sin extensión)
                color='#22D3EE',         # Tu color Cyan
                sound='default'
            )
        )
        
        msg = messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            android=android_config,
            token=token
        )
        response = messaging.send(msg)
        print(f"🔔 Push enviado exitosamente: {response}")
    except Exception as e:
        print(f"❌ Error enviando push a Firebase: {e}")

def analyze_and_notify(user_fcm_token, transfer_list, all_transfers, my_manager_name):
    """
    Prioridad de Notificaciones:
    1. ¡VENTA!: Si el usuario vendió un jugador (Dinero en caja).
    2. ¡GANGA!: Si hay jugadores muy baratos en el mercado.
    3. INFO: Resumen de finalización.
    """
    if not user_fcm_token:
        return

    print(f"🧐 Analizando notificaciones para: {my_manager_name}")

    # --- 1. DETECTAR VENTAS PROPIAS RECIENTES ---
    # Buscamos en el historial de transferencias si hay ventas donde el vendedor soy YO.
    # Como el scraper trae las últimas transferencias, si aparezco ahí es buena noticia.
    my_sales = []
    if all_transfers:
        for t in all_transfers:
            # En una venta, el 'managerName' es quien vendió, O 'seller_manager' si está detallado
            seller = t.get('seller_manager') or t.get('managerName')
            
            # Verificar si soy yo y es una venta (o alguien me compró)
            if seller and my_manager_name and seller.lower() == my_manager_name.lower():
                # Verificamos que sea reciente (hoy) para no spamear con ventas viejas
                # (Esto es una heurística simple, idealmente guardaríamos la última notificada)
                my_sales.append(t)

    if my_sales:
        # ¡Prioridad Máxima!
        last_sale = my_sales[0]
        player = last_sale.get('playerName', 'Un jugador')
        price = last_sale.get('finalPrice', 0)
        
        send_push(
            user_fcm_token, 
            "💰 ¡VENTA REALIZADA!", 
            f"Has vendido a {player} por {price}M. ¡Tienes dinero fresco en caja!"
        )
        return # Si notificamos venta, no notificamos gangas para no saturar

    # --- 2. DETECTAR GANGAS (Market) ---
    bargains = []
    if transfer_list:
        for league in transfer_list:
            for p in league.get("players_on_sale", []):
                try:
                    price = float(p.get('price', 0))
                    value = float(p.get('value', 0))
                    if value > 0:
                        ratio = price / value
                        # Ganga: Precio menor o igual a 1.15 veces su valor
                        if ratio <= 1.30: 
                            profit = (value * 2.5) - price
                            bargains.append(f"{p['name']} (+{profit:.1f}M)")
                except: continue

    if len(bargains) > 0:
        best_bargain = bargains[0]
        count = len(bargains)
        if count == 1:
            send_push(user_fcm_token, "🔥 ¡Oportunidad de Mercado!", f"Se encontró una ganga: {best_bargain}. ¡Cómpralo antes que vuele!")
        else:
            send_push(user_fcm_token, "🛒 Mercado Ardiendo", f"Se encontraron {count} gangas: {best_bargain} y más...")
        return

    # --- 3. NOTIFICACIÓN ESTÁNDAR ---
    # Si no hubo nada emocionante, solo avisamos que los datos están listos.
    send_push(
        user_fcm_token, 
        "✅ Actualización Completada", 
        "Los datos de tu liga han sido actualizados. Entra para ver el análisis."
    )
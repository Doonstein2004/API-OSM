import firebase_admin
from firebase_admin import credentials, messaging
import os
import json

# --- 1. INICIALIZACIÓN ROBUSTA ---
def init_firebase_admin():
    # Si ya está inicializado, no hacemos nada
    if firebase_admin._apps:
        return True

    print("🔄 Inicializando Firebase Admin...")
    
    # Intentamos leer la variable de entorno
    cert_content = os.getenv('FIREBASE_ADMIN_JSON')
    
    if not cert_content:
        print("⚠️ ADVERTENCIA: No se encontró la variable 'FIREBASE_ADMIN_JSON'.")
        print("   -> Asegúrate de tenerla en el .env (local) o en GitHub Secrets.")
        return False

    try:
        # Intentamos parsear el JSON
        cred_dict = json.loads(cert_content)
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)
        print("✅ Firebase Admin inicializado correctamente.")
        return True
    except Exception as e:
        print(f"❌ ERROR CRÍTICO al inicializar Firebase: {e}")
        return False

# --- 2. ENVÍO SEGURO ---
def send_push(token, title, body):
    # 1. Verificación de seguridad: ¿Está inicializado?
    if not firebase_admin._apps:
        # Intentamos inicializar de emergencia
        if not init_firebase_admin():
            print("🚫 Se omitió el envío de Push porque Firebase no está configurado.")
            return

    if not token: 
        print("⚠️ No hay token FCM para enviar notificación.")
        return

    try:
        # Configuración Android (Icono y Color)
        android_config = messaging.AndroidConfig(
            priority='high',
            notification=messaging.AndroidNotification(
                icon='ic_notification', 
                color='#22D3EE',
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

# --- 3. LÓGICA DE NEGOCIO ---
def analyze_and_notify(user_fcm_token, transfer_list, all_transfers, my_manager_name):
    # Verificación temprana
    if not user_fcm_token:
        print("🔕 El usuario no tiene token FCM. Saltando análisis.")
        return

    # Asegurar inicialización antes de procesar nada
    if not firebase_admin._apps:
        if not init_firebase_admin():
            return

    print(f"🧐 Analizando notificaciones para: {my_manager_name}")

    # 1. VENTAS PROPIAS
    my_sales = []
    if all_transfers:
        for t in all_transfers:
            seller = t.get('seller_manager') or t.get('managerName')
            if seller and my_manager_name and seller.lower() == my_manager_name.lower():
                my_sales.append(t)

    if my_sales:
        last_sale = my_sales[0]
        player = last_sale.get('playerName', 'Un jugador')
        price = last_sale.get('finalPrice', 0)
        
        send_push(
            user_fcm_token, 
            "💰 ¡VENTA REALIZADA!", 
            f"Has vendido a {player} por {price}M. ¡Tienes dinero fresco en caja!"
        )
        return 

    # 2. GANGAS
    bargains = []
    if transfer_list:
        for league in transfer_list:
            for p in league.get("players_on_sale", []):
                try:
                    price = float(p.get('price', 0))
                    value = float(p.get('value', 0))
                    if value > 0:
                        ratio = price / value
                        if ratio <= 1.15: 
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

    # 3. INFO
    send_push(
        user_fcm_token, 
        "✅ Actualización Completada", 
        "Los datos de tu liga han sido actualizados. Entra para ver el análisis."
    )

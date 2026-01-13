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

# --- 3. LÓGICA DE NEGOCIO MEJORADA ---
def analyze_and_notify(user_fcm_token, transfer_list, all_transfers, my_manager_name):
    """
    Analiza las transferencias y envía notificaciones inteligentes.
    - Omitimos nuestras compras (ya lo sabemos).
    - Notificamos ventas (flujo de caja).
    - Notificamos 'Bombazos' (>50M) de otros.
    - Notificamos 'Gangas' (Jugadores baratos en lista).
    """
    # Verificación temprana
    if not user_fcm_token:
        print("🔕 El usuario no tiene token FCM. Saltando análisis.")
        return

    # Asegurar inicialización antes de procesar nada
    if not firebase_admin._apps:
        if not init_firebase_admin():
            return

    print(f"🧐 Analizando notificaciones para: {my_manager_name}")

    if my_manager_name:
        my_name = my_manager_name.lower().strip()
    else:
        my_name = ""

    # 1. ANALIZAR ÚLTIMO MOVIMIENTO (Ventas y Bombazos de Otros)
    if all_transfers:
        # Asumimos que all_transfers[0] es la más reciente (ordenado por fecha desc)
        last_transfer = all_transfers[0] 
        
        player = last_transfer.get('playerName', 'Un jugador')
        price = last_transfer.get('finalPrice', 0)
        buyer = last_transfer.get('buyerManager')
        seller = last_transfer.get('sellerManager')
        manager_op = last_transfer.get('managerName') 
        trans_type = last_transfer.get('transactionType') 

        # Normalizar
        buyer_norm = buyer.lower().strip() if buyer else ''
        seller_norm = seller.lower().strip() if seller else ''
        op_norm = manager_op.lower().strip() if manager_op else ''

        # ID de compras propias para ignorar en bombazos también
        is_my_purchase = (buyer_norm == my_name) or (op_norm == my_name and trans_type == 'purchase')
        
        # ID Ventas propias
        is_my_sale = (seller_norm == my_name) or (op_norm == my_name and trans_type == 'sale')

        # >>> NOTIFICACIÓN 1: MI VENTA (PRIORIDAD ALTA) <<<
        if is_my_sale:
            send_push(
                user_fcm_token, 
                "💰 ¡VENTA REALIZADA!", 
                f"Has vendido a {player} por {price}M. ¡Tienes dinero fresco en caja!"
            )
            return

        # (Omitimos notificación de compra propia intencionalmente)

        # >>> NOTIFICACIÓN 2: BOMBAZO DE OTRO MANAGER (> 50M) <<<
        # Solo si no soy yo ni comprando ni vendiendo (para no spammear si ya se que vendí caro)
        if price > 50 and not is_my_sale and not is_my_purchase:
            who_bought = buyer if buyer else (manager_op if manager_op else "CPU")
            send_push(
                user_fcm_token, 
                "💸 ¡BOMBAZO EN LA LIGA!", 
                f"{who_bought} ha pagado {price}M por {player}. ¡El mercado está loco!"
            )
            # Retornamos aquí para no ensuciar con gangas si acaba de pasar algo gordo
            return

    # 2. GANGAS / OPORTUNIDADES (Lista de Transferencias Actual)
    # Busca jugadores listados actualmente que sean muy rentables
    bargains = []
    if transfer_list:
        for league in transfer_list:
            for p in league.get("players_on_sale", []):
                try:
                    price = float(p.get('price', 0))
                    value = float(p.get('value', 0))
                    
                    if value > 0:
                        # Ratio: Precio / Valor. Si es < 1.3 empieza a ser interesante.
                        # Si es < 1.15 es una GANGA ABSOLUTA.
                        ratio = price / value
                        
                        if ratio <= 1.25: # Umbral un poco más laxo para encontrar más opciones, pero destacamos las top
                            profit_potential = (value * 2.5) - price # Max venta approx
                            bargains.append({
                                'name': p['name'],
                                'price': price,
                                'value': value,
                                'ratio': ratio,
                                'profit': profit_potential
                            })
                except: continue

    if len(bargains) > 0:
        # Ordenar por mejor ratio (menor es mejor)
        bargains.sort(key=lambda x: x['ratio'])
        best = bargains[0]
        
        # Solo notificamos si es realmente buena (ratio < 1.15) 
        # O si es la única notificación que enviaremos hoy y es decente (<1.25)
        
        if best['ratio'] <= 1.15:
            send_push(
                user_fcm_token, 
                "🔥 ¡GANGA DETECTADA!", 
                f"{best['name']} está a la venta por {best['price']}M (Valor {best['value']}M). ¡Cómpralo ya!"
            )
            return
        
        # Si hay muchas "decentes"
        elif len(bargains) >= 3:
             send_push(
                user_fcm_token, 
                "🛒 Mercado Interesante", 
                f"Hay oportunidades como {best['name']} ({best['price']}M) y otros en lista."
            )
             return

    # 3. INFO (Keep Alive - Opcional, comentar si molesta)
    # print("ℹ️ Sin notificaciones relevantes por ahora.")

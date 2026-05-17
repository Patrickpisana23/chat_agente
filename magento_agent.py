import os
import json
import requests
from dotenv import load_dotenv
from google.cloud import bigquery
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.tools import tool
from langgraph.prebuilt import create_react_agent
from langgraph.checkpoint.memory import MemorySaver
from psycopg_pool import ConnectionPool
from datetime import datetime

# --- CONFIGURACIÓN ---
load_dotenv()

REQUIRED_VARS = ["MAGENTO_BASE_URL", "MAGENTO_ACCESS_TOKEN", "GOOGLE_API_KEY", "POSTGRES_URI"]
missing = [v for v in REQUIRED_VARS if not os.environ.get(v)]
if missing:
    raise RuntimeError(f"Faltan variables de entorno requeridas: {', '.join(missing)}")

MAGENTO_BASE_URL = os.environ["MAGENTO_BASE_URL"]
MAGENTO_ACCESS_TOKEN = os.environ["MAGENTO_ACCESS_TOKEN"]
POSTGRES_URI = os.environ["POSTGRES_URI"]

# Cliente de BigQuery (inicialización segura con fallback)
bq_client = None
try:
    service_account_path = os.path.join(os.path.dirname(__file__), "service_account.json")
    if os.path.exists(service_account_path):
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = service_account_path
    bq_client = bigquery.Client()
except Exception:
    pass

# Modelo LLM
model = ChatGoogleGenerativeAI(model="gemini-2.5-flash", temperature=0)

# --- POSTGRESQL: Conexión y Setup ---

connection_kwargs = {"autocommit": True, "prepare_threshold": 0}
pool = ConnectionPool(
    conninfo=POSTGRES_URI,
    max_size=5,
    kwargs=connection_kwargs,
)


def setup_database():
    """Crea la tabla customer_profiles si no existe y agrega columnas nuevas."""
    with pool.connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS customer_profiles (
                dni                      VARCHAR(20) PRIMARY KEY,
                email                    VARCHAR(255),
                nombre                   VARCHAR(255),
                resumen                  TEXT NOT NULL,
                ordenes                  JSONB DEFAULT '[]',
                carritos_abandonados     JSONB DEFAULT '[]',
                historial_conversaciones JSONB DEFAULT '[]',
                updated_at               TIMESTAMP DEFAULT NOW()
            )
        """)
        for col, tipo in [("ordenes", "JSONB DEFAULT '[]'"),
                          ("carritos_abandonados", "JSONB DEFAULT '[]'"),
                          ("historial_conversaciones", "JSONB DEFAULT '[]'")]:
            conn.execute(f"""
                DO $$ BEGIN
                    ALTER TABLE customer_profiles ADD COLUMN {col} {tipo};
                EXCEPTION WHEN duplicate_column THEN NULL;
                END $$;
            """)


def get_customer_profile(dni: str) -> dict | None:
    """Busca un perfil de cliente existente en PostgreSQL por DNI."""
    with pool.connection() as conn:
        row = conn.execute(
            """SELECT dni, email, nombre, resumen, ordenes, carritos_abandonados,
                      historial_conversaciones, updated_at
               FROM customer_profiles WHERE dni = %s""",
            (dni,),
        ).fetchone()
    if row:
        return {
            "dni": row[0],
            "email": row[1],
            "nombre": row[2],
            "resumen": row[3],
            "ordenes": row[4] or [],
            "carritos_abandonados": row[5] or [],
            "historial_conversaciones": row[6] or [],
            "updated_at": str(row[7]),
        }
    return None


def save_customer_profile(dni: str, email: str, nombre: str, resumen: str,
                          ordenes: list = None, carritos_abandonados: list = None):
    """Guarda o actualiza el perfil del cliente en PostgreSQL."""
    with pool.connection() as conn:
        conn.execute(
            """
            INSERT INTO customer_profiles (dni, email, nombre, resumen, ordenes, carritos_abandonados, updated_at)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, NOW())
            ON CONFLICT (dni) DO UPDATE SET
                email = EXCLUDED.email,
                nombre = EXCLUDED.nombre,
                resumen = EXCLUDED.resumen,
                ordenes = EXCLUDED.ordenes,
                carritos_abandonados = EXCLUDED.carritos_abandonados,
                updated_at = NOW()
            """,
            (dni, email, nombre, resumen,
             json.dumps(ordenes or []), json.dumps(carritos_abandonados or [])),
        )


def save_conversation_summary(dni: str, messages: list):
    """Genera un resumen de la conversación con LLM y lo guarda en el historial del cliente."""
    conversation_text = ""
    for msg in messages:
        role = "Cliente" if msg.type == "human" else "Agente"
        if hasattr(msg, "content") and isinstance(msg.content, str) and msg.content.strip():
            conversation_text += f"{role}: {msg.content}\n"

    if not conversation_text.strip():
        return

    prompt = f"""Resume esta conversación entre un cliente y el agente de ventas de nuestro Retail E-commerce en máximo 3 líneas.
Enfócate en: qué buscó el cliente, qué productos le interesaron, qué se le recomendó, y si compró algo o quedó pendiente.

Conversación:
{conversation_text}

Resumen conciso:"""

    summary = model.invoke(prompt).content
    fecha = datetime.now().strftime("%Y-%m-%d %H:%M")

    with pool.connection() as conn:
        conn.execute(
            """
            UPDATE customer_profiles
            SET historial_conversaciones = COALESCE(historial_conversaciones, '[]'::jsonb)
                || %s::jsonb,
                updated_at = NOW()
            WHERE dni = %s
            """,
            (json.dumps([{"fecha": fecha, "resumen": summary}]), dni),
        )


# --- MAGENTO API HELPER ---

def call_magento_api(endpoint: str, params: dict = None, method: str = "GET"):
    """Simulador de API de Magento usando archivos JSON locales."""
    base_dir = os.path.dirname(__file__)
    
    # 1. GET customers/search
    if "customers/search" in endpoint:
        try:
            with open(os.path.join(base_dir, "mock_customers.json"), "r", encoding="utf-8") as f:
                data = json.load(f)
            
            # Buscar filtros en los params
            doc_num = params.get("searchCriteria[filter_groups][0][filters][0][value]") if params else None
            firstname_like = params.get("searchCriteria[filter_groups][0][filters][0][value]") if params else None
            
            items = []
            for item in data.get("items", []):
                attrs = {at["attribute_code"]: at["value"] for at in item.get("custom_attributes", [])}
                
                # Búsqueda por DNI
                if doc_num and doc_num.isdigit() and attrs.get("document_number") == doc_num:
                    items.append(item)
                # Búsqueda difusa por nombre
                elif firstname_like and not doc_num.isdigit() and "%" in firstname_like:
                    name_clean = firstname_like.replace("%", "").lower()
                    if name_clean in item.get("firstname", "").lower() or name_clean in item.get("lastname", "").lower():
                        items.append(item)
                        
            return {"items": items, "total_count": len(items)}
        except Exception as e:
            return {"error": f"Error mock customers: {str(e)}"}
            
    # 2. GET orders
    elif "orders" in endpoint:
        try:
            with open(os.path.join(base_dir, "mock_orders.json"), "r", encoding="utf-8") as f:
                data = json.load(f)
            
            email = params.get("searchCriteria[filter_groups][0][filters][0][value]") if params else None
            
            items = []
            for order in data.get("items", []):
                if email and order.get("customer_email") == email:
                    items.append(order)
            
            # Ordenar por fecha DESC
            items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
            return {"items": items, "total_count": len(items)}
        except Exception as e:
            return {"error": f"Error mock orders: {str(e)}"}
            
    # 3. GET carts/search
    elif "carts/search" in endpoint:
        try:
            with open(os.path.join(base_dir, "mock_carts.json"), "r", encoding="utf-8") as f:
                data = json.load(f)
            
            customer_id = params.get("searchCriteria[filter_groups][0][filters][0][value]") if params else None
            
            items = []
            for cart in data.get("items", []):
                if customer_id and str(cart.get("customer_id")) == str(customer_id):
                    items.append(cart)
                    
            return {"items": items, "total_count": len(items)}
        except Exception as e:
            return {"error": f"Error mock carts: {str(e)}"}
            
    # 4. GET products
    elif "products" in endpoint:
        try:
            with open(os.path.join(base_dir, "mock_products.json"), "r", encoding="utf-8") as f:
                data = json.load(f)
            
            sku = params.get("searchCriteria[filter_groups][0][filters][0][value]") if params else None
            
            items = []
            for prod in data.get("items", []):
                if sku and prod.get("sku") == sku:
                    items.append(prod)
                    
            return {"items": items, "total_count": len(items)}
        except Exception as e:
            return {"error": f"Error mock products: {str(e)}"}
            
    # 5. GET coupons/search
    elif "coupons/search" in endpoint:
        try:
            with open(os.path.join(base_dir, "mock_coupons.json"), "r", encoding="utf-8") as f:
                data = json.load(f)
            
            code = params.get("searchCriteria[filter_groups][0][filters][0][value]") if params else None
            
            items = []
            for coupon in data.get("items", []):
                if code and coupon.get("code") == code:
                    items.append(coupon)
                    
            return {"items": items, "total_count": len(items)}
        except Exception as e:
            return {"error": f"Error mock coupons: {str(e)}"}
            
    return {"error": "Endpoint no soportado en modo mock."}


def fetch_customer_by_dni(dni: str) -> dict | None:
    """Busca TODAS las cuentas de un cliente en Magento por su DNI y las consolida."""
    params = {
        "searchCriteria[filter_groups][0][filters][0][field]": "document_number",
        "searchCriteria[filter_groups][0][filters][0][value]": dni,
        "searchCriteria[filter_groups][0][filters][0][condition_type]": "eq",
        "fields": "items[id,email,firstname,lastname,custom_attributes],total_count",
    }
    data = call_magento_api("customers/search", params=params)
    items = data.get("items", [])
    if not items:
        return None
    primary = items[0]
    attrs = {at["attribute_code"]: at["value"] for at in primary.get("custom_attributes", [])}
    all_ids = [c["id"] for c in items]
    all_emails = list({c["email"] for c in items})
    return {
        "id": primary["id"],
        "all_ids": all_ids,
        "email": primary["email"],
        "all_emails": all_emails,
        "nombre": f"{primary.get('firstname', '')} {primary.get('lastname', '')}".strip(),
        "dni": attrs.get("document_number", dni),
        "celular": attrs.get("cellphone", ""),
    }


def fetch_all_orders(email: str) -> list[dict]:
    """Obtiene TODAS las órdenes de un cliente desde Magento por su email."""
    all_orders = []
    page = 1
    page_size = 20

    while True:
        params = {
            "searchCriteria[filter_groups][0][filters][0][field]": "customer_email",
            "searchCriteria[filter_groups][0][filters][0][value]": email,
            "searchCriteria[filter_groups][0][filters][0][condition_type]": "eq",
            "searchCriteria[sortOrders][0][field]": "created_at",
            "searchCriteria[sortOrders][0][direction]": "DESC",
            "searchCriteria[pageSize]": page_size,
            "searchCriteria[currentPage]": page,
            "fields": "items[increment_id,created_at,status,grand_total,items[name,sku,qty_ordered,price]],total_count",
        }
        data = call_magento_api("orders", params=params)

        if "error" in data:
            break

        items = data.get("items", [])
        if not items:
            break

        status_map = {
            "pending": "Pendiente",
            "processing": "En proceso",
            "complete": "Completado",
            "canceled": "Cancelado",
            "closed": "Cerrado",
            "holded": "En espera",
        }

        for order in items:
            productos = []
            for item in order.get("items", []):
                productos.append({
                    "nombre": item.get("name", ""),
                    "sku": item.get("sku", ""),
                    "cantidad": int(item.get("qty_ordered", 0)),
                    "precio": item.get("price", 0),
                })
            all_orders.append({
                "orden_id": order.get("increment_id", ""),
                "fecha": order.get("created_at", "")[:10],
                "estado": status_map.get(order.get("status", ""), order.get("status", "")),
                "total": order.get("grand_total", 0),
                "productos": productos,
            })

        total_count = data.get("total_count", 0)
        if page * page_size >= total_count:
            break
        page += 1

    return all_orders


def fetch_abandoned_carts(customer_id: int) -> list[dict]:
    """Obtiene los carritos abandonados (activos sin convertir a orden) de un cliente."""
    params = {
        "searchCriteria[filter_groups][0][filters][0][field]": "customer_id",
        "searchCriteria[filter_groups][0][filters][0][value]": customer_id,
        "searchCriteria[filter_groups][0][filters][0][condition_type]": "eq",
        "searchCriteria[filter_groups][1][filters][0][field]": "is_active",
        "searchCriteria[filter_groups][1][filters][0][value]": 1,
        "searchCriteria[filter_groups][1][filters][0][condition_type]": "eq",
        "searchCriteria[sortOrders][0][field]": "updated_at",
        "searchCriteria[sortOrders][0][direction]": "DESC",
    }
    data = call_magento_api("carts/search", params=params)

    if "error" in data:
        return []

    carts = []
    for cart in data.get("items", []):
        items = []
        for item in cart.get("items", []):
            items.append({
                "nombre": item.get("name", ""),
                "sku": item.get("sku", ""),
                "cantidad": int(item.get("qty", 0)),
                "precio": item.get("price", 0),
            })
        if items:
            carts.append({
                "cart_id": cart.get("id"),
                "fecha": cart.get("updated_at", "")[:10],
                "total_items": cart.get("items_count", 0),
                "subtotal": cart.get("grand_total", 0),
                "productos": items,
            })
    return carts


def generate_profile_summary(nombre: str, orders: list[dict], abandoned_carts: list[dict] = None) -> str:
    """Genera un resumen de perfil del cliente usando el LLM basado en sus órdenes."""
    orders_text = json.dumps(orders, indent=2, ensure_ascii=False)

    prompt = f"""Analiza las siguientes órdenes de compra del cliente "{nombre}" en nuestra tienda de Retail E-commerce 
y genera un perfil resumido con la siguiente información:

1. **Productos comprados**: Lista de productos con cantidades y precios
2. **Marcas preferidas**: Qué marcas ha comprado más
3. **Categorías de interés**: Electrodomésticos, tecnología, gaming, audio, etc.
4. **Rango de gasto típico**: Monto mínimo y máximo de sus compras
5. **Frecuencia de compra**: Cada cuánto compra aproximadamente
6. **Última compra**: Fecha y producto de la compra más reciente
7. **Preferencias detectadas**: Cualquier patrón observable (ej: prefiere gama alta, busca ofertas, etc.)
8. **Carritos abandonados**: Si hay carritos sin completar, lista los productos que dejó pendientes y posibles razones

Órdenes del cliente:
{orders_text}

Carritos abandonados del cliente:
{json.dumps(abandoned_carts or [], indent=2, ensure_ascii=False)}

Genera el resumen en español, estructurado y conciso. Este resumen será usado por un agente de ventas
para personalizar la atención al cliente. Si hay carritos abandonados, destácalos como oportunidad de venta."""

    response = model.invoke(prompt)
    return response.content


# --- TOOLS ---

@tool
def identificar_cliente(dni: str) -> str:
    """
    Identifica al cliente por su DNI y recupera su perfil de compras y preferencias.
    Usa esta herramienta SIEMPRE que el cliente proporcione su DNI o documento de identidad.
    Retorna el historial resumido de compras y preferencias del cliente.
    """
    global _current_dni

    # 1. Buscar perfil existente en PostgreSQL
    profile = get_customer_profile(dni)
    if profile:
        _current_dni = dni
        partes = [
            f"Cliente encontrado en base de datos:",
            f"Nombre: {profile['nombre']}",
            f"Email: {profile['email']}",
            f"Última actualización del perfil: {profile['updated_at']}",
            f"\n--- PERFIL DE COMPRAS ---\n{profile['resumen']}",
        ]
        if profile['historial_conversaciones']:
            partes.append("\n--- CONVERSACIONES ANTERIORES ---")
            for conv in profile['historial_conversaciones'][-5:]:
                partes.append(f"[{conv['fecha']}] {conv['resumen']}")
        return "\n".join(partes)

    # 2. Si no existe, buscar al cliente en Magento
    customer = fetch_customer_by_dni(dni)
    if not customer:
        return f"No se encontró ningún cliente con DNI {dni} en el sistema."

    _current_dni = dni

    # 3. Obtener órdenes y carritos de TODAS las cuentas con este DNI
    orders = []
    abandoned = []
    seen_order_ids = set()
    for email in customer["all_emails"]:
        for o in fetch_all_orders(email):
            if o["orden_id"] not in seen_order_ids:
                orders.append(o)
                seen_order_ids.add(o["orden_id"])
    for cid in customer["all_ids"]:
        abandoned.extend(fetch_abandoned_carts(cid))

    if not orders and not abandoned:
        save_customer_profile(dni, customer["email"], customer["nombre"],
                              "Cliente sin historial de compras ni carritos abandonados.")
        return (
            f"Cliente encontrado: {customer['nombre']} ({customer['email']})\n"
            f"Aún no tiene órdenes de compra ni carritos abandonados."
        )

    # 4. Generar resumen con LLM y guardar todo en PostgreSQL
    resumen = generate_profile_summary(customer["nombre"], orders, abandoned)
    save_customer_profile(dni, customer["email"], customer["nombre"], resumen,
                          ordenes=orders, carritos_abandonados=abandoned)

    partes = [
        f"Cliente identificado: {customer['nombre']}",
        f"Email: {customer['email']}",
        f"Órdenes encontradas: {len(orders)}",
        f"Carritos abandonados: {len(abandoned)}",
        f"\n--- PERFIL DE COMPRAS ---\n{resumen}",
    ]
    return "\n".join(partes)


@tool
def buscar_producto(busqueda: str) -> str:
    """
    Busca productos en el catálogo unificado por nombre, marca o SKU. 
    Devuelve los SKUs y nombres encontrados. Úsala para encontrar qué productos existen.
    """
    base_dir = os.path.dirname(__file__)
    try:
        # Cargar catálogo local
        with open(os.path.join(base_dir, "mock_products.json"), "r", encoding="utf-8") as f:
            data = json.load(f)
        
        results = []
        busqueda_lower = busqueda.lower()
        for item in data.get("items", []):
            if (busqueda_lower in item.get("sku", "").lower() or
                busqueda_lower in item.get("name", "").lower()):
                results.append(item)
                
        items = [f"SKU: {r['sku']} | {r['name']} | S/{r['price']}" for r in results[:5]]
        return "\n".join(items) if items else "No se encontraron productos en el catálogo."
    except Exception as e:
        return f"Error en búsqueda local: {str(e)}"

@tool
def consultar_stock_real(sku: str) -> str:
    """
    Consulta el stock REAL y PRECIO actual de uno o varios SKUs en Magento (API en vivo).
    Úsala SIEMPRE que el cliente pregunte '¿Tienen stock?', '¿Está disponible?' o quiera confirmar el precio de algo específico.
    Puedes pasar varios SKUs separados por coma.
    """
    skus = [s.strip() for s in sku.split(",")]
    resultados = []
    
    for s in skus:
        p = {"searchCriteria[filter_groups][0][filters][0][field]": "sku", 
             "searchCriteria[filter_groups][0][filters][0][value]": s, 
             "searchCriteria[filter_groups][0][filters][0][condition_type]": "eq",
             "fields": "items[name,sku,price,extension_attributes[stock_item[qty]]]"}
        
        d = call_magento_api("products", params=p)
        items = d.get("items", [])
        
        if not items:
            resultados.append(f"SKU {s}: No encontrado en Magento.")
            continue
            
        i = items[0]
        ext = i.get('extension_attributes', {})
        if isinstance(ext, list):
            ext = {}
        stock = ext.get('stock_item', {})
        if isinstance(stock, list):
            stock = {}
        qty = stock.get('qty', 0)
        estado = "Disponible" if qty > 0 else "Agotado"
        resultados.append(f"{i['name']} ({s}) -> {estado} | Stock: {int(qty)} uds | Precio: S/{i['price']}")
        
    return "\n".join(resultados)

@tool
def buscar_cliente(nombre: str) -> str:
    """Busca información de un cliente por su nombre (Email, DNI, Cel)."""
    p = {"searchCriteria[filter_groups][0][filters][0][field]": "firstname", "searchCriteria[filter_groups][0][filters][0][value]": f"%{nombre}%", "searchCriteria[filter_groups][0][filters][0][condition_type]": "like", "searchCriteria[pageSize]": 2, "fields": "items[id,firstname,lastname,email,custom_attributes],total_count"}
    d = call_magento_api("customers/search", params=p)
    items = d.get("items", [])
    if not items: return "No encontrado."
    res = []
    for i in items:
        a = {at['attribute_code']: at['value'] for at in i.get('custom_attributes', [])}
        res.append(f"ID:{i['id']}|{i['firstname']} {i['lastname']}|Email:{i['email']}|DNI:{a.get('document_number')}|Cel:{a.get('cellphone')}")
    return "\n".join(res)

@tool
def consultar_ultima_orden(identificador: str) -> str:
    """Consulta el estado de la última orden de un cliente usando su email o DNI."""
    email = identificador
    if "@" not in identificador:
        d_c = call_magento_api("customers/search", params={"searchCriteria[filter_groups][0][filters][0][field]": "document_number", "searchCriteria[filter_groups][0][filters][0][value]": identificador, "searchCriteria[filter_groups][0][filters][0][condition_type]": "eq", "fields": "items[email],total_count"})
        if not d_c.get("items"): return "DNI no hallado."
        email = d_c["items"][0]["email"]

    p = {"searchCriteria[filter_groups][0][filters][0][field]": "customer_email", "searchCriteria[filter_groups][0][filters][0][value]": email, "searchCriteria[sortOrders][0][field]": "created_at", "searchCriteria[sortOrders][0][direction]": "DESC", "searchCriteria[pageSize]": 1, "fields": "items[increment_id,status,grand_total,items[name,qty_ordered]],total_count"}
    d = call_magento_api("orders", params=p)
    items = d.get("items", [])
    if not items: return "Sin pedidos."
    o = items[0]
    m = {"pending":"Pendiente", "processing":"En proceso", "complete":"Completado", "canceled":"Cancelado"}
    prods = [f"{pi['name']}(x{int(pi['qty_ordered'])})" for pi in o.get('items', [])]
    return f"Orden:{o['increment_id']}|Status:{m.get(o['status'], o['status'])}|Total:${o['grand_total']}|Items:{', '.join(prods)}"

@tool
def validar_cupon(codigo: str) -> str:
    """Valida si un cupón de descuento existe y es válido en Magento."""
    d = call_magento_api("coupons/search", params={"searchCriteria[filter_groups][0][filters][0][field]": "code", "searchCriteria[filter_groups][0][filters][0][value]": codigo, "searchCriteria[filter_groups][0][filters][0][condition_type]": "eq", "fields": "items[code,rule_id],total_count"})
    return f"Cupón {codigo} válido" if d.get("items") else "Inválido"


# --- AGENTE ---

tools = [identificar_cliente, buscar_producto, consultar_stock_real, buscar_cliente, consultar_ultima_orden, validar_cupon]

system_prompt = (
    "Eres el asistente oficial de ventas de nuestro Retail E-commerce. Sé amable, profesional y útil.\n\n"
    "REGLA IMPORTANTE: Al inicio de cada conversación, pide al cliente su DNI para identificarlo.\n"
    "Una vez que te dé su DNI, usa la herramienta 'identificar_cliente' para obtener su perfil.\n"
    "Usa la información del perfil para personalizar tus recomendaciones "
    "(por ejemplo, si compró una laptop gaming antes, ofrécele accesorios compatibles).\n\n"
    "Si el perfil incluye CONVERSACIONES ANTERIORES, úsalas para dar continuidad. "
    "Por ejemplo, si en una conversación pasada preguntó por laptops, puedes decir: "
    "'La última vez estuviste viendo laptops, ¿encontraste lo que buscabas?'\n\n"
    "Si el perfil incluye carritos abandonados, mencionalo con tacto. Por ejemplo: "
    "'Vi que tenías un [producto] pendiente en tu carrito, ¿te gustaría completar esa compra?' "
    "o sugiere productos relacionados. No seas insistente, úsalo como oportunidad de venta natural.\n\n"
    "Para buscar productos usa 'buscar_producto'.\n"
    "Para confirmar stock o precios exactos, usa 'consultar_stock_real' con el SKU.\n"
    "Si el cliente no tiene historial, atiéndelo normalmente como un cliente nuevo."
)

# Inicializar la base de datos al cargar el módulo
setup_database()

checkpointer = MemorySaver()
agent = create_react_agent(model, tools, prompt=system_prompt, checkpointer=checkpointer)

# Variable global para rastrear el DNI del cliente activo en la sesión
_current_dni = None


def ejecutar_agente(prompt: str, thread_id: str = "default"):
    """Ejecuta el agente con memoria de conversación dentro de la sesión."""
    config = {"configurable": {"thread_id": thread_id}}
    result = agent.invoke({"messages": [("user", prompt)]}, config)
    return result["messages"][-1].content


def get_conversation_messages(thread_id: str) -> list:
    """Obtiene los mensajes de la conversación actual desde el checkpointer."""
    config = {"configurable": {"thread_id": thread_id}}
    state = agent.get_state(config)
    return state.values.get("messages", [])


if __name__ == "__main__":
    print("=== Agente de Ventas de Retail E-commerce (con memoria de cliente) ===")
    print("Escribe 'salir' para terminar.\n")

    thread_id = "default"

    while True:
        user_input = input("Tú: ").strip()
        if user_input.lower() in ("salir", "exit", "quit"):
            if _current_dni:
                print("Guardando resumen de conversación...")
                messages = get_conversation_messages(thread_id)
                save_conversation_summary(_current_dni, messages)
                print("Resumen guardado.")
            print("¡Hasta luego!")
            break
        if not user_input:
            continue

        response = ejecutar_agente(user_input, thread_id=thread_id)
        print(f"Asistente: {response}\n")

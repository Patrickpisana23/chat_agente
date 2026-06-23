import os
import json
import unicodedata
import requests
from dotenv import load_dotenv
from google.cloud import bigquery
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.tools import tool
from langgraph.prebuilt import create_react_agent
from langgraph.checkpoint.memory import MemorySaver

# --- CONFIGURACIÓN E INICIALIZACIÓN ---
load_dotenv()

# Validar variables de entorno críticas
REQUIRED_VARS = ["MAGENTO_BASE_URL", "MAGENTO_ACCESS_TOKEN", "GOOGLE_API_KEY"]
missing = [v for v in REQUIRED_VARS if not os.environ.get(v)]
if missing:
    raise RuntimeError(f"Faltan variables de entorno requeridas: {', '.join(missing)}")

MAGENTO_BASE_URL = os.environ["MAGENTO_BASE_URL"]
MAGENTO_ACCESS_TOKEN = os.environ["MAGENTO_ACCESS_TOKEN"]

# Inicialización segura del Cliente de BigQuery con fallback
bq_client = None
try:
    service_account_path = os.path.join(os.path.dirname(__file__), "service_account.json")
    if os.path.exists(service_account_path):
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = service_account_path
    bq_client = bigquery.Client()
except Exception:
    pass

# Inicialización del modelo LLM (Gemini 2.5 Flash optimizado para velocidad y precisión)
model = ChatGoogleGenerativeAI(model="gemini-2.5-flash", temperature=0)

# --- MODELO DE EMBEDDINGS (compartido por RAG y búsqueda de catálogo) ---
embeddings = None
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "gemini-embedding-2")

try:
    from langchain_google_genai import GoogleGenerativeAIEmbeddings
    embeddings = GoogleGenerativeAIEmbeddings(model=EMBEDDING_MODEL)
    print(f"Modelo de embeddings inicializado: {EMBEDDING_MODEL}")
except Exception as e:
    print(f"Aviso: No se pudo inicializar el modelo de embeddings: {e}")

# --- CONFIGURACIÓN DE RAG (Múltiples índices Elasticsearch) ---
vector_stores = {}

RAG_INDICES = {
    "terminos": "rag_hiraoka_terminosycondiciones",
    "envios": "rag_hiraoka_legales_envio",
    "cambios": "rag_hiraoka_cambiosydevoluciones",
}

RAG_DOCUMENTOS = {
    "terminos": "Terminos y Condiciones",
    "envios": "Legales Envios",
    "cambios": "Cambios y Devoluciones",
}

try:
    from langchain_elasticsearch import ElasticsearchStore

    if not embeddings:
        raise ValueError("El modelo de embeddings no está disponible para RAG.")

    print("Inicializando base de conocimiento RAG con Elasticsearch...")

    ES_URL = os.getenv("ES_URL", "http://104.198.172.31:9200")
    ES_USER = os.getenv("ES_USER", "elastic")

    ES_PASSWORD = os.getenv("ES_PASSWORD")
    if not ES_PASSWORD:
        secret_path = os.path.join(os.path.dirname(__file__), "elasticstore_urp.txt")
        if os.path.exists(secret_path):
            with open(secret_path, "r", encoding="utf-8") as f:
                ES_PASSWORD = f.read().strip()

    if not ES_PASSWORD:
        raise ValueError("No se encontró la contraseña de Elasticsearch (ES_PASSWORD o elasticstore_urp.txt).")

    for key, index_name in RAG_INDICES.items():
        vector_stores[key] = ElasticsearchStore(
            index_name=index_name,
            embedding=embeddings,
            es_url=ES_URL,
            es_user=ES_USER,
            es_password=ES_PASSWORD,
        )

    first_store = next(iter(vector_stores.values()))
    if not first_store.client.ping():
        raise ConnectionError("No se pudo conectar a Elasticsearch.")

    print(f"RAG Elasticsearch inicializado correctamente ({len(vector_stores)} índices).")
except Exception as e:
    print(f"Aviso: No se pudo inicializar RAG con Elasticsearch: {e}")

# --- HELPER FUNCTIONS PARA RAG ---

# Mapeo de palabras clave -> (section_slug, store_key)
# store_key: "terminos", "envios" o "cambios"
# section_slug puede ser None para buscar en todo el índice sin filtro de sección
SECTION_HINTS = {
    # --- Legales Envíos (keywords más específicos primero) ---
    "envio hoy": ("envios_hoy", "envios"),
    "envío hoy": ("envios_hoy", "envios"),
    "hoy mismo": ("envios_hoy", "envios"),
    "mismo dia": ("envios_hoy", "envios"),
    "mismo día": ("envios_hoy", "envios"),
    "entrega hoy": ("envios_hoy", "envios"),
    "llega hoy": ("envios_hoy", "envios"),
    "envio regular": ("envio_regular", "envios"),
    "envío regular": ("envio_regular", "envios"),
    "24 horas": ("envio_regular", "envios"),
    "24hrs": ("envio_regular", "envios"),
    "programar entrega": ("envio_regular", "envios"),
    "turno entrega": ("envio_regular", "envios"),
    "primer turno": ("envio_regular", "envios"),
    "segundo turno": ("envio_regular", "envios"),
    "same day": ("same_day", "envios"),
    "entrega mismo dia": ("same_day", "envios"),
    "entrega mismo día": ("same_day", "envios"),
    "recojo tienda": ("entrega_tienda", "envios"),
    "retiro tienda": ("entrega_tienda", "envios"),
    "entrega tienda": ("entrega_tienda", "envios"),
    "recoger producto": ("entrega_tienda", "envios"),
    "documento identidad": ("entrega_tienda", "envios"),
    "otra persona": ("entrega_tienda", "envios"),
    "apoderado": ("entrega_tienda", "envios"),
    "penalidad": ("entrega_tienda", "envios"),
    "penalidades": ("entrega_tienda", "envios"),
    "instalacion": ("consideracion", "envios"),
    "instalación": ("consideracion", "envios"),
    "instalar": ("consideracion", "envios"),
    "escalera": ("consideracion", "envios"),
    "ascensor": ("consideracion", "envios"),
    "propina": ("consideracion", "envios"),
    "propinas": ("consideracion", "envios"),
    "segunda entrega": ("consideracion", "envios"),
    "no pudieron entregar": ("consideracion", "envios"),
    "cobertura": ("same_day", "envios"),
    "distritos": ("same_day", "envios"),
    "distrito": ("same_day", "envios"),
    "provincia": ("envio_regular", "envios"),
    "provincias": ("envio_regular", "envios"),
    # --- Cambios y Devoluciones ---
    "nota de credito": ("devolucion_dinero", "cambios"),
    "nota de crédito": ("devolucion_dinero", "cambios"),
    "garantia fabricante": ("garantia_fabricante", "cambios"),
    "garantía fabricante": ("garantia_fabricante", "cambios"),
    "producto usado": ("productos_usados", "cambios"),
    "producto abierto": ("productos_usados", "cambios"),
    "producto desgastado": ("productos_usados", "cambios"),
    "producto sensible": ("productos_sensibles", "cambios"),
    "productos sensibles": ("productos_sensibles", "cambios"),
    "producto digital": ("productos_digitales", "cambios"),
    "poder simple": ("acreditacion_titular", "cambios"),
    "devolucion": ("politica_devoluciones", "cambios"),
    "devolución": ("politica_devoluciones", "cambios"),
    "devolver": ("politica_devoluciones", "cambios"),
    "cambiar producto": ("politica_devoluciones", "cambios"),
    "cambio producto": ("politica_devoluciones", "cambios"),
    "restitucion": ("derecho_restitucion", "cambios"),
    "restitución": ("derecho_restitucion", "cambios"),
    "reembolso": ("devolucion_dinero", "cambios"),
    "extorno": ("devolucion_dinero", "cambios"),
    "cheque": ("devolucion_dinero", "cambios"),
    "arrepentimiento": ("derecho_arrepentimiento", "cambios"),
    "ya no lo quiero": ("derecho_arrepentimiento", "cambios"),
    "me equivoque": ("derecho_arrepentimiento", "cambios"),
    "me equivoqué": ("derecho_arrepentimiento", "cambios"),
    "no me gusto": ("derecho_arrepentimiento", "cambios"),
    "no me gustó": ("derecho_arrepentimiento", "cambios"),
    "software": ("productos_digitales", "cambios"),
    "licencia": ("productos_digitales", "cambios"),
    "fabricante": ("garantia_fabricante", "cambios"),
    "proveedor": ("garantia_fabricante", "cambios"),
    "audifonos": ("definicion_productos_sensibles", "cambios"),
    "audífonos": ("definicion_productos_sensibles", "cambios"),
    "afeitadora": ("definicion_productos_sensibles", "cambios"),
    "depiladora": ("definicion_productos_sensibles", "cambios"),
    "color producto": ("cambios_por_color_diseno", "cambios"),
    # --- Términos y Condiciones ---
    "precio envio": ("delivery_costos", "terminos"),
    "precio envío": ("delivery_costos", "terminos"),
    "costo envio": ("delivery_costos", "terminos"),
    "delivery": ("delivery", "terminos"),
    "costo": ("delivery_costos", "terminos"),
    "tarifa": ("delivery_costos", "terminos"),
    "oka": ("pago_oka", "terminos"),
    "cuotas": ("pago_oka", "terminos"),
    "credito": ("pago_oka", "terminos"),
    "crédito": ("pago_oka", "terminos"),
    "izipay": ("pago_izipay", "terminos"),
    "pagoefectivo": ("pago_efectivo", "terminos"),
    "horario": ("contacto", "terminos"),
    "telefono": ("contacto", "terminos"),
    "teléfono": ("contacto", "terminos"),
    "correo": ("contacto", "terminos"),
    "contactar": ("contacto", "terminos"),
    "combo": ("arma_combo", "terminos"),
    # --- Keywords genéricos (al final para menor prioridad) ---
    "envio": (None, "envios"),
    "envío": (None, "envios"),
    "despacho": (None, "envios"),
    "entrega": (None, "envios"),
    "recojo": ("entrega_tienda", "envios"),
    "retiro": ("entrega_tienda", "envios"),
    "recoger": ("entrega_tienda", "envios"),
    "garantia": (None, "cambios"),
    "garantía": (None, "cambios"),
    "cambio": ("politica_devoluciones", "cambios"),
    "cambiar": ("politica_devoluciones", "cambios"),
}

def build_metadata_filter(section_slug=None, documento=None):
    """
    Construye la estructura de filtros DSL de Elasticsearch basados en metadatos jerárquicos.
    Permite acotar las búsquedas a secciones, subsecciones o documentos específicos.
    """
    filters = []
    if documento:
        filters.append({"term": {"metadata.documento.keyword": documento}})
    if section_slug:
        filters.append({
            "bool": {
                "should": [
                    {"term": {"metadata.section_slug.keyword": section_slug}},
                    {"term": {"metadata.subsection_slug.keyword": section_slug}},
                    {"term": {"metadata.sub_subsection_slug.keyword": section_slug}}
                ],
                "minimum_should_match": 1
            }
        })
    return filters

def format_results_for_agent(results, max_chars=1200):
    """
    Parsea y formatea los resultados puros recuperados desde Elasticsearch.
    Devuelve un string estructurado en texto plano amigable para el modelo de lenguaje.
    """
    if not results:
        return "No se encontró información relevante en las políticas de la tienda."
    blocks = []
    for i, (doc, score) in enumerate(results, start=1):
        meta = doc.metadata
        content = doc.page_content[:max_chars].replace("\n", " ").strip()
        sec_title = meta.get('section_title', '')
        if meta.get('subsection_title'):
            sec_title += f" -> {meta.get('subsection_title')}"
        if meta.get('sub_subsection_title'):
            sec_title += f" -> {meta.get('sub_subsection_title')}"
        
        blocks.append(
            f"[Fuente {i}] Documento: {meta.get('documento', '')} | Sección: {sec_title} | Páginas: {meta.get('page_start')}-{meta.get('page_end')}\n"
            f"Contenido: {content}"
        )
    return "\n\n".join(blocks)

# --- NORMALIZACIÓN DE BÚSQUEDA ---

# Palabras que terminan en "s" pero ya son singulares (no deben ser modificadas)
_EXCEPCIONES_SINGULAR = frozenset({
    "microondas", "gas", "luz", "plus", "windows", "series", "gratis",
    "inalambrico", "inalámbrico", "ups", "pos", "gps", "usb",
})

def _singularizar_es(palabra: str) -> str:
    """
    Convierte una palabra plural del español a su forma singular para búsquedas.
    Optimizado para nombres de productos de retail.
    """
    p = palabra.lower().strip()
    if len(p) <= 3 or p in _EXCEPCIONES_SINGULAR:
        return p
    if p.endswith(("ores", "ares", "iones", "ades")):
        return p[:-2]
    if p.endswith("ces"):
        return p[:-3] + "z"
    if p.endswith("s"):
        return p[:-1]
    return p

def _normalizar_palabras_busqueda(busqueda: str) -> list[str]:
    """
    Normaliza las palabras de búsqueda: singulariza plurales y elimina acentos.
    Retorna lista de palabras normalizadas (máximo 5).
    """
    palabras = busqueda.strip().split()[:5]
    return [_singularizar_es(p) for p in palabras if p]


# --- MAGENTO API HELPER ---

def call_magento_api(endpoint: str, params: dict = None, method: str = "GET"):
    """
    Llama a la API REST de Magento 2 en vivo usando el Access Token proporcionado en el archivo .env.
    Retorna la respuesta en formato JSON o un diccionario de error controlado si falla.
    """
    url = f"{MAGENTO_BASE_URL.rstrip('/')}/{endpoint.lstrip('/')}"
    headers = {
        "Authorization": f"Bearer {MAGENTO_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    try:
        if method.upper() == "GET":
            response = requests.get(url, headers=headers, params=params)
        else:
            response = requests.request(method, url, headers=headers, json=params)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        return {"error": f"Error API Magento: {str(e)}"}


# --- DEFINICIÓN DE HERRAMIENTAS (TOOLS) PARA EL AGENTE ---

@tool
def buscar_producto(busqueda: str) -> str:
    """
    Busca productos en el catálogo de Magento por nombre, marca o SKU.
    Solo devuelve productos que tienen stock disponible, con precio y link directo.
    Si no encuentra productos con stock, usa buscar_url_web para dar un link de categoría.
    """
    busqueda_norm = " ".join(_normalizar_palabras_busqueda(busqueda))
    if not busqueda_norm:
        busqueda_norm = busqueda.strip()

    p = {
        "searchCriteria[filter_groups][0][filters][0][field]": "name",
        "searchCriteria[filter_groups][0][filters][0][value]": f"%{busqueda_norm}%",
        "searchCriteria[filter_groups][0][filters][0][condition_type]": "like",
        "searchCriteria[pageSize]": 10,
        "fields": "items[sku,name,price,custom_attributes]"
    }
    d = call_magento_api("products", params=p)
    items = d.get("items", [])
    if not items:
        p_sku = {
            "searchCriteria[filter_groups][0][filters][0][field]": "sku",
            "searchCriteria[filter_groups][0][filters][0][value]": f"%{busqueda}%",
            "searchCriteria[filter_groups][0][filters][0][condition_type]": "like",
            "searchCriteria[pageSize]": 10,
            "fields": "items[sku,name,price,custom_attributes]"
        }
        d = call_magento_api("products", params=p_sku)
        items = d.get("items", [])

    if not items:
        return "No se encontraron productos en el catálogo."

    res = []
    for r in items:
        sku = r["sku"]
        stock_data = call_magento_api(f"stockItems/{sku}")
        qty = 0
        if "error" not in stock_data:
            qty = stock_data.get("qty", 0)

        if qty <= 0:
            continue

        special_price = None
        url_key = None
        for attr in r.get("custom_attributes", []):
            if attr.get("attribute_code") == "special_price" and attr.get("value"):
                special_price = float(attr["value"])
            elif attr.get("attribute_code") == "url_key" and attr.get("value"):
                url_key = attr["value"]
        precio = special_price if special_price else r.get("price", 0)
        url = f" | {HIRAOKA_BASE_URL}/{url_key}" if url_key else ""
        res.append(f"SKU: {sku} | {r['name']} | S/{precio}{url}")

        if len(res) >= 5:
            break

    if not res:
        return "No se encontraron productos con stock disponible para esa búsqueda."
    return "\n".join(res)

@tool
def buscar_catalogo_productos(busqueda: str) -> str:
    """
    Busca productos en el catálogo completo de BigQuery por nombre, marca, modelo, SKU o categoría.
    Úsala cuando el cliente pregunte por un producto, marca o categoría y necesites identificar el SKU.
    Ideal para búsquedas amplias como 'televisor Samsung', 'refrigeradora LG' o 'laptop Lenovo'.
    Maneja variaciones de plurales automáticamente (lavadoras -> lavadora, televisores -> televisor).
    """
    global bq_client
    if not bq_client:
        return "El servicio de catálogo de productos (BigQuery) no está disponible."

    palabras = _normalizar_palabras_busqueda(busqueda)
    if not palabras:
        return "Debes indicar qué producto buscas."

    content_conditions = []
    params = []
    for i, palabra in enumerate(palabras):
        param_name = f"w{i}"
        content_conditions.append(f"CONTAINS_SUBSTR(content, @{param_name})")
        params.append(bigquery.ScalarQueryParameter(param_name, "STRING", palabra))

    params.append(bigquery.ScalarQueryParameter("busqueda_completa", "STRING", busqueda.strip()))

    query = f"""
    SELECT sku, content
    FROM `pe-hiraoka-crmda-01.raw_dtrf_ga4.catalogo_embeddings_fixed`
    WHERE ({' AND '.join(content_conditions)})
       OR CONTAINS_SUBSTR(sku, @busqueda_completa)
    LIMIT 10
    """

    job_config = bigquery.QueryJobConfig(query_parameters=params)

    try:
        filas = list(bq_client.query(query, job_config=job_config).result())

        if not filas:
            return f"No se encontraron productos en el catálogo para: '{busqueda}'."

        res = []
        for row in filas:
            contenido = row.content.strip()
            nombre = contenido.split(" - ")[0] if " - " in contenido else contenido.split("  ")[0]
            res.append(f"SKU: {row.sku} | {nombre}")
        return "\n".join(res)
    except Exception as e:
        return f"Error al buscar en el catálogo: {str(e)}"

@tool
def consultar_stock_web(sku: str) -> str:
    """
    Consulta el stock físico REAL de almacén web y el PRECIO actual de uno o varios SKUs.
    Úsala SIEMPRE que el cliente pregunte '¿Tienen stock?', '¿Está disponible?' o quiera confirmar un precio en la web.
    Soporta múltiples SKUs separados por coma.
    """
    skus = [s.strip() for s in sku.split(",")]
    resultados = []
    
    for s in skus:
        # 1. Recuperar información base del producto (precio y nombre)
        p = {
             "searchCriteria[filter_groups][0][filters][0][field]": "sku",
             "searchCriteria[filter_groups][0][filters][0][value]": s,
             "searchCriteria[filter_groups][0][filters][0][condition_type]": "eq",
             "fields": "items[name,sku,price,custom_attributes]"
        }

        d = call_magento_api("products", params=p)
        items = d.get("items", [])

        if not items:
            resultados.append(f"SKU {s}: No encontrado en Magento.")
            continue

        i = items[0]
        name = i.get("name", "Producto")
        special_price = None
        for attr in i.get("custom_attributes", []):
            if attr.get("attribute_code") == "special_price" and attr.get("value"):
                special_price = float(attr["value"])
                break
        price = special_price if special_price else i.get("price", 0)
        
        # 2. Consultar la cantidad física disponible en el endpoint de inventario
        stock_data = call_magento_api(f"stockItems/{s}")
        if "error" in stock_data:
            qty = 0
        else:
            qty = stock_data.get("qty", 0)
            
        if qty > 0:
            resultados.append(f"{name} ({s}) -> Disponible | Stock: {int(qty)} uds | Precio: S/{price}")

    if not resultados:
        return "Los productos consultados no tienen stock disponible en este momento."
    return "\n".join(resultados)

@tool
def consultar_stock_tiendas(sku: str) -> str:
    """
    Consulta el stock físico de un producto (por SKU) en las diferentes tiendas físicas 
    (Lima, Miraflores, San Miguel, Independencia, San Juan de Lurigancho) mediante BigQuery.
    Úsala SOLO cuando el cliente pregunte por la disponibilidad en una tienda física.
    """
    global bq_client
    if not bq_client:
        return "El servicio de base de datos de tiendas físicas (BigQuery) no está configurado o disponible."

    # Consulta consolidada optimizada para BigQuery
    query = """
    SELECT
      stock.artcod AS codigo_producto,
      CASE amaesuc.sucpto
        WHEN '1' THEN 'Lima'
        WHEN '2' THEN 'Miraflores'
        WHEN '3' THEN 'San Miguel'
        WHEN '4' THEN 'Independencia'
        WHEN '5' THEN 'San Juan de Lurigancho'
      END AS nombre_tienda,
      SUM(SAFE_CAST(stock.artu04 AS FLOAT64)) AS stock_total_disponible,
      cat.artnom AS nombre_producto,
      MAX(stock.fecha_de_carga) AS ultima_actualizacion
    FROM
      `pe-hiraoka-crmda-01.CRM_DA_logistica.stock_amaesuc` AS amaesuc
    INNER JOIN
      `pe-hiraoka-crmda-01.CRM_DA_logistica.stock_consolidado_actual` AS stock
      ON SAFE_CAST(amaesuc.key_sucursal AS INT64) = stock.key_sucursal
    LEFT JOIN 
      `pe-hiraoka-crmda-01.raw_dtrf_ga4.catalogo` as cat
      ON stock.artcod = cat.artcod
    WHERE
      amaesuc.sucpto IN ('1', '2', '3', '4', '5')
      AND stock.artcod = @sku
      AND amaesuc.succod IN (
                          '008',
                          '011',
                          '021',
                          '047',
                          '050',
                          '051',
                          '053',
                          '061',
                          '301',
                          '401',
                          '471',
                          '501',
                          '601',
                          '701',
                          '801'
                        )
    GROUP BY
      codigo_producto,
      nombre_tienda,
      nombre_producto
    ORDER BY
      nombre_tienda ASC,
      stock_total_disponible DESC;
    """
    
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("sku", "STRING", sku)
        ]
    )
    
    try:
        query_job = bq_client.query(query, job_config=job_config)
        filas = list(query_job.result())
        
        if not filas:
            return f"No se encontró información de stock en tiendas físicas para el SKU: {sku}."
            
        nombre_prod = filas[0].nombre_producto or sku
        res = []

        for row in filas:
            stock_qty = int(row.stock_total_disponible) if row.stock_total_disponible else 0
            if stock_qty > 0:
                res.append(f"- Tienda {row.nombre_tienda}: Disponible ({stock_qty} uds)")

        if not res:
            return f"El producto {nombre_prod} (SKU: {sku}) no tiene stock disponible en ninguna tienda física en este momento."
        res.insert(0, f"Stock en tiendas físicas para: {nombre_prod} (SKU: {sku})")
        return "\n".join(res)
    except Exception as e:
        return f"Error al consultar el stock en tiendas: {str(e)}"

@tool
def informacion_tienda(query: str) -> str:
    """
    Busca información en las políticas de la tienda: términos y condiciones, legales de envío,
    y cambios/devoluciones/garantías (Elasticsearch con 3 índices).
    Úsala para resolver dudas sobre envíos, devoluciones, garantías, pagos o políticas operativas.
    """
    if not vector_stores:
        return "El sistema de información y políticas de la tienda no está disponible."

    query_lower = query.lower()
    section_slug = None
    store_key = None
    for keyword, (detected_slug, detected_store) in SECTION_HINTS.items():
        if keyword in query_lower:
            section_slug = detected_slug
            store_key = detected_store
            break

    try:
        if store_key and store_key in vector_stores:
            documento = RAG_DOCUMENTOS.get(store_key)
            filters = build_metadata_filter(
                section_slug=section_slug,
                documento=documento
            )
            results = vector_stores[store_key].similarity_search_with_score(
                query=query,
                k=5,
                filter=filters or None,
            )
        else:
            results = []
            for store in vector_stores.values():
                try:
                    store_results = store.similarity_search_with_score(
                        query=query,
                        k=3,
                    )
                    results.extend(store_results)
                except Exception:
                    continue

        valid_results = []
        for r in results:
            if r[1] is None or r[1] >= 0.65:
                valid_results.append(r)

        valid_results = sorted(valid_results, key=lambda x: x[1] if x[1] is not None else 0, reverse=True)[:5]
        return format_results_for_agent(valid_results)
    except Exception as e:
        return f"Error al buscar en políticas: {str(e)}"

def _validar_documento(documento: str):
    """
    Valida y clasifica un documento de identidad peruano (DNI o RUC).
    Retorna (tipo, id_busqueda, doc_completo, error).
    - tipo: "DNI", "RUC_PN" (persona natural), "RUC_PJ" (persona jurídica)
    - id_busqueda: el valor a enviar al API (DNI extraído o RUC completo)
    - doc_completo: el documento completo limpio
    - error: mensaje de error si es inválido, None si es válido
    """
    doc = documento.strip().replace(" ", "").replace("-", "").replace(".", "")

    if not doc.isdigit():
        return ("", "", doc, "El documento debe contener solo números. Verifica e intenta de nuevo.")

    if len(doc) == 8:
        return ("DNI", doc, doc, None)

    if len(doc) == 11:
        prefijo = doc[:2]
        if prefijo not in ("10", "15", "17", "20"):
            return ("", "", doc,
                    f"El RUC ingresado tiene un prefijo inválido ('{prefijo}'). "
                    "Los prefijos válidos son: 10, 15 o 17 (persona natural) y 20 (persona jurídica).")

        pesos = [5, 4, 3, 2, 7, 6, 5, 4, 3, 2]
        suma = sum(int(doc[i]) * pesos[i] for i in range(10))
        residuo = suma % 11
        digito_esperado = 11 - residuo
        if digito_esperado == 10:
            digito_esperado = 0
        elif digito_esperado == 11:
            digito_esperado = 1

        if int(doc[10]) != digito_esperado:
            return ("", "", doc,
                    "El RUC ingresado no es válido (dígito verificador incorrecto). "
                    "Verifica el número e intenta de nuevo.")

        if prefijo in ("10", "15", "17"):
            dni_extraido = doc[2:10]
            return ("RUC_PN", dni_extraido, doc, None)
        else:
            return ("RUC_PJ", doc, doc, None)

    return ("", "", doc,
            f"El documento ingresado tiene {len(doc)} dígitos. "
            "Debe ser un DNI (8 dígitos) o un RUC (11 dígitos).")


def _buscar_despachos(id_busqueda, api_key):
    resp = requests.get(
        "https://hiraoka.dispatchtrack.com/api/external/v1/dispatches",
        params={"i": id_busqueda},
        headers={"X-AUTH-TOKEN": api_key},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") == "ok" and data.get("response"):
        return data["response"]
    return []


@tool
def consultar_estado_pedido(documento: str) -> str:
    """
    Consulta el estado de entrega de pedidos usando el DNI o RUC del cliente vía DispatchTrack.
    Úsala SIEMPRE que el cliente pregunte por su pedido, cuándo llega, estado de envío,
    tracking o seguimiento. Acepta DNI (8 dígitos), RUC persona natural (11 dígitos, empieza en 10)
    o RUC persona jurídica (11 dígitos, empieza en 20).
    """
    api_key = os.getenv("API_KEY_BEETRACK")
    if not api_key:
        return "El servicio de seguimiento de pedidos no está disponible en este momento."

    tipo, id_busqueda, doc_completo, error = _validar_documento(documento)
    if error:
        return error

    try:
        id_sin_cero = id_busqueda.lstrip("0")

        dispatches = _buscar_despachos(id_sin_cero, api_key)

        if not dispatches and id_sin_cero != id_busqueda:
            dispatches = _buscar_despachos(id_busqueda, api_key)

        if not dispatches and tipo == "RUC_PN":
            dispatches = _buscar_despachos(doc_completo, api_key)

        if not dispatches and tipo == "RUC_PJ":
            dni_parte = doc_completo[2:10].lstrip("0")
            dispatches = _buscar_despachos(dni_parte, api_key)
    except Exception as e:
        return f"Error al consultar el estado del pedido: {str(e)}"

    if not dispatches:
        tipo_doc = "DNI" if tipo == "DNI" else "RUC"
        return f"No se encontraron pedidos activos asociados al {tipo_doc} {doc_completo}."

    STATUS_MAP = {
        "pending": "Pendiente de despacho",
        "on_route": "En ruta de entrega",
        "completed": "Entregado",
        "delivered": "Entregado",
        "partial": "Entrega parcial",
        "failed": "No se pudo entregar",
        "cancelled": "Cancelado",
    }

    resultados = []

    for d in dispatches:
        estado = STATUS_MAP.get(d.get("status"), d.get("status", "Desconocido"))
        pedido_id = d.get("identifier", "N/A")

        tags = {t["name"]: t["value"].strip() for t in d.get("tags", [])}
        fecha_min = tags.get("FECHA MIN ENTREGA", "")
        fecha_max = tags.get("FECHA MAX ENTREGA", "")

        productos = []
        for item in d.get("items", []):
            nombre_prod = item.get("name", "").strip()
            qty = item.get("quantity", 1)
            productos.append(f"{nombre_prod} (x{qty})")

        lineas = [f"Pedido: {pedido_id} | Estado: {estado}"]

        if productos:
            lineas.append(f"Productos: {', '.join(productos)}")

        if fecha_min and fecha_max:
            lineas.append(f"Ventana de entrega estimada: {fecha_min} a {fecha_max}")
        elif fecha_min:
            lineas.append(f"Entrega estimada desde: {fecha_min}")

        resultados.append("\n".join(lineas))

    tipo_doc = "DNI" if tipo == "DNI" else "RUC"
    return f"Se encontraron {len(dispatches)} pedido(s) para el {tipo_doc} {doc_completo}:\n\n" + "\n\n---\n\n".join(resultados)

# --- DICCIONARIO DE URLs FRECUENTES (extraído de reportes WhatsApp) ---

URLS_FRECUENTES = {
    # Electrohogar - Refrigeración
    "refrigeradora": "electrohogar/refrigeracion/refrigeradoras",
    "frigobar": "electrohogar/refrigeracion/frigobares",
    "congeladora": "electrohogar/refrigeracion/congeladoras-y-exhibidoras",
    "exhibidora": "electrohogar/refrigeracion/congeladoras-y-exhibidoras",
    "side by side": "electrohogar/refrigeracion/side-by-side",
    # Electrohogar - Lavado
    "lavadora": "electrohogar/lavado-y-limpieza/lavadoras",
    "lavaseca": "electrohogar/lavado-y-limpieza/lavasecas",
    "secadora de ropa": "electrohogar/lavado-y-limpieza/secadoras-de-ropa",
    "aspiradora": "electrohogar/lavado-y-limpieza/aspirado",
    # Electrohogar - Cocina
    "cocina a gas": "electrohogar/cocina-y-empotrables/cocinas-a-gas",
    "cocina empotrable": "electrohogar/cocina-y-empotrables/cocinas-empotrable",
    "cocina de pie": "electrohogar/cocina-y-empotrables/cocinas-de-pie",
    "microondas": "electrohogar/cocina-y-empotrables/hornos-microondas",
    "campana extractora": "electrohogar/cocina-y-empotrables/campanas-extractoras",
    "cocina y empotrable": "electrohogar/cocina-y-empotrables",
    # Electrohogar - Electrodomésticos
    "licuadora": "electrohogar/electrodomesticos/licuadoras",
    "freidora": "electrohogar/electrodomesticos/freidoras",
    "olla arrocera": "electrohogar/electrodomesticos/ollas-arroceras",
    "batidora": "electrohogar/electrodomesticos/batidoras",
    "horno electrico": "electrohogar/electrodomesticos/hornos-electricos",
    "hervidor": "electrohogar/electrodomesticos/hervidores",
    "cafetera": "electrohogar/electrodomesticos/cafetera",
    "plancha de ropa": "electrohogar/electrodomesticos/planchas",
    "extractor": "electrohogar/electrodomesticos/extractores-y-exprimidores",
    "tostadora": "electrohogar/electrodomesticos/tostadoras-y-sandwicheras",
    # Cómputo
    "laptop": "computo-y-tablets/computadoras/laptops",
    "computadora": "computo-y-tablets/computadoras",
    "tablet": "computo-y-tablets/tablets",
    "impresora": "computo-y-tablets/impresoras-y-tintas/impresoras",
    "all in one": "computo-y-tablets/computadoras/all-in-one",
    "laptop gamer": "computo-y-tablets/computadoras/laptop-gamer",
    "proyector": "computo-y-tablets/proyectores",
    # Televisores
    "televisor": "televisores/televisores",
    "smart tv": "televisores/televisores",
    "tv 32": "televisores-32-pulgadas",
    "tv 43": "televisores-43-pulgadas",
    "tv 50": "televisores-50-pulgadas",
    "tv 55": "televisores-55-pulgadas",
    "tv 65": "televisores-65-pulgadas",
    "tv 75": "televisores-75-pulgadas",
    # Celulares
    "celular": "celulares-y-telefonia/celulares",
    "iphone": "celulares-y-telefonia/mundo-apple/iphone",
    "smartphone": "celulares-y-telefonia/celulares",
    "smartwatch": "celulares-y-telefonia/smartwatch",
    # Audio
    "parlante": "audio-y-musica/audio/parlantes",
    "equipo de sonido": "audio-y-musica/audio/equipo-de-sonido",
    "audifono": "audio-y-musica/audifonos",
    # Climatización
    "ventilador": "climatizacion/ventiladores",
    "aire acondicionado": "climatizacion/aire-acondicionado",
    "terma": "climatizacion/termas-y-rapiduchas/termas-y-calentadores",
    "deshumedecedor": "climatizacion/deshumedecedores-y-purificadores-de-aire",
    # Salud
    "tensiometro": "salud-y-bienestar/instrumental-medico/tensiometros",
    "glucometro": "salud-y-bienestar/instrumental-medico/glucometro",
    "secadora de cabello": "salud-y-bienestar/cuidado-personal/secadoras-de-cabello",
    "plancha de cabello": "salud-y-bienestar/cuidado-personal/plancha-de-cabello",
    "recortador": "salud-y-bienestar/cuidado-personal/recortadores-de-cabello",
    "afeitadora": "salud-y-bienestar/cuidado-personal/afeitadoras",
    "bicimoto": "salud-y-bienestar/deportes/bicimotos",
    # Otros
    "combo": "combos",
    "maquina de coser": "electrohogar/maquinas-de-coser/maquina-de-coser",
    "playstation": "gaming/consolas/consola-play-station",
    "play station": "gaming/consolas/consola-play-station",
    # Páginas estáticas / CMS
    "como comprar": "como-comprar",
    "pago efectivo": "pago-efectivo",
    "cambios y devoluciones": "cambios-y-devoluciones",
    "devolucion": "cambios-y-devoluciones",
    "catalogo": "catalogo-hiraoka",
    "reclamo": "lreclamaciones",
    "libro de reclamaciones": "lreclamaciones",
    "terminos y condiciones": "terminos-y-condiciones",
    "crear cuenta": "customer/account/create/",
    "registrarse": "customer/account/create/",
    "interbank": "interbank",
    "provincia": "provincias",
    "negocio": "negocios-y-empresas",
    "empresa": "negocios-y-empresas",
    "envio": "legales-envios",
}

HIRAOKA_BASE_URL = "https://hiraoka.com.pe"


def _normalize(text):
    return unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode("ascii")


def _buscar_en_arbol_categorias(nodo, consulta_norm, resultados, max_results=3):
    if len(resultados) >= max_results:
        return
    nombre = nodo.get("name", "")
    if consulta_norm in _normalize(nombre):
        url_path = nodo.get("custom_attributes", {})
        if isinstance(url_path, list):
            for attr in url_path:
                if attr.get("attribute_code") == "url_path":
                    url_path = attr.get("value", "")
                    break
            else:
                url_path = ""
        if not url_path:
            url_path = nodo.get("url_path", "")
        if url_path:
            resultados.append(f"{nombre}: {HIRAOKA_BASE_URL}/{url_path}")
    for hijo in nodo.get("children_data", []):
        _buscar_en_arbol_categorias(hijo, consulta_norm, resultados, max_results)


@tool
def buscar_url_web(consulta: str) -> str:
    """
    Busca la URL de una categoría de productos o página informativa en hiraoka.com.pe.
    Úsala SIEMPRE que el cliente mencione una categoría de productos, una página del sitio web,
    o pida cualquier tipo de enlace. Ejemplos: 'quiero ver refrigeradoras', 'catálogo',
    'libro de reclamaciones', 'cómo comprar', 'laptops', 'dónde veo televisores', 'pásame el link'.
    NO es necesario que el cliente diga explícitamente 'link' o 'url' para usar esta herramienta.
    """
    consulta_norm = _normalize(consulta)

    for keyword, path in URLS_FRECUENTES.items():
        if keyword in consulta_norm:
            return f"{HIRAOKA_BASE_URL}/{path}"

    data = call_magento_api("categories")
    if "error" in data:
        return "No se pudo consultar las categorías en este momento."

    resultados = []
    _buscar_en_arbol_categorias(data, consulta_norm, resultados)

    if resultados:
        return "\n".join(resultados)
    return "No se encontró una página para esa consulta en hiraoka.com.pe."


# --- CONFIGURACIÓN E INICIALIZACIÓN DEL AGENTE REACTIVO ---

tools = [buscar_producto, buscar_catalogo_productos, consultar_stock_web, consultar_stock_tiendas, informacion_tienda, consultar_estado_pedido, buscar_url_web]

system_prompt = (
    "Eres el asistente oficial de ventas de nuestro Retail E-commerce. Sé amable, profesional y útil.\n\n"
    "--- INFORMACIÓN FIJA ---\n"
    "Datos de contacto: Teléfono: (01) 680-3800 | WhatsApp: 969872372 | Correo: servicioalcliente@hiraoka.com.pe\n"
    "Empresa: IMPORTACIONES HIRAOKA S.A.C., RUC 20100016681, Av. Abancay 594, Cercado de Lima.\n"
    "Métodos de pago: Izipay, PagoEfectivo, OKA (sujeto a evaluación, consultas al (01) 705-1717).\n"
    "Horarios de tiendas físicas (Lima, Miraflores, San Miguel, Independencia, SJL):\n"
    "  - Lunes a Viernes: 10:00 a.m. a 8:00 p.m.\n"
    "  - Sábados: 10:00 a.m. a 9:00 p.m.\n"
    "  - Domingos: 10:30 a.m. a 7:00 p.m.\n\n"
    "--- USO DE HERRAMIENTAS ---\n"
    "1. 'buscar_producto': Busca productos en Magento por nombre o SKU. "
    "Solo devuelve productos CON STOCK disponible (ya filtra internamente). genera links de cada producto si se tendria.\n"
    "2. 'buscar_catalogo_productos': Busca en el catálogo completo de BigQuery por nombre, marca, modelo, SKU o categoría. "
    "Úsala para búsquedas generales ('quiero un televisor Samsung', 'tienen lavadoras LG?'). "
    "Devuelve SKUs que luego debes validar con 'buscar_producto' o 'consultar_stock_web' antes de mostrar al cliente.\n"
    "3. 'consultar_stock_web': Confirma precio y stock en el canal digital (requiere SKU).\n"
    "4. 'consultar_stock_tiendas': Stock en tiendas físicas presenciales (requiere SKU).\n"
    "5. 'informacion_tienda': Políticas de envío, cambios/devoluciones/garantías, y términos y condiciones.\n"
    "6. 'consultar_estado_pedido': Rastrea el estado de entrega de un pedido por DNI o RUC.\n"
    "7. 'buscar_url_web': Obtiene el link de una CATEGORÍA o página en hiraoka.com.pe. "
    "Úsala SIEMPRE que el cliente pregunte por productos para incluir el link de la categoría.\n\n"
    "--- FLUJO DE ATENCIÓN AL CLIENTE ---\n"
    "Cuando el cliente pregunta por un producto:\n"
    "1. Busca con 'buscar_producto' (ya filtra por stock). Si no hay resultados, intenta con 'buscar_catalogo_productos'.\n"
    "2. Usa 'buscar_url_web' para obtener el link de la CATEGORÍA (NO envíes links individuales de producto, solo de categoría).\n"
    "3. Presenta SOLO los productos con stock disponible + el link de la categoría para que navegue.\n\n"
    "Cuando NO se encuentra el producto exacto (modelo específico, accesorio puntual):\n"
    "1. NO respondas solo 'no lo tenemos'. Busca alternativas compatibles de la misma marca o tipo.\n"
    "2. Si el cliente busca un accesorio para un modelo específico (ej: 'control para TV Miray MS40-E200'), "
    "busca accesorios genéricos de esa marca que puedan ser compatibles y sugiere verificar compatibilidad.\n"
    "3. Siempre ofrece la categoría general para que el cliente explore más opciones.\n\n"
    "REGLAS CRÍTICAS:\n"
    "- Responde SIEMPRE de forma conversacional y en texto plano. NUNCA devuelvas JSON u objetos crudos.\n"
    "- NUNCA muestres productos sin stock. Solo presenta opciones con disponibilidad confirmada.\n"
    "- NO muestres la cantidad de stock al cliente (no decir '50 unidades'). Solo muestra nombre, precio y link.\n"
    "- Si 'buscar_producto' devuelve productos con stock (incluyen link del producto), usa esos links directos.\n"
    "- Si NO hay productos con stock, usa 'buscar_url_web' para dar al cliente el link general de la categoría para que explore."
)

# Memoria de LangGraph para mantener el contexto en conversaciones de múltiples turnos
checkpointer = MemorySaver()
agent = create_react_agent(model, tools, prompt=system_prompt, checkpointer=checkpointer)

def ejecutar_agente(prompt: str, thread_id: str = "default"):
    """
    Función de orquestación principal que despacha el input del usuario al Agente ReAct.
    Se encarga de limpiar y extraer el texto plano de la respuesta para evitar errores de renderizado en interfaces como Streamlit.
    """
    config = {"configurable": {"thread_id": thread_id}}
    result = agent.invoke({"messages": [("user", prompt)]}, config)
    
    # Extraer el contenido del último mensaje generado por la IA
    content = result["messages"][-1].content
    
    # Langchain/Gemini en ciertas interacciones (ej. Tool calls complejas) puede retornar un Array de objetos dict 
    # en vez de un simple String. Lo aplanamos para garantizar compatibilidad con interfaces visuales y consolas.
    if isinstance(content, list):
        text_parts = [part["text"] for part in content if isinstance(part, dict) and "text" in part]
        return " ".join(text_parts).strip()
    
    return str(content).strip()

if __name__ == "__main__":
    print("=== Agente de Ventas de Retail E-commerce ===")
    print("Escribe 'salir' para terminar.\n")

    thread_id = "default"

    while True:
        user_input = input("Tú: ").strip()
        if user_input.lower() in ("salir", "exit", "quit"):
            print("¡Hasta luego!")
            break
        if not user_input:
            continue

        response = ejecutar_agente(user_input, thread_id=thread_id)
        print(f"Asistente: {response}\n")

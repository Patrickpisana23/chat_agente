import streamlit as st
from magento_agent import ejecutar_agente

# Configuración inicial de la página web generada por Streamlit
st.set_page_config(page_title="Agente de Ventas Retail", page_icon="🛒")

# Título y saludo de bienvenida en la interfaz
st.title("🛒 Agente de Ventas de Retail E-commerce")
st.write("¡Hola! Soy el asistente virtual. ¿En qué puedo ayudarte hoy?")

# Inicializar historial de chat en el estado de la sesión
# Esto garantiza que al refrescarse el componente, los mensajes anteriores no se borren visualmente
if "messages" not in st.session_state:
    st.session_state.messages = []

# Renderizar en la interfaz gráfica los mensajes enviados previamente
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# Capturar el input del usuario desde el chat
if prompt := st.chat_input("Escribe tu consulta aquí..."):
    # Guardar la pregunta del usuario en el historial
    st.session_state.messages.append({"role": "user", "content": prompt})
    
    # Mostrar inmediatamente la pregunta del usuario en el chat visual
    with st.chat_message("user"):
        st.markdown(prompt)

    # Contenedor para la respuesta del asistente (con animación de carga)
    with st.chat_message("assistant"):
        with st.spinner("Pensando..."):
            try:
                # Ejecutar el agente orquestador definido en magento_agent.py
                # Se utiliza session_id fijo por simplicidad, pero podría ser dinámico por sesión web
                response = ejecutar_agente(prompt, thread_id="streamlit_user")
                
                # Renderizar la respuesta final en la interfaz
                st.markdown(response)
                
                # Guardar la respuesta generada en el historial de chat de Streamlit
                st.session_state.messages.append({"role": "assistant", "content": response})
            except Exception as e:
                # Manejo amigable de errores por si fallan las APIs subyacentes
                st.error(f"Ocurrió un error al procesar tu solicitud: {str(e)}")

# llm_client.py
"""
Capa de abstracción para LLMs locales (Ollama) y en la nube (Anthropic Claude).

Prioridad:
  1. Ollama (local, Orange Pi 5) — sin costo, sin latencia de red
  2. Anthropic Claude API — fallback si Ollama no está disponible o LLM_PROVIDER=anthropic

Configuración (.env):
  LLM_PROVIDER=ollama          # "ollama" | "anthropic" | "auto" (default: "auto")
  OLLAMA_HOST=http://localhost:11434
  OLLAMA_MODEL=phi3.5          # phi3.5 ~2.3GB RAM | llama3.2:3b ~2GB | mistral:7b ~4GB
  ANTHROPIC_API_KEY=...        # solo si LLM_PROVIDER=anthropic o como fallback
  ANTHROPIC_MODEL=claude-haiku-4-5-20251001
"""
import json
import os
import time

import httpx
from dotenv import load_dotenv

load_dotenv()

LLM_PROVIDER    = os.getenv("LLM_PROVIDER",    "auto")
OLLAMA_HOST     = os.getenv("OLLAMA_HOST",     "http://localhost:11434")
OLLAMA_MODEL    = os.getenv("OLLAMA_MODEL",    "phi3.5")
ANTHROPIC_KEY   = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")


# ── OLLAMA ──────────────────────────────────────────────────────────────────

def _call_ollama(messages: list[dict], temperature: float = 0.1) -> str:
    """
    Llama a la API nativa de Ollama (POST /api/chat).
    messages: [{"role": "system"|"user"|"assistant", "content": "..."}]
    """
    payload = {
        "model":   OLLAMA_MODEL,
        "messages": messages,
        "stream":  False,
        "options": {"temperature": temperature},
    }
    with httpx.Client(timeout=180.0) as client:
        resp = client.post(f"{OLLAMA_HOST}/api/chat", json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data["message"]["content"]


def ollama_available() -> bool:
    """Verifica si Ollama está corriendo y tiene el modelo configurado."""
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(f"{OLLAMA_HOST}/api/tags")
            if resp.status_code != 200:
                return False
            models = [m["name"].split(":")[0] for m in resp.json().get("models", [])]
            model_base = OLLAMA_MODEL.split(":")[0]
            return model_base in models
    except Exception:
        return False


# ── ANTHROPIC ───────────────────────────────────────────────────────────────

def _call_anthropic(messages: list[dict], temperature: float = 0.1) -> str:
    """
    Llama a la Anthropic Messages API.
    Intenta importar el SDK; si no está, usa httpx directo.
    """
    system_content = ""
    user_messages  = []
    for m in messages:
        if m["role"] == "system":
            system_content = m["content"]
        else:
            user_messages.append(m)

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        kwargs: dict = {
            "model":       ANTHROPIC_MODEL,
            "max_tokens":  1024,
            "temperature": temperature,
            "messages":    user_messages,
        }
        if system_content:
            kwargs["system"] = system_content
        msg = client.messages.create(**kwargs)
        return msg.content[0].text

    except ImportError:
        # Fallback HTTP directo si el SDK no está instalado
        headers = {
            "x-api-key":         ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "content-type":      "application/json",
        }
        body: dict = {
            "model":      ANTHROPIC_MODEL,
            "max_tokens": 1024,
            "messages":   user_messages,
        }
        if system_content:
            body["system"] = system_content
        with httpx.Client(timeout=60.0) as client:
            resp = client.post("https://api.anthropic.com/v1/messages",
                               headers=headers, json=body)
            resp.raise_for_status()
            return resp.json()["content"][0]["text"]


# ── INTERFAZ PÚBLICA ─────────────────────────────────────────────────────────

def call_llm(
    prompt: str,
    system: str = "",
    temperature: float = 0.1,
    retries: int = 2,
) -> str:
    """
    Llama al LLM configurado. Ollama primero, Anthropic como fallback.

    Args:
        prompt:      Mensaje del usuario
        system:      System prompt (instrucciones para el modelo)
        temperature: 0.0 = determinístico, 1.0 = creativo. Default 0.1 para agentes.
        retries:     Reintentos ante errores transitorios

    Returns:
        Respuesta de texto del modelo
    """
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    use_ollama     = LLM_PROVIDER in ("ollama", "auto")
    use_anthropic  = LLM_PROVIDER in ("anthropic", "auto") and bool(ANTHROPIC_KEY)

    last_error: Exception | None = None

    for attempt in range(retries + 1):
        if use_ollama:
            try:
                result = _call_ollama(messages, temperature)
                if attempt > 0:
                    print(f"  ✓ LLM (Ollama) respondió en intento {attempt + 1}")
                return result
            except Exception as e:
                last_error = e
                print(f"  ⚠️ Ollama falló (intento {attempt + 1}): {e}")
                if use_anthropic:
                    print("  → Intentando Anthropic como fallback...")
                    try:
                        return _call_anthropic(messages, temperature)
                    except Exception as ae:
                        last_error = ae
                        print(f"  ⚠️ Anthropic también falló: {ae}")
                if attempt < retries:
                    time.sleep(3)

        elif use_anthropic:
            try:
                return _call_anthropic(messages, temperature)
            except Exception as e:
                last_error = e
                print(f"  ⚠️ Anthropic falló (intento {attempt + 1}): {e}")
                if attempt < retries:
                    time.sleep(3)

    raise RuntimeError(
        f"LLM no disponible. Último error: {last_error}\n"
        f"  • Ollama: asegúrate de que esté corriendo con 'ollama serve' y el modelo '{OLLAMA_MODEL}' descargado.\n"
        f"  • Anthropic: configura ANTHROPIC_API_KEY en .env."
    )


def call_llm_json(
    prompt: str,
    system: str = "",
    temperature: float = 0.05,
) -> dict | list:
    """
    Igual que call_llm pero espera respuesta JSON válida.
    Intenta extraer el JSON aunque el modelo incluya texto adicional.
    """
    response = call_llm(prompt, system, temperature)

    # Intentar parsear directamente
    try:
        return json.loads(response)
    except json.JSONDecodeError:
        pass

    # Extraer bloque JSON de la respuesta (el modelo puede incluir texto antes/después)
    import re
    for pattern in [r'\{[\s\S]*\}', r'\[[\s\S]*\]']:
        match = re.search(pattern, response)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                continue

    raise ValueError(
        f"El modelo no devolvió JSON válido.\nRespuesta:\n{response[:500]}"
    )

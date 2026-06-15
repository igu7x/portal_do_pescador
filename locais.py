"""
Busca locais de pesca esportiva próximos a um CEP usando a API da Anthropic
(Claude Haiku) com saída estruturada via `tool_choice`.

Estratégia: forçamos o modelo a chamar a tool `entregar_locais`, que define
o schema do resultado. Isso garante JSON válido sem precisar de parsing
heurístico.

Uso:
    info = buscar_locais_pesca("74210-050")
    info["locais"]  # lista de dicts com nome, tipo, cidade_uf, dica, etc.
"""

from __future__ import annotations

import os
import time
from typing import Any

import requests

from tools import normalize_cep, validar_cep


ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
HTTP_TIMEOUT = 60


LOCAIS_TOOL = {
    "name": "entregar_locais",
    "description": (
        "Entrega a lista final de locais de pesca esportiva encontrados "
        "perto da cidade do usuário. Chame APENAS quando tiver a lista pronta."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "locais": {
                "type": "array",
                "minItems": 5,
                "maxItems": 10,
                "items": {
                    "type": "object",
                    "properties": {
                        "nome":            {"type": "string", "description": "Nome do local."},
                        "tipo":            {"type": "string", "description": "rio, lago, represa, açude, pesqueiro, lagoa, baía, cachoeira"},
                        "cidade_uf":       {"type": "string", "description": "Cidade e UF, ex: 'Aragarças/GO'"},
                        "distancia_aprox": {"type": "string", "description": "Distância aprox. da cidade do usuário. Ex: '~120km'"},
                        "peixes":          {"type": "string", "description": "Espécies mais comuns. Ex: 'tucunaré, traíra, piranha'"},
                        "dica":            {"type": "string", "description": "1-2 frases curtas — melhor época, modalidade, dica prática."},
                        "maps_query":      {"type": "string", "description": "Query pronta pra busca no Google Maps. Ex: 'Represa Serra da Mesa Uruaçu GO'"},
                    },
                    "required": ["nome", "tipo", "cidade_uf", "peixes", "dica", "maps_query"],
                },
            }
        },
        "required": ["locais"],
    },
}


def _call_anthropic(payload: dict[str, Any], retries: int = 2) -> dict[str, Any]:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY não configurada")

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    last_exc: Exception | None = None
    for tentativa in range(retries + 1):
        try:
            resp = requests.post(ANTHROPIC_URL, headers=headers, json=payload, timeout=HTTP_TIMEOUT)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in {429, 500, 502, 503, 504, 529} and tentativa < retries:
                time.sleep(1.5 * (tentativa + 1))
                continue
            raise RuntimeError(f"Anthropic {resp.status_code}: {resp.text[:300]}")
        except requests.RequestException as exc:
            last_exc = exc
            if tentativa < retries:
                time.sleep(1.5 * (tentativa + 1))
                continue
            raise RuntimeError(f"falha de rede com Anthropic: {exc}") from exc
    if last_exc:
        raise RuntimeError(str(last_exc))
    return {}


def buscar_locais_pesca(cep_raw: str) -> dict[str, Any]:
    """
    Retorna {"cidade", "uf", "locais": [...]} ou {"erro": "..."}
    """
    cep = normalize_cep(cep_raw)
    if not cep:
        return {"erro": "CEP inválido."}

    info = validar_cep(cep)
    if not info.get("sucesso"):
        return {"erro": info.get("erro", "CEP não encontrado.")}

    cidade = info["cidade"]
    uf = info["uf"]

    model = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

    system = (
        "Você é um expert em pesca esportiva no Brasil. Conhece os principais "
        "pontos de pesca de cada região: rios, lagos, represas, açudes, lagoas, "
        "pesqueiros pagos. Recomende SEMPRE locais reais e conhecidos — nunca "
        "invente nomes. Para cada local dê espécies comuns, melhor época e uma "
        "dica prática curta. Priorize spots em até ~200km da cidade pedida, "
        "misturando opções gratuitas (rios, represas) e pesqueiros pagos."
    )

    user_msg = (
        f"Liste de 6 a 9 locais de pesca esportiva próximos a "
        f"**{cidade}/{uf}** (até ~200km de distância). Inclua diversidade: "
        f"rios, represas, lagos e ao menos 1 pesqueiro pago se houver na região. "
        f"Para cada local: nome, tipo, cidade/UF, distância aproximada de {cidade}, "
        f"espécies comuns, dica de 1-2 frases, e uma query pronta pro Google Maps. "
        f"Chame `entregar_locais` com a lista final."
    )

    payload = {
        "model": model,
        "max_tokens": 2500,
        "system": system,
        "messages": [{"role": "user", "content": user_msg}],
        "tools": [LOCAIS_TOOL],
        "tool_choice": {"type": "tool", "name": "entregar_locais"},
    }

    data = _call_anthropic(payload)
    for block in data.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == "entregar_locais":
            inp = block.get("input", {})
            locais = inp.get("locais", []) or []
            return {
                "cidade": cidade,
                "uf": uf,
                "locais": locais,
            }

    return {"cidade": cidade, "uf": uf, "locais": [], "erro": "modelo não devolveu locais"}

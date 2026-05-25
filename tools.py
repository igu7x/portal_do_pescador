"""
Ferramentas client-side que o Claude pode invocar durante a conversa:
  - validar_cep:        consulta ViaCEP e retorna endereço
  - consultar_frete:    estima frete regional para um CEP

A busca de produtos é feita pelo próprio Claude via a tool nativa
`web_search` (server-side, executada nos servidores da Anthropic), então
não precisa de tool client-side aqui.
"""

from __future__ import annotations

import re
from typing import Any

import requests

VIACEP_URL = "https://viacep.com.br/ws/{cep}/json/"
DEFAULT_TIMEOUT = 12


# --------------------------------------------------------------------------- #
# CEP                                                                         #
# --------------------------------------------------------------------------- #

def normalize_cep(cep: str) -> str | None:
    """Aceita '12345-678' ou '12345678'. Retorna 8 dígitos ou None."""
    if not cep:
        return None
    digits = re.sub(r"\D", "", cep)
    return digits if len(digits) == 8 else None


# Alias interno (compatibilidade com versões antigas).
_normalize_cep = normalize_cep


def validar_cep(cep: str) -> dict[str, Any]:
    cep_digits = normalize_cep(cep)
    if not cep_digits:
        return {"sucesso": False, "erro": "CEP inválido. Informe 8 dígitos."}

    try:
        resp = requests.get(VIACEP_URL.format(cep=cep_digits), timeout=DEFAULT_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        return {"sucesso": False, "erro": f"Falha ao consultar ViaCEP: {exc}"}

    if data.get("erro"):
        return {"sucesso": False, "erro": "CEP não encontrado na base ViaCEP."}

    return {
        "sucesso": True,
        "cep": cep_digits,
        "logradouro": data.get("logradouro", ""),
        "bairro": data.get("bairro", ""),
        "cidade": data.get("localidade", ""),
        "uf": data.get("uf", ""),
    }


# --------------------------------------------------------------------------- #
# Frete (estimativa regional)                                                 #
# --------------------------------------------------------------------------- #

_FRETE_ESTIMATIVA_BASE = {
    "0": 22.0, "1": 25.0, "2": 28.0, "3": 30.0, "4": 38.0,
    "5": 42.0, "6": 48.0, "7": 32.0, "8": 30.0, "9": 32.0,
}


def _estimar_frete_regional(cep_digits: str) -> float:
    return _FRETE_ESTIMATIVA_BASE.get(cep_digits[0], 35.0)


def consultar_frete(cep: str, frete_gratis: bool = False) -> dict[str, Any]:
    cep_digits = normalize_cep(cep)
    if not cep_digits:
        return {"sucesso": False, "erro": "CEP inválido."}

    if frete_gratis:
        return {
            "sucesso": True, "cep": cep_digits, "valor": 0.0,
            "moeda": "BRL", "fonte": "frete_gratis_anunciado",
        }

    return {
        "sucesso": True,
        "cep": cep_digits,
        "valor": _estimar_frete_regional(cep_digits),
        "moeda": "BRL",
        "fonte": "estimativa_regional",
        "aviso": (
            "Frete estimado por faixa do CEP; o valor exato é confirmado "
            "no checkout do site da loja."
        ),
    }


# --------------------------------------------------------------------------- #
# Schemas (client-side tools)                                                 #
# --------------------------------------------------------------------------- #

TOOL_SCHEMAS = [
    {
        "name": "validar_cep",
        "description": (
            "Valida um CEP brasileiro e retorna o endereço (cidade/UF). "
            "Chame logo após o usuário informar o CEP."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cep": {"type": "string", "description": "CEP (12345-678 ou 12345678)"},
            },
            "required": ["cep"],
        },
    },
    {
        "name": "consultar_frete",
        "description": (
            "Retorna o frete estimado para o CEP do usuário. Chame para cada "
            "produto candidato após buscar via web_search. Se a página do "
            "produto exibir 'frete grátis', passe frete_gratis=true (retorna 0)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cep": {"type": "string", "description": "CEP de destino do usuário."},
                "frete_gratis": {
                    "type": "boolean",
                    "description": "True se a página do produto exibe frete grátis. Default false.",
                },
            },
            "required": ["cep"],
        },
    },
    {
        "name": "registrar_recomendacao",
        "description": (
            "Salva no banco de dados a recomendação que você apresentou ao "
            "usuário, pra ela aparecer no histórico em conversas futuras. "
            "Chame SEMPRE após apresentar a recomendação final no formato "
            "padrão."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nome_produto": {"type": "string", "description": "Nome exato do produto."},
                "preco": {"type": "number", "description": "Preço em reais (sem frete)."},
                "frete": {"type": "number", "description": "Frete em reais (0 se grátis)."},
                "total": {"type": "number", "description": "Custo total = preço + frete."},
                "loja": {"type": "string", "description": "Nome da loja/site."},
                "link": {"type": "string", "description": "URL canônica do produto."},
            },
            "required": ["nome_produto", "preco", "frete", "total", "loja", "link"],
        },
    },
    {
        "name": "adicionar_ao_carrinho",
        "description": (
            "Adiciona um produto ao carrinho persistente do usuário. Use "
            "quando o usuário confirmar que quer adicionar a recomendação "
            "(ex: 'adiciona no carrinho', 'pode colocar', 'quero esse'). "
            "Passe exatamente os mesmos dados da recomendação apresentada."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nome_produto": {"type": "string"},
                "preco": {"type": "number"},
                "frete": {"type": "number"},
                "total": {"type": "number"},
                "loja": {"type": "string"},
                "link": {"type": "string"},
            },
            "required": ["nome_produto", "preco", "frete", "total", "loja", "link"],
        },
    },
    {
        "name": "visualizar_carrinho",
        "description": (
            "Retorna TODOS os itens do carrinho do usuário, com cada item "
            "(nome, preço, frete, total, loja, link) e o GRAND TOTAL "
            "somado. Chame quando o usuário pedir pra ver o carrinho "
            "('meu carrinho', 'o que tem no carrinho', 'mostrar carrinho', etc.)."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "remover_do_carrinho",
        "description": (
            "Remove um item específico do carrinho pelo ID. O ID vem do "
            "resultado de visualizar_carrinho. Use quando o usuário pedir "
            "pra remover/tirar algum item."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "item_id": {
                    "type": "integer",
                    "description": "ID do item no carrinho (retornado por visualizar_carrinho).",
                },
            },
            "required": ["item_id"],
        },
    },
    {
        "name": "limpar_carrinho",
        "description": (
            "Remove TODOS os itens do carrinho. Use quando o usuário pedir "
            "'esvaziar carrinho', 'limpar carrinho'. Confirme antes de chamar."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]


# --------------------------------------------------------------------------- #
# Dispatcher                                                                  #
# --------------------------------------------------------------------------- #

def executar_tool(nome: str, parametros: dict[str, Any]) -> dict[str, Any]:
    if nome == "validar_cep":
        return validar_cep(parametros.get("cep", ""))
    if nome == "consultar_frete":
        return consultar_frete(
            cep=parametros.get("cep", ""),
            frete_gratis=parametros.get("frete_gratis", False),
        )
    return {"erro": f"Tool desconhecida: {nome}"}

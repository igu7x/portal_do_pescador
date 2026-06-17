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
# Busca de produto no Mercado Livre (server-side, confiável)                  #
# --------------------------------------------------------------------------- #

_ML_API_URL = "https://api.mercadolibre.com/sites/MLB/search"
_ML_LISTA_URL = "https://lista.mercadolivre.com.br/{slug}"

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}


def _slug(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"[áàâã]", "a", s)
    s = re.sub(r"[éèê]", "e", s)
    s = re.sub(r"[íì]", "i", s)
    s = re.sub(r"[óòôõ]", "o", s)
    s = re.sub(r"[úù]", "u", s)
    s = re.sub(r"ç", "c", s)
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def _tentar_anthropic_search(query: str, preco_max: float | None) -> dict[str, Any]:
    """
    Faz chamada Anthropic isolada (separada da conversa) com web_search +
    web_fetch ativas, pedindo SÓ um JSON com o produto. Tem timeout curto
    e instrução explícita de formato.
    """
    import json as _json
    import os as _os

    api_key = _os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return {"sucesso": False, "erro": "sem ANTHROPIC_API_KEY"}

    filtro_preco = f" (max R$ {preco_max:.0f})" if preco_max and preco_max > 0 else ""
    prompt = (
        f'Buscar UM produto de pesca, PRIORIZANDO AMAZON (mais estável). Só use outras lojas (Mercado Livre, Centauro, Casas Bahia, Decathlon) se Amazon não tiver opção.\n\n'
        f'Query: "{query}"{filtro_preco}\n\n'
        "PASSOS:\n"
        f'1. PRIMEIRA busca: "{query} amazon comprar" — Amazon BR é mais confiável.\n'
        '2. Procure URLs Amazon: ".../dp/[10 caracteres]" (ex: /dp/B08XYZ1234).\n'
        '3. Se Amazon não der resultado bom, tenta busca alternativa com Mercado Livre, Centauro ou Casas Bahia.\n'
        '4. URLs aceitas:\n'
        '   - Amazon: ".../dp/XXXXXXXXXX" ← PREFERÍVEL\n'
        '   - Centauro: ".../p/[id]"\n'
        '   - Casas Bahia: ".../p/[id]"\n'
        '   - Mercado Livre: "produto.mercadolivre.com.br/MLB-..."\n'
        '   - EVITE Magazine Luiza (tá instável agora).\n\n'
        "RESPOSTA OBRIGATÓRIA: SOMENTE este JSON, NADA antes ou depois:\n"
        '{"nome_produto":"NOME EXATO", "preco":NUMERO, "loja":"Nome da loja", '
        '"link":"URL ESPECÍFICA QUE APARECEU NA BUSCA", "frete_gratis":true_ou_false}\n\n'
        "REGRAS:\n"
        "- link: APENAS URL que apareceu REALMENTE nos resultados (nunca invente).\n"
        '- NUNCA use URL com "lista.", "/c/", "/search", "/s?", "/busca", "?q=".\n'
        '- NUNCA invente MLB-1234567890 ou similares.\n'
        "- Sem narração, sem ```json, sem texto antes/depois.\n"
        '- Se realmente não achou URL específica em 3 buscas: {"erro":"nada"}'
    )

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": _os.environ.get("ANTHROPIC_MODEL") or "claude-haiku-4-5-20251001",
        "max_tokens": 1500,
        "tools": [
            {
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 3,
                "user_location": {"type": "approximate", "country": "BR"},
            },
        ],
        "messages": [{"role": "user", "content": prompt}],
    }

    # Loop curto pra suportar pause_turn
    messages = list(payload["messages"])
    for _ in range(5):
        try:
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                json={**payload, "messages": messages},
                timeout=45,
            )
        except requests.RequestException as exc:
            return {"sucesso": False, "erro": f"anthropic: {exc}"}

        if resp.status_code != 200:
            return {"sucesso": False, "erro": f"anthropic {resp.status_code}"}

        data = resp.json()
        blocos = data.get("content") or []
        stop_reason = data.get("stop_reason")
        messages.append({"role": "assistant", "content": blocos})

        if stop_reason == "pause_turn":
            continue

        # Resposta final — extrai texto
        texto = "".join(b.get("text", "") for b in blocos if b.get("type") == "text").strip()
        texto_limpo = re.sub(r"^```(?:json)?\s*", "", texto)
        texto_limpo = re.sub(r"\s*```\s*$", "", texto_limpo)

        # ESTRATÉGIA 1: JSON puro
        try:
            parsed = _json.loads(texto_limpo)
            if isinstance(parsed, dict) and "erro" not in parsed:
                link = str(parsed.get("link", "")).strip()
                if link and any(s in link for s in ("MLB-", "/dp/", "/p/", "/produto/")):
                    return _montar_resultado_anthropic(parsed, link)
        except (_json.JSONDecodeError, ValueError, TypeError):
            pass

        # ESTRATÉGIA 2: JSON inline no meio do texto
        m_json = re.search(r"\{[^{}]*\"link\"[^{}]*\}", texto_limpo, re.DOTALL)
        if m_json:
            try:
                parsed = _json.loads(m_json.group(0))
                link = str(parsed.get("link", "")).strip()
                if link and any(s in link for s in ("MLB-", "/dp/", "/p/", "/produto/")):
                    return _montar_resultado_anthropic(parsed, link)
            except (_json.JSONDecodeError, ValueError, TypeError):
                pass

        # ESTRATÉGIA 3: extração agressiva de URL no texto
        padroes_url = [
            r"https://produto\.mercadolivre\.com\.br/MLB-\d+[\w\-]*",
            r"https://www\.mercadolivre\.com\.br/[\w\-]+-MLB-?\d+-_JM",
            r"https://www\.mercadolivre\.com\.br/[\w\-]+_MLB[\d\-]+_JM",
            r"https://articulo\.mercadoli(?:vre|bre)\.com\.br/MLB-\d+[\w\-]*",
            r"https://(?:www\.)?amazon\.com\.br/[^\s\"'<>]*?/dp/[A-Z0-9]{10}",
            r"https://(?:www\.)?amazon\.com\.br/dp/[A-Z0-9]{10}",
            r"https://(?:www\.)?amazon\.com\.br/gp/product/[A-Z0-9]{10}",
            r"https://(?:www\.)?magazineluiza\.com\.br/[\w\-/]+/p/\d+[\w\-/]*",
            r"https://(?:www\.)?casasbahia\.com\.br/[\w\-/]+/p/\d+[\w\-/]*",
            r"https://(?:www\.)?centauro\.com\.br/[\w\-/]+/p/[\w\-]+",
        ]
        link_achado = ""
        for pat in padroes_url:
            m = re.search(pat, texto, re.IGNORECASE)
            if m:
                cand = re.sub(r"[.,;:!?)\]'\"]+$", "", m.group(0))
                # Rejeita placeholders
                if any(p in cand.lower() for p in (
                    "mlb-1234567890", "mlb-xxx", "/dp/xxx", "/p/xxx",
                )):
                    continue
                link_achado = cand
                break

        if not link_achado:
            return {"sucesso": False, "erro": "anthropic sem URL canonica"}

        # Extrai nome
        nome = query.title()
        m_nome = re.search(r"\*\*([^*\n]{8,150})\*\*", texto)
        if m_nome:
            cand = m_nome.group(1).strip()
            if not cand.lower().startswith(("preço", "preco", "total", "frete")):
                nome = cand
        else:
            m_nome = re.search(r'"nome_produto"\s*:\s*"([^"]+)"', texto)
            if m_nome:
                nome = m_nome.group(1).strip()

        # Extrai preço
        preco = 0.0
        m_preco = re.search(r'"preco"\s*:\s*(\d+(?:\.\d+)?)', texto)
        if m_preco:
            try:
                preco = float(m_preco.group(1))
            except ValueError:
                pass
        else:
            m_preco = re.search(r"R\$\s*(\d[\d.]*[,\.]\d{2})", texto)
            if m_preco:
                v = m_preco.group(1).replace(".", "").replace(",", ".")
                try:
                    preco = float(v)
                except ValueError:
                    pass

        # Frete grátis (heurística)
        frete_gratis = bool(re.search(r"frete\s+gr[áa]tis|free\s+shipping|\"frete_gratis\":\s*true", texto, re.IGNORECASE))

        # Loja pelo domínio do link
        loja = "Mercado Livre"
        ll = link_achado.lower()
        if "amazon" in ll:
            loja = "Amazon"
        elif "magazineluiza" in ll or "magazinevoce" in ll:
            loja = "Magazine Luiza"
        elif "casasbahia" in ll:
            loja = "Casas Bahia"
        elif "centauro" in ll:
            loja = "Centauro"

        return {
            "sucesso": True,
            "fonte": "anthropic_extracao_agressiva",
            "nome_produto": nome,
            "preco": preco,
            "loja": loja,
            "link": link_achado,
            "frete_gratis": frete_gratis,
        }

    return {"sucesso": False, "erro": "anthropic loop esgotado"}


def _montar_resultado_anthropic(parsed: dict, link: str) -> dict[str, Any]:
    """Empacota dict do JSON do Anthropic em formato esperado."""
    return {
        "sucesso": True,
        "fonte": "anthropic_json",
        "nome_produto": str(parsed.get("nome_produto", "")).strip(),
        "preco": float(parsed.get("preco", 0) or 0),
        "loja": str(parsed.get("loja", "Mercado Livre")).strip() or "Mercado Livre",
        "link": link,
        "frete_gratis": bool(parsed.get("frete_gratis", False)),
    }


def _tentar_api_ml(query: str, preco_max: float | None) -> dict[str, Any]:
    """Tenta a API pública do Mercado Livre."""
    params: dict[str, Any] = {"q": query, "limit": 15, "condition": "new"}
    if preco_max and preco_max > 0:
        params["price"] = f"*-{int(preco_max)}"
    try:
        resp = requests.get(
            _ML_API_URL, params=params, headers=_BROWSER_HEADERS, timeout=10
        )
        if resp.status_code != 200:
            return {"sucesso": False, "erro": f"api ml status {resp.status_code}"}
        data = resp.json()
        results = data.get("results") or []
        for produto in results:
            link = (produto.get("permalink") or "").strip()
            if not link:
                continue
            # Confere que NÃO é URL de lista
            if "lista.mercado" in link.lower() or "/c/" in link.lower():
                continue
            preco = float(produto.get("price", 0))
            if preco <= 0:
                continue
            frete_gratis = bool((produto.get("shipping") or {}).get("free_shipping"))
            return {
                "sucesso": True,
                "fonte": "api_ml",
                "nome_produto": produto.get("title", "").strip(),
                "preco": round(preco, 2),
                "loja": "Mercado Livre",
                "link": link,
                "frete_gratis": frete_gratis,
                "thumbnail": produto.get("thumbnail", ""),
            }
        return {"sucesso": False, "erro": "api ml sem resultados validos"}
    except requests.RequestException as exc:
        return {"sucesso": False, "erro": f"api ml: {exc}"}


def _tentar_scrape_ml(query: str) -> dict[str, Any]:
    """
    Fallback: pega HTML da página de listagem do ML e extrai a primeira
    URL canônica de produto via regex.
    """
    url = _ML_LISTA_URL.format(slug=_slug(query))
    try:
        resp = requests.get(url, headers=_BROWSER_HEADERS, timeout=12)
        if resp.status_code != 200:
            return {"sucesso": False, "erro": f"scrape ml status {resp.status_code}"}
        html = resp.text
        # URLs canônicas: produto.mercadolivre.com.br/MLB-XXX ou
        # www.mercadolivre.com.br/...-MLB-XXX-_JM
        padroes = [
            r"https://produto\.mercadolivre\.com\.br/MLB-\d+[\w\-]*",
            r"https://www\.mercadolivre\.com\.br/[\w\-/]+-MLB-\d+-_JM",
        ]
        for pat in padroes:
            achados = re.findall(pat, html)
            if achados:
                vistos = set()
                unicos = [u for u in achados if not (u in vistos or vistos.add(u))]
                link = unicos[0]
                # Extrai título (do próprio HTML — heurística)
                titulo_match = re.search(
                    r"<h2[^>]*>([^<]{8,120})</h2>", html
                )
                nome = titulo_match.group(1).strip() if titulo_match else query.title()
                # Tenta extrair preço da página
                preco_match = re.search(
                    r"\"price\"\s*:\s*(\d+(?:\.\d+)?)", html
                )
                preco = float(preco_match.group(1)) if preco_match else 0.0
                return {
                    "sucesso": True,
                    "fonte": "scrape_ml",
                    "nome_produto": nome,
                    "preco": round(preco, 2),
                    "loja": "Mercado Livre",
                    "link": link,
                    "frete_gratis": False,
                    "thumbnail": "",
                }
        return {"sucesso": False, "erro": "scrape ml sem URL canonica"}
    except requests.RequestException as exc:
        return {"sucesso": False, "erro": f"scrape ml: {exc}"}


_SINAIS_404 = (
    "não conseguimos encontrar esta página",
    "nao conseguimos encontrar esta pagina",
    "página não encontrada",
    "pagina nao encontrada",
    "produto não encontrado",
    "anúncio não encontrado",
    "page not found",
    "404 not found",
    "we couldn't find that page",
    "this page isn't available",
    "esta página não está disponível",
    "essa página também estava por aqui",
    # Magalu — página de erro temporária + página 404
    "alguma coisa deu errado",
    "esta página não está disponível agora",
    "tente de novo em alguns instantes",
    "achei que essa página também estava por aqui",
)

# Sinais de produto que existe mas está fora de estoque / indisponível pra compra
_SINAIS_INDISPONIVEL = (
    "não disponível",
    "nao disponivel",
    "não temos previsão de quando este produto",
    "nao temos previsao de quando este produto",
    "produto está indisponível",
    "produto esta indisponivel",
    "currently unavailable",
    "atualmente sem estoque",
    "esgotado no momento",
    "anúncio pausado",
    "anuncio pausado",
    "anúncio finalizado",
    "anuncio finalizado",
    "escolha outra variação",
    "escolha outra variacao",
    "produto encerrado",
    "out of stock",
    "sem estoque",
)

_PADROES_PRECO = (
    r'<meta\s+(?:property|itemprop)="(?:product:)?price(?::amount)?"\s+content="(\d+(?:\.\d+)?)"',
    r'<meta\s+content="(\d+(?:\.\d+)?)"\s+(?:property|itemprop)="(?:product:)?price(?::amount)?"',
    r'<meta\s+itemprop="price"\s+content="(\d+(?:\.\d+)?)"',
    r'"price"\s*:\s*"?(\d+(?:\.\d+)?)"?',
    r'class="a-offscreen"[^>]*>\s*R?\$?\s*(\d{1,3}(?:[.\s]\d{3})*,\d{2})',
    r'R\$\s*(\d{1,3}(?:[.\s]?\d{3})*,\d{2})',
)


def _verificar_url_e_preco(url: str, timeout: int = 6) -> tuple[bool, float | None]:
    """
    UMA única chamada HTTP que faz duas coisas:
      - verifica se a página existe (não 404)
      - extrai o preço real, se conseguir

    Retorna (url_existe, preco_ou_None).
    Otimizado pra ser rápido: lê só ~200KB.
    """
    if not url or not url.startswith(("http://", "https://")):
        return False, None
    try:
        resp = requests.get(
            url, headers=_BROWSER_HEADERS, timeout=timeout,
            allow_redirects=True, stream=True,
        )
        status = resp.status_code

        # 404/410/451 → claramente não existe
        if status in (404, 410, 451):
            resp.close()
            return False, None

        # 403/503 → anti-bot, assume que existe pro humano (sem preço)
        if status in (403, 503):
            resp.close()
            return True, None

        if not (200 <= status < 400):
            resp.close()
            return False, None

        # Lê até 300KB (Amazon esconde preço fundo na página)
        try:
            chunk = b""
            for piece in resp.iter_content(chunk_size=16384, decode_unicode=False):
                chunk += piece
                if len(chunk) >= 300_000:
                    break
        except requests.RequestException:
            pass
        finally:
            resp.close()

        html = chunk.decode("utf-8", errors="ignore")
        texto_lower = html.lower()

        # Checa 404 disfarçado
        if any(s in texto_lower for s in _SINAIS_404):
            return False, None

        # Checa produto indisponível / fora de estoque
        if any(s in texto_lower for s in _SINAIS_INDISPONIVEL):
            return False, None

        # Extrai preço
        preco = None
        for pat in _PADROES_PRECO:
            m = re.search(pat, html, re.IGNORECASE)
            if not m:
                continue
            raw = m.group(1).replace(" ", "")
            if "," in raw:
                raw = raw.replace(".", "").replace(",", ".")
            try:
                p = float(raw)
            except ValueError:
                continue
            if 5.0 <= p <= 50000.0:
                preco = round(p, 2)
                break

        return True, preco
    except requests.RequestException:
        return False, None


def _url_eh_especifica(link: str) -> bool:
    """URL específica de produto = tem identificador único, NÃO é listagem."""
    if not link:
        return False
    l = link.lower()
    # Rejeita listagens
    if any(s in l for s in (
        "lista.mercadolivre", "lista.mercadolibre", "listado.mercadolibre",
        "mercadolivre.com.br/c/", "mercadolibre.com.br/c/",
        "amazon.com.br/s?", "amazon.com.br/s/",
        "amazon.com.br/b?", "amazon.com.br/b/",
        "/search?", "/busca?", "?q=", "&q=", "?keyword=",
    )):
        return False
    # Rejeita Magazine Luiza temporariamente (páginas instáveis durante apresentação)
    if "magazineluiza.com.br" in l or "magazinevoce.com.br" in l:
        return False
    # Rejeita placeholders alucinados
    if any(p in l for p in (
        "mlb-1234567890", "mlb-xxx", "mlb-xxxxx",
        "/dp/xxxxxxxxxx", "/dp/xxx", "/p/xxxxx", "example.com",
    )):
        return False
    # Aceita se tem identificador conhecido OU é loja pequena (não-marketplace)
    tem_id = any(s in l for s in (
        "mlb-", "/dp/", "/gp/product/", "/p/", "/produto/", "/produtos/",
    ))
    if tem_id:
        return True
    # Loja pequena sem padrão conhecido — aceita se NÃO é dos marketplaces grandes
    marketplaces_grandes = (
        "mercadolivre.com.br", "mercadolibre.com.br",
        "amazon.com.br", "magazineluiza.com.br", "magazinevoce.com.br",
        "casasbahia.com.br", "centauro.com.br", "decathlon.com.br",
        "shopee.com.br",
    )
    eh_marketplace = any(m in l for m in marketplaces_grandes)
    return not eh_marketplace  # loja pequena → aceita


def buscar_produto(query: str, preco_max: float | None = None) -> dict[str, Any]:
    """
    Busca UM produto real com URL ESPECÍFICA E ACESSÍVEL.

    Cada candidato precisa passar em DOIS testes:
      a) URL é específica de produto (não lista/busca/categoria)
      b) URL existe de verdade (HEAD HTTP, não retorna 404)

    Estratégia:
      1. API pública do Mercado Livre
      2. Anthropic search (até 4 variações de query)
      3. Scraping HTML do ML
    """
    if not query or not query.strip():
        return {"sucesso": False, "erro": "query vazia"}

    def _validar_e_corrigir(resultado: dict[str, Any]) -> dict[str, Any] | None:
        """
        UMA chamada HTTP: confere se URL existe + corrige preço se diferente.
        Retorna o dict atualizado (se OK) ou None (se URL não passa).
        """
        if not resultado.get("sucesso"):
            return None
        link = resultado.get("link", "")
        if not _url_eh_especifica(link):
            return None
        existe, preco_real = _verificar_url_e_preco(link)
        if not existe:
            return None
        # Se conseguiu preço e for muito diferente do snippet, sobrescreve
        if preco_real and preco_real > 0:
            preco_snippet = float(resultado.get("preco", 0) or 0)
            if preco_snippet <= 0 or abs(preco_real - preco_snippet) / max(preco_real, 1) > 0.15:
                resultado["preco"] = preco_real
                resultado["preco_fonte"] = "pagina_real"
        preco_final = float(resultado.get("preco", 0) or 0)
        # Amazon: se não conseguiu preço NEM da página NEM do snippet, é sinal de
        # produto indisponível (Amazon stub) ou URL inválida → rejeita
        if "amazon.com.br" in link.lower() and preco_final <= 0:
            return None
        return resultado

    # 1) API ML
    r = _tentar_api_ml(query, preco_max)
    validado = _validar_e_corrigir(r)
    if validado:
        return validado

    # 2) Anthropic search — várias variações, da mais específica pra mais simples.
    # Limpa "ruído" da query (palavras qualificativas que ofuscam o produto core)
    palavras_ruido = {
        "barata", "barato", "barat", "iniciante", "basica", "basico",
        "qualquer", "tanto", "faz", "uma", "um", "para", "pra", "pro",
    }
    palavras_query = [w for w in query.split() if w.lower() not in palavras_ruido]
    query_simples = " ".join(palavras_query).strip() or query

    variacoes = [
        query,
        query_simples,
        f"{query_simples} amazon",
    ]
    # Adiciona variantes genéricas conhecidas que sempre existem na Amazon
    query_lower = query.lower()
    if "vara" in query_lower:
        variacoes.append("vara pesca telescopica amazon")
    elif "molinete" in query_lower:
        variacoes.append("molinete pesca amazon")
    elif "anzol" in query_lower:
        variacoes.append("anzol pesca amazon kit")
    elif "isca" in query_lower:
        variacoes.append("isca artificial pesca amazon")
    elif "kit" in query_lower:
        variacoes.append("kit pesca completo amazon")
    else:
        variacoes.append(f"{query_simples} pesca amazon")

    # Remove duplicatas mantendo ordem
    vistos: set[str] = set()
    variacoes_unicas: list[str] = []
    for v in variacoes:
        v_norm = v.strip().lower()
        if v_norm and v_norm not in vistos:
            vistos.add(v_norm)
            variacoes_unicas.append(v)

    for q in variacoes_unicas:
        r = _tentar_anthropic_search(q, preco_max)
        validado = _validar_e_corrigir(r)
        if validado:
            return validado

    # 3) Scrape ML
    r = _tentar_scrape_ml(query)
    validado = _validar_e_corrigir(r)
    if validado:
        return validado

    # 4) FALLBACK INFALÍVEL — queries super genéricas que Amazon SEMPRE tem
    fallbacks_seguros = [
        "vara pesca amazon",
        "molinete pesca amazon",
        "kit pesca amazon",
        "anzol pesca amazon",
    ]
    # Coloca o que parece relevante primeiro
    if "molinete" in query_lower:
        fallbacks_seguros.insert(0, fallbacks_seguros.pop(1))
    elif "kit" in query_lower:
        fallbacks_seguros.insert(0, fallbacks_seguros.pop(2))
    elif "anzol" in query_lower or "isca" in query_lower:
        fallbacks_seguros.insert(0, fallbacks_seguros.pop(3))

    for q in fallbacks_seguros:
        if q in vistos:
            continue
        r = _tentar_anthropic_search(q, None)  # sem limite de preço
        validado = _validar_e_corrigir(r)
        if validado:
            return validado

    return {
        "sucesso": False,
        "erro": "nao consegui achar URL especifica e acessivel",
    }


# --------------------------------------------------------------------------- #
# Schemas (client-side tools)                                                 #
# --------------------------------------------------------------------------- #

TOOL_SCHEMAS = [
    {
        "name": "buscar_produto",
        "description": (
            "Busca UM produto de pesca real no Mercado Livre e retorna nome, "
            "preço, loja, URL canônica do produto (sempre específica, nunca "
            "página de busca), e se tem frete grátis. Use esta tool ao invés "
            "de web_search/web_fetch para buscas de produto — é mais "
            "confiável e sempre retorna URL canônica."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Termo de busca direto e específico em PT-BR. Ex: "
                        "'vara telescópica 1.80m', 'molinete 3000 marine sports', "
                        "'kit pesca completo'."
                    ),
                },
                "preco_max": {
                    "type": "number",
                    "description": "Preço máximo opcional em reais.",
                },
            },
            "required": ["query"],
        },
    },
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
    if nome == "buscar_produto":
        return buscar_produto(
            query=parametros.get("query", ""),
            preco_max=parametros.get("preco_max"),
        )
    if nome == "validar_cep":
        return validar_cep(parametros.get("cep", ""))
    if nome == "consultar_frete":
        return consultar_frete(
            cep=parametros.get("cep", ""),
            frete_gratis=parametros.get("frete_gratis", False),
        )
    return {"erro": f"Tool desconhecida: {nome}"}

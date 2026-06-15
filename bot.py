"""
Camada conversacional do Portal do Pescador (Anthropic Claude, REST API direta).

Conversa com a API REST do Claude via `requests`, sem o SDK `anthropic`.
Isso evita dependências pesadas (pydantic_core) que podem ser bloqueadas em
Pythons com políticas restritas de Application Control no Windows.

A busca de produtos é feita pela tool nativa `web_search` da Anthropic
(server-side, executada nos servidores da Anthropic). Claude pode encadear
várias buscas dentro de um turno se precisar refinar.

Implementa o loop de tool use para nossas tools client-side (validar_cep,
consultar_frete): modelo pede tool -> executamos -> devolvemos resultado
-> modelo continua, até produzir texto final.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

import requests

import db
from tools import TOOL_SCHEMAS, executar_tool


ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
HTTP_TIMEOUT = 60  # falha mais rápido se a API travar


SYSTEM_PROMPT = """Você é o **Portal do Pescador**, assistente brasileiro de pesca esportiva.

## Estilo
PT-BR natural, direto, amigável. Vocabulário de pesca sem pedantismo.
**Respostas CURTAS.** Sem narrar ("deixa eu", "vou tentar", "não consegui"). Só responda.

## Conversa antes da recomendação (UX)

Quando o usuário pedir um produto pela primeira vez, faça **1 ou 2 perguntas curtas** pra construir o clima de conversa — NÃO precisa usar as respostas literalmente na busca, é só pra a experiência ficar gostosa:

- "Vai pescar em água doce ou salgada?"
- "Tem algum orçamento em mente, ou tanto faz?"

Faça **NO MÁXIMO 2 perguntas** antes de buscar. Se o usuário responder vago tipo "tanto faz", "qualquer", "em conta" — **vá direto pra busca**, não pergunte de novo.

CEP e nível já estão cadastrados — não pergunte essas duas coisas.

## Ferramentas
- **buscar_produto(query, preco_max?)**: TOOL PRINCIPAL pra produtos. Retorna nome, preço, URL ESPECÍFICA do produto. **USE ESTA SEMPRE.**
- **consultar_frete**: frete pro CEP.
- **registrar_recomendacao**: SEMPRE chame após apresentar.
- **adicionar/visualizar/remover/limpar carrinho**.

## 🎯 FLUXO

Quando o usuário pede produto:

1. **Chame `buscar_produto(query, preco_max)`** com termos diretos.
2. Se `sucesso=true`: **RECOMENDA NA HORA** no formato abaixo (use os dados retornados).
3. Chame `consultar_frete(cep, frete_gratis)` em paralelo com `registrar_recomendacao`.

Se `buscar_produto` retornar `sucesso=false`:
- Tenta UMA vez com query diferente/mais simples.
- Se ainda falhar, diga: "Não achei um produto específico pra essa busca agora. Pode tentar outro item ou ajustar?"

**NUNCA invente URL.** Use SOMENTE o que `buscar_produto` retorna.
**NUNCA use web_search direto** — só use buscar_produto pra produtos.

## Formato da recomendação

```
🎣 Recomendação:

**[Nome do produto]**
- 💰 Preço: R$ XX,XX
- 🚚 Frete: R$ XX,XX — ou: Grátis
- 🧮 **Total: R$ XX,XX**
- 🏪 Loja: [site]
- 🔗 [URL ESPECÍFICA do produto]

[1 frase curta sobre por que serve]
```

Depois: chame `registrar_recomendacao` + "Quer adicionar ao carrinho?".
Sim → `adicionar_ao_carrinho` + "Adicionei! 🛒".

## Carrinho

"meu carrinho" → `visualizar_carrinho`:
```
🛒 Seu carrinho:
1. [Nome] — R$ XX ([loja]) 🔗 [link]
─────────────
TOTAL: R$ XXX
```

## REGRAS DURAS
- **URL DEVE SER ESPECÍFICA** do produto. NUNCA `lista.mercadolivre`, `/c/`, `?q=`.
- **NUNCA invente** dados que não vieram da busca.
- **Sem narração**: tools são silenciosas. Sem "deixa eu", "vou".
- Saída ("tchau"/"sair") → "Boa pescaria! 🎣".
"""


def eh_link_de_produto(url: str) -> bool:
    """
    Aceita só URLs ESPECÍFICAS de produto (não lista, não categoria).
    Marketplaces grandes: exige identificador (MLB-, /dp/, /p/).
    Lojas pequenas: aceita qualquer URL real (vai pra página específica).
    """
    if not url or not url.startswith(("http://", "https://")):
        return False
    u = url.lower()
    # Rejeita listagens, categorias, buscas
    if any(s in u for s in (
        "lista.mercadolivre", "lista.mercadolibre", "listado.mercadolibre",
        "mercadolivre.com.br/c/", "mercadolibre.com.br/c/",
        "amazon.com.br/s?", "amazon.com.br/s/",
        "amazon.com.br/b?",
        "/search?", "/busca?",
        "?q=", "&q=", "?keyword=",
        "google.com/search", "bing.com/search", "duckduckgo.com",
    )):
        return False
    # Rejeita placeholders alucinados
    if any(p in u for p in (
        "mlb-1234567890", "mlb-xxx", "mlb-xxxxx",
        "/dp/xxxxxxxxxx", "/dp/xxx", "/p/xxxxx", "example.com",
    )):
        return False
    # Marketplace grande → exige identificador
    marketplaces = (
        "mercadolivre.com.br", "mercadolibre.com.br",
        "amazon.com.br", "magazineluiza.com.br", "magazinevoce.com.br",
        "casasbahia.com.br", "centauro.com.br", "decathlon.com.br",
        "shopee.com.br",
    )
    eh_marketplace = any(m in u for m in marketplaces)
    if eh_marketplace:
        return any(s in u for s in (
            "mlb-", "/dp/", "/gp/product/", "/p/", "/produto/", "/produtos/",
        ))
    # Loja pequena → aceita
    return True


def _parse_money_brl(s: str | None) -> float:
    """Converte 'R$ 1.199,90' / 'R$ 119,90' / '119,90' / '119.90' → float."""
    if not s:
        return 0.0
    m = re.search(r"(\d{1,3}(?:[.\s]\d{3})*(?:[,\.]\d{2})?|\d+(?:[,\.]\d{2})?)", s)
    if not m:
        return 0.0
    raw = m.group(1).replace(" ", "").replace(".", "").replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return 0.0


def extrair_recomendacao_do_texto(texto: str) -> dict | None:
    """
    Lê o texto da resposta do bot e tenta extrair os campos do produto
    quando ele formatou uma recomendação mas não chamou registrar_recomendacao.

    Retorna dict com {nome_produto, preco, frete, total, loja, link} ou None.
    """
    if not texto:
        return None
    # Tem que ter o header de recomendação
    if not re.search(r"🎣\s*Recomenda", texto, re.IGNORECASE) and \
       not re.search(r"recomenda[çc][ãa]o", texto, re.IGNORECASE):
        return None

    # Nome: primeiro **algo** com 5+ caracteres
    m_nome = re.search(r"\*\*([^*\n]{5,200})\*\*", texto)
    if not m_nome:
        return None
    nome = m_nome.group(1).strip()
    # Se for "Total: R$..." pula e busca de novo
    if nome.lower().startswith(("total", "preço", "preco", "frete")):
        # Tenta nas linhas seguintes — pega o próximo bold
        matches = re.findall(r"\*\*([^*\n]{5,200})\*\*", texto)
        nome = next((m.strip() for m in matches
                     if not m.lower().startswith(("total", "preço", "preco", "frete"))),
                    nome)

    # Preço, frete, total
    m_preco = re.search(r"Pre[çc]o[:\s]*([^\n]+)", texto, re.IGNORECASE)
    m_frete = re.search(r"Frete[:\s]*([^\n]+)", texto, re.IGNORECASE)
    m_total = re.search(r"Total(?:\s+estimado)?[:\s\*]*([^\n*]+)", texto, re.IGNORECASE)

    preco = _parse_money_brl(m_preco.group(1) if m_preco else None)
    frete_str = (m_frete.group(1).lower() if m_frete else "")
    if any(w in frete_str for w in ("grátis", "gratis", "grátis!", "free", "0,00", "0.00")):
        frete = 0.0
    else:
        frete = _parse_money_brl(m_frete.group(1) if m_frete else None)
    total = _parse_money_brl(m_total.group(1) if m_total else None) or (preco + frete)

    # Link: PRIMEIRO https?:// que pareça ser de produto (não busca/lista)
    link = ""
    for m in re.finditer(r"https?://[^\s<>()\[\]]+", texto):
        candidato = re.sub(r"[.,;:!?)\]]+$", "", m.group(0)).strip()
        if eh_link_de_produto(candidato):
            link = candidato
            break
    if not link:
        # Não achou link válido de produto → não considera recomendação
        return None

    # Loja: aceita "Loja: X", "🏪 X", ou infere pelo domínio do link
    loja = ""
    m_loja = re.search(r"(?:Loja[:\s]+|🏪\s*)([^\n]+)", texto, re.IGNORECASE)
    if m_loja:
        loja = re.sub(r"[\*🏪🛍🏬]+", "", m_loja.group(1)).strip()
    if not loja and link:
        dominio = re.sub(r"^https?://(www\.|produto\.)?", "", link).split("/")[0].lower()
        loja = {
            "mercadolivre.com.br": "Mercado Livre",
            "amazon.com.br": "Amazon",
            "magazineluiza.com.br": "Magazine Luiza",
            "magazinevoce.com.br": "Magazine Luiza",
            "casasbahia.com.br": "Casas Bahia",
            "centauro.com.br": "Centauro",
            "decathlon.com.br": "Decathlon",
            "pescagerais.com.br": "Pesca Gerais",
            "pescabrasil.com.br": "Pesca Brasil",
            "lojaodapesca.com.br": "Lojão da Pesca",
            "shopee.com.br": "Shopee",
        }.get(dominio, dominio.split(".")[0].capitalize())

    if not nome or not link or preco <= 0:
        return None

    return {
        "nome_produto": nome,
        "preco": round(preco, 2),
        "frete": round(frete, 2),
        "total": round(total, 2) if total > 0 else round(preco + frete, 2),
        "loja": loja or "—",
        "link": link,
    }


def verificar_link_acessivel(url: str, timeout: int = 6) -> tuple[bool, str]:
    """
    Verifica se uma URL de produto está acessível (não 404, não fora do ar).

    Retorna (ok, motivo). ok=True significa que o link parece estar funcionando.
    Aceita 2xx, 3xx, e 403 (anti-bot detection em ML/Amazon não significa link quebrado).
    Rejeita 404, 410, 5xx e timeouts/erros de rede.
    """
    if not url or not url.startswith(("http://", "https://")):
        return False, "URL inválida"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    }
    try:
        # Tenta HEAD primeiro (mais rápido)
        resp = requests.head(url, headers=headers, timeout=timeout, allow_redirects=True)
        if resp.status_code == 405:  # Method Not Allowed → tenta GET
            resp = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True, stream=True)
            resp.close()
    except requests.RequestException as exc:
        return False, f"erro de rede: {exc.__class__.__name__}"

    status = resp.status_code
    if 200 <= status < 300:
        return True, "ok"
    if 300 <= status < 400:
        return True, f"redirect ({status})"
    if status == 403:
        # Anti-bot detection. Link tipicamente válido pra humanos.
        return True, "403 (anti-bot, mas link humano funciona)"
    if status in (404, 410):
        return False, f"página não existe ({status})"
    if 500 <= status < 600:
        return False, f"servidor com erro ({status})"
    return False, f"status inesperado ({status})"


def _extrair_retry_after(resp: requests.Response) -> float | None:
    """Tenta extrair tempo de espera do header Retry-After ou do corpo JSON."""
    header = resp.headers.get("retry-after") or resp.headers.get("Retry-After")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    m = re.search(r'"retry_after"\s*:\s*(\d+(?:\.\d+)?)', resp.text)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None


# --------------------------------------------------------------------------- #
# Bot                                                                         #
# --------------------------------------------------------------------------- #

class PortalDoPescadorBot:
    def __init__(
        self,
        user_profile: dict[str, Any],
        conversation_id: int,
        recent_recommendations: list[dict[str, Any]] | None = None,
        cart_summary: dict[str, Any] | None = None,
        api_key: str | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        verbose_tools: bool = False,
    ) -> None:
        chave = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not chave:
            raise RuntimeError("ANTHROPIC_API_KEY não definido.")

        self.api_key = chave
        self.model = model or os.environ.get("ANTHROPIC_MODEL") or "claude-haiku-4-5-20251001"
        self.max_tokens = max_tokens
        self.verbose_tools = verbose_tools
        self.user_profile = user_profile
        self.conversation_id = conversation_id
        self.recent_recommendations = recent_recommendations or []
        self.cart_summary = cart_summary or {"quantidade": 0, "total_geral": 0.0}
        self.tools_payload = self._construir_tools_anthropic()
        self.history: list[dict[str, Any]] = []
        self.system_prompt = self._construir_system_prompt()
        # Última recomendação registrada pelo bot no turno corrente — usada
        # pela UI pra mostrar o botão "Adicionar ao carrinho".
        self.last_recommendation: dict[str, Any] | None = None

    @staticmethod
    def _construir_tools_anthropic() -> list[dict[str, Any]]:
        """
        Combina nossas tools client-side (JSON Schema padrão) com a tool
        nativa server-side `web_search` da Anthropic.
        """
        client_side = [
            {
                "name": s["name"],
                "description": s["description"],
                "input_schema": s["input_schema"],
            }
            for s in TOOL_SCHEMAS
        ]
        web_search_tool = {
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": 3,
            "user_location": {
                "type": "approximate",
                "country": "BR",
            },
        }
        web_fetch_tool = {
            "type": "web_fetch_20250910",
            "name": "web_fetch",
            "max_uses": 3,
            "max_content_tokens": 5000,
        }
        # cache_control no último item: cacheia toda a lista de tools
        tools_list = [web_search_tool, web_fetch_tool, *client_side]
        tools_list[-1] = {**tools_list[-1], "cache_control": {"type": "ephemeral"}}
        return tools_list

    def _construir_system_prompt(self) -> str:
        """Adiciona ao SYSTEM_PROMPT um bloco com perfil + histórico do usuário."""
        nome = self.user_profile.get("nome", "")
        cep = self.user_profile.get("cep", "")
        nivel = self.user_profile.get("nivel_experiencia") or "não informado"

        if self.recent_recommendations:
            linhas = []
            for r in self.recent_recommendations[:5]:
                linhas.append(
                    f"- {r['nome_produto']} (R$ {r['total']}, {r['loja']}) "
                    f"— status: {r['status']}"
                )
            historico = "\n".join(linhas)
        else:
            historico = "(nenhuma recomendação anterior — primeira conversa do usuário)"

        # Carrinho atual
        qtd_carrinho = int(self.cart_summary.get("quantidade", 0))
        total_carrinho = float(self.cart_summary.get("total_geral", 0))
        if qtd_carrinho > 0:
            bloco_carrinho = (
                f"O carrinho de {nome} tem **{qtd_carrinho} item(ns)** "
                f"somando **R$ {total_carrinho:.2f}**. Comente isso na abertura "
                "(ex: \"vi que você tem X itens no carrinho, quer revisar antes "
                "ou tá procurando algo novo?\")."
            )
        else:
            bloco_carrinho = "O carrinho está vazio no momento."

        bloco_perfil = f"""

## Perfil do usuário desta sessão
- Nome: **{nome}** — chame ele assim, com naturalidade.
- CEP cadastrado: {cep} — **USO INTERNO APENAS**. Passe este valor em `consultar_frete`, mas **NUNCA mencione o CEP nem a cidade/UF derivada dele na conversa** (você não tem como saber a cidade certa só pelo CEP, e mencionar pode confundir o usuário com info errada).
- Nível de experiência: **{nivel}** — adapte recomendações a isso.

## Carrinho atual
{bloco_carrinho}

## Histórico de recomendações já feitas a {nome}
{historico}

⚠️  Cumprimente {nome} pelo nome e vá direto pra entender o que ele procura. **NÃO mencione CEP, cidade ou estado** na abertura nem em nenhum outro momento. NÃO pergunte CEP/nível/nome (já cadastrados).
"""
        return SYSTEM_PROMPT + bloco_perfil

    # ------------------------------------------------------------------ #
    # API pública                                                         #
    # ------------------------------------------------------------------ #

    def turno(self, mensagem_usuario: str, log_user: bool = True) -> str:
        if log_user:
            try:
                db.log_message(self.conversation_id, "usuario", mensagem_usuario)
            except Exception as exc:  # noqa: BLE001 — falha de DB não derruba conversa
                if self.verbose_tools:
                    print(f"  [db] Falha ao logar mensagem do usuário: {exc}")

        # Reseta a "última recomendação do turno" — só será preenchida se
        # o bot chamar registrar_recomendacao neste turno.
        self.last_recommendation = None

        self.history.append({"role": "user", "content": mensagem_usuario})
        resposta = self._loop_ate_resposta_final()

        # Limpa narrativa ("deixa eu tentar...", "vou buscar...") do texto
        resposta = self._limpar_narrativa(resposta)

        # Se a resposta final é mensagem de erro/fallback, mas o bot acabou
        # setando last_recommendation antes, descarta — botão sem contexto não
        # faz sentido.
        if self.last_recommendation is not None and self._resposta_eh_fallback(resposta):
            self.last_recommendation = None

        # Se tem URL inválida na recomendação que o bot mandou, descarta
        # last_recommendation pra não mostrar botão de link ruim.
        if self.last_recommendation is not None:
            link_atual = (self.last_recommendation.get("link") or "").strip()
            if not eh_link_de_produto(link_atual):
                self.last_recommendation = None

        # FALLBACK: se o bot escreveu uma recomendação no texto mas não chamou
        # registrar_recomendacao, a gente extrai os dados do texto e salva.
        if self.last_recommendation is None and not self._resposta_eh_fallback(resposta):
            extraido = extrair_recomendacao_do_texto(resposta)
            if extraido:
                if self.verbose_tools:
                    print(f"  [bot] recuperou recomendação do texto: {extraido['nome_produto']!r}")
                self.last_recommendation = extraido
                try:
                    db.log_recommendation(
                        user_id=int(self.user_profile["id"]),
                        conversation_id=self.conversation_id,
                        nome_produto=extraido["nome_produto"],
                        preco=extraido["preco"],
                        frete=extraido["frete"],
                        total=extraido["total"],
                        loja=extraido["loja"],
                        link=extraido["link"],
                    )
                except Exception as exc:  # noqa: BLE001
                    if self.verbose_tools:
                        print(f"  [bot] falha ao persistir extração: {exc}")

        try:
            db.log_message(self.conversation_id, "bot", resposta)
        except Exception as exc:  # noqa: BLE001
            if self.verbose_tools:
                print(f"  [db] Falha ao logar resposta do bot: {exc}")

        return resposta

    @staticmethod
    def _resposta_eh_fallback(texto: str) -> bool:
        """Detecta mensagens de erro/recovery — sem produto pra mostrar."""
        if not texto:
            return True
        t = texto.strip().lower()
        marcadores = (
            "desculpa, não consegui",
            "desculpa, fiquei pensando demais",
            "(sem resposta)",
            "(resposta truncada",
            "rate limit",
            "servidores",
        )
        return any(m in t for m in marcadores)

    @staticmethod
    def _eh_admissao_de_falha(texto: str) -> bool:
        """
        Detecta quando o bot tá admitindo derrota sem dar um produto real
        (sinais clássicos de 'pensando em voz alta' e desistência).
        """
        if not texto:
            return False
        t = texto.lower()
        sinais = (
            "tô tendo dificuldade",
            "to tendo dificuldade",
            "estou tendo dificuldade",
            "tava buscando os melhores",
            "deixa eu tentar uma busca diferente",
            "não consegui pegar os link",
            "nao consegui pegar os link",
            "ainda preso",
            "preso em urls",
            "quer que eu continue ou prefere",
            "quer que eu busque novamente",
            "deixa eu tentar de novo",
        )
        return any(s in t for s in sinais)

    @staticmethod
    def _limpar_narrativa(texto: str) -> str:
        """
        Remove linhas em que o bot 'pensa em voz alta' em vez de responder
        (proibidas explicitamente no system prompt mas o Haiku ainda escapa).
        """
        if not texto:
            return texto
        padroes_remover = (
            # "deixa eu" + verbos de ação técnica
            r"^\s*deixa eu (buscar|tentar|verificar|fazer|pegar|achar|olhar|conferir|checar|entrar|extrair|puxar|filtrar|navegar)",
            # "vou" + verbos
            r"^\s*vou (tentar|buscar|entrar|pegar|achar|extrair|fazer fetch|navegar|olhar)",
            # narrativa de progresso
            r"^\s*(tava buscando|t[ôo] tentando|estou tentando|t[ôo] buscando|estou buscando)",
            r"^\s*(ainda preso|ainda tem urls?|ainda tem url|bom,?\s*ainda|[óo]timo!?\s*achei urls?)",
            r"^\s*(os resultados (est[ãa]o|s[ãa]o) (muito )?gen[ée]rico|os resultados (s[ãa]o|est[ãa]o) (urls?|p[áa]ginas))",
            r"^\s*(perfeito!\s*deixa|certo,?\s*deixa|opa!?\s*deixa)",
            r"^\s*preciso de mais (uma|umas) busca",
            # admissões de falha (queremos que ele entregue, não desista)
            r"^\s*(infelizmente )?n[ãa]o consigo acess",
            r"^\s*n[ãa]o consegui acess",
            r"^\s*os resultados mostram (s[óo] )?p[áa]ginas",
            r"^\s*n[ãa]o achei (um )?link direto",
            r"^\s*deixa a gente fazer diferente",
            r"^\s*beleza!?\s*deixa eu",
            r"^\s*beleza!?\s*vou",
            r"^\s*preciso de um link",
            r"^\s*achei v[áa]rias op[çc][õo]es",
        )
        linhas = texto.split("\n")
        filtradas = [
            l for l in linhas
            if not any(re.match(p, l, re.IGNORECASE) for p in padroes_remover)
        ]
        # Junta mas remove triplas quebras
        resultado = "\n".join(filtradas).strip()
        resultado = re.sub(r"\n{3,}", "\n\n", resultado)
        return resultado

    @staticmethod
    def _tem_url_invalida_em_recomendacao(texto: str) -> bool:
        """
        True se o texto parece uma recomendação MAS contém uma URL inválida
        (página de busca/lista/categoria em vez do produto).
        """
        if not texto:
            return False
        # Tem que parecer recomendação (tem header ou estrutura típica)
        baixo = texto.lower()
        if "recomenda" not in baixo and "🎣" not in texto:
            return False
        # Procura URLs e testa cada uma
        for m in re.finditer(r"https?://[^\s<>()\[\]]+", texto):
            url = re.sub(r"[.,;:!?)\]]+$", "", m.group(0)).strip()
            if not eh_link_de_produto(url):
                return True
        return False

    def reset(self) -> None:
        self.history.clear()

    def update_user_profile(self, user_profile: dict[str, Any]) -> None:
        """
        Atualiza o perfil do usuário em memória e reconstrói o system prompt
        — usado quando o usuário muda CEP/nível pelo modal de perfil.
        """
        self.user_profile = user_profile
        self.system_prompt = self._construir_system_prompt()

    # ------------------------------------------------------------------ #
    # Dispatch de tools (injeta contexto pra registrar_recomendacao)      #
    # ------------------------------------------------------------------ #

    def _dispatch_tool(self, nome: str, params: dict[str, Any]) -> dict[str, Any]:
        user_id = int(self.user_profile["id"])

        if nome == "registrar_recomendacao":
            try:
                nome_produto = str(params.get("nome_produto", "")).strip()
                preco = float(params.get("preco", 0))
                frete = float(params.get("frete", 0))
                total = float(params.get("total", 0))
                loja = str(params.get("loja", "")).strip()
                link = str(params.get("link", "")).strip()

                # Só rejeita URLs claramente de busca com query string
                if not eh_link_de_produto(link):
                    return {
                        "sucesso": False,
                        "erro": "Link parece ser uma busca com query string. Use a URL da página em si.",
                    }

                rec_id = db.log_recommendation(
                    user_id=user_id,
                    conversation_id=self.conversation_id,
                    nome_produto=nome_produto,
                    preco=preco,
                    frete=frete,
                    total=total,
                    loja=loja,
                    link=link,
                )
                # Guarda pra UI exibir botão "Adicionar ao carrinho"
                self.last_recommendation = {
                    "nome_produto": nome_produto,
                    "preco": preco,
                    "frete": frete,
                    "total": total,
                    "loja": loja,
                    "link": link,
                }
                return {"sucesso": True, "id": rec_id, "mensagem": "Recomendação salva no histórico."}
            except Exception as exc:  # noqa: BLE001
                return {"sucesso": False, "erro": f"Falha ao salvar: {exc}"}

        if nome == "adicionar_ao_carrinho":
            try:
                item_id = db.add_to_cart(
                    user_id=user_id,
                    nome_produto=str(params.get("nome_produto", "")).strip(),
                    preco=float(params.get("preco", 0)),
                    frete=float(params.get("frete", 0)),
                    total=float(params.get("total", 0)),
                    loja=str(params.get("loja", "")).strip(),
                    link=str(params.get("link", "")).strip(),
                )
                return {
                    "sucesso": True,
                    "item_id": item_id,
                    "mensagem": "Item adicionado ao carrinho.",
                }
            except Exception as exc:  # noqa: BLE001
                return {"sucesso": False, "erro": f"Falha ao adicionar: {exc}"}

        if nome == "visualizar_carrinho":
            try:
                resumo = db.get_cart_summary(user_id)
                # Converte Decimal pra float, datetime pra str
                itens_serializaveis = []
                for it in resumo["itens"]:
                    itens_serializaveis.append({
                        "id": int(it["id"]),
                        "nome_produto": it["nome_produto"],
                        "preco": float(it["preco"]),
                        "frete": float(it["frete"]),
                        "total": float(it["total"]),
                        "loja": it["loja"],
                        "link": it["link"],
                        "adicionado_em": it["adicionado_em"].isoformat() if it.get("adicionado_em") else None,
                    })
                return {
                    "sucesso": True,
                    "quantidade": resumo["quantidade"],
                    "itens": itens_serializaveis,
                    "subtotal_produtos": resumo["subtotal_produtos"],
                    "subtotal_fretes": resumo["subtotal_fretes"],
                    "total_geral": resumo["total_geral"],
                    "moeda": "BRL",
                }
            except Exception as exc:  # noqa: BLE001
                return {"sucesso": False, "erro": f"Falha ao consultar: {exc}"}

        if nome == "remover_do_carrinho":
            try:
                item_id = int(params.get("item_id", 0))
                ok = db.remove_from_cart(user_id, item_id)
                if ok:
                    return {"sucesso": True, "mensagem": f"Item {item_id} removido do carrinho."}
                return {"sucesso": False, "erro": f"Item {item_id} não encontrado no seu carrinho."}
            except Exception as exc:  # noqa: BLE001
                return {"sucesso": False, "erro": f"Falha ao remover: {exc}"}

        if nome == "limpar_carrinho":
            try:
                n = db.clear_cart(user_id)
                return {"sucesso": True, "removidos": n, "mensagem": f"Carrinho esvaziado ({n} itens removidos)."}
            except Exception as exc:  # noqa: BLE001
                return {"sucesso": False, "erro": f"Falha ao limpar: {exc}"}

        return executar_tool(nome, params)

    # ------------------------------------------------------------------ #
    # Loop de tool use                                                    #
    # ------------------------------------------------------------------ #

    def _loop_ate_resposta_final(self, max_iteracoes: int = 8) -> str:
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

        for _ in range(max_iteracoes):
            payload = {
                "model": self.model,
                "max_tokens": self.max_tokens,
                # system como lista de blocos com cache_control: o system
                # prompt (que é grande e fixo) fica cacheado por ~5min,
                # reduzindo MUITO o custo dos turnos seguintes.
                "system": [
                    {
                        "type": "text",
                        "text": self.system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                "tools": self.tools_payload,
                "messages": self.history,
            }

            # Retry com backoff para 429 / 5xx (rate limit / sobrecarga).
            # Tempos curtos: se travar, o usuário não fica esperando minutos.
            resp = None
            ultimo_erro = ""
            for tentativa in range(3):
                try:
                    resp = requests.post(
                        ANTHROPIC_API_URL, headers=headers, json=payload, timeout=HTTP_TIMEOUT
                    )
                except requests.RequestException as exc:
                    ultimo_erro = f"Falha de rede: {exc}"
                    time.sleep(min(2 ** tentativa, 8))
                    continue

                if resp.status_code == 200:
                    break

                if resp.status_code == 429:
                    delay_servidor = _extrair_retry_after(resp)
                    espera = delay_servidor if delay_servidor else (4 + 6 * tentativa)
                    espera = min(espera, 20)  # cap em 20s pra não congelar a UX
                    ultimo_erro = f"Anthropic API erro 429 (rate limit)"
                    time.sleep(espera)
                    continue

                if resp.status_code in (500, 502, 503, 504, 529):
                    ultimo_erro = f"Anthropic API erro {resp.status_code} (sobrecarga)"
                    time.sleep(min(2 ** tentativa + 1, 10))
                    continue

                # erro permanente
                ultimo_erro = f"Anthropic API erro {resp.status_code}: {resp.text[:300]}"
                break

            if resp is None or resp.status_code != 200:
                raise RuntimeError(ultimo_erro or "Falha desconhecida ao chamar Anthropic")

            data = resp.json()
            blocos: list[dict[str, Any]] = data.get("content") or []
            stop_reason = data.get("stop_reason")

            if self.verbose_tools:
                tipos = [b.get("type", "?") for b in blocos]
                print(f"  [resp] stop_reason={stop_reason} blocos={tipos}")

            # Adiciona a resposta do assistant ao histórico
            self.history.append({"role": "assistant", "content": blocos})

            # pause_turn: o modelo está no meio de uma sequência de
            # ferramentas server-side (ex: web_search) e precisa continuar.
            if stop_reason == "pause_turn":
                continue

            # max_tokens: a resposta foi truncada. Pede pra ele finalizar
            # de forma concisa, em vez de retornar resposta vazia/cortada.
            if stop_reason == "max_tokens":
                texto_parcial = "\n".join(
                    b.get("text", "") for b in blocos if b.get("type") == "text"
                ).strip()
                if texto_parcial:
                    # Tem texto parcial — usa ele e adiciona um aviso
                    return texto_parcial + "\n\n(resposta truncada — me peça pra continuar)"
                # Sem texto: tenta continuar pedindo um resumo
                self.history.append({
                    "role": "user",
                    "content": "Sua resposta foi cortada. Resuma o que você achou em até 8 linhas, no formato padrão de recomendação se já tem produto, ou faça uma pergunta curta se ainda precisa de info.",
                })
                continue

            if stop_reason != "tool_use":
                # resposta final
                texto = "\n".join(
                    b.get("text", "") for b in blocos if b.get("type") == "text"
                ).strip()
                if texto:
                    return texto

                # Sem texto: tenta entender o porquê e recuperar
                tem_server_tool = any(
                    b.get("type") in ("server_tool_use", "web_search_tool_result", "web_fetch_tool_result")
                    for b in blocos
                )
                if tem_server_tool:
                    # Modelo usou web_search/web_fetch mas não escreveu texto.
                    # Força ele a responder usando o que coletou.
                    self.history.append({
                        "role": "user",
                        "content": "Você fez buscas mas não me respondeu. Me dê agora uma resposta curta com base no que encontrou — ou se nada serviu, me pergunte algo pra refinar.",
                    })
                    continue

                # Sem texto e sem tools — provavelmente bloqueio de safety
                return (
                    "Desculpa, não consegui formular uma resposta dessa vez. "
                    "Pode reformular ou ser mais específico no que está procurando?"
                )

            # Executa cada tool_use deste turno e devolve os resultados
            tool_results: list[dict[str, Any]] = []
            for bloco in blocos:
                if bloco.get("type") != "tool_use":
                    continue
                nome = bloco.get("name", "")
                params = bloco.get("input") or {}
                tool_use_id = bloco.get("id", "")

                if self.verbose_tools:
                    print(f"\n  [tool] {nome}({json.dumps(params, ensure_ascii=False)})")

                try:
                    resultado = self._dispatch_tool(nome, params)
                except Exception as exc:  # noqa: BLE001
                    resultado = {"erro": f"Falha ao executar {nome}: {exc}"}

                if self.verbose_tools:
                    preview = json.dumps(resultado, ensure_ascii=False)[:240]
                    print(f"  [tool] -> {preview}{'...' if len(preview) >= 240 else ''}\n")

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": json.dumps(resultado, ensure_ascii=False),
                })

            self.history.append({"role": "user", "content": tool_results})

        return (
            "Desculpa, fiquei pensando demais nessa busca e me enrolei. "
            "Pode reformular o que você procura?"
        )

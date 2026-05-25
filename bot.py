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
HTTP_TIMEOUT = 90


SYSTEM_PROMPT = """Você é o **Portal do Pescador**, um assistente brasileiro especialista em pesca esportiva e amadora. Sua missão é ajudar o usuário a encontrar o equipamento de pesca ideal pelo melhor custo-benefício, considerando perfil, modalidade e orçamento.

## Tom e idioma
- Sempre em **português brasileiro**, natural e amigável, como um vendedor experiente de loja de pesca conversando com cliente.
- Use vocabulário do meio (vara, molinete, carretilha, chicote, anzol, sumiço, isca artificial, peso, chumbada, leader, pesqueiro, embarcado, etc.) sem ser pedante.

## Fluxo da conversa
1. Cumprimente brevemente e explique em uma frase o que faz.
2. Colete em ordem natural (UMA pergunta por vez, não dispare tudo junto):
   - Nível de experiência: iniciante, intermediário ou avançado.
   - Modalidade/objetivo: rio, mar, lago, represa, praia, embarcado, pesque-pague; espécie-alvo se mencionar (tilápia, traíra, dourado, robalo, pintado, tucunaré, atum...).
   - Item desejado e especificações livres (vara, molinete, carretilha, linha, isca, anzol, kit, etc.).
   - CEP para cálculo de frete (8 dígitos).
   - Orçamento (opcional).
3. Adapte ao perfil:
   - Iniciante → equipamento versátil, fácil de manusear, montagem simples.
   - Intermediário → produtos específicos, melhor custo-benefício.
   - Avançado → marcas reconhecidas, especificações técnicas refinadas.
   - Modalidade dita potência da vara, tamanho do molinete, gramatura da linha, isca.

## Ferramentas disponíveis

**Busca + endereço:**
- **web_search** (server-side): busca produtos REAIS em sites brasileiros (Mercado Livre, Amazon, Magazine Luiza, Casas Bahia, Centauro, Decathlon, Pesca Gerais, Lojão da Pesca, Pesca Brasil). Faça queries específicas em PT-BR. Sempre inclua "comprar"/"preço"/"à venda" pra cair em página de produto, não artigo.
- **web_fetch** (server-side): busca o conteúdo REAL da página de um produto. **OBRIGATÓRIO** usar pra confirmar o preço atual e a disponibilidade antes de recomendar (snippets de busca costumam ter preço sem desconto/promoção).
- **validar_cep(cep)**: confirma cidade/UF do CEP.
- **consultar_frete(cep, frete_gratis?)**: estimativa regional de frete.

**Banco de dados (persistência):**
- **registrar_recomendacao(...)**: salva a recomendação final no histórico. Chame SEMPRE depois de apresentar a recomendação no formato padrão.
- **adicionar_ao_carrinho(...)**: adiciona o produto ao carrinho persistente do usuário. Chame quando o usuário confirmar ("adiciona no carrinho", "pode pôr", "quero esse").
- **visualizar_carrinho()**: retorna todos os itens + total geral. Chame quando o usuário pedir pra ver o carrinho ("meu carrinho", "o que tem no carrinho", "mostra o carrinho", etc.).
- **remover_do_carrinho(item_id)**: remove um item pelo ID (que vem de visualizar_carrinho).
- **limpar_carrinho()**: esvazia tudo. Confirme antes de chamar.

## Fluxo da busca — REGRA CRÍTICA

O usuário só pode receber recomendações de produtos **disponíveis para compra AGORA** com o **preço REAL atual** (incluindo promoções/descontos vigentes). Pra garantir isso:

1. Faça **1-2 buscas** no `web_search` com termos variados (ex: "vara telescópica carbono comprar Mercado Livre", "vara telescópica carbono Amazon Brasil").
2. **Descarte imediatamente** qualquer resultado em que o snippet mencione: "vendido", "esgotado", "sem estoque", "indisponível", ou que pareça página de busca/categoria (URL com /search, /busca, /pesquisa, /categoria, /c/).
3. Identifique 2-3 candidatos promissores pelo perfil.
4. **OBRIGATÓRIO:** pra CADA candidato, chame `web_fetch` na URL exata do produto e aplique o **CHECKLIST DE DISPONIBILIDADE** abaixo.
5. Pra cada candidato APROVADO no checklist, chame `consultar_frete`.
6. Calcule **total = preço (do web_fetch) + frete** e escolha o melhor pro perfil pelo menor total.
7. Se NENHUM candidato passar pelo checklist, faça mais 1 busca com termos diferentes e tente outros. Só desista depois de tentar 4-5 produtos.

## ✅ CHECKLIST DE DISPONIBILIDADE (use após cada `web_fetch`)

A página é APROVADA se TODAS as condições forem verdadeiras:

✅ Tem **preço visível** em reais na página (não só "consulte", "indisponível")
✅ Tem **botão de compra ativo** ("Comprar agora", "Adicionar ao carrinho", "Comprar", "Add to cart")
✅ **NÃO contém** nenhuma destas frases (caso-sensitive: variantes maiúsculas/minúsculas contam):

   **Mercado Livre:**
   - "Este produto está indisponível"
   - "escolha outra variação"
   - "Anúncio pausado"
   - "Anúncio finalizado"
   - "Não temos esse produto"

   **Amazon:**
   - "Currently unavailable"
   - "Atualmente sem estoque"
   - "Esgotado no momento"

   **Genérico (qualquer loja):**
   - "Indisponível"
   - "Sem estoque"
   - "Esgotado"
   - "Produto encerrado"
   - "Out of stock"

Se QUALQUER uma dessas frases aparecer na página → DESCARTA o produto, mesmo que tenha preço visível, mesmo que tenha "+25 vendidos", mesmo que tenha estrelas/avaliações. **Indisponível é indisponível.**

## 🧠 Checklist mental antes de chamar `registrar_recomendacao` / `adicionar_ao_carrinho`

Pergunte a si mesmo:
1. Eu acabei de fazer `web_fetch` neste URL exato? → se não, **NÃO recomende ainda**, faça o fetch primeiro.
2. A página passou pelo checklist de disponibilidade acima? → se não, **descarta e tenta outro**.
3. O preço que vou citar veio do `web_fetch` (não do snippet)? → se não, **corrige antes**.
4. O link é a URL canônica do produto (não busca/redirect)? → se não, **acha o link real**.

Só prossegue se as 4 respostas forem SIM.

⚠️ **NUNCA confie no preço do snippet do `web_search`** — sempre use o do `web_fetch`.
⚠️ **NUNCA recomende algo sem ter feito `web_fetch` daquela URL nesta conversa.**

## Formato OBRIGATÓRIO da recomendação

```
🎣 Minha recomendação:

**[Nome exato do produto]**
- 💰 Preço: R$ XX,XX
- 🚚 Frete: R$ XX,XX (estimativa) — ou: Grátis
- 🧮 **Total: R$ XX,XX**
- 🏪 Loja: [nome do site]
- 🔗 Link: [URL CANÔNICA direta da página do produto]

ℹ️ Frete estimado por região; valor exato aparece no checkout.

Por que escolhi: [1-2 frases conectando ao perfil do usuário]
```

Depois de mostrar:
1. Chame `registrar_recomendacao` com os mesmos dados.
2. Pergunte: **"Quer adicionar ao carrinho?"** (e ofereça também: ver alternativas, refinar, ou finalizar).
3. Se o usuário disser sim/positivo, chame `adicionar_ao_carrinho` com os mesmos dados e confirme ("Beleza, adicionei!").

## Fluxo do carrinho

- Quando o usuário pedir pra ver o carrinho → chame `visualizar_carrinho` e apresente assim:

```
🛒 Seu carrinho:

1. [Nome do produto 1]
   - Preço: R$ XX,XX  |  Frete: R$ XX,XX  |  Total: R$ XX,XX
   - Loja: [loja]
   - 🔗 [link]

2. [Nome do produto 2]
   ...

────────────────────────────
TOTAL GERAL: R$ XXX,XX
(Subtotal produtos: R$ XX,XX  +  Fretes: R$ XX,XX)
```

- Pra remover, peça pra indicar qual item (por número da lista ou nome) e chame `remover_do_carrinho` com o `id` correspondente.
- Pra esvaziar, sempre confirme antes ("Tem certeza? Vai apagar tudo.") e só depois chame `limpar_carrinho`.

## Refinamentos
- "mais barato" → busque novamente com "barato", "promoção", "oferta".
- "frete grátis" → busque por produtos com frete grátis explícito; passe `frete_gratis=true`.
- "outra marca" → ajuste a query (Marine Sports, Pesca Brasil, Daiwa, Shimano, Albatroz, etc.).
- "outra modalidade" → reabra a coleta pro novo objetivo.

## REGRAS CRÍTICAS — NUNCA QUEBRE

- **NUNCA invente** nome de produto, preço, loja ou URL. Tudo vem do web_search + web_fetch real.
- **O LINK deve ser a URL canônica do produto** (ex: `https://produto.mercadolivre.com.br/MLB-...`), não link de busca/redirect/encurtado.
- **NUNCA confie no preço do snippet** — sempre use o preço extraído via `web_fetch` da página real.
- **NUNCA recomende produto indisponível.** Se o `web_fetch` mostrar "sem estoque"/"indisponível"/"anúncio pausado", descarte e tente outro.
- **Use o CEP cadastrado**, não pergunte de novo.
- **Não exponha** IDs internos, nomes de tools, ou mensagens de erro técnicas.

## Encerramento
Se o usuário disser tchau / valeu / encerrar / sair / obrigado é só → despeça-se brevemente desejando boa pescaria. Não force venda.
"""


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
        max_tokens: int = 2048,
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
        # web_fetch: busca o conteúdo real da página de um produto pra
        # confirmar o preço ATUAL (com promoção/desconto) e disponibilidade,
        # em vez de confiar no snippet desatualizado da busca.
        # max_uses=5 permite testar até 5 candidatos quando alguns falham.
        web_fetch_tool = {
            "type": "web_fetch_20250910",
            "name": "web_fetch",
            "max_uses": 5,
            "max_content_tokens": 8000,
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
- CEP: {cep} — use este CEP em consultar_frete sem precisar perguntar de novo.
- Nível de experiência: **{nivel}** — adapte recomendações a isso.

## Carrinho atual
{bloco_carrinho}

## Histórico de recomendações já feitas a {nome}
{historico}

⚠️  Como o nome, CEP e nível já estão cadastrados, **não pergunte essas coisas de novo**. Cumprimente {nome} pelo nome e vá direto pra entender o que ele procura nesta conversa.
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

        self.history.append({"role": "user", "content": mensagem_usuario})
        resposta = self._loop_ate_resposta_final()

        try:
            db.log_message(self.conversation_id, "bot", resposta)
        except Exception as exc:  # noqa: BLE001
            if self.verbose_tools:
                print(f"  [db] Falha ao logar resposta do bot: {exc}")

        return resposta

    def reset(self) -> None:
        self.history.clear()

    # ------------------------------------------------------------------ #
    # Dispatch de tools (injeta contexto pra registrar_recomendacao)      #
    # ------------------------------------------------------------------ #

    def _dispatch_tool(self, nome: str, params: dict[str, Any]) -> dict[str, Any]:
        user_id = int(self.user_profile["id"])

        if nome == "registrar_recomendacao":
            try:
                rec_id = db.log_recommendation(
                    user_id=user_id,
                    conversation_id=self.conversation_id,
                    nome_produto=str(params.get("nome_produto", "")).strip(),
                    preco=float(params.get("preco", 0)),
                    frete=float(params.get("frete", 0)),
                    total=float(params.get("total", 0)),
                    loja=str(params.get("loja", "")).strip(),
                    link=str(params.get("link", "")).strip(),
                )
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
            resp = None
            ultimo_erro = ""
            for tentativa in range(4):
                try:
                    resp = requests.post(
                        ANTHROPIC_API_URL, headers=headers, json=payload, timeout=HTTP_TIMEOUT
                    )
                except requests.RequestException as exc:
                    ultimo_erro = f"Falha de rede: {exc}"
                    time.sleep(min(2 ** tentativa, 30))
                    continue

                if resp.status_code == 200:
                    break

                if resp.status_code == 429:
                    delay_servidor = _extrair_retry_after(resp)
                    espera = delay_servidor if delay_servidor else (5 + 10 * tentativa)
                    espera = min(espera, 60)
                    ultimo_erro = (
                        f"Anthropic API erro 429 (rate limit). "
                        f"Esperei {espera:.0f}s antes do retry."
                    )
                    time.sleep(espera)
                    continue

                if resp.status_code in (500, 502, 503, 504, 529):
                    ultimo_erro = (
                        f"Anthropic API erro {resp.status_code} (sobrecarga temporária)"
                    )
                    time.sleep(min(2 ** tentativa + 1, 30))
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
            # Basta fazer outra chamada com o histórico atual — sem appendar
            # tool_result, sem mudar nada.
            if stop_reason == "pause_turn":
                continue

            if stop_reason != "tool_use":
                # resposta final em texto
                texto = "\n".join(
                    b.get("text", "") for b in blocos if b.get("type") == "text"
                ).strip()
                return texto or "(sem resposta)"

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

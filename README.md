# Portal do Pescador 🎣

Aplicação web em **português brasileiro** que ajuda o pescador a achar equipamento de pesca pelo melhor custo-benefício. Conversa em linguagem natural via chatbot, entende seu perfil (iniciante / intermediário / avançado), busca produtos reais em marketplaces brasileiros (Amazon, Mercado Livre, Magazine Luiza, Centauro, Casas Bahia), calcula **preço + frete** até o seu CEP e entrega uma recomendação com link direto pra página de compra.

Tem **cadastro com senha**, **carrinho persistente**, **histórico de recomendações**, **modal de perfil editável** e até busca de **locais de pesca** próximos ao seu CEP.

A camada conversacional e a busca de produtos usam a **API da Anthropic (Claude Haiku 4.5)** via *tool use* e a tool nativa `web_search`. A persistência é em **PostgreSQL**.

---

## Stack

| Camada | Tecnologia |
|---|---|
| **Frontend** | SPA vanilla JS (sem framework) + CSS animado |
| **Backend** | Python 3.10+ / Flask |
| **Conversa & busca** | Anthropic Claude (Haiku 4.5) via REST + tool use |
| **Persistência** | PostgreSQL 14+ via `psycopg 3` |
| **APIs externas** | Anthropic (web_search), ViaCEP, Mercado Livre |
| **Auth** | E-mail + senha (PBKDF2-SHA256, 600k iterações) |

---

## Como funciona

```
┌─────────────────────────────────────────────────────────────────┐
│  Frontend SPA (templates/index.html + static/js/app.js)         │
│  Login/Cadastro · Chat · Carrinho · Perfil · Locais             │
└──────────────────────────┬──────────────────────────────────────┘
                           │ JSON via fetch()
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│  Flask Backend (app.py)                                         │
│  /api/auth · /api/chat · /api/cart · /api/cep · /api/locais     │
└──────────┬─────────────────────────────────┬────────────────────┘
           │                                 │
           ▼                                 ▼
┌──────────────────────┐         ┌────────────────────────────────┐
│ PostgreSQL (db.py)   │         │ Bot conversacional (bot.py)    │
│                      │         │  - Claude Haiku 4.5            │
│ - usuarios           │         │  - tool use loop               │
│ - conversas          │         │  - history em memória/sessão   │
│ - mensagens          │         └────────────┬───────────────────┘
│ - recomendacoes      │                      │
│ - carrinho           │                      ▼
└──────────────────────┘         ┌────────────────────────────────┐
                                  │ Tools (tools.py)               │
                                  │  - buscar_produto (multi-loja) │
                                  │  - validar_cep (ViaCEP)        │
                                  │  - consultar_frete (regional)  │
                                  │  - registrar_recomendacao      │
                                  │  - carrinho (add/view/del)     │
                                  └────────────────────────────────┘
```

### Estratégia de busca de produtos

`buscar_produto(query, preco_max?)` tenta em cascata:

1. **API pública do Mercado Livre** — rápido se funcionar
2. **Chamada Anthropic secundária** isolada da conversa principal: faz `web_search` em Amazon / Magazine Luiza / Centauro / Mercado Livre e extrai a URL canônica com regex agressiva
3. **Scraping HTML** da listagem ML — último recurso

Em todos os casos, valida que a URL seja **específica de produto** (com `MLB-`, `/dp/XXX`, `/p/XXX`, `/produto/`), nunca de busca/listagem (`lista.`, `/c/`, `?q=`).

---

## O que fica salvo no banco

| Tabela | Conteúdo |
|---|---|
| `usuarios` | nome, e-mail, senha (hash), CEP, nível de experiência |
| `conversas` | uma por sessão; com início e fim |
| `mensagens` | tudo que o usuário e o bot trocaram |
| `recomendacoes` | produtos sugeridos (nome, preço, frete, total, loja, link) |
| `carrinho` | itens persistidos entre sessões |

---

## Pré-requisitos

- **Python 3.10 ou superior**
- **PostgreSQL 14+** rodando localmente (ou remoto)
- Chave da **Anthropic** com crédito (a partir de US$ 5) — https://console.anthropic.com/settings/keys
- Conexão de internet

---

## Instalação

### 1. Clone o repositório

```powershell
git clone https://github.com/igu7x/portal_do_pescador.git
cd portal_do_pescador
```

### 2. Instale o PostgreSQL e crie o banco

Baixe o instalador oficial: https://www.postgresql.org/download/windows/

Depois, no `psql`:
```sql
CREATE DATABASE pescador;
```

> Ou via pgAdmin: clique direito em **Databases** → **Create** → **Database** → nome `pescador`.

As tabelas são criadas automaticamente pelo Flask na primeira execução.

### 3. Crie um virtualenv

**Windows (PowerShell):**
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**Linux / macOS:**
```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 4. Instale as dependências

```powershell
pip install -r requirements.txt
```

### 5. Configure o `.env`

```powershell
Copy-Item .env.example .env
notepad .env
```

Preencha:
```env
ANTHROPIC_API_KEY=sk-ant-api03-...
ANTHROPIC_MODEL=claude-haiku-4-5-20251001
DATABASE_URL=postgresql://postgres:SUA_SENHA@localhost:5432/pescador
```

> ⚠️ O arquivo `.env` está no `.gitignore` — nunca commite ele.

---

## Como rodar

### Modo Web (recomendado)

```powershell
python app.py
```

Abra http://127.0.0.1:5000 no navegador.

### Modo CLI (legado)

```powershell
python main.py
```

---

## Endpoints da API

| Método | Rota | Descrição |
|---|---|---|
| `GET` | `/` | Serve a SPA |
| `POST` | `/api/auth/check` | Verifica se e-mail está cadastrado |
| `POST` | `/api/auth/register` | Cadastro de novo usuário (com senha) |
| `POST` | `/api/auth/login` | Login (e-mail + senha) |
| `POST` | `/api/auth/logout` | Sair |
| `GET`/`PATCH` | `/api/auth/me` | Ver/editar perfil (CEP, nível) |
| `POST` | `/api/chat/start` | Inicia conversa com o bot |
| `POST` | `/api/chat/message` | Envia mensagem para o bot |
| `GET` | `/api/cart` | Lista itens do carrinho |
| `POST` | `/api/cart/add` | Adiciona item via clique no botão |
| `DELETE` | `/api/cart/item/<id>` | Remove um item |
| `DELETE` | `/api/cart` | Esvazia o carrinho |
| `POST` | `/api/cep/validate` | Valida CEP via ViaCEP |
| `POST` | `/api/locais` | Busca pesqueiros próximos ao CEP |

---

## Funcionalidades

### 🔐 Login e cadastro
- E-mail + senha (PBKDF2-SHA256 com 600k iterações)
- Verificação de CEP em tempo real durante o cadastro
- Modal de perfil pra editar CEP/nível depois

### 💬 Chat com o bot
- IA com tom amigável em PT-BR
- Pergunta modalidade/orçamento pro UX, mas busca igual
- Spinner "pensando..." enquanto processa
- Mensagens persistidas no banco

### 🛒 Carrinho persistente
- Adiciona produto recomendado com 1 clique
- Modal de carrinho com total + remover item
- Sobrevive entre sessões (vinculado ao usuário)

### 🎣 Busca de produtos
- Multi-marketplace: Amazon, Mercado Livre, Magazine Luiza, Centauro, Casas Bahia
- Retorna URL **específica** do produto (nunca lista/busca)
- Estimativa de frete por região do CEP

### 📍 Locais de pesca
- Botão "Locais" no header
- Busca pesqueiros próximos ao CEP do usuário via Claude
- Retorna nome, tipo (pesque-pague, represa, etc.), endereço, distância

### 🎨 Frontend animado
- Fundo aquático com peixes nadando, bolhas e ondas
- Logo SVG inline com gradiente
- Mobile-friendly

---

## Custo estimado

- **Claude Haiku 4.5:** ~US$ 1/M input, ~US$ 5/M output tokens
- **Web search nativo:** ~US$ 10 por 1000 buscas (~US$ 0.01 por busca)
- Cada turno do bot: **~US$ 0,03 a 0,10** (depende se usa busca)
- **US$ 5 ≈ 100–200 conversas completas. US$ 20 ≈ 400–800.**

---

## Estrutura do projeto

```
portalDoPescador/
├── app.py                  # Flask backend + endpoints REST
├── auth_utils.py           # Hash de senha PBKDF2-SHA256
├── bot.py                  # Cliente Claude + tool use loop
├── tools.py                # Tools: busca, CEP, frete, carrinho
├── db.py                   # Schema PostgreSQL + queries
├── locais.py               # Busca de pesqueiros via Claude
├── main.py                 # CLI alternativo (legado)
├── requirements.txt        # Flask, requests, psycopg, python-dotenv
├── .env.example
├── .gitignore
├── README.md
├── static/
│   ├── css/style.css       # Estilo + animações
│   └── js/app.js           # SPA logic (vanilla JS)
├── templates/
│   └── index.html          # Tela única
└── tests/
    └── test_smoke.py
```

---

## Decisões de arquitetura

- **REST direto sem SDK Anthropic** — evita dependências pesadas (pydantic_core) que podem ser bloqueadas pelo Application Control do Windows.
- **Vanilla JS no frontend** — zero build, zero `node_modules`, recarrega na hora.
- **PostgreSQL com schema auto-criado** — primeira execução cria as tabelas; sem necessidade de migrations.
- **Tool use híbrido** — combina tool nativa server-side `web_search` da Anthropic com tools client-side (`validar_cep`, `consultar_frete`, `registrar_recomendacao`, carrinho).
- **Validador de URL estrito** — rejeita URLs de listagem (`lista.`, `/c/`, `?q=`) e placeholders alucinados (`MLB-1234567890`, `MLB-XXX`).
- **Busca multi-loja** — quando o ML não responde, ainda achamos produto em Amazon/Magalu/Centauro.
- **Filtro de narrativa** — strip de "deixa eu", "vou tentar", "não consegui" do output do modelo pra entregar respostas limpas.

---

## Solução de problemas

| Sintoma | Causa provável | Solução |
|---|---|---|
| `ANTHROPIC_API_KEY não definido` | `.env` ausente | Crie o `.env` (veja **Instalação**) |
| `Erro de autenticação` (401/403) | Chave inválida ou sem crédito | Verifique em https://console.anthropic.com/settings/billing |
| `429` no Claude | Rate limit | Espere alguns segundos |
| `Conexão recusada na porta 5432` | PostgreSQL fora do ar | Inicie o serviço PostgreSQL |
| `database "pescador" does not exist` | Banco não criado | Rode `CREATE DATABASE pescador;` |
| Bot retorna URL de busca | (não deve acontecer) | Atualize pra última versão — agora rejeita lista/categoria |

---

## Limitações conhecidas

- Frete é uma **estimativa regional**. O valor real aparece no checkout da loja.
- Cobertura depende do que o `web_search` da Anthropic indexa.
- Bot funciona melhor em PT-BR; queries em inglês podem dar resultados estranhos.

---

Boa pescaria! 🎣

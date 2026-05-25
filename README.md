# Portal do Pescador

Chatbot de linha de comando, em **português brasileiro**, que ajuda você a achar o equipamento de pesca ideal pelo melhor custo-benefício. Conversa com você no terminal, entende seu perfil (iniciante / intermediário / avançado), modalidade (rio, mar, lago, embarcado, etc.), item desejado e CEP, busca produtos reais em sites brasileiros, calcula **preço + frete** até o seu CEP e recomenda a melhor opção — com link direto para a página de compra.

Tem **cadastro de usuário** (nome, e-mail, CEP, nível) e **persistência em PostgreSQL**: tudo que você conversa fica salvo, e o bot lembra de você nas conversas seguintes (cumprimenta pelo nome, conhece seu nível, vê seu histórico de recomendações).

A camada conversacional **e** a busca de produtos usam a **API da Anthropic (Claude)**: a conversa via *tool use*, e a busca via a tool nativa `web_search` da Anthropic, que executa as pesquisas server-side e retorna resultados com URLs reais.

---

## Como funciona

```
┌─────────┐  email/cadastro  ┌─────────┐                 ┌──────────────┐
│ usuário │ ───────────────▶ │ main.py │ ──────────────▶ │ PostgreSQL   │
│ (CMD)   │                  │  (CLI)  │  log mensagens  │  (usuários,  │
└─────────┘                  └────┬────┘  + recomendações│   conversas, │
                                  │                       │   mensagens, │
                                  ▼                       │   recomend.) │
                          ┌──────────────────┐            └──────────────┘
                          │ Claude Sonnet    │ ─┐
                          │   (conversa)     │  │
                          └──────────────────┘  │
                                                │
                       ┌────────────────────────┼─────────────────────────────┐
                       ▼                        ▼                             ▼
               web_search               validar_cep                    consultar_frete
              (busca em sites BR)       (ViaCEP)                       (estimativa regional)
                       │                                                       │
                       └─────► registrar_recomendacao ──► PostgreSQL ◄──────────┘
```

- **Conversa + busca:** Claude (`claude-sonnet-4-6` por padrão) via API REST direta.
- **Persistência:** PostgreSQL via `psycopg 3` — schema criado automaticamente na primeira execução.
- **Busca de produtos:** tool nativa `web_search` da Anthropic, server-side, com `user_location: BR`.
- **CEP:** [ViaCEP](https://viacep.com.br/) — gratuito, sem auth.
- **Frete:** estimativa regional baseada na primeira faixa do CEP.

---

## O que fica salvo no banco

| Tabela | Conteúdo |
|---|---|
| `usuarios` | nome, email, CEP, nível de experiência, data do cadastro |
| `conversas` | uma por sessão; com início e fim |
| `mensagens` | tudo que o usuário e o bot trocaram (texto bruto) |
| `recomendacoes` | produto, preço, frete, total, loja, link, status (recomendado / comprado / descartado) |

---

## Pré-requisitos

- **Python 3.10 ou superior**
- **PostgreSQL 14+** rodando localmente
- Chave da **Anthropic** com crédito (a partir de US$ 5) — https://console.anthropic.com/settings/keys
- Conexão de internet

---

## Instalação

### 1. Instale o PostgreSQL no Windows

1. Baixe o instalador oficial: https://www.postgresql.org/download/windows/
2. Rode o setup (**EnterpriseDB Installer**). Sugestões durante a instalação:
   - **Senha do superuser `postgres`:** escolha uma e **anote** (vai pro `.env`).
   - **Porta:** deixe a padrão `5432`.
   - **Locale:** Default ou `Portuguese, Brazil`.
3. Marque **Stack Builder** como opcional (pode pular).
4. Ao final, o serviço **PostgreSQL** já fica rodando como serviço do Windows (auto-start).

### 2. Crie o banco de dados `pescador`

Abra o **SQL Shell (psql)** que vem com o instalador (Iniciar → "SQL Shell"). Pressione Enter pros padrões e digite a senha que você definiu. Então:

```sql
CREATE DATABASE pescador;
\q
```

> Alternativa via PowerShell:
> ```powershell
> & "C:\Program Files\PostgreSQL\16\bin\psql.exe" -U postgres -c "CREATE DATABASE pescador;"
> ```

As tabelas (`usuarios`, `conversas`, `mensagens`, `recomendacoes`) são criadas automaticamente pelo bot na primeira execução.

### 3. Entre no diretório do projeto e crie um venv

```powershell
cd c:\caminho\para\portalDoPescador
python -m venv .venv
.\.venv\Scripts\Activate.ps1
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
ANTHROPIC_MODEL=claude-sonnet-4-6
DATABASE_URL=postgresql://postgres:SUA_SENHA@localhost:5432/pescador
```

> **Importante:** o `.env` está no `.gitignore` — nunca commite ele em repositório público.

---

## Como rodar

```powershell
python main.py
```

### Primeira vez (cadastro)

```
========================================================
            P O R T A L   D O   P E S C A D O R
========================================================

  Pra começar, me informa seu e-mail:
  email > teixeiraigor09@gmail.com

  Parece que é sua primeira vez aqui. Vou te cadastrar rapidinho.

  seu nome > Igor
  seu CEP (8 dígitos) > 04567-000
    → São Paulo/SP
  Qual seu nível de experiência com pesca?
    [1] iniciante  [2] intermediário  [3] avançado
  nível > 2

  Pronto, Igor! Cadastro feito. Vamos pescar! 🎣

  Olá, Igor! Tudo certo? O que você tá procurando hoje?
você> ...
```

### Volta de outro dia

```
  email > teixeiraigor09@gmail.com
  Bem-vindo de volta, Igor! 🎣

  E aí Igor! Vi que semana passada você levou aquela vara
  Aramis Albatroz pra represa. Como tá indo? O que tu procura
  hoje?
você> ...
```

### Flags úteis

| Flag                 | Descrição                                            |
|----------------------|------------------------------------------------------|
| `--verbose` / `-v`   | Mostra cada chamada de tool client-side (útil pra debug). |
| `--model NOME` / `-m`| Override do modelo Claude (ex: `--model claude-opus-4-7`). |

### Encerrando

Digite **`sair`**, **`exit`**, **`quit`** ou **`fim`**. A conversa é marcada como finalizada no banco.

---

## Conferindo o que o banco salvou

Pelo `psql`:

```sql
\c pescador

-- Usuários
SELECT id, nome, email, cep, nivel_experiencia FROM usuarios;

-- Última conversa
SELECT id, autor, conteudo, criado_em
FROM mensagens
WHERE conversa_id = (SELECT MAX(id) FROM conversas)
ORDER BY id;

-- Histórico de recomendações
SELECT u.nome, r.nome_produto, r.preco, r.total, r.loja, r.criado_em
FROM recomendacoes r JOIN usuarios u ON u.id = r.usuario_id
ORDER BY r.criado_em DESC;
```

---

## Custo estimado

- **Claude Sonnet 4.6:** ~US$ 3 por milhão de tokens de input, US$ 15 por milhão de output.
- **Web search:** ~US$ 10 por 1000 buscas (~US$ 0,01 por busca).
- Cada turno do bot: **~US$ 0,03 a 0,15** (depende se usa busca).
- **US$ 5 ≈ 50–100 conversas. US$ 20 ≈ 200–400.**

---

## Estrutura do projeto

```
portalDoPescador/
├── main.py            # CLI: cadastro/login, loop de input, mensagens de erro
├── bot.py             # Cliente Claude (REST) + tool use loop + system prompt
├── tools.py           # validar_cep, consultar_frete (busca é server-side)
├── db.py              # Schema + queries do PostgreSQL
├── requirements.txt   # requests, python-dotenv, psycopg[binary]
├── .env.example
├── .gitignore
└── README.md
```

### Decisões de arquitetura

- **Um único provedor (Claude pra tudo):** conversa, raciocínio e busca rodam pela API da Anthropic. Mais simples de gerenciar (uma chave, uma fatura).
- **REST direto, sem SDKs:** evita pacotes pesados (pydantic_core) que podem ser bloqueados pelo Application Control do Windows. Só usa `requests`, `python-dotenv` e `psycopg[binary]`.
- **Tool use híbrido:** combina a tool nativa server-side `web_search` com tools client-side (`validar_cep`, `consultar_frete`, `registrar_recomendacao`).
- **Schema auto-criado:** `db.init_schema()` roda no startup; primeira execução cria as tabelas.
- **Perfil injetado no system prompt:** o Claude recebe nome/CEP/nível + últimas 5 recomendações antes de cada turno, então não pergunta o que já sabe e pode comentar o histórico.
- **Logging em camadas:** mensagens user/bot vão pra `mensagens`; toda recomendação final vira linha em `recomendacoes` via tool call.
- **Falhas de DB não derrubam a conversa:** se algo der errado no log, a conversa continua (apenas registra o erro no modo --verbose).

---

## Solução de problemas

| Sintoma | Provável causa | O que fazer |
|---------|----------------|-------------|
| `DATABASE_URL não definido` | `.env` ausente ou sem a linha | Adicione `DATABASE_URL=...` no `.env`. |
| `connection failed` / `password authentication failed` | senha errada na URL | Confira a senha do `postgres` no `.env`. |
| `database "pescador" does not exist` | esqueceu de criar o banco | Rode `CREATE DATABASE pescador;` no psql. |
| Serviço PostgreSQL não está rodando | desligaram o serviço | `services.msc` → `postgresql-x64-16` → Iniciar. |
| `ANTHROPIC_API_KEY não definido` | `.env` sem a chave | Cole a chave no `.env`. |
| `Erro de autenticação` (401/403) | chave inválida ou sem crédito | Confira em https://console.anthropic.com/settings/billing |
| `429` no Claude | rate limit (raro) | Espere alguns segundos e reenvie. |

---

## Limitações conhecidas

- O frete é uma **estimativa regional**. O valor exato sempre aparece no checkout do site da loja.
- A busca depende do que o Google indexou — produtos muito recentes podem demorar a aparecer.
- O histórico carregado pra contexto é só das últimas 5 recomendações (limitação proposital, pra não inflar o prompt).

Boa pescaria! 🎣

"""
Camada de persistência (PostgreSQL via psycopg 3).

Tabelas:
  - usuarios       (cadastro: nome, email, cep, nivel)
  - conversas      (1 por sessão do programa)
  - mensagens      (cada turno user/bot dentro de uma conversa)
  - recomendacoes  (produtos recomendados ao longo das conversas)

Conexão é única por processo (lazy). Schema é criado se não existir.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row


_conn: psycopg.Connection | None = None  # type: ignore[type-arg]


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS usuarios (
    id                 SERIAL PRIMARY KEY,
    email              VARCHAR(255) UNIQUE NOT NULL,
    nome               VARCHAR(255) NOT NULL,
    cep                VARCHAR(8)   NOT NULL,
    nivel_experiencia  VARCHAR(20),
    criado_em          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS conversas (
    id              SERIAL PRIMARY KEY,
    usuario_id      INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    iniciada_em     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    finalizada_em   TIMESTAMP
);

CREATE TABLE IF NOT EXISTS mensagens (
    id           SERIAL PRIMARY KEY,
    conversa_id  INTEGER NOT NULL REFERENCES conversas(id) ON DELETE CASCADE,
    autor        VARCHAR(20) NOT NULL CHECK (autor IN ('usuario','bot')),
    conteudo     TEXT NOT NULL,
    criado_em    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS recomendacoes (
    id            SERIAL PRIMARY KEY,
    usuario_id    INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    conversa_id   INTEGER REFERENCES conversas(id) ON DELETE SET NULL,
    nome_produto  TEXT NOT NULL,
    preco         NUMERIC(10,2),
    frete         NUMERIC(10,2),
    total         NUMERIC(10,2),
    loja          VARCHAR(120),
    link          TEXT,
    status        VARCHAR(20) DEFAULT 'recomendado'
                  CHECK (status IN ('recomendado','comprado','descartado')),
    criado_em     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS carrinho (
    id             SERIAL PRIMARY KEY,
    usuario_id     INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    nome_produto   TEXT NOT NULL,
    preco          NUMERIC(10,2) NOT NULL,
    frete          NUMERIC(10,2) NOT NULL DEFAULT 0,
    total          NUMERIC(10,2) NOT NULL,
    loja           VARCHAR(120),
    link           TEXT NOT NULL,
    adicionado_em  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_mensagens_conversa     ON mensagens(conversa_id);
CREATE INDEX IF NOT EXISTS idx_recomendacoes_usuario  ON recomendacoes(usuario_id);
CREATE INDEX IF NOT EXISTS idx_conversas_usuario      ON conversas(usuario_id);
CREATE INDEX IF NOT EXISTS idx_carrinho_usuario       ON carrinho(usuario_id);
"""


# --------------------------------------------------------------------------- #
# Conexão                                                                     #
# --------------------------------------------------------------------------- #

def _get_conn() -> psycopg.Connection:  # type: ignore[type-arg]
    global _conn
    if _conn is None or _conn.closed:
        url = os.environ.get("DATABASE_URL")
        if not url:
            raise RuntimeError(
                "DATABASE_URL não definido. Veja .env.example para o formato."
            )
        _conn = psycopg.connect(url, row_factory=dict_row)
    return _conn


@contextmanager
def _cursor() -> Iterator[psycopg.Cursor]:  # type: ignore[type-arg]
    """Cursor com commit automático no fim do bloco (rollback em erro)."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_schema() -> None:
    """Cria tabelas e índices se não existirem. Idempotente."""
    with _cursor() as cur:
        cur.execute(SCHEMA_SQL)


def close() -> None:
    global _conn
    if _conn is not None and not _conn.closed:
        _conn.close()
    _conn = None


# --------------------------------------------------------------------------- #
# Usuários                                                                    #
# --------------------------------------------------------------------------- #

def get_user_by_email(email: str) -> dict[str, Any] | None:
    with _cursor() as cur:
        cur.execute(
            "SELECT * FROM usuarios WHERE email = %s",
            (email.strip().lower(),),
        )
        return cur.fetchone()


def create_user(
    email: str,
    nome: str,
    cep: str,
    nivel_experiencia: str | None = None,
) -> dict[str, Any]:
    with _cursor() as cur:
        cur.execute(
            """
            INSERT INTO usuarios (email, nome, cep, nivel_experiencia)
            VALUES (%s, %s, %s, %s)
            RETURNING *
            """,
            (email.strip().lower(), nome.strip(), cep, nivel_experiencia),
        )
        row = cur.fetchone()
        assert row is not None
        return row


def update_user_profile(
    user_id: int,
    *,
    cep: str | None = None,
    nivel_experiencia: str | None = None,
) -> None:
    """Atualiza CEP e/ou nível se vierem não-nulos."""
    sets: list[str] = []
    valores: list[Any] = []
    if cep is not None:
        sets.append("cep = %s")
        valores.append(cep)
    if nivel_experiencia is not None:
        sets.append("nivel_experiencia = %s")
        valores.append(nivel_experiencia)
    if not sets:
        return
    valores.append(user_id)
    with _cursor() as cur:
        cur.execute(
            f"UPDATE usuarios SET {', '.join(sets)} WHERE id = %s",
            tuple(valores),
        )


# --------------------------------------------------------------------------- #
# Conversas                                                                   #
# --------------------------------------------------------------------------- #

def start_conversation(user_id: int) -> int:
    with _cursor() as cur:
        cur.execute(
            "INSERT INTO conversas (usuario_id) VALUES (%s) RETURNING id",
            (user_id,),
        )
        row = cur.fetchone()
        assert row is not None
        return int(row["id"])


def end_conversation(conversation_id: int) -> None:
    with _cursor() as cur:
        cur.execute(
            "UPDATE conversas SET finalizada_em = CURRENT_TIMESTAMP WHERE id = %s",
            (conversation_id,),
        )


# --------------------------------------------------------------------------- #
# Mensagens                                                                   #
# --------------------------------------------------------------------------- #

def log_message(conversation_id: int, autor: str, conteudo: str) -> None:
    """autor deve ser 'usuario' ou 'bot'."""
    if autor not in ("usuario", "bot"):
        raise ValueError(f"autor inválido: {autor}")
    with _cursor() as cur:
        cur.execute(
            "INSERT INTO mensagens (conversa_id, autor, conteudo) VALUES (%s, %s, %s)",
            (conversation_id, autor, conteudo),
        )


# --------------------------------------------------------------------------- #
# Recomendações                                                               #
# --------------------------------------------------------------------------- #

def log_recommendation(
    user_id: int,
    conversation_id: int | None,
    nome_produto: str,
    preco: float,
    frete: float,
    total: float,
    loja: str,
    link: str,
) -> int:
    with _cursor() as cur:
        cur.execute(
            """
            INSERT INTO recomendacoes
              (usuario_id, conversa_id, nome_produto, preco, frete, total, loja, link)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (user_id, conversation_id, nome_produto, preco, frete, total, loja, link),
        )
        row = cur.fetchone()
        assert row is not None
        return int(row["id"])


def get_user_recommendations(user_id: int, limit: int = 10) -> list[dict[str, Any]]:
    """Histórico (mais recentes primeiro) — usado pra dar contexto ao bot."""
    with _cursor() as cur:
        cur.execute(
            """
            SELECT id, nome_produto, preco, frete, total, loja, link, status, criado_em
            FROM recomendacoes
            WHERE usuario_id = %s
            ORDER BY criado_em DESC
            LIMIT %s
            """,
            (user_id, limit),
        )
        return list(cur.fetchall())


# --------------------------------------------------------------------------- #
# Carrinho                                                                    #
# --------------------------------------------------------------------------- #

def add_to_cart(
    user_id: int,
    nome_produto: str,
    preco: float,
    frete: float,
    total: float,
    loja: str,
    link: str,
) -> int:
    """Adiciona um item ao carrinho. Retorna o ID criado."""
    with _cursor() as cur:
        cur.execute(
            """
            INSERT INTO carrinho
              (usuario_id, nome_produto, preco, frete, total, loja, link)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (user_id, nome_produto, preco, frete, total, loja, link),
        )
        row = cur.fetchone()
        assert row is not None
        return int(row["id"])


def get_cart(user_id: int) -> list[dict[str, Any]]:
    """Retorna todos os itens do carrinho do usuário (mais antigos primeiro)."""
    with _cursor() as cur:
        cur.execute(
            """
            SELECT id, nome_produto, preco, frete, total, loja, link, adicionado_em
            FROM carrinho
            WHERE usuario_id = %s
            ORDER BY adicionado_em ASC
            """,
            (user_id,),
        )
        return list(cur.fetchall())


def get_cart_summary(user_id: int) -> dict[str, Any]:
    """Carrinho + somas (subtotal produtos, fretes, total geral)."""
    itens = get_cart(user_id)
    subtotal_produtos = sum(float(i["preco"]) for i in itens)
    subtotal_fretes = sum(float(i["frete"]) for i in itens)
    total_geral = sum(float(i["total"]) for i in itens)
    return {
        "quantidade": len(itens),
        "itens": itens,
        "subtotal_produtos": round(subtotal_produtos, 2),
        "subtotal_fretes": round(subtotal_fretes, 2),
        "total_geral": round(total_geral, 2),
        "moeda": "BRL",
    }


def remove_from_cart(user_id: int, item_id: int) -> bool:
    """Remove um item específico. Retorna True se removeu, False se não achou."""
    with _cursor() as cur:
        cur.execute(
            "DELETE FROM carrinho WHERE id = %s AND usuario_id = %s",
            (item_id, user_id),
        )
        return cur.rowcount > 0


def clear_cart(user_id: int) -> int:
    """Remove todos os itens do carrinho. Retorna a quantidade removida."""
    with _cursor() as cur:
        cur.execute("DELETE FROM carrinho WHERE usuario_id = %s", (user_id,))
        return cur.rowcount

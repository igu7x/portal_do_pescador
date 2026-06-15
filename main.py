"""
Portal do Pescador — chatbot CLI.

Uso:
    python main.py
    python main.py --verbose      # mostra tool calls (debug)
    python main.py --model claude-opus-4-7
"""

from __future__ import annotations

import argparse
import itertools
import os
import re
import sys
import threading
import time

from dotenv import load_dotenv

import db
from bot import PortalDoPescadorBot
from tools import normalize_cep, validar_cep


PALAVRAS_DE_SAIDA = {
    "sair", "/sair", "exit", "/exit", "quit", "/quit", "fim", "/fim",
}

NIVEIS_VALIDOS = {"iniciante", "intermediario", "intermediário", "avancado", "avançado"}


BANNER = r"""
========================================================
            P O R T A L   D O   P E S C A D O R
              seu copiloto de equipamento de pesca
========================================================
Digite 'sair' a qualquer momento para encerrar a conversa.
"""


# --------------------------------------------------------------------------- #
# Spinner (feedback visual durante operações longas)                          #
# --------------------------------------------------------------------------- #

class Spinner:
    """
    Mostra animação 'Pensando... (Xs)' no terminal enquanto algo demora.
    Use como context manager:
        with Spinner("Pensando"):
            resposta = bot.turno(msg)
    """
    CHARS = ["|", "/", "-", "\\"]

    def __init__(self, message: str = "Pensando") -> None:
        self.message = message
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "Spinner":
        self._stop.clear()
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *args) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
        # limpa a linha do spinner
        sys.stdout.write("\r" + " " * 60 + "\r")
        sys.stdout.flush()

    def _spin(self) -> None:
        chars = itertools.cycle(self.CHARS)
        inicio = time.monotonic()
        while not self._stop.is_set():
            elapsed = int(time.monotonic() - inicio)
            char = next(chars)
            sys.stdout.write(f"\r  {char} {self.message}... ({elapsed}s)  ")
            sys.stdout.flush()
            # checa a flag em chunks curtos pra encerrar rápido
            self._stop.wait(timeout=0.1)


# --------------------------------------------------------------------------- #
# Helpers de I/O                                                              #
# --------------------------------------------------------------------------- #

def _print_assistente(texto: str) -> None:
    print()
    for linha in texto.splitlines():
        print(f"  {linha}")
    print()


def _print_erro(texto: str) -> None:
    print(f"\n[erro] {texto}\n", file=sys.stderr)


def _print_info(texto: str) -> None:
    print(f"  {texto}")


def _ler_input(prompt: str) -> str | None:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def _ler_input_obrigatorio(prompt: str, validador=None, mensagem_erro: str = "Inválido, tente de novo.") -> str | None:
    """Lê até o usuário fornecer valor não-vazio (e opcionalmente válido)."""
    while True:
        valor = _ler_input(prompt)
        if valor is None:
            return None  # ctrl+c / ctrl+z
        if not valor:
            print("  (não pode ficar em branco)")
            continue
        if validador and not validador(valor):
            print(f"  ({mensagem_erro})")
            continue
        return valor


def _email_valido(email: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email))


def _normalizar_nivel(s: str) -> str | None:
    s = s.lower().strip()
    if s in ("iniciante", "i", "1"):
        return "iniciante"
    if s in ("intermediario", "intermediário", "intermediario", "inter", "m", "2"):
        return "intermediario"
    if s in ("avancado", "avançado", "avanc", "avan", "a", "3"):
        return "avancado"
    return None


# --------------------------------------------------------------------------- #
# Cadastro / login                                                            #
# --------------------------------------------------------------------------- #

def _fluxo_login_ou_cadastro() -> dict | None:
    """Pergunta o email; se já existe, retorna o usuário; se não, cadastra."""
    print()
    print("  Pra começar, me informa seu e-mail:")
    email = _ler_input_obrigatorio(
        "  email > ",
        validador=_email_valido,
        mensagem_erro="formato inválido — ex: nome@dominio.com",
    )
    if email is None:
        return None

    usuario = db.get_user_by_email(email)
    if usuario is not None:
        _print_info(f"")
        _print_info(f"Bem-vindo de volta, {usuario['nome']}! 🎣")
        return usuario

    # Novo cadastro
    print()
    _print_info("Parece que é sua primeira vez aqui. Vou te cadastrar rapidinho.")
    print()

    nome = _ler_input_obrigatorio("  seu nome > ")
    if nome is None:
        return None

    while True:
        cep_raw = _ler_input_obrigatorio("  seu CEP (8 dígitos) > ")
        if cep_raw is None:
            return None
        cep_norm = normalize_cep(cep_raw)
        if cep_norm is None:
            print("  (CEP inválido — informe 8 dígitos, com ou sem hífen)")
            continue
        # Confirma com ViaCEP
        info = validar_cep(cep_norm)
        if not info.get("sucesso"):
            print(f"  ({info.get('erro', 'não consegui validar')})")
            continue
        _print_info(f"  → {info['cidade']}/{info['uf']}")
        break

    print()
    _print_info("Qual seu nível de experiência com pesca?")
    _print_info("  [1] iniciante  [2] intermediário  [3] avançado")
    while True:
        nivel_raw = _ler_input_obrigatorio("  nível > ")
        if nivel_raw is None:
            return None
        nivel = _normalizar_nivel(nivel_raw)
        if nivel is None:
            print("  (digite iniciante, intermediário, avançado — ou 1, 2, 3)")
            continue
        break

    try:
        usuario = db.create_user(email=email, nome=nome, cep=cep_norm, nivel_experiencia=nivel)
    except Exception as exc:  # noqa: BLE001
        _print_erro(f"Falha ao salvar cadastro: {exc}")
        return None

    print()
    _print_info(f"Pronto, {usuario['nome']}! Cadastro feito. Vamos pescar! 🎣")
    return usuario


# --------------------------------------------------------------------------- #
# Loop principal                                                              #
# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(description="Portal do Pescador — chatbot CLI")
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Mostra as chamadas de tool (debug).",
    )
    parser.add_argument(
        "--model", "-m", default=None,
        help="Override do modelo Claude (sobrescreve ANTHROPIC_MODEL).",
    )
    args = parser.parse_args()

    load_dotenv()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        _print_erro(
            "ANTHROPIC_API_KEY não definido. Cole sua chave da Anthropic "
            "no arquivo .env (veja .env.example)."
        )
        return 1

    if not os.environ.get("DATABASE_URL"):
        _print_erro(
            "DATABASE_URL não definido. Configure a conexão com o PostgreSQL "
            "no arquivo .env (veja .env.example e o README)."
        )
        return 1

    # Inicializa schema do banco (idempotente)
    try:
        db.init_schema()
    except Exception as exc:  # noqa: BLE001
        _print_erro(
            f"Não consegui conectar/criar o schema no PostgreSQL: {exc}\n"
            "Confira sua DATABASE_URL e se o servidor está rodando."
        )
        return 1

    print(BANNER)

    # Login ou cadastro
    usuario = _fluxo_login_ou_cadastro()
    if usuario is None:
        print("\nAté mais!")
        return 0

    # Inicia conversa no banco
    try:
        conversation_id = db.start_conversation(usuario["id"])
    except Exception as exc:  # noqa: BLE001
        _print_erro(f"Falha ao iniciar conversa no banco: {exc}")
        return 1

    # Carrega histórico de recomendações e carrinho pra dar contexto ao bot
    try:
        recent_recs = db.get_user_recommendations(usuario["id"], limit=5)
    except Exception:  # noqa: BLE001
        recent_recs = []

    try:
        cart_summary = db.get_cart_summary(usuario["id"])
    except Exception:  # noqa: BLE001
        cart_summary = {"quantidade": 0, "total_geral": 0.0}

    # Inicializa bot
    try:
        bot = PortalDoPescadorBot(
            user_profile=dict(usuario),
            conversation_id=conversation_id,
            recent_recommendations=[dict(r) for r in recent_recs],
            cart_summary=cart_summary,
            model=args.model,
            verbose_tools=args.verbose,
        )
    except Exception as exc:  # noqa: BLE001
        _print_erro(f"Falha ao inicializar o bot: {exc}")
        return 1

    # Abertura — pede ao bot pra cumprimentar (sem logar essa mensagem-gatilho)
    primeiro_recado = (
        "Acabei de abrir o terminal. Como já me cadastrei e você sabe meu "
        "perfil, só me cumprimenta pelo nome e me pergunta o que estou "
        "procurando hoje."
    )
    try:
        if args.verbose:
            abertura = bot.turno(primeiro_recado, log_user=False)
        else:
            with Spinner("Pensando"):
                abertura = bot.turno(primeiro_recado, log_user=False)
        _print_assistente(abertura)
    except Exception as exc:  # noqa: BLE001
        _print_erro(f"Falha na abertura: {exc}")
        db.end_conversation(conversation_id)
        return 1

    # Loop principal
    while True:
        msg = _ler_input("você> ")
        if msg is None:
            print("\nAté a próxima pescaria! 🎣")
            db.end_conversation(conversation_id)
            return 0
        if not msg:
            continue
        if msg.lower() in PALAVRAS_DE_SAIDA:
            try:
                if args.verbose:
                    despedida = bot.turno(f"O usuário disse: '{msg}'. Despeça-se brevemente.")
                else:
                    with Spinner("Pensando"):
                        despedida = bot.turno(f"O usuário disse: '{msg}'. Despeça-se brevemente.")
                _print_assistente(despedida)
            except Exception:  # noqa: BLE001
                print("\nAté a próxima pescaria! 🎣")
            db.end_conversation(conversation_id)
            return 0

        try:
            if args.verbose:
                resposta = bot.turno(msg)
            else:
                with Spinner("Pensando"):
                    resposta = bot.turno(msg)
        except KeyboardInterrupt:
            print("\nAté a próxima pescaria! 🎣")
            db.end_conversation(conversation_id)
            return 0
        except Exception as exc:  # noqa: BLE001
            mensagem = str(exc)
            if "429" in mensagem:
                _print_erro(
                    "Rate limit atingido. Espere um momento e reenvie a "
                    "mesma mensagem."
                )
            elif any(c in mensagem for c in ("503", "500", "502", "504", "529")):
                _print_erro(
                    "Os servidores estão sobrecarregados agora. "
                    "Tenta de novo em alguns segundos."
                )
            elif "401" in mensagem or "403" in mensagem:
                _print_erro(
                    "Erro de autenticação. Confira sua ANTHROPIC_API_KEY no .env "
                    "e se a conta tem crédito disponível."
                )
            else:
                _print_erro(f"Erro durante a conversa: {mensagem}")
            if bot.history and bot.history[-1].get("role") == "user":
                bot.history.pop()
            continue

        _print_assistente(resposta)


if __name__ == "__main__":
    try:
        codigo = main()
    finally:
        db.close()
    sys.exit(codigo)

"""
Portal do Pescador — backend Flask para o frontend web.

Endpoints:
  GET  /                       → renderiza o app SPA
  POST /api/auth/check         → email existe? retorna {exists, user?}
  POST /api/auth/register      → cria usuário novo
  POST /api/auth/login         → "login" via email (cria sessão)
  POST /api/auth/logout        → encerra sessão + conversa
  POST /api/chat/start         → inicia conversa no DB, instancia bot, devolve abertura
  POST /api/chat/message       → envia mensagem ao bot
  GET  /api/cart               → resumo do carrinho
  DELETE /api/cart/item/<id>   → remove item do carrinho
  DELETE /api/cart             → esvazia o carrinho
  POST /api/cep/validate       → valida CEP via ViaCEP

Mantém uma instância de PortalDoPescadorBot em memória por sessão (chaveada
por session id Flask) — assim a história da conversa fica preservada entre
turnos do mesmo browser.
"""

from __future__ import annotations

import os
import secrets
import threading
from typing import Any

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, session

import db
from auth_utils import hash_password, validar_senha, verify_password
from bot import PortalDoPescadorBot
from locais import buscar_locais_pesca
from tools import normalize_cep, validar_cep


load_dotenv()


# --------------------------------------------------------------------------- #
# App + estado em memória                                                     #
# --------------------------------------------------------------------------- #

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# bots vivos: {session_id: PortalDoPescadorBot}
_bots: dict[str, PortalDoPescadorBot] = {}
_bots_lock = threading.Lock()

# cache de locais por CEP normalizado (evita re-consultar a Anthropic)
_locais_cache: dict[str, dict[str, Any]] = {}
_locais_lock = threading.Lock()


def _ensure_session_id() -> str:
    sid = session.get("sid")
    if not sid:
        sid = secrets.token_urlsafe(24)
        session["sid"] = sid
    return sid


def _get_bot() -> PortalDoPescadorBot | None:
    sid = session.get("sid")
    if not sid:
        return None
    with _bots_lock:
        return _bots.get(sid)


def _set_bot(bot: PortalDoPescadorBot) -> None:
    sid = _ensure_session_id()
    with _bots_lock:
        _bots[sid] = bot


def _drop_bot() -> None:
    sid = session.get("sid")
    if not sid:
        return
    with _bots_lock:
        _bots.pop(sid, None)


def _require_user() -> dict[str, Any] | None:
    """Retorna o usuário logado da sessão, ou None."""
    user_id = session.get("user_id")
    if not user_id:
        return None
    return {
        "id": int(user_id),
        "nome": session.get("user_nome", ""),
        "email": session.get("user_email", ""),
        "cep": session.get("user_cep", ""),
        "nivel_experiencia": session.get("user_nivel", ""),
    }


# --------------------------------------------------------------------------- #
# Inicialização do schema                                                     #
# --------------------------------------------------------------------------- #

_schema_inicializado = False
_schema_lock = threading.Lock()


def _init_once() -> tuple[bool, str | None]:
    global _schema_inicializado
    with _schema_lock:
        if _schema_inicializado:
            return True, None
        try:
            db.init_schema()
            _schema_inicializado = True
            return True, None
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)


# --------------------------------------------------------------------------- #
# Páginas                                                                     #
# --------------------------------------------------------------------------- #

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
# Auth                                                                        #
# --------------------------------------------------------------------------- #

@app.post("/api/auth/check")
def auth_check():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    if not email or "@" not in email:
        return jsonify({"error": "email inválido"}), 400

    ok, err = _init_once()
    if not ok:
        return jsonify({"error": f"db indisponível: {err}"}), 500

    try:
        usuario = db.get_user_by_email(email)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"falha ao consultar: {exc}"}), 500

    if usuario:
        # Não devolve dados sensíveis aqui — só sinaliza que existe e se já tem
        # senha cadastrada (pra UI distinguir "fazer login" de "completar cadastro
        # de conta legada sem senha").
        return jsonify({
            "exists": True,
            "tem_senha": bool(usuario.get("password_hash")),
            "nome": usuario["nome"],  # só pra UI exibir "Olá, Igor"
        })
    return jsonify({"exists": False})


@app.post("/api/auth/register")
def auth_register():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    nome = (data.get("nome") or "").strip()
    cep_raw = (data.get("cep") or "").strip()
    nivel = (data.get("nivel_experiencia") or "").strip().lower()
    senha = data.get("senha") or ""

    if not email or "@" not in email:
        return jsonify({"error": "email inválido"}), 400
    if not nome:
        return jsonify({"error": "nome obrigatório"}), 400

    cep_norm = normalize_cep(cep_raw)
    if not cep_norm:
        return jsonify({"error": "CEP inválido (8 dígitos)"}), 400

    if nivel not in {"iniciante", "intermediario", "intermediário", "avancado", "avançado"}:
        return jsonify({"error": "nível inválido"}), 400
    if nivel == "intermediário":
        nivel = "intermediario"
    if nivel == "avançado":
        nivel = "avancado"

    ok_senha, err_senha = validar_senha(senha)
    if not ok_senha:
        return jsonify({"error": err_senha}), 400

    ok, err = _init_once()
    if not ok:
        return jsonify({"error": f"db indisponível: {err}"}), 500

    info_cep = validar_cep(cep_norm)
    if not info_cep.get("sucesso"):
        return jsonify({"error": info_cep.get("erro", "CEP não encontrado")}), 400

    try:
        existente = db.get_user_by_email(email)
        if existente:
            return jsonify({"error": "email já cadastrado"}), 409
        usuario = db.create_user(
            email=email,
            nome=nome,
            cep=cep_norm,
            nivel_experiencia=nivel,
            password_hash=hash_password(senha),
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"falha ao salvar: {exc}"}), 500

    _login_user_session(usuario)
    return jsonify({
        "user": {
            "id": int(usuario["id"]),
            "nome": usuario["nome"],
            "email": usuario["email"],
            "cep": usuario["cep"],
            "nivel_experiencia": usuario.get("nivel_experiencia"),
            "cidade": info_cep.get("cidade"),
            "uf": info_cep.get("uf"),
        }
    })


@app.post("/api/auth/login")
def auth_login():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    senha = data.get("senha") or ""

    if not email:
        return jsonify({"error": "email obrigatório"}), 400
    if not senha:
        return jsonify({"error": "senha obrigatória"}), 400

    ok, err = _init_once()
    if not ok:
        return jsonify({"error": f"db indisponível: {err}"}), 500

    try:
        usuario = db.get_user_by_email(email)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"falha ao consultar: {exc}"}), 500

    # Mensagem genérica pra não vazar se o email existe ou não (timing aparte)
    if not usuario or not usuario.get("password_hash"):
        # Conta legada sem senha: pede pra recadastrar
        if usuario and not usuario.get("password_hash"):
            return jsonify({
                "error": "essa conta foi criada antes da senha existir — defina uma agora",
                "precisa_definir_senha": True,
            }), 409
        return jsonify({"error": "email ou senha incorretos"}), 401

    if not verify_password(senha, usuario["password_hash"]):
        return jsonify({"error": "email ou senha incorretos"}), 401

    _login_user_session(usuario)
    return jsonify({
        "user": {
            "id": int(usuario["id"]),
            "nome": usuario["nome"],
            "email": usuario["email"],
            "cep": usuario["cep"],
            "nivel_experiencia": usuario.get("nivel_experiencia"),
        }
    })


@app.post("/api/auth/set-password")
def auth_set_password():
    """
    Permite definir senha pra uma conta legada (criada antes de existir
    autenticação com senha). Não permite resetar senha existente.
    """
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    senha = data.get("senha") or ""

    if not email:
        return jsonify({"error": "email obrigatório"}), 400
    ok_senha, err_senha = validar_senha(senha)
    if not ok_senha:
        return jsonify({"error": err_senha}), 400

    ok, err = _init_once()
    if not ok:
        return jsonify({"error": f"db indisponível: {err}"}), 500

    try:
        usuario = db.get_user_by_email(email)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"falha ao consultar: {exc}"}), 500

    if not usuario:
        return jsonify({"error": "conta não encontrada"}), 404
    if usuario.get("password_hash"):
        return jsonify({"error": "essa conta já tem senha — faça login normalmente"}), 409

    try:
        db.set_user_password(int(usuario["id"]), hash_password(senha))
        usuario["password_hash"] = "set"  # placeholder pra log
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"falha ao salvar senha: {exc}"}), 500

    _login_user_session(usuario)
    return jsonify({
        "user": {
            "id": int(usuario["id"]),
            "nome": usuario["nome"],
            "email": usuario["email"],
            "cep": usuario["cep"],
            "nivel_experiencia": usuario.get("nivel_experiencia"),
        }
    })


def _login_user_session(usuario: dict[str, Any]) -> None:
    session["user_id"] = int(usuario["id"])
    session["user_nome"] = usuario["nome"]
    session["user_email"] = usuario["email"]
    session["user_cep"] = usuario["cep"]
    session["user_nivel"] = usuario.get("nivel_experiencia") or ""
    _ensure_session_id()


@app.post("/api/auth/logout")
def auth_logout():
    conv_id = session.get("conversation_id")
    if conv_id:
        try:
            db.end_conversation(int(conv_id))
        except Exception:  # noqa: BLE001
            pass
    _drop_bot()
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/auth/me")
def auth_me():
    user = _require_user()
    if not user:
        return jsonify({"user": None})
    return jsonify({"user": user})


@app.patch("/api/auth/me")
def auth_update_me():
    user = _require_user()
    if not user:
        return jsonify({"error": "não autenticado"}), 401

    data = request.get_json(silent=True) or {}
    updates: dict[str, Any] = {}

    if "cep" in data and data["cep"] is not None:
        cep_norm = normalize_cep(str(data["cep"]))
        if not cep_norm:
            return jsonify({"error": "CEP inválido (8 dígitos)"}), 400
        info_cep = validar_cep(cep_norm)
        if not info_cep.get("sucesso"):
            return jsonify({"error": info_cep.get("erro", "CEP não encontrado")}), 400
        updates["cep"] = cep_norm

    if "nivel_experiencia" in data and data["nivel_experiencia"] is not None:
        nivel = str(data["nivel_experiencia"]).strip().lower()
        if nivel == "intermediário":
            nivel = "intermediario"
        if nivel == "avançado":
            nivel = "avancado"
        if nivel not in {"iniciante", "intermediario", "avancado"}:
            return jsonify({"error": "nível inválido"}), 400
        updates["nivel_experiencia"] = nivel

    if not updates:
        return jsonify({"error": "nenhuma alteração informada"}), 400

    try:
        db.update_user_profile(user["id"], **updates)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"falha ao salvar: {exc}"}), 500

    # Atualiza a sessão
    if "cep" in updates:
        session["user_cep"] = updates["cep"]
        # cache de locais é por CEP — não precisa invalidar o antigo, mas
        # o novo CEP será buscado na próxima abertura do modal
    if "nivel_experiencia" in updates:
        session["user_nivel"] = updates["nivel_experiencia"]

    # Se tem bot vivo, atualiza o perfil dele em memória pra próxima resposta
    bot = _get_bot()
    if bot is not None:
        novo_perfil = {
            "id": user["id"],
            "nome": session.get("user_nome", ""),
            "email": session.get("user_email", ""),
            "cep": session.get("user_cep", ""),
            "nivel_experiencia": session.get("user_nivel", ""),
        }
        try:
            bot.update_user_profile(novo_perfil)
        except Exception:  # noqa: BLE001
            pass

    return jsonify({
        "user": {
            "id": user["id"],
            "nome": session.get("user_nome", ""),
            "email": session.get("user_email", ""),
            "cep": session.get("user_cep", ""),
            "nivel_experiencia": session.get("user_nivel", ""),
        }
    })


# --------------------------------------------------------------------------- #
# Chat                                                                        #
# --------------------------------------------------------------------------- #

@app.post("/api/chat/start")
def chat_start():
    user = _require_user()
    if not user:
        return jsonify({"error": "não autenticado"}), 401

    if not os.environ.get("ANTHROPIC_API_KEY"):
        return jsonify({"error": "ANTHROPIC_API_KEY não configurada no servidor"}), 500

    try:
        conversation_id = db.start_conversation(user["id"])
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"falha ao iniciar conversa: {exc}"}), 500
    session["conversation_id"] = conversation_id

    try:
        recent_recs = db.get_user_recommendations(user["id"], limit=5)
    except Exception:  # noqa: BLE001
        recent_recs = []
    try:
        cart_summary = db.get_cart_summary(user["id"])
    except Exception:  # noqa: BLE001
        cart_summary = {"quantidade": 0, "total_geral": 0.0}

    try:
        bot = PortalDoPescadorBot(
            user_profile=user,
            conversation_id=conversation_id,
            recent_recommendations=[dict(r) for r in recent_recs],
            cart_summary=cart_summary,
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"falha ao iniciar bot: {exc}"}), 500
    _set_bot(bot)

    primeiro = (
        "Acabei de abrir o terminal. Como já me cadastrei e você sabe meu "
        "perfil, só me cumprimenta pelo nome e me pergunta o que estou "
        "procurando hoje."
    )
    try:
        abertura = bot.turno(primeiro, log_user=False)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"falha na abertura: {exc}"}), 502

    return jsonify({
        "conversation_id": conversation_id,
        "greeting": abertura,
    })


@app.post("/api/chat/message")
def chat_message():
    user = _require_user()
    if not user:
        return jsonify({"error": "não autenticado"}), 401

    bot = _get_bot()
    if not bot:
        return jsonify({"error": "sessão de chat expirada — reinicie a conversa"}), 409

    data = request.get_json(silent=True) or {}
    mensagem = (data.get("message") or "").strip()
    if not mensagem:
        return jsonify({"error": "mensagem vazia"}), 400

    try:
        resposta = bot.turno(mensagem)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if "429" in msg:
            return jsonify({"error": "rate limit — tente novamente em alguns segundos"}), 429
        if any(c in msg for c in ("500", "502", "503", "504", "529")):
            return jsonify({"error": "servidores Anthropic sobrecarregados — tente de novo"}), 502
        if "401" in msg or "403" in msg:
            return jsonify({"error": "credenciais Anthropic inválidas ou sem crédito"}), 502
        return jsonify({"error": f"erro durante a conversa: {msg}"}), 500

    try:
        cart_summary = db.get_cart_summary(user["id"])
    except Exception:  # noqa: BLE001
        cart_summary = {"quantidade": 0, "total_geral": 0.0}

    # Se o bot registrou uma recomendação neste turno, devolve pro frontend
    # mostrar o botão "Adicionar ao carrinho".
    product = bot.last_recommendation

    return jsonify({
        "reply": resposta,
        "product": product,
        "cart": {
            "quantidade": cart_summary.get("quantidade", 0),
            "total_geral": float(cart_summary.get("total_geral", 0) or 0),
        },
    })


# --------------------------------------------------------------------------- #
# Carrinho                                                                    #
# --------------------------------------------------------------------------- #

@app.get("/api/cart")
def cart_get():
    user = _require_user()
    if not user:
        return jsonify({"error": "não autenticado"}), 401
    try:
        resumo = db.get_cart_summary(user["id"])
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"falha ao consultar: {exc}"}), 500

    itens = []
    for it in resumo.get("itens", []):
        itens.append({
            "id": int(it["id"]),
            "nome_produto": it["nome_produto"],
            "preco": float(it["preco"]),
            "frete": float(it["frete"]),
            "total": float(it["total"]),
            "loja": it["loja"],
            "link": it["link"],
        })
    return jsonify({
        "quantidade": resumo.get("quantidade", 0),
        "itens": itens,
        "subtotal_produtos": float(resumo.get("subtotal_produtos", 0) or 0),
        "subtotal_fretes": float(resumo.get("subtotal_fretes", 0) or 0),
        "total_geral": float(resumo.get("total_geral", 0) or 0),
    })


@app.post("/api/cart/add")
def cart_add():
    """
    Adiciona um produto direto ao carrinho (usado pelo botão clicável no chat).
    Aceita o mesmo formato que o bot usa em registrar_recomendacao.
    """
    user = _require_user()
    if not user:
        return jsonify({"error": "não autenticado"}), 401

    data = request.get_json(silent=True) or {}
    nome_produto = (data.get("nome_produto") or "").strip()
    link = (data.get("link") or "").strip()
    loja = (data.get("loja") or "").strip()

    if not nome_produto:
        return jsonify({"error": "nome_produto obrigatório"}), 400
    if not link:
        return jsonify({"error": "link obrigatório"}), 400

    try:
        preco = float(data.get("preco", 0) or 0)
        frete = float(data.get("frete", 0) or 0)
        total = float(data.get("total", preco + frete) or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "valores numéricos inválidos"}), 400

    try:
        item_id = db.add_to_cart(
            user_id=user["id"],
            nome_produto=nome_produto,
            preco=preco,
            frete=frete,
            total=total,
            loja=loja,
            link=link,
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"falha ao adicionar: {exc}"}), 500

    # Devolve o resumo atualizado pra UI refletir o badge
    try:
        resumo = db.get_cart_summary(user["id"])
    except Exception:  # noqa: BLE001
        resumo = {"quantidade": 1, "total_geral": total}

    return jsonify({
        "ok": True,
        "item_id": item_id,
        "cart": {
            "quantidade": resumo.get("quantidade", 0),
            "total_geral": float(resumo.get("total_geral", 0) or 0),
        },
    })


@app.delete("/api/cart/item/<int:item_id>")
def cart_remove(item_id: int):
    user = _require_user()
    if not user:
        return jsonify({"error": "não autenticado"}), 401
    try:
        ok = db.remove_from_cart(user["id"], item_id)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"falha ao remover: {exc}"}), 500
    if not ok:
        return jsonify({"error": "item não encontrado"}), 404
    return jsonify({"ok": True})


@app.delete("/api/cart")
def cart_clear():
    user = _require_user()
    if not user:
        return jsonify({"error": "não autenticado"}), 401
    try:
        n = db.clear_cart(user["id"])
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"falha ao limpar: {exc}"}), 500
    return jsonify({"removidos": n})


# --------------------------------------------------------------------------- #
# CEP                                                                         #
# --------------------------------------------------------------------------- #

@app.post("/api/cep/validate")
def cep_validate():
    data = request.get_json(silent=True) or {}
    cep = (data.get("cep") or "").strip()
    info = validar_cep(cep)
    if not info.get("sucesso"):
        return jsonify({"error": info.get("erro", "CEP inválido")}), 400
    return jsonify({
        "cep": info["cep"],
        "cidade": info["cidade"],
        "uf": info["uf"],
        "bairro": info["bairro"],
        "logradouro": info["logradouro"],
    })


# --------------------------------------------------------------------------- #
# Locais de pesca próximos                                                    #
# --------------------------------------------------------------------------- #

@app.get("/api/locais")
def locais_get():
    user = _require_user()
    if not user:
        return jsonify({"error": "não autenticado"}), 401

    cep = normalize_cep(user.get("cep", ""))
    if not cep:
        return jsonify({"error": "CEP do usuário inválido"}), 400

    # Cache: chave = CEP normalizado. Se refresh=1 na query, ignora o cache.
    refresh = request.args.get("refresh") == "1"
    with _locais_lock:
        if not refresh and cep in _locais_cache:
            return jsonify(_locais_cache[cep])

    if not os.environ.get("ANTHROPIC_API_KEY"):
        return jsonify({"error": "ANTHROPIC_API_KEY não configurada"}), 500

    try:
        data = buscar_locais_pesca(cep)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if "429" in msg:
            return jsonify({"error": "rate limit — tente em alguns segundos"}), 429
        return jsonify({"error": f"falha ao buscar locais: {msg}"}), 502

    if data.get("erro"):
        return jsonify({"error": data["erro"]}), 502

    with _locais_lock:
        _locais_cache[cep] = data

    return jsonify(data)


# --------------------------------------------------------------------------- #
# Main                                                                        #
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    debug = os.environ.get("FLASK_DEBUG") == "1"
    app.run(host="127.0.0.1", port=port, debug=debug, threaded=True)

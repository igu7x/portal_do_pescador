"""
Smoke tests do frontend web.

Não exigem PostgreSQL nem ANTHROPIC_API_KEY rodando — testam só a camada
HTTP: rotas, validações de entrada, proteção de rotas autenticadas, e a
renderização da página principal.

Para os caminhos que dependem do DB, fazemos monkey-patch das funções de
`db` antes de invocar os endpoints.

Como rodar:
    pip install -r requirements.txt
    python -m pytest tests/ -v
    # ou direto:
    python tests/test_smoke.py
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from unittest.mock import patch

# Garante que o root do projeto esteja no path
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Forçar variáveis ANTES de importar app.py
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-fake")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test_smoke")
os.environ.setdefault("FLASK_SECRET_KEY", "secret-de-teste-nao-usar-em-prod")

import app as app_module  # noqa: E402


class SmokeBase(unittest.TestCase):
    def setUp(self) -> None:
        app_module.app.config["TESTING"] = True
        self.client = app_module.app.test_client()
        # garante que cada teste comece sem schema flag set
        app_module._schema_inicializado = True  # evita chamadas reais ao DB

    def tearDown(self) -> None:
        app_module._bots.clear()


# --------------------------------------------------------------------------- #
# Página principal e healthcheck                                              #
# --------------------------------------------------------------------------- #

class TestStaticPages(SmokeBase):
    def test_healthz_responde_200(self):
        resp = self.client.get("/healthz")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"ok": True})

    def test_index_renderiza_html(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        # checks básicos de conteúdo
        self.assertIn("Portal do Pescador", html)
        self.assertIn("style.css", html)
        self.assertIn("app.js", html)
        self.assertIn("auth-screen", html)
        self.assertIn("chat-screen", html)

    def test_assets_estaticos_servidos(self):
        css = self.client.get("/static/css/style.css")
        self.assertEqual(css.status_code, 200)
        self.assertIn("text/css", css.headers.get("Content-Type", ""))

        js = self.client.get("/static/js/app.js")
        self.assertEqual(js.status_code, 200)
        body = js.get_data(as_text=True)
        self.assertIn("Portal do Pescador", body)


# --------------------------------------------------------------------------- #
# Auth — validação de input                                                   #
# --------------------------------------------------------------------------- #

class TestAuthValidation(SmokeBase):
    def test_check_sem_corpo_retorna_400(self):
        resp = self.client.post("/api/auth/check", json={})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("error", resp.get_json())

    def test_check_email_invalido_retorna_400(self):
        resp = self.client.post("/api/auth/check", json={"email": "nao-eh-email"})
        self.assertEqual(resp.status_code, 400)

    def test_register_email_invalido_retorna_400(self):
        resp = self.client.post("/api/auth/register", json={"email": "x", "nome": "Y", "cep": "12345678"})
        self.assertEqual(resp.status_code, 400)

    def test_register_sem_nome_retorna_400(self):
        resp = self.client.post("/api/auth/register", json={
            "email": "joao@example.com", "nome": "", "cep": "01310100",
            "nivel_experiencia": "iniciante",
        })
        self.assertEqual(resp.status_code, 400)

    def test_register_cep_invalido_retorna_400(self):
        resp = self.client.post("/api/auth/register", json={
            "email": "joao@example.com", "nome": "João", "cep": "abc",
            "nivel_experiencia": "iniciante",
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn("CEP", resp.get_json()["error"])

    def test_register_nivel_invalido_retorna_400(self):
        resp = self.client.post("/api/auth/register", json={
            "email": "joao@example.com", "nome": "João", "cep": "01310100",
            "nivel_experiencia": "ninja",
        })
        self.assertEqual(resp.status_code, 400)

    def test_login_sem_email_retorna_400(self):
        resp = self.client.post("/api/auth/login", json={})
        self.assertEqual(resp.status_code, 400)

    def test_me_sem_sessao_retorna_user_null(self):
        resp = self.client.get("/api/auth/me")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"user": None})


# --------------------------------------------------------------------------- #
# Rotas protegidas (401 sem autenticação)                                     #
# --------------------------------------------------------------------------- #

class TestRotasProtegidas(SmokeBase):
    def test_chat_start_sem_sessao_retorna_401(self):
        resp = self.client.post("/api/chat/start", json={})
        self.assertEqual(resp.status_code, 401)

    def test_chat_message_sem_sessao_retorna_401(self):
        resp = self.client.post("/api/chat/message", json={"message": "oi"})
        self.assertEqual(resp.status_code, 401)

    def test_cart_get_sem_sessao_retorna_401(self):
        resp = self.client.get("/api/cart")
        self.assertEqual(resp.status_code, 401)

    def test_cart_clear_sem_sessao_retorna_401(self):
        resp = self.client.delete("/api/cart")
        self.assertEqual(resp.status_code, 401)

    def test_cart_remove_item_sem_sessao_retorna_401(self):
        resp = self.client.delete("/api/cart/item/1")
        self.assertEqual(resp.status_code, 401)


# --------------------------------------------------------------------------- #
# CEP — validação local                                                       #
# --------------------------------------------------------------------------- #

class TestCepValidation(SmokeBase):
    def test_cep_vazio_retorna_400(self):
        resp = self.client.post("/api/cep/validate", json={"cep": ""})
        self.assertEqual(resp.status_code, 400)

    def test_cep_curto_retorna_400(self):
        resp = self.client.post("/api/cep/validate", json={"cep": "123"})
        self.assertEqual(resp.status_code, 400)


# --------------------------------------------------------------------------- #
# Fluxos com DB mockado                                                       #
# --------------------------------------------------------------------------- #

class TestFluxoComDBMockado(SmokeBase):
    """Verifica auth_check para email existente / novo, com DB simulado."""

    def test_check_email_existente(self):
        usuario_fake = {
            "id": 42,
            "email": "fulano@example.com",
            "nome": "Fulano",
            "cep": "01310100",
            "nivel_experiencia": "iniciante",
        }
        with patch.object(app_module.db, "get_user_by_email", return_value=usuario_fake):
            resp = self.client.post("/api/auth/check", json={"email": "fulano@example.com"})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["exists"])
        self.assertEqual(data["user"]["nome"], "Fulano")
        self.assertEqual(data["user"]["id"], 42)

    def test_check_email_novo(self):
        with patch.object(app_module.db, "get_user_by_email", return_value=None):
            resp = self.client.post("/api/auth/check", json={"email": "novo@example.com"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"exists": False})

    def test_login_cria_sessao(self):
        usuario_fake = {
            "id": 7,
            "email": "logado@example.com",
            "nome": "Logado",
            "cep": "01310100",
            "nivel_experiencia": "intermediario",
        }
        with patch.object(app_module.db, "get_user_by_email", return_value=usuario_fake):
            resp = self.client.post("/api/auth/login", json={"email": "logado@example.com"})
        self.assertEqual(resp.status_code, 200)
        # Sessão deve estar preenchida — confirmamos via /me
        me = self.client.get("/api/auth/me")
        self.assertEqual(me.status_code, 200)
        body = me.get_json()
        self.assertIsNotNone(body["user"])
        self.assertEqual(body["user"]["nome"], "Logado")

    def test_logout_limpa_sessao(self):
        usuario_fake = {
            "id": 9, "email": "out@example.com", "nome": "Out",
            "cep": "01310100", "nivel_experiencia": "iniciante",
        }
        with patch.object(app_module.db, "get_user_by_email", return_value=usuario_fake):
            self.client.post("/api/auth/login", json={"email": "out@example.com"})

        resp = self.client.post("/api/auth/logout", json={})
        self.assertEqual(resp.status_code, 200)
        me = self.client.get("/api/auth/me")
        self.assertEqual(me.get_json(), {"user": None})

    def test_cart_autenticado_retorna_resumo(self):
        usuario_fake = {
            "id": 11, "email": "carrinho@example.com", "nome": "Cart",
            "cep": "01310100", "nivel_experiencia": "iniciante",
        }
        carrinho_fake = {
            "quantidade": 1,
            "itens": [{
                "id": 1, "nome_produto": "Vara", "preco": 200.0, "frete": 30.0,
                "total": 230.0, "loja": "ML", "link": "https://example.com/x",
            }],
            "subtotal_produtos": 200.0,
            "subtotal_fretes": 30.0,
            "total_geral": 230.0,
            "moeda": "BRL",
        }
        with patch.object(app_module.db, "get_user_by_email", return_value=usuario_fake):
            self.client.post("/api/auth/login", json={"email": usuario_fake["email"]})
        with patch.object(app_module.db, "get_cart_summary", return_value=carrinho_fake):
            resp = self.client.get("/api/cart")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["quantidade"], 1)
        self.assertEqual(body["total_geral"], 230.0)
        self.assertEqual(len(body["itens"]), 1)
        self.assertEqual(body["itens"][0]["nome_produto"], "Vara")


# --------------------------------------------------------------------------- #
# Entry point pra rodar com `python tests/test_smoke.py`                      #
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    unittest.main(verbosity=2)

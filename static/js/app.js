/* =========================================================================
   Portal do Pescador — frontend SPA logic (no framework).
   ========================================================================= */
"use strict";

// --------------------------------------------------------------------------- //
// Estado global                                                               //
// --------------------------------------------------------------------------- //
const state = {
  user: null,
  cartCount: 0,
  chatStarted: false,
};

// --------------------------------------------------------------------------- //
// DOM helpers                                                                 //
// --------------------------------------------------------------------------- //
const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function show(el) {
  if (!el) return;
  el.classList.remove("hidden");
  el.hidden = false;          // remove o atributo HTML `hidden` (que ganha sobre o CSS)
}
function hide(el) {
  if (!el) return;
  el.classList.add("hidden");
  el.hidden = true;
}

function toast(message, kind = "info", duration = 3200) {
  const stack = $("#toasts");
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  stack.appendChild(el);
  setTimeout(() => {
    el.style.opacity = "0";
    el.style.transition = "opacity 0.25s";
    setTimeout(() => el.remove(), 260);
  }, duration);
}

// --------------------------------------------------------------------------- //
// Fetch helper                                                                //
// --------------------------------------------------------------------------- //
async function api(path, { method = "GET", body = null } = {}) {
  const init = {
    method,
    headers: { "Accept": "application/json" },
    credentials: "same-origin",
  };
  if (body !== null) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let resp;
  try {
    resp = await fetch(path, init);
  } catch (err) {
    throw new Error("falha de rede — verifique sua conexão");
  }
  let data = null;
  try { data = await resp.json(); } catch { /* sem corpo */ }
  if (!resp.ok) {
    const msg = (data && data.error) ? data.error : `erro ${resp.status}`;
    const err = new Error(msg);
    err.status = resp.status;
    throw err;
  }
  return data || {};
}

// --------------------------------------------------------------------------- //
// Spinner de botão                                                            //
// --------------------------------------------------------------------------- //
function setBtnLoading(btn, loading) {
  if (!btn) return;
  btn.disabled = loading;
  const sp = btn.querySelector(".btn-spinner");
  if (sp) sp.hidden = !loading;
}

// --------------------------------------------------------------------------- //
// AUTH                                                                        //
// --------------------------------------------------------------------------- //
const elAuthScreen      = $("#auth-screen");
const elChatScreen      = $("#chat-screen");
const formEmail         = $("#form-email");
const formCadastro      = $("#form-cadastro");
const formLogin         = $("#form-login");
const formSetPassword   = $("#form-set-password");
const welcomeText       = $("#welcome-text");
const inputEmail        = $("#email-input");
const inputNome         = $("#nome-input");
const inputCep          = $("#cep-input");
const inputSenha        = $("#senha-input");
const inputSenhaConfirm = $("#senha-confirm-input");
const inputLoginSenha   = $("#login-senha-input");
const inputSetSenha     = $("#set-senha-input");
const inputSetSenhaConfirm = $("#set-senha-confirm-input");
const cepFeedback       = $("#cep-feedback");
const levelPills        = $("#level-pills");
const authError         = $("#auth-error");
const btnEmailSubmit    = $("#email-submit");
const btnCadSubmit      = $("#cadastro-submit");
const btnEntrar         = $("#entrar-submit");
const btnSetSenhaSubmit = $("#set-senha-submit");
const btnVoltar1        = $("#voltar-email");
const btnVoltar2        = $("#voltar-email-2");
const btnVoltar3        = $("#voltar-email-3");

let nivelEscolhido = null;
let cepValidado = null; // {cep, cidade, uf} se válido

function showAuthStep(step) {
  // step: "email" | "cadastro" | "login" | "set-password"
  hide(authError);
  for (const el of [formEmail, formCadastro, formLogin, formSetPassword]) hide(el);
  if      (step === "email")        show(formEmail);
  else if (step === "cadastro")     show(formCadastro);
  else if (step === "login")        show(formLogin);
  else if (step === "set-password") show(formSetPassword);
}

// Toggle de visibilidade da senha (botão olho)
document.addEventListener("click", (e) => {
  const tgl = e.target.closest("[data-toggle-pwd]");
  if (!tgl) return;
  const sel = tgl.dataset.togglePwd;
  const input = $(sel);
  if (!input) return;
  input.type = input.type === "password" ? "text" : "password";
  tgl.textContent = input.type === "password" ? "👁" : "🙈";
});

function showAuthError(msg) {
  authError.textContent = msg;
  show(authError);
}

// Pills de nível
levelPills.addEventListener("click", (e) => {
  const btn = e.target.closest(".pill");
  if (!btn) return;
  $$(".pill", levelPills).forEach(b => b.classList.remove("selected"));
  btn.classList.add("selected");
  nivelEscolhido = btn.dataset.value;
});

// Máscara CEP
inputCep.addEventListener("input", () => {
  let v = inputCep.value.replace(/\D/g, "").slice(0, 8);
  if (v.length > 5) v = v.slice(0, 5) + "-" + v.slice(5);
  inputCep.value = v;
  cepValidado = null;
  hide(cepFeedback);
});

// Validação on-blur do CEP
let cepValidating = false;
inputCep.addEventListener("blur", async () => {
  const raw = inputCep.value.replace(/\D/g, "");
  if (raw.length !== 8) return;
  if (cepValidating) return;
  cepValidating = true;
  try {
    const info = await api("/api/cep/validate", { method: "POST", body: { cep: raw } });
    cepValidado = info;
    cepFeedback.textContent = `📍 ${info.cidade}/${info.uf}`;
    cepFeedback.className = "cep-feedback ok";
    show(cepFeedback);
  } catch (err) {
    cepValidado = null;
    cepFeedback.textContent = `⚠ ${err.message}`;
    cepFeedback.className = "cep-feedback err";
    show(cepFeedback);
  } finally {
    cepValidating = false;
  }
});

// Formulário: email
formEmail.addEventListener("submit", async (e) => {
  e.preventDefault();
  hide(authError);
  const email = inputEmail.value.trim().toLowerCase();
  if (!email.includes("@")) {
    showAuthError("e-mail inválido"); return;
  }
  setBtnLoading(btnEmailSubmit, true);
  try {
    const data = await api("/api/auth/check", { method: "POST", body: { email } });
    if (data.exists && data.tem_senha) {
      welcomeText.textContent = `Bem-vindo de volta, ${data.nome}! 🎣`;
      showAuthStep("login");
      setTimeout(() => inputLoginSenha.focus(), 60);
    } else if (data.exists && !data.tem_senha) {
      showAuthStep("set-password");
      setTimeout(() => inputSetSenha.focus(), 60);
    } else {
      showAuthStep("cadastro");
      setTimeout(() => inputNome.focus(), 60);
    }
  } catch (err) {
    showAuthError(err.message);
  } finally {
    setBtnLoading(btnEmailSubmit, false);
  }
});

// Formulário: login (email + senha)
formLogin.addEventListener("submit", async (e) => {
  e.preventDefault();
  hide(authError);
  const email = inputEmail.value.trim().toLowerCase();
  const senha = inputLoginSenha.value;
  if (!senha) { showAuthError("informe sua senha"); return; }
  setBtnLoading(btnEntrar, true);
  try {
    const data = await api("/api/auth/login", { method: "POST", body: { email, senha } });
    state.user = data.user;
    inputLoginSenha.value = "";
    await entrarNoChat();
  } catch (err) {
    showAuthError(err.message);
    // se for conta legada, jogamos pra etapa de definir senha
    if (err.status === 409 && /sem senha|antes da senha/i.test(err.message)) {
      showAuthStep("set-password");
      setTimeout(() => inputSetSenha.focus(), 60);
    }
  } finally {
    setBtnLoading(btnEntrar, false);
  }
});

// Formulário: cadastro
formCadastro.addEventListener("submit", async (e) => {
  e.preventDefault();
  hide(authError);
  const email = inputEmail.value.trim().toLowerCase();
  const nome  = inputNome.value.trim();
  const cep   = inputCep.value.replace(/\D/g, "");
  const senha = inputSenha.value;
  const senhaConfirm = inputSenhaConfirm.value;

  if (!nome) { showAuthError("informe seu nome"); return; }
  if (cep.length !== 8) { showAuthError("CEP precisa ter 8 dígitos"); return; }
  if (!nivelEscolhido) { showAuthError("escolha um nível de experiência"); return; }
  if (senha.length < 6) { showAuthError("a senha precisa ter pelo menos 6 caracteres"); return; }
  if (senha !== senhaConfirm) { showAuthError("as senhas não conferem"); return; }

  setBtnLoading(btnCadSubmit, true);
  try {
    const data = await api("/api/auth/register", {
      method: "POST",
      body: { email, nome, cep, nivel_experiencia: nivelEscolhido, senha },
    });
    state.user = data.user;
    inputSenha.value = ""; inputSenhaConfirm.value = "";
    toast(`Cadastro feito, ${data.user.nome}! 🎣`, "success");
    await entrarNoChat();
  } catch (err) {
    showAuthError(err.message);
  } finally {
    setBtnLoading(btnCadSubmit, false);
  }
});

// Formulário: definir senha pra conta legada
formSetPassword.addEventListener("submit", async (e) => {
  e.preventDefault();
  hide(authError);
  const email = inputEmail.value.trim().toLowerCase();
  const senha = inputSetSenha.value;
  const senhaConfirm = inputSetSenhaConfirm.value;
  if (senha.length < 6) { showAuthError("a senha precisa ter pelo menos 6 caracteres"); return; }
  if (senha !== senhaConfirm) { showAuthError("as senhas não conferem"); return; }

  setBtnLoading(btnSetSenhaSubmit, true);
  try {
    const data = await api("/api/auth/set-password", {
      method: "POST", body: { email, senha },
    });
    state.user = data.user;
    inputSetSenha.value = ""; inputSetSenhaConfirm.value = "";
    toast("Senha definida! 🎣", "success");
    await entrarNoChat();
  } catch (err) {
    showAuthError(err.message);
  } finally {
    setBtnLoading(btnSetSenhaSubmit, false);
  }
});

// Voltar para email
function voltarEmail() {
  showAuthStep("email");
  inputNome.value = ""; inputCep.value = "";
  inputSenha.value = ""; inputSenhaConfirm.value = "";
  inputLoginSenha.value = "";
  inputSetSenha.value = ""; inputSetSenhaConfirm.value = "";
  nivelEscolhido = null;
  $$(".pill", levelPills).forEach(b => b.classList.remove("selected"));
}
btnVoltar1.addEventListener("click", voltarEmail);
btnVoltar2.addEventListener("click", voltarEmail);
btnVoltar3.addEventListener("click", voltarEmail);

// --------------------------------------------------------------------------- //
// CHAT                                                                        //
// --------------------------------------------------------------------------- //
const messagesEl     = $("#messages");
const composer       = $("#composer");
const inputMessage   = $("#input-message");
const btnSend        = $("#btn-send");
const typingEl       = $("#typing");
const btnClearChat   = $("#btn-clear-chat");
const cartCountEl    = $("#cart-count");
const userNameEl     = $("#user-name");
const userAvatarEl   = $("#user-avatar");
const userChip       = $("#user-chip");

async function entrarNoChat() {
  // Mostra tela de chat
  hide(elAuthScreen);
  show(elChatScreen);
  userNameEl.textContent = state.user.nome;
  userAvatarEl.textContent = (state.user.nome || "P")[0].toUpperCase();

  if (state.chatStarted) return;

  // Pede ao backend pra iniciar a conversa (greeting do bot)
  showTyping(true);
  try {
    const data = await api("/api/chat/start", { method: "POST", body: {} });
    state.chatStarted = true;
    addMessage("bot", data.greeting);
  } catch (err) {
    addMessage("system", `Erro ao iniciar a conversa: ${err.message}`);
    toast(err.message, "error", 5000);
  } finally {
    showTyping(false);
    refreshCart();
  }
}

function showTyping(visible) {
  if (visible) show(typingEl);
  else         hide(typingEl);
}

function addMessage(kind, text, product = null) {
  const el = document.createElement("div");
  el.className = `msg ${kind}`;
  if (kind === "bot") {
    el.innerHTML = renderBotText(text);
    if (product) {
      el.appendChild(buildAddToCartButton(product));
    }
  } else {
    el.textContent = text;
  }
  messagesEl.appendChild(el);
  // Scroll suave pro fim
  requestAnimationFrame(() => {
    messagesEl.scrollTop = messagesEl.scrollHeight;
  });
}

function buildAddToCartButton(product) {
  const wrap = document.createElement("div");
  wrap.className = "msg-action-row";

  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "btn-add-cart";
  btn.innerHTML = `
    <span class="btn-add-cart-icon">🛒</span>
    <span class="btn-add-cart-label">Adicionar ao carrinho</span>
    <span class="btn-add-cart-price">R$ ${Number(product.total || 0).toFixed(2).replace(".", ",")}</span>
  `;

  btn.addEventListener("click", async () => {
    if (btn.disabled) return;
    btn.disabled = true;
    btn.classList.add("loading");
    btn.querySelector(".btn-add-cart-label").textContent = "Adicionando…";
    try {
      const data = await api("/api/cart/add", { method: "POST", body: product });
      btn.classList.remove("loading");
      btn.classList.add("done");
      btn.querySelector(".btn-add-cart-icon").textContent = "✓";
      btn.querySelector(".btn-add-cart-label").textContent = "Adicionado ao carrinho";
      toast(`"${product.nome_produto}" no carrinho 🛒`, "success");
      if (data.cart) updateCartCount(data.cart.quantidade);
    } catch (err) {
      btn.classList.remove("loading");
      btn.disabled = false;
      btn.querySelector(".btn-add-cart-label").textContent = "Adicionar ao carrinho";
      toast(`Erro: ${err.message}`, "error", 5000);
    }
  });

  wrap.appendChild(btn);
  return wrap;
}

/**
 * Renderiza markdown simples + autolink, com escaping HTML safe.
 *  - **bold**, *italic*
 *  - `code`
 *  - URLs viram links clicáveis (target=_blank, rel=noopener)
 *  - emojis e quebras de linha preservadas
 */
function renderBotText(raw) {
  let s = String(raw || "");

  // 1) Escape HTML
  s = s.replace(/&/g, "&amp;")
       .replace(/</g, "&lt;")
       .replace(/>/g, "&gt;");

  // 2) Links: http(s)://... — captura até espaço/parêntese/<
  s = s.replace(/\bhttps?:\/\/[^\s<>)\]]+/g, (url) => {
    // remove pontuação final solta
    let clean = url.replace(/[.,;:!?)]+$/, "");
    const tail = url.slice(clean.length);
    return `<a href="${clean}" target="_blank" rel="noopener noreferrer">${clean}</a>${tail}`;
  });

  // 3) `code`
  s = s.replace(/`([^`]+)`/g, "<code>$1</code>");

  // 4) **bold**
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");

  // 5) *italic* (não pegar ** já tratado)
  s = s.replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<em>$2</em>");

  return s;
}

// Auto-resize do textarea
inputMessage.addEventListener("input", () => {
  inputMessage.style.height = "auto";
  inputMessage.style.height = Math.min(inputMessage.scrollHeight, 200) + "px";
});

// Enter envia, Shift+Enter quebra linha
inputMessage.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    composer.requestSubmit();
  }
});

composer.addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = inputMessage.value.trim();
  if (!text) return;

  addMessage("user", text);
  inputMessage.value = "";
  inputMessage.style.height = "auto";
  btnSend.disabled = true;
  showTyping(true);

  try {
    const data = await api("/api/chat/message", { method: "POST", body: { message: text } });
    addMessage("bot", data.reply, data.product || null);
    if (data.cart) updateCartCount(data.cart.quantidade);
    // Recarrega o badge sem esperar
    refreshCart();
  } catch (err) {
    if (err.status === 409) {
      addMessage("system", "Sessão expirada — reiniciando…");
      state.chatStarted = false;
      await entrarNoChat();
    } else {
      addMessage("system", `⚠ ${err.message}`);
      toast(err.message, "error", 5000);
    }
  } finally {
    showTyping(false);
    btnSend.disabled = false;
    inputMessage.focus();
  }
});

// --------------------------------------------------------------------------- //
// Carrinho                                                                    //
// --------------------------------------------------------------------------- //
const btnOpenCart  = $("#btn-open-cart");
const modalCart    = $("#modal-cart");
const cartEmpty    = $("#cart-empty");
const cartList     = $("#cart-list");
const cartFooter   = $("#cart-footer");
const cartTotalVal = $("#cart-total-value");
const btnClearCart = $("#btn-clear-cart");

function fmtBRL(n) {
  const v = Number(n || 0);
  return v.toLocaleString("pt-BR", { style: "currency", currency: "BRL" });
}

function updateCartCount(n) {
  state.cartCount = n;
  cartCountEl.textContent = String(n);
  cartCountEl.style.display = n > 0 ? "inline-block" : "none";
}

async function refreshCart() {
  try {
    const data = await api("/api/cart");
    renderCart(data);
    updateCartCount(data.quantidade);
  } catch (err) {
    // silencioso — só falha no badge
  }
}

function renderCart(data) {
  cartList.innerHTML = "";
  if (!data.itens || data.itens.length === 0) {
    show(cartEmpty); hide(cartList); hide(cartFooter);
    return;
  }
  hide(cartEmpty); show(cartList); show(cartFooter);

  for (const it of data.itens) {
    const li = document.createElement("li");
    li.className = "cart-item";
    li.innerHTML = `
      <div class="cart-item-info">
        <p class="cart-item-name"></p>
        <div class="cart-item-meta">
          <span class="loja"></span> · frete <span class="frete"></span>
        </div>
        <a class="cart-item-link" target="_blank" rel="noopener noreferrer"></a>
      </div>
      <div>
        <div class="cart-item-price"></div>
        <button class="cart-item-remove" title="Remover">✕</button>
      </div>`;
    li.querySelector(".cart-item-name").textContent = it.nome_produto;
    li.querySelector(".loja").textContent = it.loja || "—";
    li.querySelector(".frete").textContent = it.frete > 0 ? fmtBRL(it.frete) : "grátis";
    const a = li.querySelector(".cart-item-link");
    a.href = it.link; a.textContent = it.link;
    li.querySelector(".cart-item-price").textContent = fmtBRL(it.total);
    li.querySelector(".cart-item-remove").addEventListener("click", () => removeCartItem(it.id));
    cartList.appendChild(li);
  }
  cartTotalVal.textContent = fmtBRL(data.total_geral);
}

async function removeCartItem(id) {
  try {
    await api(`/api/cart/item/${id}`, { method: "DELETE" });
    toast("Item removido", "info");
    refreshCart();
  } catch (err) {
    toast(err.message, "error");
  }
}

btnOpenCart.addEventListener("click", async () => {
  await refreshCart();
  show(modalCart);
});

btnClearCart.addEventListener("click", async () => {
  if (!confirm("Esvaziar todo o carrinho?")) return;
  try {
    await api("/api/cart", { method: "DELETE" });
    toast("Carrinho esvaziado", "info");
    refreshCart();
  } catch (err) {
    toast(err.message, "error");
  }
});

// Fecha modal ao clicar no backdrop ou X
modalCart.addEventListener("click", (e) => {
  if (e.target.matches("[data-close-modal]")) hide(modalCart);
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!modalCart.classList.contains("hidden"))    hide(modalCart);
  if (!modalProfile.classList.contains("hidden")) hide(modalProfile);
  if (!modalLocais.classList.contains("hidden"))  hide(modalLocais);
});

// --------------------------------------------------------------------------- //
// Locais de pesca (modal acessado pelo botão 📍 no header)                    //
// --------------------------------------------------------------------------- //
const btnOpenLocais   = $("#btn-open-locais");
const btnRefreshLocais = $("#btn-refresh-locais");
const modalLocais     = $("#modal-locais");
const locaisLoading   = $("#locais-loading");
const locaisList      = $("#locais-list");
const locaisError     = $("#locais-error");
const locaisSubtitle  = $("#locais-subtitle");

let locaisLoaded = false; // só carrega uma vez por sessão de browser

async function openLocais(forceRefresh = false) {
  show(modalLocais);

  if (locaisLoaded && !forceRefresh) return; // cacheado client-side

  show(locaisLoading);
  hide(locaisList);
  hide(locaisError);
  locaisSubtitle.textContent = "Carregando…";

  try {
    const url = forceRefresh ? "/api/locais?refresh=1" : "/api/locais";
    const data = await api(url);
    renderLocais(data);
    locaisLoaded = true;
  } catch (err) {
    locaisError.textContent = `⚠ ${err.message}`;
    show(locaisError);
    locaisSubtitle.textContent = "Não consegui carregar agora.";
  } finally {
    hide(locaisLoading);
  }
}

function renderLocais(data) {
  locaisList.innerHTML = "";
  if (!data.locais || data.locais.length === 0) {
    locaisError.textContent = "Nenhum local encontrado pra essa região.";
    show(locaisError);
    return;
  }
  locaisSubtitle.textContent = `${data.locais.length} sugestões próximas a ${data.cidade}/${data.uf}`;
  show(locaisList);

  for (const loc of data.locais) {
    const li = document.createElement("li");
    li.className = "local-card";
    const mapsHref = `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(loc.maps_query || loc.nome)}`;
    li.innerHTML = `
      <div class="local-card-head">
        <h3 class="local-card-name"></h3>
        <span class="local-card-tipo"></span>
      </div>
      <p class="local-card-meta"><strong></strong> · <span class="local-card-dist"></span></p>
      <p class="local-card-peixes"></p>
      <p class="local-card-dica"></p>
      <a class="local-card-link" target="_blank" rel="noopener noreferrer">📍 Abrir no Google Maps</a>
    `;
    li.querySelector(".local-card-name").textContent  = loc.nome || "—";
    li.querySelector(".local-card-tipo").textContent  = (loc.tipo || "").toUpperCase();
    li.querySelector(".local-card-meta strong").textContent = loc.cidade_uf || "—";
    li.querySelector(".local-card-dist").textContent  = loc.distancia_aprox || "";
    li.querySelector(".local-card-peixes").textContent = loc.peixes || "";
    li.querySelector(".local-card-dica").textContent  = loc.dica || "";
    li.querySelector(".local-card-link").href = mapsHref;
    locaisList.appendChild(li);
  }
}

btnOpenLocais.addEventListener("click", () => openLocais(false));
btnRefreshLocais.addEventListener("click", () => openLocais(true));

modalLocais.addEventListener("click", (e) => {
  if (e.target.matches("[data-close-modal]")) hide(modalLocais);
});

// --------------------------------------------------------------------------- //
// Perfil (modal acessado pelo chip do usuário)                                //
// --------------------------------------------------------------------------- //
const modalProfile      = $("#modal-profile");
const profileNomeEl     = $("#profile-nome");
const profileEmailEl    = $("#profile-email");
const profileCepInput   = $("#profile-cep");
const profileCepFeedback = $("#profile-cep-feedback");
const profileLevelPills = $("#profile-level-pills");
const profileError      = $("#profile-error");
const profileSuccess    = $("#profile-success");
const btnSaveProfile    = $("#btn-save-profile");
const btnLogout         = $("#btn-logout");

let profileNivelEscolhido = null;
let profileCepValidado = null;

function fmtCep(raw) {
  const d = String(raw || "").replace(/\D/g, "").slice(0, 8);
  return d.length > 5 ? d.slice(0, 5) + "-" + d.slice(5) : d;
}

function openProfile() {
  if (!state.user) return;
  hide(profileError);
  hide(profileSuccess);
  hide(profileCepFeedback);

  profileNomeEl.textContent  = state.user.nome || "—";
  profileEmailEl.textContent = state.user.email || "—";
  profileCepInput.value      = fmtCep(state.user.cep);
  profileCepValidado = null;

  profileNivelEscolhido = state.user.nivel_experiencia || null;
  $$(".pill", profileLevelPills).forEach(b => {
    b.classList.toggle("selected", b.dataset.value === profileNivelEscolhido);
  });

  show(modalProfile);
}

// Pills do nível no modal de perfil
profileLevelPills.addEventListener("click", (e) => {
  const btn = e.target.closest(".pill");
  if (!btn) return;
  $$(".pill", profileLevelPills).forEach(b => b.classList.remove("selected"));
  btn.classList.add("selected");
  profileNivelEscolhido = btn.dataset.value;
});

// Máscara CEP no modal de perfil
profileCepInput.addEventListener("input", () => {
  let v = profileCepInput.value.replace(/\D/g, "").slice(0, 8);
  if (v.length > 5) v = v.slice(0, 5) + "-" + v.slice(5);
  profileCepInput.value = v;
  profileCepValidado = null;
  hide(profileCepFeedback);
});

// Valida CEP on-blur (preview da cidade)
profileCepInput.addEventListener("blur", async () => {
  const raw = profileCepInput.value.replace(/\D/g, "");
  if (raw.length !== 8) return;
  try {
    const info = await api("/api/cep/validate", { method: "POST", body: { cep: raw } });
    profileCepValidado = info;
    profileCepFeedback.textContent = `📍 ${info.cidade}/${info.uf}`;
    profileCepFeedback.className = "cep-feedback ok";
    show(profileCepFeedback);
  } catch (err) {
    profileCepValidado = null;
    profileCepFeedback.textContent = `⚠ ${err.message}`;
    profileCepFeedback.className = "cep-feedback err";
    show(profileCepFeedback);
  }
});

// Salvar alterações
btnSaveProfile.addEventListener("click", async () => {
  hide(profileError);
  hide(profileSuccess);

  const cepRaw = profileCepInput.value.replace(/\D/g, "");
  if (cepRaw.length !== 8) {
    profileError.textContent = "CEP precisa ter 8 dígitos";
    show(profileError);
    return;
  }
  if (!profileNivelEscolhido) {
    profileError.textContent = "escolha um nível de experiência";
    show(profileError);
    return;
  }

  const body = {};
  if (cepRaw !== state.user.cep)             body.cep = cepRaw;
  if (profileNivelEscolhido !== state.user.nivel_experiencia)
    body.nivel_experiencia = profileNivelEscolhido;

  if (Object.keys(body).length === 0) {
    profileSuccess.textContent = "Nada mudou.";
    show(profileSuccess);
    return;
  }

  setBtnLoading(btnSaveProfile, true);
  try {
    const data = await api("/api/auth/me", { method: "PATCH", body });
    state.user = data.user;
    // se mudou o CEP, invalida o cache de locais pra próxima abertura buscar de novo
    if (body.cep) locaisLoaded = false;
    profileSuccess.textContent = "Perfil atualizado!";
    show(profileSuccess);
    toast("Perfil atualizado 🎣", "success");
  } catch (err) {
    profileError.textContent = err.message;
    show(profileError);
  } finally {
    setBtnLoading(btnSaveProfile, false);
  }
});

// Fecha modal de perfil ao clicar no backdrop ou X
modalProfile.addEventListener("click", (e) => {
  if (e.target.matches("[data-close-modal]")) hide(modalProfile);
});

// --------------------------------------------------------------------------- //
// Limpar chat / logout                                                        //
// --------------------------------------------------------------------------- //
btnClearChat.addEventListener("click", async () => {
  if (!confirm("Encerrar esta conversa e começar do zero?")) return;
  await logout();
});

// Clicar no chip do usuário abre o modal de perfil (logout agora é dentro dele)
userChip.addEventListener("click", openProfile);

// Botão "Sair" dentro do modal de perfil
btnLogout.addEventListener("click", async () => {
  if (!confirm(`Sair como ${state.user?.nome || ""}?`)) return;
  hide(modalProfile);
  await logout();
});

async function logout() {
  try { await api("/api/auth/logout", { method: "POST", body: {} }); } catch {}
  state.user = null;
  state.chatStarted = false;
  state.cartCount = 0;
  messagesEl.innerHTML = "";
  inputEmail.value = "";
  hide(elChatScreen);
  show(elAuthScreen);
  showAuthStep("email");
  setTimeout(() => inputEmail.focus(), 60);
}

// --------------------------------------------------------------------------- //
// Bootstrap (tenta reusar sessão existente)                                   //
// --------------------------------------------------------------------------- //
(async function bootstrap() {
  showAuthStep("email");
  setTimeout(() => inputEmail.focus(), 80);

  try {
    const me = await api("/api/auth/me");
    if (me.user) {
      state.user = me.user;
      // não auto-inicia chat — o bot precisa ser construído por /chat/start
      // mas se quisermos, podemos pular direto
      await entrarNoChat();
    }
  } catch {
    // ignora — vai pra tela de email
  }
})();

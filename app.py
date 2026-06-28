import os, re, json, urllib.request, urllib.parse
from flask import Flask, request, jsonify

app = Flask(__name__)

# ── Configuração ──────────────────────────────────────────────────────────────
ZAPI_INSTANCE = os.environ.get("ZAPI_INSTANCE", "")
ZAPI_TOKEN    = os.environ.get("ZAPI_TOKEN", "")
ZAPI_URL      = f"https://api.z-api.io/instances/{ZAPI_INSTANCE}/token/{ZAPI_TOKEN}"

TRELLO_KEY    = os.environ.get("TRELLO_KEY", "")
TRELLO_TOKEN  = os.environ.get("TRELLO_TOKEN", "")
TRELLO_BOARD  = os.environ.get("TRELLO_BOARD", "tGmj0Fik")

CLAUDE_KEY    = os.environ.get("CLAUDE_API_KEY", "")

# Cache de listas do Trello
_trello_lists = {}

# ── Trello ────────────────────────────────────────────────────────────────────
def trello_get_lists():
    global _trello_lists
    if _trello_lists:
        return _trello_lists
    url = f"https://api.trello.com/1/boards/{TRELLO_BOARD}/lists?key={TRELLO_KEY}&token={TRELLO_TOKEN}"
    with urllib.request.urlopen(url, timeout=10) as r:
        lists = json.loads(r.read())
    _trello_lists = {l["name"].lower(): l["id"] for l in lists}
    return _trello_lists

def trello_criar_card(lista_nome, titulo, descricao):
    listas = trello_get_lists()
    list_id = None
    for nome, lid in listas.items():
        if lista_nome.lower() in nome:
            list_id = lid
            break
    if not list_id:
        list_id = list(listas.values())[0]

    dados = urllib.parse.urlencode({
        "name": titulo,
        "desc": descricao,
        "idList": list_id,
        "key": TRELLO_KEY,
        "token": TRELLO_TOKEN
    }).encode()
    req = urllib.request.Request("https://api.trello.com/1/cards", data=dados, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

# ── Z-API ─────────────────────────────────────────────────────────────────────
def zapi_enviar(telefone, mensagem):
    dados = json.dumps({"phone": telefone, "message": mensagem}).encode()
    req = urllib.request.Request(
        f"{ZAPI_URL}/send-text",
        data=dados,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

# ── Claude AI ─────────────────────────────────────────────────────────────────
def claude_analisar(mensagem):
    prompt = f"""Você é um assistente da Move Online Contabilidade. Analise a mensagem do cliente e responda em JSON:

Mensagem: "{mensagem}"

Classifique em uma das intenções:
- "abertura_empresa": cliente quer abrir empresa
- "troca_contador": cliente quer trocar de contador
- "nota_fiscal": dúvida ou pedido sobre notas fiscais
- "imposto": dúvida sobre impostos/tributos
- "financeiro": assunto financeiro
- "duvida_simples": dúvida que pode ser respondida rapidamente
- "duvida_complexa": dúvida que precisa de análise
- "outro": não identificado

Responda APENAS com JSON neste formato:
{{
  "intencao": "abertura_empresa",
  "lista_trello": "Abertura de Empresa",
  "resposta_cliente": "mensagem de confirmação para enviar ao cliente",
  "titulo_card": "título resumido para o card do Trello",
  "precisa_analise": false
}}"""

    dados = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 500,
        "messages": [{"role": "user", "content": prompt}]
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=dados,
        headers={
            "Content-Type": "application/json",
            "x-api-key": CLAUDE_KEY,
            "anthropic-version": "2023-06-01"
        },
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.loads(r.read())

    texto = resp["content"][0]["text"]
    return json.loads(texto)

# ── Webhook ───────────────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True)

    # Ignora mensagens enviadas pelo próprio bot
    if data.get("fromMe"):
        return jsonify({"ok": True})

    # Ignora grupos
    if data.get("isGroup"):
        return jsonify({"ok": True})

    telefone = data.get("phone", "")
    mensagem = data.get("text", {}).get("message", "")
    nome = data.get("senderName", "Cliente")

    if not mensagem or not telefone:
        return jsonify({"ok": True})

    try:
        analise = claude_analisar(mensagem)

        # Envia confirmação ao cliente
        zapi_enviar(telefone, analise["resposta_cliente"])

        # Cria card no Trello
        descricao = f"**Cliente:** {nome}\n**Telefone:** {telefone}\n**Mensagem:** {mensagem}"
        trello_criar_card(analise["lista_trello"], analise["titulo_card"], descricao)

    except Exception as e:
        print(f"Erro: {e}")
        zapi_enviar(telefone, "Olá! Recebemos sua mensagem e em breve entraremos em contato. 😊")

    return jsonify({"ok": True})

@app.route("/")
def index():
    return "Move Online Bot - Ativo ✅"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

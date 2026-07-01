import os, re, json, urllib.request, urllib.parse, threading
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
import pytz

app = Flask(__name__)

ZAPI_INSTANCE = os.environ.get("ZAPI_INSTANCE", "")
ZAPI_TOKEN    = os.environ.get("ZAPI_TOKEN", "")
ZAPI_URL      = f"https://api.z-api.io/instances/{ZAPI_INSTANCE}/token/{ZAPI_TOKEN}"
TRELLO_KEY    = os.environ.get("TRELLO_KEY", "")
TRELLO_TOKEN  = os.environ.get("TRELLO_TOKEN", "")
TRELLO_BOARD  = os.environ.get("TRELLO_BOARD", "tGmj0Fik")
CLAUDE_KEY    = os.environ.get("CLAUDE_API_KEY", "")
BR_TZ = pytz.timezone("America/Sao_Paulo")
_trello_lists = {}
_pendentes = {}
_ultima_resposta = {}

SPAM_PALAVRAS = ["promoção","oferta","ganhe","grátis","gratis","clique aqui","acesse agora","sorteio","prêmio","premio","newsletter","broadcast","divulgação","spam"]

GDOC_URL = "https://docs.google.com/document/d/1wBVprhctTXtDmhdE-wVEApNvZDCMwvWBlhDLAsExcu4/export?format=txt"
_prompt_cache = {"texto": "", "ts": 0}

def buscar_prompt():
    import time
    agora = time.time()
    if agora - _prompt_cache["ts"] < 300 and _prompt_cache["texto"]:
        return _prompt_cache["texto"]
    try:
        with urllib.request.urlopen(GDOC_URL, timeout=10) as r:
            texto = r.read().decode("utf-8")
        _prompt_cache["texto"] = texto
        _prompt_cache["ts"] = agora
        return texto
    except Exception as e:
        print(f"[PROMPT] Erro ao buscar Google Doc: {e}")
        return _prompt_cache["texto"] or PROMPT_FALLBACK

PROMPT_FALLBACK = (
    "Voce e Claudio-AI, atendente da Move Online Contabilidade Medica.\n"
    "Classifique a mensagem e responda em JSON com: categoria, lista_trello, titulo_card, complexidade, acao, resposta_cliente, faltam_dados, dados_necessarios."
)

def e_spam(mensagem):
    return any(p in mensagem.lower() for p in SPAM_PALAVRAS)

def get_delay_segundos():
    agora = datetime.now(BR_TZ)
    hora, dia = agora.hour, agora.weekday()
    if dia < 5:
        if 8 <= hora < 17: return 10 * 60
        elif 17 <= hora < 22: return 60 * 60
        else: return None
    else:
        return 2 * 60 * 60 if 8 <= hora < 16 else None

def calcular_prazo(categoria, subcategoria=""):
    agora = datetime.now(BR_TZ)
    if categoria == "NOTA":
        return agora.strftime("%d/%m/%Y") + (" (hoje)" if agora.hour < 17 else " (proximo dia util)")
    elif categoria == "IMPOSTO": return "Ate o dia 10 do mes"
    elif categoria == "FINANCEIRO": return "Ate 4 horas uteis"
    elif categoria == "DUVIDAS":
        if "alta" in subcategoria.lower(): return "Ate 5 dias uteis"
        elif "media" in subcategoria.lower(): return "Ate 48 horas uteis"
        else: return "Ate 4 horas"
    return "Em breve"

def trello_get_lists():
    global _trello_lists
    if _trello_lists: return _trello_lists
    url = f"https://api.trello.com/1/boards/{TRELLO_BOARD}/lists?key={TRELLO_KEY}&token={TRELLO_TOKEN}"
    with urllib.request.urlopen(url, timeout=10) as r:
        lists = json.loads(r.read())
    _trello_lists = {l["name"].lower(): l["id"] for l in lists}
    return _trello_lists

def trello_buscar_card_por_telefone(telefone, lista_nome=None):
    listas = trello_get_lists()
    for nome_l, list_id in listas.items():
        url = f"https://api.trello.com/1/lists/{list_id}/cards?filter=open&key={TRELLO_KEY}&token={TRELLO_TOKEN}"
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                cards = json.loads(r.read())
            card = next((c for c in cards if telefone in c.get("name", "")), None)
            if card:
                return card
        except Exception:
            continue
    return None

def trello_atualizar_card(card_id, nova_mensagem, horario):
    url_get = f"https://api.trello.com/1/cards/{card_id}?key={TRELLO_KEY}&token={TRELLO_TOKEN}"
    with urllib.request.urlopen(url_get, timeout=10) as r:
        card = json.loads(r.read())
    desc_atual = card.get("desc", "")
    nova_atualizacao = f"\n---\nMensagem: {nova_mensagem}\nHorário: {horario}"
    nova_desc = desc_atual + nova_atualizacao
    dados = urllib.parse.urlencode({"desc": nova_desc, "key": TRELLO_KEY, "token": TRELLO_TOKEN}).encode()
    req = urllib.request.Request(f"https://api.trello.com/1/cards/{card_id}", data=dados, method="PUT")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

def trello_criar_card(lista_nome, titulo, nome, telefone, mensagem, horario):
    listas = trello_get_lists()
    list_id = next((lid for nome_l, lid in listas.items() if lista_nome.lower() in nome_l), list(listas.values())[0])
    descricao = (
        f"Nome: {nome}\n"
        f"Telefone: {telefone}\n"
        f"Mensagem: {mensagem}\n"
        f"Horário Brasília: {horario}\n\n"
        f"Atualizações:"
    )
    card_titulo = f"{nome} | {telefone}"
    dados = urllib.parse.urlencode({"name": card_titulo, "desc": descricao, "idList": list_id, "key": TRELLO_KEY, "token": TRELLO_TOKEN}).encode()
    with urllib.request.urlopen(urllib.request.Request("https://api.trello.com/1/cards", data=dados, method="POST"), timeout=10) as r:
        return json.loads(r.read())

def zapi_enviar(telefone, mensagem):
    dados = json.dumps({"phone": telefone, "message": mensagem}).encode()
    req = urllib.request.Request(f"{ZAPI_URL}/send-text", data=dados, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

def ja_respondeu_recentemente(telefone, minutos=30):
    ultima = _ultima_resposta.get(telefone)
    if not ultima: return False
    return (datetime.now(BR_TZ) - ultima).total_seconds() < minutos * 60

def e_mensagem_vazia(mensagem):
    palavras_vazias = {"oi","ola","olá","tudo bem","tudo","bom dia","boa tarde","boa noite","obrigado","obrigada","ok","sim","nao","não","👍","😊"}
    return mensagem.strip().lower() in palavras_vazias or len(mensagem.strip()) <= 5

def processar_mensagem(telefone, mensagem, nome):
    try:
        if e_mensagem_vazia(mensagem):
            print(f"[IGNORADO] Mensagem vazia de {telefone}: {mensagem}")
            return

        analise = claude_analisar(mensagem)
        acao, categoria = analise.get("acao","criar"), analise.get("categoria","DUVIDAS")

        if acao == "ignorar" or categoria == "IGNORAR":
            return

        agora = datetime.now(BR_TZ).strftime("%d/%m/%Y %H:%M")
        lista = analise.get("lista_trello", "Entrada")

        card = trello_buscar_card_por_telefone(telefone)
        if card:
            trello_atualizar_card(card["id"], mensagem, agora)
            print(f"[ATUALIZADO] Card de {nome} ({telefone})")
        else:
            trello_criar_card(lista, None, nome, telefone, mensagem, agora)
            print(f"[CRIADO] Card de {nome} ({telefone}) em '{lista}'")

        resposta = analise.get("resposta_cliente", "")
        if resposta and not ja_respondeu_recentemente(telefone):
            zapi_enviar(telefone, resposta)
            _ultima_resposta[telefone] = datetime.now(BR_TZ)
            print(f"[RESPOSTA] Enviada para {telefone}")
        elif resposta:
            print(f"[RESPOSTA] Suprimida (ja respondeu recentemente) para {telefone}")

    except Exception as e:
        print("[ERRO] " + str(e))

def claude_analisar(mensagem):
    prompt = buscar_prompt() + "\n\nMensagem do cliente: " + mensagem + '\n\nResponda APENAS em JSON: {"categoria":"NOTA","lista_trello":"Notas Fiscais","titulo_card":"titulo","complexidade":"baixa","acao":"criar","resposta_cliente":"msg","faltam_dados":false,"dados_necessarios":""}'
    dados = json.dumps({"model": "claude-haiku-4-5-20251001", "max_tokens": 500,
        "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=dados,
        headers={"Content-Type": "application/json", "x-api-key": CLAUDE_KEY, "anthropic-version": "2023-06-01"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.loads(r.read())
    texto = resp["content"][0]["text"]
    m = re.search(r'\{.*\}', texto, re.DOTALL)
    return json.loads(m.group() if m else texto)

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    if data.get("fromMe") or data.get("isGroup") or data.get("broadcast"):
        return jsonify({"ok": True})
    telefone = data.get("phone","")
    mensagem = data.get("text",{}).get("message","")
    nome = data.get("senderName","Cliente")
    if not mensagem or not telefone: return jsonify({"ok": True})
    if e_spam(mensagem): return jsonify({"ok": True})
    delay = get_delay_segundos()
    if delay is None: return jsonify({"ok": True})
    if telefone in _pendentes: _pendentes[telefone].cancel()
    timer = threading.Timer(delay, processar_mensagem, args=[telefone, mensagem, nome])
    _pendentes[telefone] = timer
    timer.start()
    return jsonify({"ok": True})

@app.route("/")
def index():
    agora = datetime.now(BR_TZ).strftime("%d/%m/%Y %H:%M")
    delay = get_delay_segundos()
    return "Claudio-AI Move Online OK | " + agora + " | Delay: " + str(delay or "SILENCIOSO")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",5000)), debug=False)

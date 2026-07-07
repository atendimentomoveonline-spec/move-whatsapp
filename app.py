import os, re, json, urllib.request, urllib.parse, threading, sys
from memoria_claudio import salvar as mem_salvar, montar_contexto

_env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and "=" in _line and not _line.startswith("#"):
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())
sys.stdout.reconfigure(line_buffering=True)
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
_ultima_resposta = {}  # telefone -> datetime ultima resposta enviada
_contratos_processados = {}  # card_id -> datetime ultimo processamento

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
    "Voce e um atendente da Move Online Contabilidade Medica. Nunca se apresente pelo nome.\n"
    "Classifique a mensagem e responda em JSON com: categoria, lista_trello, titulo_card, complexidade, acao, resposta_cliente, faltam_dados, dados_necessarios."
)

def e_spam(mensagem):
    return any(p in mensagem.lower() for p in SPAM_PALAVRAS)

def get_delay_segundos():
    agora = datetime.now(BR_TZ)
    hora, dia = agora.hour, agora.weekday()
    if dia < 5:
        if 8 <= hora < 17: return 120        # comercial: 2 minutos
        elif 17 <= hora < 22: return 300     # noite: 5 minutos
        else: return None                    # madrugada: silencioso
    else:
        return 300 if 8 <= hora < 16 else None  # fds: 5 minutos

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
    """Busca card em TODAS as listas do board pelo telefone no titulo."""
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
    # Adiciona atualização na descrição do card
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

PROMPT_SISTEMA = (
    "Voce e um atendente da Move Online Contabilidade Medica. Nunca se apresente pelo nome.\n\n"
    "SLA: Notas 09-17h=mesmo dia, apos 17h=prox dia util, DAS/INSS=dia 10, baixa=4h, media=48h, alta=5du.\n\n"
    "CATEGORIAS: NOTA(nota fiscal,NF,emitir,cancelar), IMPOSTO(DAS,INSS,DARF,guia,TFE), "
    "FINANCEIRO(pix,boleto,paguei,comprovante), DUVIDAS(fator R,pro-labore,planejamento,simples), "
    "ABERTURA_EMPRESA(abrir empresa,CNPJ), TROCA_CONTADOR(trocar contador), ENTRADA(oi,bom dia,ola).\n\n"
    "REGRAS: DAS alto=DUVIDAS. ok/sim/entendi=continuidade. spam=ignorar.\n"
    "FASE 1: apenas classificar e avisar recebimento. Nao responder tecnicamente.\n\n"
    'JSON: {"categoria":"NOTA","lista_trello":"Notas Fiscais","titulo_card":"titulo","complexidade":"baixa","acao":"criar","resposta_cliente":"msg","faltam_dados":false,"dados_necessarios":""}'
)

def claude_analisar(mensagem, contexto: str = None):
    conteudo = contexto if contexto else f"Mensagem do cliente: {mensagem}"
    prompt = buscar_prompt() + "\n\n" + conteudo + '\n\nResponda APENAS em JSON: {"categoria":"NOTA","lista_trello":"Notas Fiscais","titulo_card":"titulo","complexidade":"baixa","acao":"criar","resposta_cliente":"msg","faltam_dados":false,"dados_necessarios":""}'
    dados = json.dumps({"model": "claude-haiku-4-5-20251001", "max_tokens": 500,
        "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=dados,
        headers={"Content-Type": "application/json", "x-api-key": CLAUDE_KEY, "anthropic-version": "2023-06-01"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.loads(r.read())
    texto = resp["content"][0]["text"]
    m = re.search(r'\{.*\}', texto, re.DOTALL)
    return json.loads(m.group() if m else texto)

def ja_respondeu_recentemente(telefone, minutos=10):
    """Retorna True se ja enviou resposta para esse telefone nos ultimos X minutos."""
    ultima = _ultima_resposta.get(telefone)
    if not ultima: return False
    return (datetime.now(BR_TZ) - ultima).total_seconds() < minutos * 60

def e_mensagem_vazia(mensagem):
    """Apenas emojis isolados ou mensagens com 1-2 caracteres são ignoradas."""
    return len(mensagem.strip()) <= 2

# ─── FLUXO TROCA CONTADOR ────────────────────────────────────

ETAPAS_COLETA = [
    {
        "campo": "gov_br",
        "pergunta": (
            "Boa tarde! Que ótimo ter você aqui! Para darmos início à transição, "
            "preciso de algumas informações da sua empresa. 😊\n\n"
            "Primeiro: você acessa o Gov.BR com login e senha, ou possui Certificado Digital (e-CNPJ)?\n"
            "Se tiver login Gov.BR, me envia o CPF/email e a senha. Se tiver certificado, me avisa que orientamos o envio."
        ),
    },
    {
        "campo": "cnpj",
        "pergunta": "Perfeito! Agora me envia o CNPJ da sua empresa. 🙏",
    },
    {
        "campo": "prefeitura",
        "pergunta": (
            "Ótimo! Precisamos também do acesso à Prefeitura para emissão de notas fiscais. "
            "Me envia o login e a senha do portal de notas fiscais do seu município."
        ),
    },
    {
        "campo": "contrato_social",
        "pergunta": (
            "Quase lá! Me envia o Contrato Social da empresa (pode ser o PDF mesmo). 👊"
        ),
    },
    {
        "campo": "dados_pessoais",
        "pergunta": (
            "Última etapa! Me envia os dados pessoais do sócio responsável:\n"
            "- Nome completo\n"
            "- CPF\n"
            "- Email\n"
            "- Data de nascimento"
        ),
    },
]

CAMPO_CONCLUIDO = "coleta_concluida"

def coleta_proxima_etapa(desc_card):
    """Analisa a descrição do card e retorna qual etapa ainda não foi coletada."""
    for etapa in ETAPAS_COLETA:
        marcador = f"[{etapa['campo'].upper()}]"
        if marcador not in desc_card:
            return etapa
    return None  # tudo coletado

def coleta_salvar_resposta(card_id, campo, mensagem, horario):
    """Adiciona a resposta do cliente com marcador de campo na descrição do card."""
    url_get = f"https://api.trello.com/1/cards/{card_id}?key={TRELLO_KEY}&token={TRELLO_TOKEN}"
    with urllib.request.urlopen(url_get, timeout=10) as r:
        card = json.loads(r.read())
    desc_atual = card.get("desc", "")
    nova_desc = desc_atual + f"\n\n[{campo.upper()}] {horario}\n{mensagem}"
    dados = urllib.parse.urlencode({"desc": nova_desc, "key": TRELLO_KEY, "token": TRELLO_TOKEN}).encode()
    req = urllib.request.Request(f"https://api.trello.com/1/cards/{card_id}", data=dados, method="PUT")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

def coleta_mover_para_revisao(card_id):
    """Move card para a lista Revisão Fiscal quando coleta estiver completa."""
    url = f"https://api.trello.com/1/boards/{TRELLO_BOARD}/lists?key={TRELLO_KEY}&token={TRELLO_TOKEN}"
    with urllib.request.urlopen(url, timeout=10) as r:
        lists = json.loads(r.read())
    listas = {l["name"].lower(): l["id"] for l in lists}
    list_id = next((lid for nome, lid in listas.items() if "revis" in nome and "fiscal" in nome), None)
    if not list_id:
        print("[COLETA] Lista 'Revisão Fiscal' não encontrada no Trello")
        return
    dados = urllib.parse.urlencode({"idList": list_id, "key": TRELLO_KEY, "token": TRELLO_TOKEN}).encode()
    req = urllib.request.Request(f"https://api.trello.com/1/cards/{card_id}", data=dados, method="PUT")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

def processar_troca_contador(telefone, mensagem, nome, card):
    """Fluxo de coleta progressiva para leads de troca de contador."""
    agora = datetime.now(BR_TZ).strftime("%d/%m/%Y %H:%M")
    card_id = card["id"]
    desc_card = card.get("desc", "")

    # Salva a mensagem recebida vinculada à etapa atual
    proxima = coleta_proxima_etapa(desc_card)

    if proxima is None:
        # Coleta completa — só confirma e aguarda revisão
        if CAMPO_CONCLUIDO not in desc_card:
            dados = urllib.parse.urlencode({
                "desc": desc_card + f"\n\n[{CAMPO_CONCLUIDO.upper()}] {agora}",
                "key": TRELLO_KEY, "token": TRELLO_TOKEN
            }).encode()
            urllib.request.urlopen(
                urllib.request.Request(f"https://api.trello.com/1/cards/{card_id}", data=dados, method="PUT"),
                timeout=10
            )
            coleta_mover_para_revisao(card_id)
            resposta = (
                "Recebemos tudo! 🙏 Já iniciamos a revisão fiscal da sua empresa. "
                "Em breve nosso time entra em contato com o resumo completo e próximos passos."
            )
            zapi_enviar(telefone, resposta)
            _ultima_resposta[telefone] = datetime.now(BR_TZ)
        return

    # Identifica qual campo esta mensagem responde (a etapa mais recente sem resposta)
    campo_atual = proxima["campo"]

    # Salva a resposta do cliente na descrição do card
    coleta_salvar_resposta(card_id, campo_atual, mensagem, agora)
    print(f"[COLETA] Campo '{campo_atual}' salvo para {nome} ({telefone})")

    # Recarrega desc atualizada para verificar próxima etapa
    url_get = f"https://api.trello.com/1/cards/{card_id}?key={TRELLO_KEY}&token={TRELLO_TOKEN}"
    with urllib.request.urlopen(url_get, timeout=10) as r:
        desc_atualizada = json.loads(r.read()).get("desc", "")

    proxima_etapa = coleta_proxima_etapa(desc_atualizada)

    if proxima_etapa:
        # Pergunta a próxima etapa
        zapi_enviar(telefone, proxima_etapa["pergunta"])
        _ultima_resposta[telefone] = datetime.now(BR_TZ)
        print(f"[COLETA] Pergunta enviada para etapa '{proxima_etapa['campo']}'")
    else:
        # Concluiu na última resposta
        dados = urllib.parse.urlencode({
            "desc": desc_atualizada + f"\n\n[{CAMPO_CONCLUIDO.upper()}] {agora}",
            "key": TRELLO_KEY, "token": TRELLO_TOKEN
        }).encode()
        urllib.request.urlopen(
            urllib.request.Request(f"https://api.trello.com/1/cards/{card_id}", data=dados, method="PUT"),
            timeout=10
        )
        coleta_mover_para_revisao(card_id)
        zapi_enviar(telefone, (
            "Perfeito! Recebemos todas as informações. 🙏 "
            "Já iniciamos a revisão fiscal da sua empresa e em breve nosso time entra em contato com o próximo passo."
        ))
        _ultima_resposta[telefone] = datetime.now(BR_TZ)
        print(f"[COLETA] Coleta concluída para {nome} — card movido para Revisão Fiscal")

# ─── PROCESSAR MENSAGEM ───────────────────────────────────────

def processar_mensagem(telefone, mensagem, nome):
    try:
        # Mensagem vazia (só 1-2 chars) — ignora
        if e_mensagem_vazia(mensagem):
            print(f"[IGNORADO] Mensagem muito curta de {telefone}: {mensagem}")
            return

        # Salva mensagem recebida no histórico
        mem_salvar(telefone, mensagem, "recebida", nome)

        agora = datetime.now(BR_TZ).strftime("%d/%m/%Y %H:%M")

        # Verifica se ja existe card de troca de contador em andamento para esse telefone
        card_existente = trello_buscar_card_por_telefone(telefone)
        if card_existente:
            desc = card_existente.get("desc", "")
            # Se card e de troca de contador e coleta ainda nao concluida, continua fluxo
            if "[GOV_BR]" in desc or "[CNPJ]" in desc or "[PREFEITURA]" in desc or \
               "[CONTRATO_SOCIAL]" in desc or "[DADOS_PESSOAIS]" in desc or \
               "troca de contador" in desc.lower() or "trocar contador" in desc.lower():
                if CAMPO_CONCLUIDO.upper() not in desc:
                    processar_troca_contador(telefone, mensagem, nome, card_existente)
                    return
                # Coleta ja concluida — atualiza card normalmente
                trello_atualizar_card(card_existente["id"], mensagem, agora)
                return

        # Monta contexto completo da conversa para o Claude
        contexto = montar_contexto(telefone, mensagem, nome)
        analise = claude_analisar(mensagem, contexto)
        acao, categoria = analise.get("acao","criar"), analise.get("categoria","DUVIDAS")

        # Ignorar apenas spam/broadcast explícito
        if acao == "ignorar" or categoria == "IGNORAR":
            return

        lista = analise.get("lista_trello", "Entrada")

        if categoria == "TROCA_CONTADOR":
            # Cria card e inicia coleta progressiva
            if not card_existente:
                trello_criar_card(lista, None, nome, telefone, mensagem, agora)
                card_existente = trello_buscar_card_por_telefone(telefone)
            if card_existente:
                # Envia primeira pergunta da coleta
                zapi_enviar(telefone, ETAPAS_COLETA[0]["pergunta"])
                _ultima_resposta[telefone] = datetime.now(BR_TZ)
                print(f"[COLETA] Iniciada para {nome} ({telefone})")
            return

        # Fluxo normal para outras categorias
        if card_existente:
            trello_atualizar_card(card_existente["id"], mensagem, agora)
            print(f"[ATUALIZADO] Card de {nome} ({telefone})")
        else:
            trello_criar_card(lista, None, nome, telefone, mensagem, agora)
            print(f"[CRIADO] Card de {nome} ({telefone}) em '{lista}'")

        resposta = analise.get("resposta_cliente", "")
        if resposta and not ja_respondeu_recentemente(telefone):
            zapi_enviar(telefone, resposta)
            mem_salvar(telefone, resposta, "enviada", "Claudio")
            _ultima_resposta[telefone] = datetime.now(BR_TZ)
            print(f"[RESPOSTA] Enviada para {telefone}")
        elif resposta:
            print(f"[RESPOSTA] Suprimida (ja respondeu recentemente) para {telefone}")

    except Exception as e:
        print("[ERRO] " + str(e))

@app.route("/trello-webhook", methods=["POST", "HEAD"])
def trello_webhook():
    if request.method == "HEAD":
        return "", 200
    data = request.get_json(force=True)
    action = data.get("action", {})
    tipo = action.get("type", "")
    action_data = action.get("data", {})
    card = action_data.get("card", {})
    # createCard usa "list", updateCard (mover card) usa "listAfter"
    lista_info = action_data.get("listAfter") or action_data.get("list") or {}
    lista_nome = lista_info.get("name", "")
    print(f"[WEBHOOK] tipo={tipo} lista={lista_nome!r}", flush=True)
    if tipo in ("createCard", "updateCard") and "troca" in lista_nome.lower():
        card_id = card.get("id")
        agora = datetime.now(BR_TZ)
        ultimo = _contratos_processados.get("troca_" + card_id)
        if ultimo and (agora - ultimo).total_seconds() < 60:
            return jsonify({"ok": True})
        _contratos_processados["troca_" + card_id] = agora
        try:
            url = f"https://api.trello.com/1/cards/{card_id}?key={TRELLO_KEY}&token={TRELLO_TOKEN}"
            with urllib.request.urlopen(url, timeout=10) as r:
                card_full = json.loads(r.read())
            nome_card = card_full.get("name", "")
            # Extrai telefone do título: "Nome | 5519999999999"
            m_tel = re.search(r'(\d{10,13})', nome_card.replace(" ", "").replace("-", ""))
            telefone = m_tel.group(1) if m_tel else ""
            if not telefone.startswith("55"):
                telefone = "55" + telefone
            if telefone and len(telefone) >= 12:
                zapi_enviar(telefone, ETAPAS_COLETA[0]["pergunta"])
                print(f"[TROCA] Primeira mensagem enviada para {telefone} — card {card_id}", flush=True)
            else:
                print(f"[TROCA] Telefone não encontrado no card '{nome_card}'", flush=True)
        except Exception as e:
            print(f"[TROCA ERRO] {e}", flush=True)

    if tipo in ("createCard", "updateCard") and "contrato" in lista_nome.lower():
            card_id = card.get("id")
            agora = datetime.now(BR_TZ)
            ultimo = _contratos_processados.get(card_id)
            if ultimo and (agora - ultimo).total_seconds() < 30:
                print(f"[CONTRATO] Ignorando duplicata para card {card_id}", flush=True)
                return jsonify({"ok": True})
            _contratos_processados[card_id] = agora
            print(f"[CONTRATO] Iniciando para card {card_id}", flush=True)
            try:
                url = f"https://api.trello.com/1/cards/{card_id}?key={TRELLO_KEY}&token={TRELLO_TOKEN}"
                with urllib.request.urlopen(url, timeout=10) as r:
                    card_full = json.loads(r.read())
                from contrato_clicksign import processar_contrato_trello
                threading.Thread(
                    target=processar_contrato_trello,
                    args=[card_full.get("name",""), card_full.get("desc",""), card_id],
                    daemon=True
                ).start()
            except Exception as e:
                print(f"[CONTRATO ERRO] {e}", flush=True)
    return jsonify({"ok": True})

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

"""
Automação de contratos via Trello + ClickSign
- Webhook do Trello detecta novo card no quadro "Contratos"
- Preenche PDF do contrato com dados do card
- Faz upload na pasta ClickSign com nome CNPJ_NomeCliente.pdf
- Adiciona signatário e envia para assinatura
"""
import os, re, io, json, base64, tempfile, urllib.request, urllib.parse
from pypdf import PdfReader, PdfWriter
import pdfplumber
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4

CLICKSIGN_TOKEN   = os.environ.get("CLICKSIGN_TOKEN", "")
CLICKSIGN_URL     = "https://app.clicksign.com/api/v1"
TRELLO_KEY        = os.environ.get("TRELLO_KEY", "")
TRELLO_TOKEN      = os.environ.get("TRELLO_TOKEN", "")
GOOGLE_API_KEY    = os.environ.get("GOOGLE_API_KEY", "")
DRIVE_FOLDER_ID   = os.environ.get("CONTRATO_FOLDER_ID", "1AhHM01rBVOD-Zmz-xjpuA23e0ajKbuSi")
PASTA_CLICKSIGN   = os.environ.get("PASTA_CLICKSIGN", "/Contratos")
ZAPI_INSTANCE     = os.environ.get("ZAPI_INSTANCE", "")
ZAPI_TOKEN        = os.environ.get("ZAPI_TOKEN", "")

def baixar_pdf_modelo():
    """Baixa o PDF modelo do Google Drive."""
    url = (f"https://www.googleapis.com/drive/v3/files"
           f"?q=%27{DRIVE_FOLDER_ID}%27+in+parents+and+trashed%3Dfalse"
           f"&fields=files(id,name)&key={GOOGLE_API_KEY}")
    with urllib.request.urlopen(url, timeout=10) as r:
        arquivos = json.loads(r.read())["files"]
    pdf = next((f for f in arquivos if f["name"].lower().endswith(".pdf")), None)
    if not pdf:
        raise Exception("PDF modelo não encontrado na pasta do Drive")
    url_download = f"https://www.googleapis.com/drive/v3/files/{pdf['id']}?alt=media&key={GOOGLE_API_KEY}"
    with urllib.request.urlopen(url_download, timeout=30) as r:
        return r.read()
    print(f"[DRIVE] PDF modelo baixado: {pdf['name']}")

def parse_descricao(desc):
    """
    Extrai campos da descrição do card Trello.
    Formato esperado:
        Nome: Dr. João Silva
        CNPJ: 12.345.678/0001-90   (ou CPF: 123.456.789-00 para pessoa física)
        Email: joao@email.com
        Telefone: 11 99999-9999
        Endereço: Rua X, 123 - SP
        Plano: PRO
        Vencimento: 10
        Pagamento: PIX
        Municipio: São Paulo
    """
    campos = {}
    for linha in desc.splitlines():
        if ":" in linha:
            chave, _, valor = linha.partition(":")
            # Remove markdown links do Trello: [texto](url) → texto
            valor = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', valor)
            # Remove caracteres de controle invisíveis
            valor = re.sub(r'[​-‏‪-‮]', '', valor)
            campos[chave.strip().lower()] = valor.strip()
    # Normaliza chaves com variações
    for alias, chave in [("razão social", "razao_social"), ("razao social", "razao_social"),
                         ("município", "municipio"), ("endereço", "endereco")]:
        if alias in campos and chave not in campos:
            campos[chave] = campos[alias]

    # Normaliza: se vier "cpf" usa como documento, coloca também em "cnpj" para compatibilidade
    if "cpf" in campos and "cnpj" not in campos:
        campos["cnpj"] = campos["cpf"]
        campos["documento"] = campos["cpf"]
        campos["tipo_documento"] = "CPF"
    elif "cnpj" in campos:
        campos["documento"] = campos["cnpj"]
        campos["tipo_documento"] = "CNPJ"
    return campos

def _criar_overlay(page_width, page_height, substituicoes):
    """Cria PDF de overlay com textos posicionados sobre os campos."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(page_width, page_height))
    for (x, y, w, h, texto, font_size) in substituicoes:
        c.setFont("Helvetica", font_size)
        c.setFillColorRGB(1, 1, 1)
        c.rect(x, y, w, h, fill=1, stroke=0)
        c.setFillColorRGB(0, 0, 0)
        c.drawString(x + 2, y + 2, str(texto))
    c.save()
    buf.seek(0)
    return buf.read()

# Labels buscados no PDF → chave nos campos do card (ordem importa: mais específico primeiro)
_LABELS_PDF = [
    ("CONTRATANTE",                "nome"),
    ("RAZÃO SOCIAL",               "razao_social"),
    ("RAZAO SOCIAL",               "razao_social"),
    ("INSCRITO NO CNPJ",           "cnpj"),
    ("E-MAIL CADASTRADO",          "email"),
    ("TELEFONE CADASTRADO",        "telefone"),
    ("MUNICÍPIO DE EMISSÃO NFS-E", "municipio"),
    ("MUNICIPIO DE EMISSAO NFS-E", "municipio"),
    ("MUNICÍPIO DE EMISSÃO",       "municipio"),
    ("MUNICIPIO DE EMISSAO",       "municipio"),
    ("NOME",                       "nome"),
    ("E-MAIL",                     "email"),
    ("TELEFONE",                   "telefone"),
    ("ENDEREÇO",                   "endereco"),
    ("ENDERECO",                   "endereco"),
]

# Labels com múltiplas opções (X) — apaga toda a célula e reescreve só o escolhido
# chave_campo → {valor_card: texto_a_escrever}
_LABELS_OPCOES = {
    "PLANO CONTRATADO": {
        "campo": "plano",
        "opcoes": ["START", "PRO", "GROWTH", "SCALE"],
    },
    "VENCIMENTO MENSAL": {
        "campo": "vencimento",
        "opcoes": ["10", "15", "20"],
    },
    "FORMA DE PAGAMENTO": {
        "campo": "pagamento",
        "opcoes": ["BOLETO", "PIX", "CRÉDITO", "DÉBITO", "CREDITO", "DEBITO"],
    },
}

def _encontrar_label(words, label_tokens, margem_y=6):
    """Retorna (y_top, y_bot, x_label_fim) do label se encontrado na página."""
    for i, word in enumerate(words):
        if word["text"].upper().rstrip(":") != label_tokens[0]:
            continue
        match = True
        for j, tok in enumerate(label_tokens[1:], 1):
            if i + j >= len(words) or words[i + j]["text"].upper().rstrip(":") != tok:
                match = False
                break
        if match:
            last = words[i + len(label_tokens) - 1]
            return float(word["top"]), float(last["bottom"]), float(last["x1"])
    return None

def _detectar_font_size(words, y_top, y_bot, fallback=9.0):
    """Detecta o tamanho médio da fonte nas palavras da linha."""
    linha_y = (y_top + y_bot) / 2
    sizes = []
    for w in words:
        wy = (float(w.get("top", 0)) + float(w.get("bottom", 0))) / 2
        if abs(wy - linha_y) < 6 and w.get("size"):
            sizes.append(float(w["size"]))
    return round(sum(sizes) / len(sizes), 1) if sizes else fallback

def preencher_pdf(campos):
    """Preenche o PDF buscando os labels e sobrepondo os valores na coluna DESCRIÇÃO."""
    pdf_bytes = baixar_pdf_modelo()

    if not campos.get("razao_social"):
        campos["razao_social"] = campos.get("nome", "")

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as plumber_pdf:
        paginas_subs = {}

        for pg_idx, pg in enumerate(plumber_pdf.pages):
            w_pt = float(pg.width)
            h_pt = float(pg.height)
            words = pg.extract_words(extra_attrs=["size"])
            if not words:
                continue

            subs = []
            ja_preenchidos = set()

            FONT_SIZE = 9.0  # fonte fixa compatível com o PDF

            def _overlay(label_str, texto):
                label_tokens = [t.upper() for t in label_str.split()]
                resultado = _encontrar_label(words, label_tokens)
                if not resultado:
                    return
                y_top, y_bot, x_label_fim = resultado
                # Inicia o retângulo branco logo após o label (cobre qualquer XX residual)
                val_x = x_label_fim + 4
                val_w = w_pt - val_x - 15
                val_h = (y_bot - y_top) + 4
                val_y_rl = h_pt - y_bot - 1
                subs.append((val_x, val_y_rl, val_w, val_h, texto, FONT_SIZE))
                print(f"[PDF] Pág {pg_idx+1} '{label_str}' → '{texto}'", flush=True)

            # --- Campos texto simples ---
            for label_str, campo_key in _LABELS_PDF:
                if campo_key in ja_preenchidos:
                    continue
                valor = campos.get(campo_key, "").strip()
                if not valor:
                    continue
                _overlay(label_str, valor)
                ja_preenchidos.add(campo_key)

            # --- Data de início ---
            from datetime import date
            import locale
            try:
                locale.setlocale(locale.LC_TIME, "pt_BR.UTF-8")
            except Exception:
                try:
                    locale.setlocale(locale.LC_TIME, "Portuguese_Brazil.1252")
                except Exception:
                    pass
            hoje = date.today()
            MESES = ["JANEIRO","FEVEREIRO","MARÇO","ABRIL","MAIO","JUNHO",
                     "JULHO","AGOSTO","SETEMBRO","OUTUBRO","NOVEMBRO","DEZEMBRO"]
            data_str = f"SÃO PAULO, {hoje.day:02d} DE {MESES[hoje.month-1]} DE {hoje.year}"
            _overlay("DATA DE INÍCIO DA PRESTAÇÃO DE SERVIÇO", data_str)
            _overlay("DATA DE INÍCIO", data_str)

            # --- Campos com opções (Plano, Vencimento, Pagamento) ---
            for label_str, cfg in _LABELS_OPCOES.items():
                campo_key = cfg["campo"]
                valor_escolhido = campos.get(campo_key, "").strip().upper()
                if not valor_escolhido:
                    continue
                opcao_texto = next(
                    (op for op in cfg["opcoes"] if op.upper() in valor_escolhido),
                    valor_escolhido
                )
                opcao_texto = opcao_texto.replace("CREDITO", "CRÉDITO").replace("DEBITO", "DÉBITO")
                _overlay(label_str, opcao_texto)

            if subs:
                paginas_subs[pg_idx] = (w_pt, h_pt, subs)

    if not paginas_subs:
        print("[PDF] AVISO: nenhum campo preenchido", flush=True)

    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter()

    for pg_idx, page in enumerate(reader.pages):
        if pg_idx in paginas_subs:
            w_pt, h_pt, subs = paginas_subs[pg_idx]
            overlay_bytes = _criar_overlay(w_pt, h_pt, subs)
            overlay_reader = PdfReader(io.BytesIO(overlay_bytes))
            page.merge_page(overlay_reader.pages[0])
        writer.add_page(page)

    doc_numero = re.sub(r"\D", "", campos.get("cnpj", campos.get("cpf", "00000000000000")))
    nome_limpo = re.sub(r"[^\w\s]", "", campos.get("nome", "cliente")).strip().replace(" ", "_")
    nome_arquivo = f"{doc_numero}_{nome_limpo}.pdf"
    caminho = os.path.join(tempfile.gettempdir(), nome_arquivo)

    output = io.BytesIO()
    writer.write(output)
    with open(caminho, "wb") as f:
        f.write(output.getvalue())

    return caminho, nome_arquivo

def clicksign_upload(caminho_pdf, nome_arquivo):
    """Faz upload do PDF para a pasta no ClickSign."""
    with open(caminho_pdf, "rb") as f:
        conteudo = base64.b64encode(f.read()).decode()

    payload = json.dumps({
        "document": {
            "path": f"{PASTA_CLICKSIGN}/{nome_arquivo}",
            "content_base64": f"data:application/pdf;base64,{conteudo}"
        }
    }).encode()

    req = urllib.request.Request(
        f"{CLICKSIGN_URL}/documents?access_token={CLICKSIGN_TOKEN}",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.loads(r.read())

    doc_key = resp["document"]["key"]
    print(f"[CLICKSIGN] Upload OK — key: {doc_key}")
    return doc_key

def clicksign_adicionar_signatario(doc_key, email, nome):
    """Adiciona signatário ao documento."""
    # Criar signatário
    payload = json.dumps({
        "signer": {
            "email": email,
            "name": nome,
            "phone_number": "",
            "auths": ["email"],
            "delivery_method": "email",
            "has_documentation": False
        }
    }).encode()

    req = urllib.request.Request(
        f"{CLICKSIGN_URL}/signers?access_token={CLICKSIGN_TOKEN}",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        resp = json.loads(r.read())
    signer_key = resp["signer"]["key"]

    # Vincular signatário ao documento
    payload2 = json.dumps({
        "list": {
            "document_key": doc_key,
            "signer_key": signer_key,
            "sign_as": "sign",
            "message": "Por favor, assine o contrato de prestação de serviços contábeis da Move Online."
        }
    }).encode()

    req2 = urllib.request.Request(
        f"{CLICKSIGN_URL}/lists?access_token={CLICKSIGN_TOKEN}",
        data=payload2,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req2, timeout=15) as r2:
        resp2 = json.loads(r2.read())

    # Notificar signatário por email
    request_signature_key = resp2.get("list", {}).get("request_signature_key", "")
    if request_signature_key:
        payload3 = json.dumps({
            "request_signature_key": request_signature_key,
            "message": "Por favor, assine o contrato de prestação de serviços contábeis da Move Online Contabilidade Médica."
        }).encode()
        req3 = urllib.request.Request(
            f"{CLICKSIGN_URL}/notifications?access_token={CLICKSIGN_TOKEN}",
            data=payload3,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req3, timeout=15) as r3:
                r3.read()
            print(f"[CLICKSIGN] Notificação enviada para: {email}")
        except Exception as e:
            print(f"[CLICKSIGN] Erro ao notificar {email}: {e}")

    print(f"[CLICKSIGN] Signatário adicionado: {email}")
    return signer_key

def clicksign_enviar(doc_key):
    """Finaliza o documento para assinatura (ignora se ja estiver ativo)."""
    payload = json.dumps({"document": {"key": doc_key}}).encode()
    req = urllib.request.Request(
        f"{CLICKSIGN_URL}/documents/{doc_key}/finish?access_token={CLICKSIGN_TOKEN}",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="PATCH"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            print(f"[CLICKSIGN] Documento finalizado e enviado para assinatura!")
    except Exception as e:
        # 422 = documento ja esta ativo (running), nao e um erro real
        print(f"[CLICKSIGN] Documento ja ativo ou enviado: {e}")

def zapi_enviar_whatsapp(telefone, mensagem):
    """Envia mensagem WhatsApp via Z-API."""
    telefone_limpo = re.sub(r"\D", "", telefone)
    if not telefone_limpo or not ZAPI_INSTANCE or not ZAPI_TOKEN:
        return
    url = f"https://api.z-api.io/instances/{ZAPI_INSTANCE}/token/{ZAPI_TOKEN}/send-text"
    payload = json.dumps({"phone": telefone_limpo, "message": mensagem}).encode()
    req = urllib.request.Request(url, data=payload,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            print(f"[WHATSAPP] Aviso enviado para {telefone_limpo}")
    except Exception as e:
        print(f"[WHATSAPP] Erro ao enviar aviso: {e}")

def trello_comentar_card(card_id, texto):
    """Adiciona comentário no card do Trello."""
    if not card_id or not TRELLO_KEY or not TRELLO_TOKEN:
        return
    dados = urllib.parse.urlencode({"text": texto, "key": TRELLO_KEY, "token": TRELLO_TOKEN}).encode()
    req = urllib.request.Request(
        f"https://api.trello.com/1/cards/{card_id}/actions/comments",
        data=dados, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            print(f"[TRELLO] Comentário adicionado no card")
    except Exception as e:
        print(f"[TRELLO] Erro ao comentar: {e}")

def processar_contrato_trello(card_nome, card_desc, card_id=None):
    """Processa um novo card do quadro Contratos."""
    print(f"[CONTRATO] Processando: {card_nome}")
    campos = parse_descricao(card_desc)

    if not campos.get("email"):
        print(f"[CONTRATO] Email não encontrado na descrição — abortando")
        return False

    # 1. Preencher PDF
    caminho_pdf, nome_arquivo = preencher_pdf(campos)
    print(f"[CONTRATO] PDF gerado: {nome_arquivo}")

    # 2. Upload no ClickSign
    doc_key = clicksign_upload(caminho_pdf, nome_arquivo)

    # 3. Adicionar signatários: cliente + Move (evita duplicar se email for igual)
    MOVE_EMAIL = "suportemoveonline@gmail.com"
    clicksign_adicionar_signatario(doc_key, campos["email"], campos.get("nome", "Cliente"))
    if campos["email"].lower() != MOVE_EMAIL.lower():
        clicksign_adicionar_signatario(doc_key, MOVE_EMAIL, "Move Online Contabilidade")

    # 4. Enviar para assinatura
    clicksign_enviar(doc_key)

    # 5. Avisar cliente no WhatsApp
    telefone = campos.get("telefone", "")
    nome_curto = campos.get("nome", "").split()[0]
    email = campos.get("email", "")
    if telefone:
        mensagem = (
            f"Olá {nome_curto}! 😊\n\n"
            f"Seu contrato foi enviado para o email *{email}*.\n"
            f"Por favor, verifique sua caixa de entrada e assine o documento.\n\n"
            f"Qualquer dúvida estamos à disposição! 🤝"
        )
        zapi_enviar_whatsapp(telefone, mensagem)

    # 6. Comentar no card do Trello
    from datetime import datetime
    import pytz
    agora = datetime.now(pytz.timezone("America/Sao_Paulo")).strftime("%d/%m/%Y %H:%M")
    trello_comentar_card(card_id, f"✅ Contrato enviado para assinatura\n📧 {campos['email']}\n🕐 {agora}")

    # 7. Limpar PDF temporário
    try:
        os.remove(caminho_pdf)
    except:
        pass

    print(f"[CONTRATO] Concluído! Contrato enviado para {campos['email']}")
    return True

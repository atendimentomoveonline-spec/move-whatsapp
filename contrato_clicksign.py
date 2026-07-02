"""
Automação de contratos via Trello + ClickSign
- Webhook do Trello detecta card no quadro "Contratos"
- Preenche template DOCX com dados do card via docxtpl
- Converte para PDF com LibreOffice
- Faz upload no ClickSign, adiciona signatários e envia para assinatura
"""
import os, re, io, json, base64, tempfile, subprocess, urllib.request, urllib.parse
from datetime import date

CLICKSIGN_TOKEN = os.environ.get("CLICKSIGN_TOKEN", "")
CLICKSIGN_URL   = "https://app.clicksign.com/api/v1"
TRELLO_KEY      = os.environ.get("TRELLO_KEY", "")
TRELLO_TOKEN    = os.environ.get("TRELLO_TOKEN", "")
GOOGLE_API_KEY  = os.environ.get("GOOGLE_API_KEY", "")
DRIVE_FOLDER_ID = os.environ.get("CONTRATO_FOLDER_ID", "1AhHM01rBVOD-Zmz-xjpuA23e0ajKbuSi")
PASTA_CLICKSIGN = os.environ.get("PASTA_CLICKSIGN", "/Contratos")
ZAPI_INSTANCE   = os.environ.get("ZAPI_INSTANCE", "")
ZAPI_TOKEN      = os.environ.get("ZAPI_TOKEN", "")

MESES = ["JANEIRO","FEVEREIRO","MARÇO","ABRIL","MAIO","JUNHO",
         "JULHO","AGOSTO","SETEMBRO","OUTUBRO","NOVEMBRO","DEZEMBRO"]


def baixar_docx_modelo():
    """Baixa o template DOCX do Google Drive (procura arquivo .docx na pasta)."""
    url = (f"https://www.googleapis.com/drive/v3/files"
           f"?q=%27{DRIVE_FOLDER_ID}%27+in+parents+and+trashed%3Dfalse"
           f"&fields=files(id,name)&key={GOOGLE_API_KEY}")
    with urllib.request.urlopen(url, timeout=10) as r:
        arquivos = json.loads(r.read())["files"]

    # Prefere template DOCX; fallback para qualquer DOCX
    docx = next((f for f in arquivos if "template" in f["name"].lower() and f["name"].lower().endswith(".docx")), None)
    if not docx:
        docx = next((f for f in arquivos if f["name"].lower().endswith(".docx")), None)
    if not docx:
        raise Exception("Template DOCX não encontrado na pasta do Drive")

    url_dl = f"https://www.googleapis.com/drive/v3/files/{docx['id']}?alt=media&key={GOOGLE_API_KEY}"
    with urllib.request.urlopen(url_dl, timeout=30) as r:
        conteudo = r.read()
    print(f"[DRIVE] Template baixado: {docx['name']} ({len(conteudo)//1024} KB)")
    return conteudo


def parse_descricao(desc):
    """
    Extrai campos da descrição do card Trello.
    Formato esperado (uma chave por linha):
        Nome: Dr. João Silva
        CNPJ: 12.345.678/0001-90   (ou CPF: 123.456.789-00)
        Email: joao@email.com
        Telefone: 11 99999-9999
        Endereço: Rua X, 123 - SP
        Plano: PRO
        Vencimento: 10
        Pagamento: PIX
        Municipio: São Paulo
        Razao Social: Clínica XYZ Ltda
    """
    campos = {}
    for linha in desc.splitlines():
        if ":" in linha:
            chave, _, valor = linha.partition(":")
            valor = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', valor)  # remove markdown links
            valor = re.sub(r'[​-‏‪-‮]', '', valor)                   # remove chars invisíveis
            campos[chave.strip().lower()] = valor.strip()

    # Normaliza aliases
    for alias, chave in [
        ("razão social", "razao_social"), ("razao social", "razao_social"),
        ("município", "municipio"), ("municipio", "municipio"),
        ("endereço", "endereco"), ("endereco", "endereco"),
    ]:
        if alias in campos and chave not in campos:
            campos[chave] = campos[alias]

    if "cpf" in campos and "cnpj" not in campos:
        campos["cnpj"] = campos["cpf"]
        campos["tipo_documento"] = "CPF"
    elif "cnpj" in campos:
        campos["tipo_documento"] = "CNPJ"

    if not campos.get("razao_social"):
        campos["razao_social"] = campos.get("nome", "")

    return campos


def _checkbox(valor_campo, opcao):
    """Retorna '(X)' se o valor do campo corresponde à opção, senão '( )'."""
    return "(X)" if opcao.upper() in str(valor_campo).upper() else "( )"


def preencher_docx(campos):
    """
    Preenche o template DOCX com os dados do cliente e converte para PDF.
    Retorna (caminho_pdf, nome_arquivo).
    """
    from docxtpl import DocxTemplate

    docx_bytes = baixar_docx_modelo()

    hoje = date.today()
    plano = campos.get("plano", "").upper()
    venc  = campos.get("vencimento", "").strip()
    pag   = campos.get("pagamento", "").upper()

    contexto = {
        # Página 1 – QUADRO-RESUMO
        "nome":         campos.get("nome", ""),
        "email":        campos.get("email", ""),
        "telefone":     campos.get("telefone", ""),
        "municipio":    campos.get("municipio", ""),
        # Data de início
        "dia":          f"{hoje.day:02d}",
        "mes":          MESES[hoje.month - 1],
        # Plano (checkboxes)
        "plano_start":  _checkbox(plano, "START"),
        "plano_pro":    _checkbox(plano, "PRO"),
        "plano_growth": _checkbox(plano, "GROWTH"),
        "plano_scale":  _checkbox(plano, "SCALE"),
        # Vencimento (checkboxes)
        "venc_10":  _checkbox(venc, "10"),
        "venc_15":  _checkbox(venc, "15"),
        "venc_20":  _checkbox(venc, "20"),
        # Pagamento (checkboxes)
        "pag_boleto":  _checkbox(pag, "BOLETO"),
        "pag_pix":     _checkbox(pag, "PIX"),
        "pag_credito": _checkbox(pag, "CRÉD") or _checkbox(pag, "CRED"),
        "pag_debito":  _checkbox(pag, "DÉB") or _checkbox(pag, "DEB"),
        # Página 3 – dados CONTRATANTE
        "razao_social": campos.get("razao_social", campos.get("nome", "")),
        "cnpj":         campos.get("cnpj", campos.get("cpf", "")),
        "endereco":     campos.get("endereco", ""),
    }

    # Corrige pag_credito/pag_debito (resultado de or pode ser string vazia)
    for k in ("pag_credito", "pag_debito"):
        if not contexto[k]:
            contexto[k] = "( )"

    # Salva DOCX temporário
    tmp_dir = tempfile.gettempdir()
    doc_numero = re.sub(r"\D", "", campos.get("cnpj", campos.get("cpf", "00000000000000")))
    nome_limpo = re.sub(r"[^\w\s]", "", campos.get("nome", "cliente")).strip().replace(" ", "_")
    nome_base  = f"{doc_numero}_{nome_limpo}"
    docx_path  = os.path.join(tmp_dir, f"{nome_base}.docx")
    pdf_path   = os.path.join(tmp_dir, f"{nome_base}.pdf")

    tpl = DocxTemplate(io.BytesIO(docx_bytes))
    tpl.render(contexto)
    tpl.save(docx_path)
    print(f"[DOCX] Template preenchido: {docx_path}")

    # Converte para PDF via LibreOffice
    result = subprocess.run(
        ["libreoffice", "--headless", "--convert-to", "pdf", "--outdir", tmp_dir, docx_path],
        capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        raise Exception(f"LibreOffice falhou: {result.stderr}")
    print(f"[DOCX] Convertido para PDF: {pdf_path}")

    try:
        os.remove(docx_path)
    except Exception:
        pass

    return pdf_path, f"{nome_base}.pdf"


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
    """Adiciona signatário e envia notificação por email."""
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
            print(f"[CLICKSIGN] Notificacao enviada para: {email}")
        except Exception as e:
            print(f"[CLICKSIGN] Erro ao notificar {email}: {e}")

    print(f"[CLICKSIGN] Signatario adicionado: {email}")
    return signer_key


def clicksign_enviar(doc_key):
    """Finaliza o documento para assinatura."""
    payload = json.dumps({"document": {"key": doc_key}}).encode()
    req = urllib.request.Request(
        f"{CLICKSIGN_URL}/documents/{doc_key}/finish?access_token={CLICKSIGN_TOKEN}",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="PATCH"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            print("[CLICKSIGN] Documento finalizado e enviado para assinatura!")
    except Exception as e:
        print(f"[CLICKSIGN] Documento ja ativo ou erro: {e}")


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
            print("[TRELLO] Comentario adicionado no card")
    except Exception as e:
        print(f"[TRELLO] Erro ao comentar: {e}")


def processar_contrato_trello(card_nome, card_desc, card_id=None):
    """Processa um novo card do quadro Contratos."""
    print(f"[CONTRATO] Processando: {card_nome}")
    campos = parse_descricao(card_desc)

    if not campos.get("email"):
        print("[CONTRATO] Email nao encontrado na descricao — abortando")
        return False

    # 1. Preencher DOCX e converter para PDF
    caminho_pdf, nome_arquivo = preencher_docx(campos)
    print(f"[CONTRATO] PDF gerado: {nome_arquivo}")

    # 2. Upload no ClickSign
    doc_key = clicksign_upload(caminho_pdf, nome_arquivo)

    # 3. Adicionar signatários
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
            f"Ola {nome_curto}!\n\n"
            f"Seu contrato foi enviado para o email *{email}*.\n"
            f"Por favor, verifique sua caixa de entrada e assine o documento.\n\n"
            f"Qualquer duvida estamos a disposicao!"
        )
        zapi_enviar_whatsapp(telefone, mensagem)

    # 6. Comentar no card do Trello
    from datetime import datetime
    import pytz
    agora = datetime.now(pytz.timezone("America/Sao_Paulo")).strftime("%d/%m/%Y %H:%M")
    trello_comentar_card(card_id, f"Contrato enviado para assinatura\nEmail: {campos['email']}\nHora: {agora}")

    # 7. Limpar PDF temporário
    try:
        os.remove(caminho_pdf)
    except Exception:
        pass

    print(f"[CONTRATO] Concluido! Contrato enviado para {campos['email']}")
    return True

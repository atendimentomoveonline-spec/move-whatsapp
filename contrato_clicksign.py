import os, re, json, base64, io, urllib.request, urllib.parse
from pypdf import PdfReader, PdfWriter

CLICKSIGN_TOKEN   = os.environ.get("CLICKSIGN_TOKEN", "")
CLICKSIGN_URL     = "https://app.clicksign.com/api/v3"
GOOGLE_API_KEY    = os.environ.get("GOOGLE_API_KEY", "")
DRIVE_FOLDER_ID   = os.environ.get("CONTRATO_FOLDER_ID", "1fRj1KFrKM0CFsI3xOYb0fn84OwNprrET")
PASTA_CLICKSIGN   = os.environ.get("PASTA_CLICKSIGN", "/Contratos")

def baixar_pdf_modelo():
    url = (f"https://www.googleapis.com/drive/v3/files"
           f"?q=%27{DRIVE_FOLDER_ID}%27+in+parents+and+trashed%3Dfalse"
           f"&fields=files(id,name)&key={GOOGLE_API_KEY}")
    with urllib.request.urlopen(url, timeout=10) as r:
        arquivos = json.loads(r.read())["files"]
    pdf = next((f for f in arquivos if f["name"].lower().endswith(".pdf")), None)
    if not pdf:
        raise Exception("PDF modelo nao encontrado na pasta do Drive")
    url_dl = f"https://www.googleapis.com/drive/v3/files/{pdf['id']}?alt=media&key={GOOGLE_API_KEY}"
    with urllib.request.urlopen(url_dl, timeout=30) as r:
        dados = r.read()
    print(f"[DRIVE] PDF modelo baixado: {pdf['name']}")
    return dados

def parse_descricao(desc):
    campos = {}
    for linha in desc.splitlines():
        if ":" in linha:
            chave, _, valor = linha.partition(":")
            campos[chave.strip().lower()] = valor.strip()
    return campos

def preencher_pdf(campos):
    pdf_bytes = baixar_pdf_modelo()
    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    cnpj_limpo = re.sub(r"\D", "", campos.get("cnpj", "00000000000000"))
    nome_limpo = re.sub(r"[^\w\s]", "", campos.get("nome", "cliente")).strip().replace(" ", "_")
    nome_arquivo = f"{cnpj_limpo}_{nome_limpo}.pdf"
    buf = io.BytesIO()
    writer.write(buf)
    buf.seek(0)
    return buf.read(), nome_arquivo

def clicksign_upload(pdf_bytes, nome_arquivo):
    conteudo = base64.b64encode(pdf_bytes).decode()
    payload = json.dumps({
        "document": {
            "path": f"{PASTA_CLICKSIGN}/{nome_arquivo}",
            "content_base64": f"data:application/pdf;base64,{conteudo}"
        }
    }).encode()
    req = urllib.request.Request(
        f"{CLICKSIGN_URL}/documents?access_token={CLICKSIGN_TOKEN}",
        data=payload, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.loads(r.read())
    doc_key = resp["document"]["key"]
    print(f"[CLICKSIGN] Upload OK - key: {doc_key}")
    return doc_key

def clicksign_adicionar_signatario(doc_key, email, nome):
    payload = json.dumps({
        "signer": {"email": email, "name": nome, "phone_number": "",
                   "auth_type": "email", "delivery_method": "email", "has_documentation": False}
    }).encode()
    req = urllib.request.Request(
        f"{CLICKSIGN_URL}/signers?access_token={CLICKSIGN_TOKEN}",
        data=payload, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        resp = json.loads(r.read())
    signer_key = resp["signer"]["key"]
    payload2 = json.dumps({
        "list": {"document_key": doc_key, "signer_key": signer_key, "sign_as": "contractee",
                 "message": "Por favor, assine o contrato de prestacao de servicos contabeis da Move Online."}
    }).encode()
    req2 = urllib.request.Request(
        f"{CLICKSIGN_URL}/lists?access_token={CLICKSIGN_TOKEN}",
        data=payload2, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req2, timeout=15) as r2:
        json.loads(r2.read())
    print(f"[CLICKSIGN] Signatario adicionado: {email}")
    return signer_key

def clicksign_enviar(doc_key):
    payload = json.dumps({"document": {"key": doc_key}}).encode()
    req = urllib.request.Request(
        f"{CLICKSIGN_URL}/documents/{doc_key}/finish?access_token={CLICKSIGN_TOKEN}",
        data=payload, headers={"Content-Type": "application/json"}, method="PATCH")
    with urllib.request.urlopen(req, timeout=15) as r:
        r.read()
    print(f"[CLICKSIGN] Enviado para assinatura!")

def processar_contrato_trello(card_nome, card_desc):
    print(f"[CONTRATO] Processando: {card_nome}")
    campos = parse_descricao(card_desc)
    if not campos.get("email"):
        print(f"[CONTRATO] Email nao encontrado - abortando")
        return False
    pdf_bytes, nome_arquivo = preencher_pdf(campos)
    doc_key = clicksign_upload(pdf_bytes, nome_arquivo)
    clicksign_adicionar_signatario(doc_key, campos["email"], campos.get("nome", "Cliente"))
    clicksign_enviar(doc_key)
    print(f"[CONTRATO] Concluido! Enviado para {campos['email']}")
    return True

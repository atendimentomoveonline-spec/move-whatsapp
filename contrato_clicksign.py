"""
Automação de contratos via Trello + ClickSign
- Webhook do Trello detecta novo card no quadro "Contratos"
- Preenche PDF do contrato com dados do card
- Faz upload na pasta ClickSign com nome CNPJ_NomeCliente.pdf
- Adiciona signatário e envia para assinatura
"""
import os, re, io, json, base64, urllib.request, urllib.parse
from pypdf import PdfReader, PdfWriter

CLICKSIGN_TOKEN   = os.environ.get("CLICKSIGN_TOKEN", "")
CLICKSIGN_URL     = "https://app.clicksign.com/api/v3"
TRELLO_KEY        = os.environ.get("TRELLO_KEY", "")
TRELLO_TOKEN      = os.environ.get("TRELLO_TOKEN", "")
GOOGLE_API_KEY    = os.environ.get("GOOGLE_API_KEY", "")
DRIVE_FOLDER_ID   = os.environ.get("CONTRATO_FOLDER_ID", "1fRj1KFrKM0CFsI3xOYb0fn84OwNprrET")
PASTA_CLICKSIGN   = os.environ.get("PASTA_CLICKSIGN", "/Contratos")

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
    # Normaliza: se vier "cpf" usa como documento, coloca também em "cnpj" para compatibilidade
    if "cpf" in campos and "cnpj" not in campos:
        campos["cnpj"] = campos["cpf"]
        campos["documento"] = campos["cpf"]
        campos["tipo_documento"] = "CPF"
    elif "cnpj" in campos:
        campos["documento"] = campos["cnpj"]
        campos["tipo_documento"] = "CNPJ"
    return campos

def preencher_pdf(campos):
    """Preenche o PDF do contrato com os dados do cliente."""
    pdf_bytes = baixar_pdf_modelo()
    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter()

    substituicoes = {
        "XXXXXXXXXXXXXXXXXXXXXXXXXXXX": campos.get("nome", ""),
        "XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX": campos.get("cnpj", campos.get("nome", "")),
    }

    # Copiar todas as páginas
    for page in reader.pages:
        writer.add_page(page)

    # Preencher campos de formulário se houver
    writer.update_page_form_field_values(writer.pages[0], campos)

    # Salvar PDF preenchido temporariamente (usa CNPJ ou CPF no nome do arquivo)
    doc_numero = re.sub(r"\D", "", campos.get("cnpj", campos.get("cpf", "00000000000000")))
    nome_limpo = re.sub(r"[^\w\s]", "", campos.get("nome", "cliente")).strip().replace(" ", "_")
    nome_arquivo = f"{doc_numero}_{nome_limpo}.pdf"
    caminho = os.path.join("/tmp", nome_arquivo)

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
            "auth_type": "email",
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
            "sign_as": "contractee",
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
        json.loads(r2.read())

    print(f"[CLICKSIGN] Signatário adicionado: {email}")
    return signer_key

def clicksign_enviar(doc_key):
    """Envia o documento para assinatura."""
    payload = json.dumps({"document": {"key": doc_key}}).encode()
    req = urllib.request.Request(
        f"{CLICKSIGN_URL}/documents/{doc_key}/finish?access_token={CLICKSIGN_TOKEN}",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="PATCH"
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        print(f"[CLICKSIGN] Enviado para assinatura!")

def processar_contrato_trello(card_nome, card_desc):
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

    # 3. Adicionar signatário
    clicksign_adicionar_signatario(doc_key, campos["email"], campos.get("nome", "Cliente"))

    # 4. Enviar para assinatura
    clicksign_enviar(doc_key)

    # 5. Limpar PDF temporário
    try:
        os.remove(caminho_pdf)
    except:
        pass

    print(f"[CONTRATO] Concluído! Contrato enviado para {campos['email']}")
    return True

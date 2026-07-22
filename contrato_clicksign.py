"""
Automação de contratos via Trello + ClickSign
- Webhook do Trello detecta card no quadro "Contratos"
- Preenche template DOCX com dados do card via docxtpl
- Converte para PDF com LibreOffice
- Faz upload no ClickSign, adiciona signatários e envia para assinatura
"""
import os, re, io, json, base64, tempfile, urllib.request, urllib.parse, urllib.error
from datetime import date

CLICKSIGN_TOKEN = os.environ.get("CLICKSIGN_TOKEN", "")
CLICKSIGN_URL   = "https://app.clicksign.com/api/v1"
TRELLO_KEY      = os.environ.get("TRELLO_KEY", "")
TRELLO_TOKEN    = os.environ.get("TRELLO_TOKEN", "")
GOOGLE_API_KEY  = os.environ.get("GOOGLE_API_KEY", "")
DRIVE_FOLDER_ID = os.environ.get("CONTRATO_FOLDER_ID", "1fRj1KFrKM0CFsl3xOYb0fn84OwNprrET")
PASTA_CLICKSIGN = os.environ.get("PASTA_CLICKSIGN", "/Contratos")
ZAPI_INSTANCE   = os.environ.get("ZAPI_INSTANCE", "")
ZAPI_TOKEN      = os.environ.get("ZAPI_TOKEN", "")

MESES = ["JANEIRO","FEVEREIRO","MARÇO","ABRIL","MAIO","JUNHO",
         "JULHO","AGOSTO","SETEMBRO","OUTUBRO","NOVEMBRO","DEZEMBRO"]

# Testemunhas adicionadas automaticamente em todo contrato gerado
TESTEMUNHAS_PERMANENTES = [
    {
        "nome": "Renata dos Reis Araujo das Neves",
        "cpf": "45601108838",
        "email": "renata.reis172@gmail.com",
    },
    {
        "nome": "Anair Araujo Pires",
        "cpf": "03361954517",
        "email": "anair@moveonline.com.br",
    },
]


def baixar_docx_modelo():
    """Carrega o template DOCX do repositório local."""
    base = os.path.dirname(os.path.abspath(__file__))
    caminho = os.path.join(base, "contrato_template.docx")
    if not os.path.exists(caminho):
        raise Exception(f"Template DOCX não encontrado em: {caminho}")
    with open(caminho, "rb") as f:
        conteudo = f.read()
    print(f"[TEMPLATE] Carregado: {len(conteudo)//1024} KB")
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
            valor = re.sub(r'\*{1,3}|_{1,3}', '', valor)             # remove negrito/italico markdown
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

    # EMAIL: o Trello escapa underscores como \_ (ex: cris\_22\_lopes@x.com). O strip de
    # markdown/underscore la em cima apaga os underscores e quebra o email -> ClickSign
    # recusa ("E-mail nao pode ficar em branco") e o contrato trava sem signatario.
    # Solucao: parseia a linha CRUA do email, desescapando \_ -> _ (nao apagando).
    for linha in desc.splitlines():
        if '@' in linha and re.match(r'\s*e-?mail\s*:', linha, re.IGNORECASE):
            mt = re.search(r'mailto:\s*([\w.+-]+@[\w.-]+\.\w+)', linha, re.IGNORECASE)
            if mt:
                campos["email"] = mt.group(1).lower()
            else:
                bruto = re.sub(r'\\(.)', r'\1', linha.split(':', 1)[1])  # desescapa \_ -> _
                val = re.search(r'[\w.+-]+@[\w.-]+\.\w+', bruto)
                if val:
                    campos["email"] = val.group(0).lower()
            break

    return campos


def _checkbox(valor_campo, opcao):
    """Retorna '(X)' se o valor do campo corresponde à opção, senão '( )'."""
    return "(X)" if opcao.upper() in str(valor_campo).upper() else "( )"


def docx_para_pdf_via_cloudconvert(docx_bytes, nome_arquivo):
    """Converte DOCX para PDF usando CloudConvert API (token via env CLOUDCONVERT_TOKEN)."""
    import json, base64
    token = os.environ.get("CLOUDCONVERT_TOKEN", "")
    if not token:
        raise Exception("CLOUDCONVERT_TOKEN não configurado")

    # 1. Criar job
    job_payload = json.dumps({
        "tasks": {
            "upload": {"operation": "import/upload"},
            "convert": {"operation": "convert", "input": "upload",
                        "input_format": "docx", "output_format": "pdf"},
            "export": {"operation": "export/url", "input": "convert"}
        }
    }).encode()
    req = urllib.request.Request("https://api.cloudconvert.com/v2/jobs",
        data=job_payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        job = json.loads(r.read())

    upload_task = next(t for t in job["data"]["tasks"] if t["name"] == "upload")
    upload_url = upload_task["result"]["form"]["url"]
    upload_params = upload_task["result"]["form"]["parameters"]

    # 2. Upload multipart
    boundary = "b0undary_move_99"
    body = b""
    for k, v in upload_params.items():
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{nome_arquivo}\"\r\nContent-Type: application/vnd.openxmlformats-officedocument.wordprocessingml.document\r\n\r\n".encode()
    body += docx_bytes + f"\r\n--{boundary}--".encode()

    req2 = urllib.request.Request(upload_url, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, method="POST")
    with urllib.request.urlopen(req2, timeout=60) as r:
        r.read()

    # 3. Aguarda e baixa PDF
    job_id = job["data"]["id"]
    import time
    for _ in range(30):
        time.sleep(3)
        req3 = urllib.request.Request(f"https://api.cloudconvert.com/v2/jobs/{job_id}",
            headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req3, timeout=15) as r:
            status = json.loads(r.read())
        if status["data"]["status"] == "finished":
            export_task = next(t for t in status["data"]["tasks"] if t["name"] == "export")
            pdf_url = export_task["result"]["files"][0]["url"]
            with urllib.request.urlopen(pdf_url, timeout=60) as r:
                return r.read()
        if status["data"]["status"] == "error":
            raise Exception(f"CloudConvert erro: {status}")
    raise Exception("CloudConvert timeout")


def docx_para_pdf_via_drive(docx_bytes, nome_arquivo):
    """
    Faz upload do DOCX no Google Drive com OAuth (service não disponível),
    usando a API Drive v3 com a chave de API do usuário autenticado via
    multipart upload + exportação como PDF.
    Alternativa sem LibreOffice: usa a API Drive para converter.
    """
    import urllib.parse, json, base64

    # Upload do DOCX como Google Doc (converte automaticamente)
    boundary = "boundary_move_online_12345"
    metadata = json.dumps({
        "name": nome_arquivo.replace(".pdf", ""),
        "mimeType": "application/vnd.google-apps.document"
    }).encode()

    body = (
        f"--{boundary}\r\n"
        f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
    ).encode() + metadata + (
        f"\r\n--{boundary}\r\n"
        f"Content-Type: application/vnd.openxmlformats-officedocument.wordprocessingml.document\r\n\r\n"
    ).encode() + docx_bytes + f"\r\n--{boundary}--".encode()

    url = f"https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&key={GOOGLE_API_KEY}"
    req = urllib.request.Request(url, data=body,
        headers={"Content-Type": f"multipart/related; boundary={boundary}"},
        method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        file_info = json.loads(r.read())

    file_id = file_info["id"]
    print(f"[DRIVE] DOCX convertido para Google Doc: {file_id}")

    # Exporta como PDF
    url_export = f"https://www.googleapis.com/drive/v3/files/{file_id}/export?mimeType=application/pdf&key={GOOGLE_API_KEY}"
    with urllib.request.urlopen(url_export, timeout=60) as r:
        pdf_bytes = r.read()

    # Remove o arquivo temporário do Drive
    try:
        req_del = urllib.request.Request(
            f"https://www.googleapis.com/drive/v3/files/{file_id}?key={GOOGLE_API_KEY}",
            method="DELETE")
        urllib.request.urlopen(req_del, timeout=10)
    except Exception:
        pass

    print(f"[DRIVE] PDF exportado: {len(pdf_bytes)//1024} KB")
    return pdf_bytes


def preencher_docx(campos):
    """
    Preenche o template DOCX com os dados do cliente e converte para PDF via Google Drive.
    Retorna (caminho_pdf, nome_arquivo).
    """
    from docxtpl import DocxTemplate

    docx_bytes = baixar_docx_modelo()

    hoje = date.today()
    plano = campos.get("plano", "").upper()
    venc  = campos.get("vencimento", "").strip()
    pag   = campos.get("pagamento", "").upper()

    def up(v): return v.upper() if v else ""

    contexto = {
        # Página 1 – QUADRO-RESUMO
        "nome":         up(campos.get("nome", "")),
        "email":        campos.get("email", "").lower(),
        "telefone":     campos.get("telefone", ""),
        "municipio":    up(campos.get("municipio", "")),
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
        # Inline no corpo do contrato
        "plano":        up(campos.get("plano", "")),
        # Página 3 – dados CONTRATANTE
        "razao_social": up(campos.get("razao_social", campos.get("nome", ""))),
        "cnpj":         campos.get("cnpj", campos.get("cpf", "")),
        "endereco":     up(campos.get("endereco", "")),
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

    print(f"[CONTEXTO] nome={contexto['nome']!r} razao_social={contexto['razao_social']!r} cnpj={contexto['cnpj']!r}")
    tpl = DocxTemplate(io.BytesIO(docx_bytes))
    tpl.render(contexto)
    docx_buf = io.BytesIO()
    tpl.save(docx_buf)
    docx_preenchido = docx_buf.getvalue()
    print(f"[DOCX] Template preenchido ({len(docx_preenchido)//1024} KB)")

    # Salva DOCX preenchido — ClickSign converte para PDF internamente
    with open(docx_path, "wb") as f:
        f.write(docx_preenchido)

    return docx_path, f"{nome_base}.docx"


def _clicksign_request(req, timeout):
    """Executa a requisição e, em caso de erro HTTP, propaga o corpo da resposta
    (mensagem de erro real do ClickSign) em vez de um HTTPError genérico."""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        corpo = e.read().decode(errors="replace")
        raise Exception(f"ClickSign respondeu {e.code} em {req.full_url.split('?')[0]}: {corpo}") from e


def clicksign_upload(caminho_pdf, nome_arquivo):
    """Faz upload do PDF para a pasta no ClickSign."""
    with open(caminho_pdf, "rb") as f:
        conteudo = base64.b64encode(f.read()).decode()

    payload = json.dumps({
        "document": {
            "path": f"{PASTA_CLICKSIGN}/{nome_arquivo}",
            "content_base64": f"data:application/vnd.openxmlformats-officedocument.wordprocessingml.document;base64,{conteudo}"
        }
    }).encode()

    req = urllib.request.Request(
        f"{CLICKSIGN_URL}/documents?access_token={CLICKSIGN_TOKEN}",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    resp = _clicksign_request(req, timeout=30)

    doc_key = resp["document"]["key"]
    print(f"[CLICKSIGN] Upload OK — key: {doc_key}")
    return doc_key


def clicksign_adicionar_signatario(doc_key, email, nome, papel="sign", cpf=""):
    """
    Adiciona signatário (ou testemunha) e envia notificação por email.
    papel: "sign" (assinante padrão) ou "witness" (testemunha).
    cpf: opcional, usado como documentation quando fornecido.
    """
    payload = json.dumps({
        "signer": {
            "email": email,
            "name": nome,
            "documentation": cpf or None,
            "phone_number": "",
            "auths": ["email"],
            "delivery_method": "email",
            "has_documentation": bool(cpf)
        }
    }).encode()

    req = urllib.request.Request(
        f"{CLICKSIGN_URL}/signers?access_token={CLICKSIGN_TOKEN}",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    resp = _clicksign_request(req, timeout=15)
    signer_key = resp["signer"]["key"]

    mensagem = (
        "Por favor, assine como testemunha o contrato de prestação de serviços contábeis da Move Online."
        if papel == "witness" else
        "Por favor, assine o contrato de prestação de serviços contábeis da Move Online."
    )
    payload2 = json.dumps({
        "list": {
            "document_key": doc_key,
            "signer_key": signer_key,
            "sign_as": papel,
            "message": mensagem
        }
    }).encode()
    req2 = urllib.request.Request(
        f"{CLICKSIGN_URL}/lists?access_token={CLICKSIGN_TOKEN}",
        data=payload2,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    resp2 = _clicksign_request(req2, timeout=15)

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
    # Z-API exige DDI: adiciona 55 se não tiver
    if not telefone_limpo.startswith("55"):
        telefone_limpo = "55" + telefone_limpo
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
        trello_comentar_card(card_id, "Contrato NAO enviado — nao encontrei o campo \"Email:\" na descricao do card.")
        return False

    doc_key = None
    etapa = "gerar PDF do contrato"
    try:
        # 1. Preencher DOCX e converter para PDF
        caminho_pdf, nome_arquivo = preencher_docx(campos)
        print(f"[CONTRATO] PDF gerado: {nome_arquivo}")

        # 2. Upload no ClickSign
        etapa = "upload no ClickSign"
        doc_key = clicksign_upload(caminho_pdf, nome_arquivo)

        # 3. Adicionar signatários
        MOVE_EMAIL = "wanderson@moveonline.com.br"
        etapa = f"adicionar signatario cliente ({campos['email']})"
        clicksign_adicionar_signatario(doc_key, campos["email"], campos.get("nome", "Cliente"))
        if campos["email"].lower() != MOVE_EMAIL.lower():
            etapa = "adicionar signatario Wanderson"
            clicksign_adicionar_signatario(doc_key, MOVE_EMAIL, "Wanderson - Move Online Contabilidade")

        # Testemunhas permanentes em todo contrato
        for testemunha in TESTEMUNHAS_PERMANENTES:
            etapa = f"adicionar testemunha ({testemunha['email']})"
            clicksign_adicionar_signatario(
                doc_key, testemunha["email"], testemunha["nome"],
                papel="witness", cpf=testemunha["cpf"]
            )

        # 4. Enviar para assinatura
        etapa = "finalizar documento no ClickSign"
        clicksign_enviar(doc_key)
    except Exception as e:
        print(f"[CONTRATO ERRO] Falhou na etapa '{etapa}': {e}", flush=True)
        detalhe = f"Contrato NAO enviado corretamente.\nFalhou na etapa: {etapa}\nErro: {e}"
        if doc_key:
            detalhe += f"\nDocumento ja foi criado no ClickSign (key: {doc_key}) mas ficou incompleto — verificar/reenviar manualmente."
        trello_comentar_card(card_id, detalhe)
        return False

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

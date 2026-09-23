# -*- coding: utf-8 -*-
"""
Pagina de assinatura de contrato — Move Online Contabilidade Medica.
O cliente preenche os dados, escolhe o plano, assina na tela e gera o
contrato final: o modelo ja assinado por Wanderson + Anair + Renata, com os
dados preenchidos no corpo + a assinatura do cliente + pagina de comprovacao.
Sem dependencia de terceiros.
"""
import os, re, io, json, base64, hashlib, datetime, unicodedata, subprocess, tempfile
from flask import Flask, request, send_file, abort

BASE = os.path.dirname(os.path.abspath(__file__))
MODELO = os.path.join(BASE, "contrato_modelo_assinado.docx")  # ja assinado + placeholders [ ]
BASE_PDF = os.path.join(BASE, "base_contrato.pdf")            # modelo em PDF (assinado + placeholders)
LEITURA_PDF = os.path.join(BASE, "static_leitura", "contrato_leitura.pdf")  # p/ leitura na tela (com assinaturas)
OUT_DIR = os.path.join(BASE, "assinados")
os.makedirs(OUT_DIR, exist_ok=True)

# ─────────────────────── PERSISTENCIA DURAVEL ───────────────────────
# O disco do Render e efemero: some a cada deploy/restart. Por isso a
# LISTA vai pro Supabase (tabela) e o PDF de cada contrato vai pro
# Supabase Storage (bucket privado). Assim o painel de controle e os
# PDFs sobrevivem a qualquer restart. O salvamento local fica so como
# cache imediato.
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
SUPA_TABLE   = os.environ.get("SUPA_TABLE", "contratos_assinados")
SUPA_BUCKET  = os.environ.get("SUPA_BUCKET", "contratos")

def _supa_headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json"}

def supa_inserir(reg):
    """Grava a linha do contrato no Supabase. Nao levanta erro (nao pode
    quebrar a assinatura). Retorna (ok, detalhe)."""
    if not (SUPABASE_URL and SUPABASE_KEY):
        return False, "supabase nao configurado"
    try:
        import requests
        r = requests.post(f"{SUPABASE_URL}/rest/v1/{SUPA_TABLE}",
                          headers={**_supa_headers(), "Prefer": "return=minimal"},
                          json=reg, timeout=15)
        return (r.status_code in (200, 201, 204)), f"{r.status_code} {r.text[:200]}"
    except Exception as e:
        return False, str(e)

def supa_listar():
    """Le a lista de contratos do Supabase (mais novos primeiro).
    Retorna lista de dicts, ou None se indisponivel."""
    if not (SUPABASE_URL and SUPABASE_KEY):
        return None
    try:
        import requests
        r = requests.get(f"{SUPABASE_URL}/rest/v1/{SUPA_TABLE}"
                         "?select=*&order=created_at.desc",
                         headers=_supa_headers(), timeout=15)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print("[painel] supabase indisponivel:", e)
    return None

def supa_upload_pdf(fname, pdf_bytes):
    """Sobe o PDF assinado pro bucket privado no Supabase Storage.
    Nao levanta erro. Retorna (ok, detalhe)."""
    if not (SUPABASE_URL and SUPABASE_KEY):
        return False, "supabase nao configurado"
    try:
        import requests
        url = f"{SUPABASE_URL}/storage/v1/object/{SUPA_BUCKET}/{fname}"
        r = requests.post(url, data=pdf_bytes, timeout=30, headers={
            "apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/pdf", "x-upsert": "true"})
        return (r.status_code in (200, 201)), f"{r.status_code} {r.text[:200]}"
    except Exception as e:
        return False, str(e)

def supa_baixar_pdf(fname):
    """Baixa o PDF do bucket privado. Retorna bytes, ou None se nao achar."""
    if not (SUPABASE_URL and SUPABASE_KEY):
        return None
    try:
        import requests
        url = f"{SUPABASE_URL}/storage/v1/object/{SUPA_BUCKET}/{fname}"
        r = requests.get(url, timeout=30, headers={
            "apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"})
        if r.status_code == 200:
            return r.content
    except Exception as e:
        print("[baixar] supabase storage indisponivel:", e)
    return None

_PAG_CACHE = {}
def leitura_num_paginas():
    import fitz
    with fitz.open(LEITURA_PDF) as d:
        return d.page_count

def render_pagina(n):
    if n in _PAG_CACHE:
        return _PAG_CACHE[n]
    import fitz
    with fitz.open(LEITURA_PDF) as d:
        png = d.load_page(n - 1).get_pixmap(dpi=110).tobytes("png")
    _PAG_CACHE[n] = png
    return png

app = Flask(__name__)

PLANOS = {
    "START":  "MOVE START (R$ 89,00/mês)",
    "BASICO": "MOVE BÁSICO (R$ 129,00/mês - após 12 meses)",
    "PRO":    "MOVE PRÓ (R$ 169,00/mês)",
    "GROWTH": "MOVE GROWTH (R$ 229,00/mês)",
    "SCALE":  "MOVE SCALE (R$ 497,00/mês)",
}

# ─────────────────────────── PAGINA ───────────────────────────
PAGINA = r"""<!doctype html>
<html lang="pt-BR"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>Assinatura de Contrato — Move Online</title>
<style>
  :root{--brand:#0E9C86;--brand2:#0B7F6E;--ink:#0f172a;--mut:#64748b;--line:#e2e8f0;--bg:#f1f5f9;--ok:#16a34a}
  *{box-sizing:border-box}
  body{margin:0;font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;background:var(--bg);color:var(--ink);-webkit-font-smoothing:antialiased}
  .wrap{max-width:520px;margin:0 auto;padding:0 16px 48px}
  .top{background:linear-gradient(135deg,var(--brand),var(--brand2));color:#fff;margin:0 -16px;padding:32px 24px 44px;border-radius:0 0 26px 26px;box-shadow:0 10px 30px rgba(11,127,110,.25)}
  .brand{display:flex;align-items:center;gap:10px;font-weight:700;font-size:17px}
  .brand .dot{width:26px;height:26px;border-radius:8px;background:#fff;display:grid;place-items:center;color:var(--brand);font-weight:800;font-size:15px}
  .top h1{margin:16px 0 6px;font-size:22px;line-height:1.25}
  .top p{margin:0;opacity:.92;font-size:14px}
  .card{background:#fff;border:1px solid var(--line);border-radius:18px;padding:20px;margin-top:-24px;box-shadow:0 8px 24px rgba(15,23,42,.06)}
  .sel{display:flex;align-items:center;gap:12px;background:#f0fdfa;border:1px solid #ccfbf1;border-radius:14px;padding:12px 14px;margin-bottom:14px}
  .sel b{color:var(--brand2)}
  .readbtn{display:flex;align-items:center;justify-content:center;gap:8px;text-decoration:none;width:100%;margin-bottom:14px;padding:12px;border:1.5px solid var(--brand);border-radius:12px;color:var(--brand2);font-weight:700;font-size:14px;background:#fff}
  .readbtn:active{background:#f0fdfa}
  .sec{font-size:12px;font-weight:800;letter-spacing:.5px;color:var(--brand2);text-transform:uppercase;margin:20px 0 4px}
  label{display:block;font-size:13px;font-weight:600;color:var(--mut);margin:12px 0 6px}
  input[type=text],input[type=email],select{width:100%;padding:14px;border:1.5px solid var(--line);border-radius:12px;font-size:16px;outline:none;background:#fff;transition:border .15s}
  input:focus,select:focus{border-color:var(--brand)}
  .row{display:flex;gap:10px}.row>div{flex:1}
  .pad-wrap{margin-top:6px;border:1.5px dashed #cbd5e1;border-radius:12px;background:#fafcff;position:relative}
  canvas{display:block;width:100%;height:170px;border-radius:12px;touch-action:none}
  .pad-hint{position:absolute;left:0;right:0;top:50%;transform:translateY(-50%);text-align:center;color:#94a3b8;font-size:14px;pointer-events:none}
  .pad-bar{display:flex;justify-content:space-between;align-items:center;margin-top:8px}
  .clr{background:none;border:none;color:var(--brand2);font-weight:600;font-size:13px;cursor:pointer}
  .csum{background:#f0fdfa;border:1px solid #ccfbf1;border-radius:12px;padding:8px 14px 12px;font-size:13px;color:var(--ink);margin-bottom:8px}
  .qr-t{font-size:11px;font-weight:800;letter-spacing:.5px;color:var(--brand2);text-transform:uppercase;padding:6px 0;border-bottom:1px solid #ccfbf1;margin-bottom:4px}
  .qr{display:flex;justify-content:space-between;gap:12px;padding:4px 0;border-bottom:1px dashed #d7f5ee}
  .qr span{color:var(--mut)}.qr b{text-align:right;color:var(--ink)}
  .cbox{height:300px;overflow-y:auto;border:1.5px solid var(--line);border-radius:12px;padding:14px;background:#fff;font-size:13px;line-height:1.55;color:#334155;-webkit-overflow-scrolling:touch}
  .cbox h4{margin:14px 0 4px;font-size:13px;color:var(--brand2);font-weight:800}
  .cbox p{margin:0 0 8px}
  .cpage{width:100%;display:block;margin:0 auto 8px;border:1px solid #eef2f7;border-radius:4px}
  .cread{margin-top:8px;font-size:13px;font-weight:700;color:#b45309;text-align:center;background:#fffbeb;border:1px solid #fde68a;border-radius:10px;padding:9px}
  .cread.done{color:var(--ok);background:#f0fdf4;border-color:#bbf7d0}
  .accept{display:flex;gap:10px;align-items:flex-start;margin:18px 0 6px;font-size:13px;color:var(--mut)}
  .accept input{margin-top:2px;width:18px;height:18px;accent-color:var(--brand)}
  .btn{width:100%;margin-top:18px;padding:16px;border:none;border-radius:14px;background:var(--brand);color:#fff;font-size:16px;font-weight:700;cursor:pointer;transition:.15s;box-shadow:0 8px 18px rgba(14,156,134,.35)}
  .btn:disabled{background:#cbd5e1;box-shadow:none;cursor:not-allowed}
  .btn:not(:disabled):active{transform:translateY(1px)}
  .foot{text-align:center;color:var(--mut);font-size:12px;margin-top:18px;line-height:1.6}
  .err{background:#fef2f2;color:#b91c1c;border:1px solid #fecaca;border-radius:10px;padding:10px 12px;font-size:13px;margin-top:14px;display:none}
  .ok-screen{display:none;text-align:center;padding:16px 8px}
  .ok-badge{width:76px;height:76px;border-radius:50%;background:#dcfce7;color:var(--ok);display:grid;place-items:center;margin:6px auto 14px;font-size:40px;animation:pop .4s ease}
  @keyframes pop{0%{transform:scale(.6);opacity:0}100%{transform:scale(1);opacity:1}}
  .ok-screen h2{margin:0 0 6px;font-size:20px}.ok-screen p{color:var(--mut);font-size:14px;margin:0 0 18px}
  .dl{display:inline-block;text-decoration:none;background:var(--brand);color:#fff;padding:14px 22px;border-radius:12px;font-weight:700}
  .spin{display:inline-block;width:18px;height:18px;border:3px solid rgba(255,255,255,.5);border-top-color:#fff;border-radius:50%;animation:sp .7s linear infinite;vertical-align:-3px}
  @keyframes sp{to{transform:rotate(360deg)}}
</style></head>
<body><div class="wrap">
  <div class="top">
    <div class="brand"><span class="dot">M</span> Move Online <span style="opacity:.7;font-weight:500">Contabilidade Médica</span></div>
    <h1>Assinatura do seu contrato</h1>
    <p>Preencha seus dados, escolha o plano e assine. Rápido e seguro.</p>
  </div>

  <form class="card" id="form">
    <div class="sel">📄 <div><b>Contrato de Prestação de Serviços</b></div></div>
    <a class="readbtn" href="/contrato" target="_blank" rel="noopener">📖 Ler contrato completo</a>

    <div class="sec">Seus dados</div>
    <label>Nome completo</label>
    <input type="text" id="nome" placeholder="Seu nome completo" autocomplete="name">
    <label>CPF</label>
    <input type="text" id="cpf" placeholder="000.000.000-00" inputmode="numeric">
    <label>E-mail</label>
    <input type="email" id="email" placeholder="seu@email.com" autocomplete="email">
    <label>Telefone</label>
    <input type="text" id="telefone" placeholder="(00) 00000-0000" inputmode="tel">
    <label>Endereço completo</label>
    <input type="text" id="endereco" placeholder="Rua, número, bairro">
    <label>Município (emissão de NF)</label>
    <input type="text" id="municipio" placeholder="Cidade">

    <div class="sec">Plano e pagamento</div>
    <label>Plano contratado</label>
    <select id="plano">
      <option value="">Selecione o plano...</option>
      <option value="START">MOVE START — R$ 89,00/mês</option>
      <option value="BASICO">MOVE BÁSICO — R$ 129,00/mês (após 12 meses)</option>
      <option value="PRO">MOVE PRÓ — R$ 169,00/mês</option>
      <option value="GROWTH">MOVE GROWTH — R$ 229,00/mês</option>
      <option value="SCALE">MOVE SCALE — R$ 497,00/mês</option>
    </select>
    <div class="row">
      <div><label>Vencimento</label>
        <select id="vencimento"><option value="10">Dia 10</option><option value="15">Dia 15</option><option value="20">Dia 20</option></select></div>
      <div><label>Pagamento</label>
        <select id="pagamento"><option value="BOLETO">Boleto</option><option value="PIX">Pix</option><option value="CRÉDITO">Crédito</option><option value="DÉBITO">Débito</option></select></div>
    </div>

    <div class="sec">Leia o contrato</div>
    <div class="csum" id="csum">Preencha seus dados acima para vê-los no resumo.</div>
    <div class="cbox" id="cbox">__CONTRATO_HTML__</div>
    <div class="cread" id="cread">⬇ Role o contrato até o final para poder assinar</div>

    <div class="sec">Assinatura</div>
    <div class="pad-wrap"><canvas id="pad"></canvas><div class="pad-hint" id="hint">assine aqui com o dedo ou mouse</div></div>
    <div class="pad-bar"><span style="font-size:12px;color:#94a3b8">Desenhe sua assinatura acima</span><button type="button" class="clr" id="clr">Limpar</button></div>

    <label class="accept"><input type="checkbox" id="ok"><span>Li e concordo com os termos do contrato e assino eletronicamente este documento.</span></label>
    <div class="err" id="err"></div>
    <button class="btn" id="send" type="submit" disabled>Assinar contrato</button>
    <div class="foot">🔒 Assinatura eletrônica segura<br>Move Online Contabilidade Médica LTDA · CNPJ 27.124.625/0001-11</div>
  </form>

  <div class="card ok-screen" id="okscreen">
    <div class="ok-badge">✓</div>
    <h2>Contrato assinado!</h2>
    <p>Prontinho. Seu contrato foi preenchido, assinado e uma cópia está disponível abaixo.</p>
    <a class="dl" id="dl" href="#">Baixar meu contrato</a>
    <div class="foot" style="margin-top:20px">Uma cópia também foi encaminhada à Move Online.</div>
  </div>
</div>
<script>
  const pad=document.getElementById('pad'),ctx=pad.getContext('2d'),hint=document.getElementById('hint');
  let drawing=false,has=false;
  function fit(){const r=pad.getBoundingClientRect(),d=window.devicePixelRatio||1;pad.width=r.width*d;pad.height=r.height*d;ctx.scale(d,d);ctx.lineWidth=2.4;ctx.lineCap='round';ctx.lineJoin='round';ctx.strokeStyle='#0f172a';}
  fit();
  function pos(e){const r=pad.getBoundingClientRect(),t=e.touches?e.touches[0]:e;return{x:t.clientX-r.left,y:t.clientY-r.top};}
  function start(e){drawing=true;has=true;hint.style.display='none';const p=pos(e);ctx.beginPath();ctx.moveTo(p.x,p.y);e.preventDefault();check();}
  function move(e){if(!drawing)return;const p=pos(e);ctx.lineTo(p.x,p.y);ctx.stroke();e.preventDefault();}
  function end(){drawing=false;}
  pad.addEventListener('mousedown',start);pad.addEventListener('mousemove',move);window.addEventListener('mouseup',end);
  pad.addEventListener('touchstart',start,{passive:false});pad.addEventListener('touchmove',move,{passive:false});pad.addEventListener('touchend',end);
  document.getElementById('clr').onclick=()=>{ctx.clearRect(0,0,pad.width,pad.height);has=false;hint.style.display='block';check();};

  const F=id=>document.getElementById(id);
  const nome=F('nome'),cpf=F('cpf'),email=F('email'),tel=F('telefone'),ende=F('endereco'),
        muni=F('municipio'),plano=F('plano'),ok=F('ok'),send=F('send'),err=F('err');
  cpf.addEventListener('input',()=>{let v=cpf.value.replace(/\D/g,'').slice(0,11);v=v.replace(/(\d{3})(\d)/,'$1.$2').replace(/(\d{3})(\d)/,'$1.$2').replace(/(\d{3})(\d{1,2})$/,'$1-$2');cpf.value=v;check();});
  tel.addEventListener('input',()=>{let v=tel.value.replace(/\D/g,'').slice(0,11);if(v.length>6)v=v.replace(/(\d{2})(\d{4,5})(\d{0,4}).*/,'($1) $2-$3');else if(v.length>2)v=v.replace(/(\d{2})(\d+)/,'($1) $2');tel.value=v;});
  [nome,email,ende,muni,plano,ok].forEach(el=>el.addEventListener('input',()=>{upsum();check();}));
  cpf.addEventListener('input',upsum);F('vencimento').addEventListener('input',upsum);F('pagamento').addEventListener('input',upsum);
  function q(rot,val){return `<div class="qr"><span>${rot}</span><b>${val||'—'}</b></div>`;}
  function upsum(){const pt=plano.value&&plano.options[plano.selectedIndex]?plano.options[plano.selectedIndex].text:'';
    F('csum').innerHTML='<div class="qr-t">Quadro-resumo da contratação</div>'+
      q('Contratante',nome.value)+q('CPF',cpf.value)+q('E-mail',email.value)+
      q('Telefone',tel.value)+q('Município',muni.value)+q('Plano',pt)+
      q('Vencimento','Dia '+F('vencimento').value)+q('Pagamento',F('pagamento').value);}
  upsum();

  // leitura obrigatoria: rolar o contrato ate o fim
  const cbox=F('cbox'),cread=F('cread');let lido=false;
  cbox.addEventListener('scroll',()=>{
    if(!lido && cbox.scrollTop+cbox.clientHeight>=cbox.scrollHeight-12){
      lido=true;cread.textContent='✓ Você leu o contrato até o final';cread.classList.add('done');check();}});

  function check(){send.disabled=!(nome.value.trim().split(' ').length>=2 && cpf.value.replace(/\D/g,'').length===11 && email.value.includes('@') && plano.value && lido && has && ok.checked);}

  F('form').addEventListener('submit',async(e)=>{
    e.preventDefault();err.style.display='none';send.disabled=true;send.innerHTML='<span class="spin"></span> Gerando e assinando...';
    try{
      const r=await fetch('/assinar',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
        nome:nome.value.trim(),cpf:cpf.value.replace(/\D/g,''),email:email.value.trim(),telefone:tel.value.trim(),
        endereco:ende.value.trim(),municipio:muni.value.trim(),plano:plano.value,
        vencimento:F('vencimento').value,pagamento:F('pagamento').value,assinatura:pad.toDataURL('image/png')})});
      const j=await r.json();if(!j.ok)throw new Error(j.erro||'Erro ao assinar');
      F('form').style.display='none';F('dl').href=j.url;document.getElementById('okscreen').style.display='block';window.scrollTo(0,0);
    }catch(ex){err.textContent=ex.message;err.style.display='block';send.disabled=false;send.textContent='Assinar contrato';}
  });
</script>
</body></html>"""

_CONTRATO_CACHE = None
def contrato_html():
    """Gera o HTML do contrato (clausulas) a partir do modelo, para leitura na tela."""
    global _CONTRATO_CACHE
    if _CONTRATO_CACHE:
        return _CONTRATO_CACHE
    from docx import Document
    import html as _html
    d = Document(MODELO)
    out = []
    for p in d.paragraphs:
        t = p.text.strip()
        if not t:
            continue
        # nao mostra os placeholders soltos
        t = re.sub(r"\[[^\]]+\]", "____", t)
        esc = _html.escape(t)
        up = t.upper()
        if ("CLÁUSULA" in up or "CLAUSULA" in up) or (t.isupper() and 3 < len(t) < 90):
            out.append(f"<h4>{esc}</h4>")
        else:
            out.append(f"<p>{esc}</p>")
    _CONTRATO_CACHE = "\n".join(out)
    return _CONTRATO_CACHE

# ─────────────────────────── PREENCHER + PDF ───────────────────────────
def _fold(s):
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().upper().strip()

def _slug(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")

def preencher_docx(dados, destino_docx):
    """Substitui os placeholders [ ] do modelo pelos dados do cliente."""
    from docx import Document
    doc = Document(MODELO)
    mapa = {
        "CONTRATANTE": dados["nome"], "NOME": dados["nome"], "RAZAO SOCIAL": dados["nome"],
        "E-MAIL CADASTRADO": dados["email"], "E-MAIL": dados["email"],
        "TELEFONE CADASTRADO": dados["telefone"], "TELEFONE": dados["telefone"],
        "MUNICIPIO DE EMISSAO NFS-E": dados["municipio"],
        "PLANO CONTRATADO": dados["plano_desc"],
        "VENCIMENTO MENSAL": f"Dia {dados['vencimento']}",
        "FORMA DE PAGAMENTO": dados["pagamento"],
        "INSCRITO NO CPF": dados["cpf_fmt"], "ENDERECO": dados["endereco"],
    }
    def troca(texto):
        def rep(m):
            v = mapa.get(_fold(m.group(1)))
            return v if v is not None else m.group(0)
        return re.sub(r"\[([^\]]+)\]", rep, texto)

    def proc_paragrafos(paras):
        for p in paras:
            if "[" in p.text and "]" in p.text:
                novo = troca(p.text)
                if novo != p.text:
                    for r in p.runs:
                        r.text = ""
                    if p.runs:
                        p.runs[0].text = novo
                    else:
                        p.add_run(novo)
    proc_paragrafos(doc.paragraphs)
    for t in doc.tables:
        for row in t.rows:
            for cell in row.cells:
                proc_paragrafos(cell.paragraphs)
    doc.save(destino_docx)

def docx_para_pdf(docx_path, pdf_path):
    """Converte DOCX -> PDF via Word (PowerShell COM). Retorna True se ok."""
    ps = (
        "$w=New-Object -ComObject Word.Application;$w.Visible=$false;"
        f"$d=$w.Documents.Open('{docx_path}',$false,$true);"
        f"$d.SaveAs([ref]'{pdf_path}',[ref]17);$d.Close($false);$w.Quit()"
    )
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, text=True, timeout=120)
    return os.path.isfile(pdf_path)

def pagina_comprovante(dados, ip, base_pdf_bytes):
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib.utils import ImageReader
    agora = datetime.datetime.now().strftime("%d/%m/%Y às %H:%M:%S")
    h = hashlib.sha256(base_pdf_bytes + f"{dados['nome']}{dados['cpf_fmt']}{agora}".encode()).hexdigest()
    png = base64.b64decode(dados["assinatura"].split(",", 1)[-1])
    buf = io.BytesIO(); c = rl_canvas.Canvas(buf, pagesize=A4); W, H = A4
    c.setFillColorRGB(14/255,156/255,134/255); c.rect(0,H-28*mm,W,28*mm,fill=1,stroke=0)
    c.setFillColorRGB(1,1,1); c.setFont("Helvetica-Bold",15); c.drawString(20*mm,H-14*mm,"Move Online Contabilidade Médica")
    c.setFont("Helvetica",10); c.drawString(20*mm,H-20*mm,"Comprovante de Assinatura Eletrônica")
    c.setFillColorRGB(0.06,0.09,0.16); c.setFont("Helvetica-Bold",13); c.drawString(20*mm,H-42*mm,"CONTRATANTE")
    c.setFont("Helvetica",11); y=H-50*mm
    linhas=[("Nome",dados["nome"]),("CPF",dados["cpf_fmt"]),("E-mail",dados["email"]),
            ("Telefone",dados["telefone"]),("Plano",dados["plano_desc"]),
            ("Assinado em",agora),("Endereço IP",ip)]
    for rot,val in linhas:
        c.setFillColorRGB(0.39,0.45,0.55); c.drawString(20*mm,y,rot+":")
        c.setFillColorRGB(0.06,0.09,0.16); c.drawString(52*mm,y,str(val)); y-=8*mm
    c.setFillColorRGB(0.39,0.45,0.55); c.drawString(20*mm,y-4*mm,"Assinatura:")
    try: c.drawImage(ImageReader(io.BytesIO(png)),20*mm,y-40*mm,width=80*mm,height=32*mm,mask='auto',preserveAspectRatio=True,anchor='sw')
    except Exception: pass
    c.setStrokeColorRGB(0.8,0.85,0.9); c.line(20*mm,y-42*mm,100*mm,y-42*mm)
    c.setFont("Helvetica",8); c.setFillColorRGB(0.45,0.5,0.6)
    c.drawString(20*mm,24*mm,"Documento assinado eletronicamente (assinatura eletrônica simples, MP 2.200-2/2001).")
    c.drawString(20*mm,20*mm,f"Código de verificação (SHA-256): {h}")
    c.showPage(); c.save(); buf.seek(0)
    return buf, h, agora

def preencher_pdf(dados):
    """Preenche os placeholders [ ] direto no PDF-modelo (PyMuPDF). Retorna bytes do PDF."""
    import fitz
    nome_up = dados["nome"].upper()
    repl = {
        "[CONTRATANTE]": nome_up, "[NOME]": nome_up, "[RAZÃO SOCIAL]": nome_up,
        "[E-MAIL CADASTRADO]": dados["email"],
        "[TELEFONE CADASTRADO]": dados["telefone"],
        "[MUNICÍPIO DE EMISSÃO NFS-E]": dados["municipio"].upper(),
        "[PLANO CONTRATADO]": dados["plano_desc"],
        "[VENCIMENTO MENSAL]": f"Dia {dados['vencimento']}",
        "[FORMA DE PAGAMENTO]": dados["pagamento"],
        "[INSCRITO NO CPF]": dados["cpf_fmt"],
        "[ENDEREÇO]": dados["endereco"].upper(),
    }
    with open(BASE_PDF, "rb") as f:
        d = fitz.open(stream=f.read(), filetype="pdf")
    for page in d:
        ins = []
        for tok, val in repl.items():
            for r in page.search_for(tok):
                ins.append((r, val))
                page.add_redact_annot(r, fill=(1, 1, 1))
        if ins:
            # nao re-encodar imagens (marca d'agua/assinaturas) -> evita inflar o PDF
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
            for r, val in ins:
                fs = min(r.height * 0.78, 9.5)
                page.insert_text((r.x0, r.y1 - 2.2), val, fontsize=fs, color=(0.06, 0.09, 0.16))
    return d.tobytes()

def gerar_contrato(dados, ip):
    from pypdf import PdfReader, PdfWriter
    base_name = f"{dados['cpf']}_{_slug(dados['nome'])}"
    base_bytes = preencher_pdf(dados)
    comp, h, agora = pagina_comprovante(dados, ip, base_bytes)
    writer = PdfWriter()
    for p in PdfReader(io.BytesIO(base_bytes)).pages:
        writer.add_page(p)
    writer.add_page(PdfReader(comp).pages[0])
    fname = base_name + "_assinado.pdf"
    # bytes do PDF final (para gravar local E subir pro Storage)
    _buf = io.BytesIO(); writer.write(_buf); pdf_bytes = _buf.getvalue()
    pdf_path = os.path.join(OUT_DIR, fname)
    with open(pdf_path, "wb") as f:
        f.write(pdf_bytes)

    reg = {**{k: dados[k] for k in ("nome","cpf_fmt","email","telefone",
              "endereco","municipio","plano","vencimento","pagamento")},
           "arquivo": fname, "data": agora, "ip": ip, "hash": h}

    # 1) cache local imediato (efemero no Render, mas util enquanto o processo vive)
    try:
        with open(os.path.join(OUT_DIR, "_registro.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(reg, ensure_ascii=False) + "\n")
    except Exception as e:
        print("[registro] falha ao gravar jsonl local:", e)

    # 2) PDF duravel no Supabase Storage (bucket privado)
    ok_pdf, det_pdf = supa_upload_pdf(fname, pdf_bytes)
    if not ok_pdf:
        print("[registro] storage NAO subiu o PDF:", det_pdf)

    # 3) lista duravel no Supabase (sobrevive a restart/deploy)
    ok_supa, det_supa = supa_inserir(reg)
    if not ok_supa:
        print("[registro] supabase NAO gravou:", det_supa)

    return fname

# ─────────────────────────── ROTAS ───────────────────────────
@app.route("/")
def home():
    try:
        n = leitura_num_paginas()
        imgs = "".join(f'<img class="cpage" src="/leitura/{i}.png">' for i in range(1, n + 1))
    except Exception:
        imgs = contrato_html()  # fallback: texto
    return PAGINA.replace("__CONTRATO_HTML__", imgs)

# ── Calculadora Simples Nacional x Simples Híbrido (página estática) ──
CALC_DIR = os.path.join(BASE, "calculadora")

@app.route("/calculadora/")
def calculadora():
    return send_file(os.path.join(CALC_DIR, "index.html"), mimetype="text/html")

@app.route("/calculadora/<nome>.png")
def calculadora_img(nome):
    f = os.path.join(CALC_DIR, os.path.basename(nome) + ".png")
    if not os.path.isfile(f):
        abort(404)
    return send_file(f, mimetype="image/png")

# ── Calculadora de orçamento RK Distribuição (página estática) ──
RK_DIR = os.path.join(BASE, "orcamento-rk")

@app.route("/orcamento-rk/")
def orcamento_rk():
    return send_file(os.path.join(RK_DIR, "index.html"), mimetype="text/html")

@app.route("/leitura/<int:n>.png")
def leitura(n):
    try:
        png = render_pagina(n)
    except Exception:
        abort(404)
    return send_file(io.BytesIO(png), mimetype="image/png")

@app.route("/contrato")
def contrato():
    # gera um preview do modelo em PDF (com placeholders visiveis) — apenas leitura
    prev = os.path.join(BASE, "base_contrato.pdf")
    if os.path.isfile(prev):
        return send_file(prev, mimetype="application/pdf", download_name="Contrato_Move_Online.pdf")
    abort(404)

@app.route("/assinar", methods=["POST"])
def assinar():
    d = request.get_json(force=True)
    nome = (d.get("nome") or "").strip()
    cpf = re.sub(r"\D", "", d.get("cpf") or "")
    plano = (d.get("plano") or "").upper()
    if len(nome.split()) < 2 or len(cpf) != 11 or plano not in PLANOS or "base64" not in (d.get("assinatura") or ""):
        return {"ok": False, "erro": "Preencha nome, CPF, plano e assine."}, 400
    dados = {
        "nome": nome, "cpf": cpf, "cpf_fmt": f"{cpf[:3]}.{cpf[3:6]}.{cpf[6:9]}-{cpf[9:]}",
        "email": (d.get("email") or "").strip(), "telefone": (d.get("telefone") or "").strip(),
        "endereco": (d.get("endereco") or "").strip(), "municipio": (d.get("municipio") or "").strip(),
        "plano": plano, "plano_desc": PLANOS[plano],
        "vencimento": (d.get("vencimento") or "10").strip(), "pagamento": (d.get("pagamento") or "BOLETO").strip(),
        "assinatura": d.get("assinatura"),
    }
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "")
    try:
        fname = gerar_contrato(dados, ip)
    except Exception as e:
        return {"ok": False, "erro": f"Falha ao gerar contrato: {e}"}, 500
    return {"ok": True, "url": f"/baixar/{fname}"}

@app.route("/baixar/<path:fname>")
def baixar(fname):
    if ".." in fname or "/" in fname:
        abort(404)
    fpath = os.path.join(OUT_DIR, fname)
    # 1) tem no disco local (recem-assinado) -> serve direto
    if os.path.isfile(fpath):
        return send_file(fpath, mimetype="application/pdf", download_name="Contrato_Move_Assinado.pdf")
    # 2) sumiu do disco (restart do Render) -> busca no Supabase Storage
    pdf = supa_baixar_pdf(fname)
    if pdf:
        return send_file(io.BytesIO(pdf), mimetype="application/pdf",
                         download_name="Contrato_Move_Assinado.pdf")
    abort(404)

@app.route("/painel")
def painel():
    # Fonte principal: Supabase (duravel). Fallback: arquivo local (efemero).
    linhas = supa_listar()
    fonte = "Supabase"
    if linhas is None:
        fonte = "arquivo local (temporário)"
        reg = os.path.join(OUT_DIR, "_registro.jsonl"); linhas = []
        if os.path.isfile(reg):
            for l in open(reg, encoding="utf-8"):
                try: linhas.append(json.loads(l))
                except Exception: pass
        linhas.reverse()
    def pdf_cell(x):
        arq = x.get("arquivo", "")
        if arq:
            return f"<a href='/baixar/{arq}'>abrir</a>"
        return "<span style='color:#94a3b8'>—</span>"
    rows = "".join(
        f"<tr><td>{x.get('nome','')}</td><td>{x.get('cpf_fmt','')}</td><td>{x.get('plano','')}</td>"
        f"<td>{x.get('data','')}</td><td>{pdf_cell(x)}</td></tr>" for x in linhas)
    return ("<html><head><meta charset=utf-8><title>Contratos assinados</title>"
            "<style>body{font-family:Segoe UI,Arial;margin:30px;color:#0f172a}h1{font-size:20px}"
            ".fonte{color:#64748b;font-size:12px;margin:-6px 0 16px}"
            "table{border-collapse:collapse;width:100%;font-size:14px}td,th{border-bottom:1px solid #e2e8f0;padding:10px;text-align:left}"
            "a{color:#0B7F6E}</style></head><body>"
            f"<h1>Contratos assinados ({len(linhas)})</h1>"
            f"<div class='fonte'>Fonte: {fonte} · PDF guardado no Supabase (abre pelo botão).</div>"
            "<table><tr><th>Nome</th><th>CPF</th><th>Plano</th><th>Assinado em</th><th>PDF</th></tr>"
            f"{rows}</table></body></html>")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5001)), debug=False)

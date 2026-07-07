"""
Banco de memória do Claudio — Supabase (PostgreSQL na nuvem)
Histórico permanente mesmo com Z-API ou Render desconectados.
"""

import os
import requests

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=minimal"
}


def salvar(telefone: str, mensagem: str, direcao: str, nome: str = None):
    """Salva mensagem no Supabase."""
    try:
        requests.post(
            f"{SUPABASE_URL}/rest/v1/mensagens",
            headers=HEADERS,
            json={"telefone": telefone, "nome": nome, "direcao": direcao, "mensagem": mensagem},
            timeout=5
        )
        # Salva/atualiza cliente
        if nome:
            requests.post(
                f"{SUPABASE_URL}/rest/v1/clientes",
                headers={**HEADERS, "Prefer": "resolution=merge-duplicates"},
                json={"telefone": telefone, "nome": nome},
                timeout=5
            )
    except Exception as e:
        print(f"[Supabase] Erro ao salvar: {e}")


def buscar_historico(telefone: str, limite: int = 10) -> list:
    """Busca últimas N mensagens da conversa."""
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/mensagens",
            headers=HEADERS,
            params={
                "telefone": f"eq.{telefone}",
                "order": "criado_em.desc",
                "limit": limite
            },
            timeout=5
        )
        if r.status_code == 200:
            return list(reversed(r.json()))
    except Exception as e:
        print(f"[Supabase] Erro ao buscar histórico: {e}")
    return []


def montar_contexto(telefone: str, mensagem_atual: str, nome: str) -> str:
    """Monta contexto completo da conversa para o Claude analisar."""
    historico = buscar_historico(telefone, limite=8)

    if not historico:
        return f"Mensagem do cliente {nome}: {mensagem_atual}"

    linhas = [f"Histórico da conversa com {nome} ({telefone}):"]
    for m in historico:
        quem = "Cliente" if m["direcao"] == "recebida" else "Claudio"
        hora = m.get("criado_em", "")[-14:-9] if m.get("criado_em") else ""
        linhas.append(f"[{hora}] {quem}: {m['mensagem']}")

    linhas.append(f"\nMensagem atual do cliente: {mensagem_atual}")
    linhas.append("\nAnalise o contexto completo e responda adequadamente.")
    return "\n".join(linhas)


def salvar_card(card_id: str, titulo: str, lista: str, telefone: str = None, origem: str = None):
    """Registra card criado no Trello."""
    try:
        requests.post(
            f"{SUPABASE_URL}/rest/v1/cards_trello",
            headers={**HEADERS, "Prefer": "resolution=ignore-duplicates"},
            json={"card_id": card_id, "titulo": titulo, "lista": lista, "telefone": telefone, "origem": origem},
            timeout=5
        )
    except Exception as e:
        print(f"[Supabase] Erro ao salvar card: {e}")

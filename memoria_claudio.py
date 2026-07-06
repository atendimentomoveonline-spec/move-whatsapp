"""
Banco de memória do Claudio — SQLite
Guarda histórico de conversas por telefone para contexto completo
"""

import sqlite3
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "claudio_memoria.db")


def conectar():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def inicializar():
    with conectar() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS mensagens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telefone TEXT NOT NULL,
                nome TEXT,
                direcao TEXT NOT NULL CHECK(direcao IN ('recebida','enviada')),
                mensagem TEXT NOT NULL,
                criado_em TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE INDEX IF NOT EXISTS idx_telefone ON mensagens(telefone);
        """)


def salvar(telefone: str, mensagem: str, direcao: str, nome: str = None):
    with conectar() as conn:
        conn.execute(
            "INSERT INTO mensagens (telefone, nome, direcao, mensagem) VALUES (?,?,?,?)",
            (telefone, nome, direcao, mensagem)
        )


def buscar_historico(telefone: str, limite: int = 10) -> list:
    """Retorna últimas N mensagens da conversa com esse telefone."""
    with conectar() as conn:
        rows = conn.execute("""
            SELECT direcao, nome, mensagem, criado_em
            FROM mensagens WHERE telefone = ?
            ORDER BY criado_em DESC LIMIT ?
        """, (telefone, limite)).fetchall()
    return [dict(r) for r in reversed(rows)]


def montar_contexto(telefone: str, mensagem_atual: str, nome: str) -> str:
    """Monta texto de contexto completo para enviar ao Claude."""
    historico = buscar_historico(telefone, limite=8)

    if not historico:
        return f"Mensagem do cliente {nome}: {mensagem_atual}"

    linhas = [f"Histórico da conversa com {nome} ({telefone}):"]
    for m in historico:
        quem = "Cliente" if m["direcao"] == "recebida" else "Claudio"
        linhas.append(f"[{m['criado_em'][-8:-3]}] {quem}: {m['mensagem']}")

    linhas.append(f"\nMensagem atual do cliente: {mensagem_atual}")
    linhas.append("\nAnalise o contexto completo e responda adequadamente.")

    return "\n".join(linhas)


inicializar()

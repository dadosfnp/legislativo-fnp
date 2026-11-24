from __future__ import annotations

import datetime
import json
import re
from pathlib import Path
from typing import Any

from django.utils.text import slugify


def normalize_text(value: Any) -> str:
    if value is None:
        return ''
    return str(value).strip()


def parse_date(value: str) -> datetime.date | None:
    if not value:
        return None
    match = re.search(r'(\d{2})/(\d{2})/(\d{4})', value)
    if not match:
        return None
    day, month, year = match.groups()
    try:
        return datetime.date(int(year), int(month), int(day))
    except ValueError:
        return None


def is_on_pauta(item: dict[str, Any]) -> bool:
    return normalize_text(item.get('Pauta')).lower() == 'sim'


def is_urgente(item: dict[str, Any]) -> bool:
    if is_on_pauta(item):
        return True

    status = normalize_text(item.get('Status da Tramitação')).lower()
    proximos = normalize_text(item.get('Próximos Eventos/Ações Esperadas')).lower()
    ultima = normalize_text(item.get('Última Movimentação')).lower()
    pauta = normalize_text(item.get('Pauta')).lower()
    interlocutores = normalize_text(item.get('Interlocutores Estratégicos (Relatores, Lideranças, Técnicos)')).lower()

    has = (
        'urgência' in status
        or 'art. 155' in status
        or 'art.155' in status
        or 'urgência' in proximos
        or 'sessão deliberativa' in proximos
        or 'em deliberação' in proximos
        or 'sessão deliberativa' in ultima
        or 'discussão' in ultima
        or 'art.155' in pauta
        or 'urgência' in pauta
        or 'urgência por' in interlocutores
    )
    if not has:
        return False
    if 'sem movimentação há' in proximos:
        return False
    return True


def is_aprovada(item: dict[str, Any]) -> bool:
    combined = ' '.join(
        normalize_text(item.get(field))
        for field in ('Status da Tramitação', 'Local', 'Última Movimentação')
    ).lower()
    # Sinais TERMINAIS (aprovada no Congresso -> virou lei/norma ou foi à
    # sanção): valem mesmo que o texto ainda cite comissão. "Aguardando/enviada
    # à sanção" é aprovação definitiva do Legislativo (falta só o Executivo);
    # sem isso, matéria aprovada na Câmara e remetida à sanção aparecia "em
    # acompanhamento" (caso PLP 299/2026, 24/11/2026).
    virou_norma = (
        'promulgad' in combined
        or 'sancionad' in combined
        or 'sanção' in combined
        or ('transformad' in combined and ('lei' in combined or 'norma' in combined))
        or ('convertid' in combined and 'lei' in combined)
    )
    if virou_norma:
        return True
    # Aprovação só em comissão (sem plenário) ainda não é aprovação final.
    if 'comissão' in combined and 'plenário' not in combined:
        return False
    return 'aprovad' in combined and (
        'plenário' in combined or 'votação final' in combined or 'redação final' in combined
    )


def is_parada(item: dict[str, Any]) -> bool:
    proximos = normalize_text(item.get('Próximos Eventos/Ações Esperadas'))
    if 'Sem movimentação' in proximos:
        return True
    ultima = normalize_text(item.get('Última Movimentação'))
    date_value = parse_date(ultima)
    if date_value:
        age_years = (datetime.date.today() - date_value).days / 365.25
        return age_years >= 4
    return False


# --- Reconciliação bicameral: seguir a matéria para a Casa ativa -------------
# Uma matéria (TIPO N/ANO) pode existir na Câmara e no Senado. O painel deve
# rastrear a Casa onde ela está ATIVA e trocar sozinho quando ela muda de Casa.

def _dados_terminal(dados: dict[str, Any] | None) -> bool:
    """A Casa é terminal (autoritativa, fim de linha) quando a matéria ali foi
    aprovada/sancionada/promulgada/virou norma, ou foi arquivada."""
    if not dados:
        return False
    item = {
        'Status da Tramitação': dados.get('status_tramitacao', ''),
        'Última Movimentação': dados.get('ultima_movimentacao', ''),
    }
    texto = f"{item['Status da Tramitação']} {item['Última Movimentação']}".lower()
    return is_aprovada(item) or 'arquivad' in texto


def casa_ativa(cam: dict[str, Any] | None, sen: dict[str, Any] | None) -> str | None:
    """Decide a Casa ativa de uma matéria a partir das duas fichas oficiais.

    Ordem das regras: (1) terminal — Casa que já concluiu (aprovou/sancionou/
    promulgou/arquivou) manda; (2) handoff — "remetida à Câmara/ao Senado"
    aponta a Casa que recebeu; (3) recência — a de movimentação mais nova.
    Devolve 'camara', 'senado' ou None (indeterminado)."""
    if cam and not sen:
        return 'camara'
    if sen and not cam:
        return 'senado'
    if not cam and not sen:
        return None

    tc, ts = _dados_terminal(cam), _dados_terminal(sen)
    if tc and not ts:
        return 'camara'
    if ts and not tc:
        return 'senado'

    sc = f"{cam.get('status_tramitacao', '')} {cam.get('ultima_movimentacao', '')}".lower()
    ss = f"{sen.get('status_tramitacao', '')} {sen.get('ultima_movimentacao', '')}".lower()
    if 'remetid' in ss and ('câmara' in ss or 'camara' in ss):
        return 'camara'
    if 'remetid' in sc and 'senado' in sc:
        return 'senado'

    dc, ds = parse_date(cam.get('ultima_movimentacao', '')), parse_date(sen.get('ultima_movimentacao', ''))
    if dc and ds:
        return 'camara' if dc >= ds else 'senado'
    if dc:
        return 'camara'
    if ds:
        return 'senado'
    return None


def ementa_compativel(a: str, b: str, limiar: float = 0.3):
    """Trava anticolisão: número igual entre as Casas pode ser matéria
    diferente. Compara as ementas por sobreposição de palavras (Jaccard).
    Devolve True/False, ou None quando não dá para julgar (ementa vazia)."""
    ta = set(re.findall(r'\w{4,}', (a or '').lower()))
    tb = set(re.findall(r'\w{4,}', (b or '').lower()))
    if not ta or not tb:
        return None
    return (len(ta & tb) / len(ta | tb)) >= limiar


def priority_from_text(value: Any) -> str:
    text = normalize_text(value).lower()
    if 'alta' in text:
        return 'alta'
    if 'media' in text or 'média' in text:
        return 'media'
    if 'baixa' in text:
        return 'baixa'
    return 'normal'


def slug_for_name(name: str) -> str:
    return slugify(normalize_text(name))


def split_temas(value: Any) -> list[str]:
    raw = normalize_text(value)
    if not raw:
        return []
    parts = re.split(r'[,/]', raw)
    return [p.strip() for p in parts if p.strip()]


def load_json_file(path: str) -> list[dict[str, Any]]:
    content = Path(path).read_text(encoding='utf-8')
    parsed = json.loads(content)
    if isinstance(parsed, dict) and 'data' in parsed and isinstance(parsed['data'], list):
        return parsed['data']
    if isinstance(parsed, list):
        return parsed
    raise ValueError('JSON must contain a top-level list or object with a "data" list.')

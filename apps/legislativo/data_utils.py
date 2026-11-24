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
    # Sinais TERMINAIS (virou lei/norma): valem mesmo que o texto ainda cite
    # comissão -- uma lei sancionada/promulgada/transformada é o fim da linha.
    # Antes, matérias já transformadas em norma apareciam "em acompanhamento".
    virou_norma = (
        'promulgad' in combined
        or 'sancionad' in combined
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

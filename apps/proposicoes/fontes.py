"""Busca de proposições nas fontes oficiais (Câmara e Senado).

Reutilizado pelo cadastro no painel e, no futuro, pelo `sync_camara`. Cada
função devolve um dicionário normalizado (mesmas chaves do `ingest_legislativo`)
ou ``None`` quando não encontra. Nenhuma exceção de rede escapa: falha de API
vira ``None`` (quem chama trata como "não encontrada / tente de novo").
"""
from __future__ import annotations

from datetime import datetime

import requests

CAMARA_API = 'https://dadosabertos.camara.leg.br/api/v2'
SENADO_API = 'https://legis.senado.leg.br/dadosabertos'
TIMEOUT = 15

# Tipos aceitos no formulário; a sigla é a mesma nas duas casas.
TIPOS = ['PL', 'PEC', 'PLP', 'PLN', 'PLV', 'MPV', 'PDL', 'PRC', 'PLC', 'PLS']


def _normalizar(titulo, casa, *, status='', local='', ultima='', ementa='', link=''):
    return {
        'titulo': titulo,
        'casa': casa,
        'status_tramitacao': status or '',
        'local': local or '',
        'ultima_movimentacao': ultima or '',
        'ementa_resumida': ementa or '',
        'link': link or '',
    }


def buscar_camara(tipo: str, numero: int, ano: int):
    """Busca na API de Dados Abertos da Câmara. ``None`` se não achar."""
    try:
        resp = requests.get(
            f'{CAMARA_API}/proposicoes',
            params={'siglaTipo': tipo, 'numero': numero, 'ano': ano},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        dados = resp.json().get('dados', [])
    except requests.RequestException:
        return None
    if not dados:
        return None

    item = dados[0]
    prop_id = item['id']
    ementa = item.get('ementa') or ''
    status_situacao = ''
    sigla_orgao = ''
    ultima = ''
    try:
        det = requests.get(f'{CAMARA_API}/proposicoes/{prop_id}', timeout=TIMEOUT)
        det.raise_for_status()
        status = det.json().get('dados', {}).get('statusProposicao') or {}
        status_situacao = status.get('descricaoSituacao') or ''
        sigla_orgao = status.get('siglaOrgao') or ''
        descricao_tram = status.get('descricaoTramitacao') or ''
        data_hora = status.get('dataHora') or ''
        ultima = descricao_tram
        if data_hora:
            try:
                dt = datetime.fromisoformat(data_hora)
                ultima = f'{descricao_tram} em {dt.strftime("%d/%m/%Y")}'.strip()
            except ValueError:
                pass
    except requests.RequestException:
        pass

    return _normalizar(
        f'{tipo} {numero}/{ano}',
        'camara',
        status=status_situacao or 'Em tramitação',
        local=sigla_orgao,
        ultima=ultima,
        ementa=ementa,
        link=f'https://www.camara.leg.br/proposicoesWeb/fichadetramitacao?idProposicao={prop_id}',
    )


def buscar_senado(tipo: str, numero: int, ano: int):
    """Busca na API de Dados Abertos do Senado. ``None`` se não achar."""
    try:
        resp = requests.get(
            f'{SENADO_API}/processo',
            params={'sigla': tipo, 'numero': numero, 'ano': ano},
            headers={'Accept': 'application/json'},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        dados = resp.json()
    except requests.RequestException:
        return None
    if isinstance(dados, dict):
        dados = dados.get('ListaProcesso', {}).get('Processos', {}).get('Processo', [])
    if not isinstance(dados, list) or not dados:
        return None

    item = dados[0] if isinstance(dados[0], dict) else {}
    codigo = item.get('codigoMateria') or item.get('CodigoMateria')
    if not codigo:
        return None
    ementa = item.get('ementa') or item.get('Ementa') or ''
    situacao = (
        item.get('descricaoSituacao')
        or item.get('situacao')
        or item.get('descricaoObjetivoProcesso')
        or ''
    )
    data_situacao = item.get('dataSituacaoAtual') or ''
    ultima = ''
    if data_situacao:
        try:
            ultima = datetime.fromisoformat(data_situacao).strftime('%d/%m/%Y')
        except ValueError:
            ultima = str(data_situacao)

    return _normalizar(
        f'{tipo} {numero}/{ano}',
        'senado',
        status=situacao or 'Em tramitação',
        ultima=ultima,
        ementa=ementa,
        link=f'https://www25.senado.leg.br/web/atividade/materias/-/materia/{codigo}',
    )


def buscar_status_camara(prop_id):
    """Situação atual de uma proposição da Câmara pelo ``idProposicao``.

    Usado pelo atualizador dirigido (``atualizar_tramitacao``) para refrescar a
    última movimentação das proposições já no painel. Devolve um dicionário com
    a movimentação já formatada em ``ultima`` (``None`` se a API falhar ou não
    trouxer dados — nenhuma exceção de rede escapa)."""
    try:
        det = requests.get(f'{CAMARA_API}/proposicoes/{prop_id}', timeout=TIMEOUT)
        det.raise_for_status()
        status = det.json().get('dados', {}).get('statusProposicao') or {}
    except requests.RequestException:
        return None
    if not status:
        return None

    situacao = status.get('descricaoSituacao') or ''
    tramitacao = status.get('descricaoTramitacao') or ''
    despacho = (status.get('despacho') or '').strip()
    orgao = status.get('siglaOrgao') or ''
    data_hora = status.get('dataHora') or ''
    data_fmt = ''
    if data_hora:
        try:
            data_fmt = datetime.fromisoformat(data_hora).strftime('%d/%m/%Y')
        except ValueError:
            data_fmt = ''

    corpo = despacho or tramitacao or situacao
    if not corpo:
        return None
    ultima = f'{data_fmt}: {corpo}' if data_fmt else corpo
    return {
        'situacao': situacao,
        'orgao': orgao,
        'tramitacao': tramitacao,
        'despacho': despacho,
        'data': data_fmt,
        'ultima': ultima.strip(),
    }


def buscar_proposicao(tipo: str, numero: int, ano: int):
    """Tenta a Câmara; se não achar, o Senado. Devolve o dicionário
    normalizado ou ``None``."""
    tipo = (tipo or '').strip().upper()
    return buscar_camara(tipo, numero, ano) or buscar_senado(tipo, numero, ano)

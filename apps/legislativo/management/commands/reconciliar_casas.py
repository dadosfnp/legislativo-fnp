"""Reconciliação bicameral: faz cada matéria SEGUIR a Casa onde está ativa.

O problema: uma matéria (TIPO N/ANO) pode tramitar na Câmara e no Senado, mas o
painel guarda um único link. Quando ela troca de Casa (ex.: PLP 299/2026,
remetida do Senado à Câmara e lá aprovada), o rastreamento automático (que lê
só fichas da Câmara) deixa de alcançá-la.

Este comando consulta AS DUAS Casas pelo título, decide a Casa ativa (ver
``data_utils.casa_ativa``: terminal > handoff > recência) e, quando ela difere
da Casa do link atual, **troca o link e os campos factuais** (status, local,
última movimentação) e recomputa o selo APROVADA (só False->True).

Trava anticolisão (número igual entre Casas pode ser matéria diferente): só
troca se a ementa bater (``ementa_compativel``). Na dúvida, não troca.

NUNCA toca em campos curados (posicionamento, tag FNP, prioridade, temas,
apensação). Pauta/parada continuam com seus próprios comandos.

Modos:
  (padrão)   troca e alerta os seguidores das que mudaram de Casa
  --quiet    troca SEM alertar (baseline)
  --dry-run  só mostra o que mudaria
  --limit N  processa no máximo N proposições
"""
from __future__ import annotations

import re

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.legislativo.data_utils import casa_ativa, ementa_compativel, is_aprovada
from apps.proposicoes import fontes
from apps.proposicoes.models import Proposicao

TITULO_RE = re.compile(r'\b(PEC|PLP|PLN|PLV|PDL|PDC|PDS|PRC|MPV|PL)\s*(\d{1,5})\s*/\s*(\d{4})', re.IGNORECASE)


def casa_do_link(link: str) -> str:
    link = (link or '').lower()
    if 'idproposicao=' in link:
        return 'camara'
    if '/materia/' in link or 'senado' in link:
        return 'senado'
    return ''


class Command(BaseCommand):
    help = 'Faz cada matéria seguir a Casa onde está ativa (reconciliação bicameral).'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', dest='dry_run',
                            help='Não grava; apenas mostra o que mudaria.')
        parser.add_argument('--quiet', action='store_true', dest='quiet',
                            help='Troca sem disparar alerta — use no primeiro baseline.')
        parser.add_argument('--limit', type=int, default=0,
                            help='Processa no máximo N proposições (0 = todas).')

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        quiet = options['quiet']
        limit = options['limit']

        trocadas = indeterminadas = bloqueadas = processadas = 0
        for prop in Proposicao.objects.publicadas().order_by('pk'):
            m = TITULO_RE.search(prop.titulo or '')
            if not m:
                continue
            if limit and processadas >= limit:
                break
            processadas += 1

            tipo, numero, ano = m.group(1).upper(), int(m.group(2)), int(m.group(3))
            cam = fontes.buscar_camara(tipo, numero, ano)
            sen = fontes.buscar_senado(tipo, numero, ano)
            ativa = casa_ativa(cam, sen)
            if ativa is None:
                indeterminadas += 1
                continue

            if ativa == casa_do_link(prop.link):
                continue  # já está na Casa certa; outros comandos cuidam da atualização

            dados = cam if ativa == 'camara' else sen

            # Trava anticolisão: confirma que é a MESMA matéria antes de trocar.
            compat = ementa_compativel(dados.get('ementa_resumida', ''), prop.ementa_resumida)
            if compat is False:
                bloqueadas += 1
                continue
            if compat is None:
                # Ementa curada vazia: só troca se as duas Casas baterem entre si
                # (confirma que é uma matéria bicameral só, não colisão de número).
                entre_casas = (
                    ementa_compativel(cam.get('ementa_resumida', ''), sen.get('ementa_resumida', ''))
                    if cam and sen else None
                )
                if entre_casas is not True:
                    bloqueadas += 1
                    continue

            agora = timezone.now()
            nova_aprovada = is_aprovada({
                'Status da Tramitação': dados.get('status_tramitacao', ''),
                'Local': dados.get('local', ''),
                'Última Movimentação': dados.get('ultima_movimentacao', ''),
            })
            vira_aprovada = nova_aprovada and not prop.aprovada

            if dry_run:
                self.stdout.write(
                    f'[dry-run] {prop.titulo}: {casa_do_link(prop.link) or "?"} -> {ativa} '
                    f'| {dados.get("status_tramitacao", "")}'
                )
                trocadas += 1
                continue

            campos = {
                'casa': ativa,
                'status_tramitacao': dados.get('status_tramitacao', ''),
                'local': dados.get('local', ''),
                'ultima_movimentacao': dados.get('ultima_movimentacao', ''),
                'link': dados.get('link', ''),
                'sincronizado_em': agora,
            }
            if vira_aprovada:
                campos['aprovada'] = True

            if quiet:
                Proposicao.objects.filter(pk=prop.pk).update(**campos)
            else:
                for campo, valor in campos.items():
                    setattr(prop, campo, valor)
                prop.save()  # alerta os seguidores (status/última/aprovada em CAMPOS_ALERTA)
            trocadas += 1
            self.stdout.write(f'Seguiu para {ativa}: {prop.titulo} -> {dados.get("status_tramitacao", "")}')

        modo = 'dry-run' if dry_run else ('quiet' if quiet else 'normal')
        self.stdout.write(self.style.SUCCESS(
            f'[{modo}] {trocadas} trocada(s) de Casa, {bloqueadas} bloqueada(s) '
            f'(ementa não bateu), {indeterminadas} indeterminada(s).'
        ))

"""Atualizador dirigido de tramitação (v1 — Câmara).

Percorre as proposições JÁ publicadas no painel e refresca a *última
movimentação* de cada uma a partir da ficha oficial da Câmara (pelo
``idProposicao`` presente no link curado). Quando a movimentação muda, grava e
dispara o alerta individual (sino + e-mail) para quem segue a proposição --
reusa o signal de ``comentarios/alertas.py``.

Diferente do ``sync_camara`` (desativado), este NÃO varre a Câmara por
palavra-chave nem cria proposições novas: só atualiza o que a equipe já curou.
Não mexe nos campos editoriais (posicionamento, status redigido à mão, etc.);
só a última movimentação, que é factual.

O Senado fica de fora na v1: a API de Dados Abertos do Senado está em migração
(``situacaoatual`` em descontinuação; substituto ``/processo``). Essas ficam
para atualização manual até a v2.

Modos:
  (padrão)     atualiza e alerta os seguidores nas que mudaram
  --quiet      atualiza SEM alertar (use no primeiro baseline, pra não floodar)
  --dry-run    só mostra o que mudaria, não grava
  --limit N    processa no máximo N proposições da Câmara
  --titulo "X" processa só a proposição de título exato X
"""
from __future__ import annotations

import re

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.legislativo.data_utils import is_aprovada
from apps.proposicoes import fontes
from apps.proposicoes.models import Proposicao

ID_CAMARA = re.compile(r'idProposicao=(\d+)')


class Command(BaseCommand):
    help = 'Atualiza a última movimentação das proposições publicadas pela ficha oficial da Câmara (v1; Senado manual).'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', dest='dry_run',
                            help='Não grava; apenas mostra o que mudaria.')
        parser.add_argument('--quiet', action='store_true', dest='quiet',
                            help='Atualiza sem disparar alerta (sino/e-mail) — use no primeiro baseline.')
        parser.add_argument('--limit', type=int, default=0,
                            help='Processa no máximo N proposições da Câmara (0 = todas).')
        parser.add_argument('--titulo', type=str, default='',
                            help='Processa só a proposição com este título exato.')

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        quiet = options['quiet']
        limit = options['limit']
        titulo = (options.get('titulo') or '').strip()

        qs = Proposicao.objects.publicadas().order_by('pk')
        if titulo:
            qs = qs.filter(titulo=titulo)

        atualizadas = sem_dados = fora_camara = 0
        processadas = 0
        for prop in qs:
            m = ID_CAMARA.search(prop.link or '')
            if not m:
                fora_camara += 1
                continue
            if limit and processadas >= limit:
                break
            processadas += 1

            dados = fontes.buscar_status_camara(m.group(1))
            if not dados or not dados.get('ultima'):
                sem_dados += 1
                continue

            # A fonte foi consultada com sucesso: registra a data da consulta,
            # mesmo que nada tenha mudado (é o que dá ao usuário a garantia de
            # que o dado foi conferido). sincronizado_em não está em
            # CAMPOS_ALERTA, então tocá-lo nunca dispara alerta.
            agora = timezone.now()
            novo = dados['ultima']
            mudou = novo != (prop.ultima_movimentacao or '')

            # "Virou lei": reconhece, pela situação fresca da Câmara, a matéria
            # que foi transformada em norma/sancionada e ainda constava "em
            # acompanhamento". Flipamos aprovada só de False->True: nunca
            # regredimos uma aprovação já curada que a fonte do momento não
            # reflita (ex.: PEC promulgada cujo retorno não bate o heurístico).
            registro = {
                'Status da Tramitação': dados.get('situacao') or '',
                'Local': dados.get('orgao') or '',
                'Última Movimentação': novo,
            }
            virou_lei = is_aprovada(registro) and not prop.aprovada

            if dry_run:
                if mudou or virou_lei:
                    extra = ' [vira APROVADA]' if virou_lei else ''
                    self.stdout.write(f'[dry-run] {prop.titulo}: {prop.ultima_movimentacao!r} -> {novo!r}{extra}')
                    atualizadas += 1
                continue

            if mudou or virou_lei:
                if quiet:
                    # .update() não dispara o signal de alerta (silencioso).
                    campos = {'ultima_movimentacao': novo, 'sincronizado_em': agora}
                    if virou_lei:
                        campos['aprovada'] = True
                    Proposicao.objects.filter(pk=prop.pk).update(**campos)
                else:
                    prop.ultima_movimentacao = novo
                    prop.sincronizado_em = agora
                    if virou_lei:
                        prop.aprovada = True
                    prop.save()  # dispara o alerta aos seguidores (CAMPOS_ALERTA)
                atualizadas += 1
                marca = ' [APROVADA]' if virou_lei else ''
                self.stdout.write(f'Atualizada: {prop.titulo} -> {novo}{marca}')
            else:
                # Nada mudou, mas consultamos a fonte: grava só a data da
                # consulta, de forma silenciosa (sem save → sem alerta).
                Proposicao.objects.filter(pk=prop.pk).update(sincronizado_em=agora)

        modo = 'dry-run' if dry_run else ('quiet' if quiet else 'normal')
        self.stdout.write(self.style.SUCCESS(
            f'[{modo}] {atualizadas} atualizada(s), {sem_dados} sem dados na Câmara, '
            f'{fora_camara} fora da Câmara (puladas — Senado/sem id).'
        ))

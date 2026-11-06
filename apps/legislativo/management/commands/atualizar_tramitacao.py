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

            novo = dados['ultima']
            if novo == (prop.ultima_movimentacao or ''):
                continue

            if dry_run:
                self.stdout.write(f'[dry-run] {prop.titulo}: {prop.ultima_movimentacao!r} -> {novo!r}')
                atualizadas += 1
                continue

            if quiet:
                # .update() não dispara o signal de alerta (silencioso).
                Proposicao.objects.filter(pk=prop.pk).update(ultima_movimentacao=novo)
            else:
                prop.ultima_movimentacao = novo
                prop.save()  # dispara o alerta aos seguidores (CAMPOS_ALERTA)
            atualizadas += 1
            self.stdout.write(f'Atualizada: {prop.titulo} -> {novo}')

        modo = 'dry-run' if dry_run else ('quiet' if quiet else 'normal')
        self.stdout.write(self.style.SUCCESS(
            f'[{modo}] {atualizadas} atualizada(s), {sem_dados} sem dados na Câmara, '
            f'{fora_camara} fora da Câmara (puladas — Senado/sem id).'
        ))

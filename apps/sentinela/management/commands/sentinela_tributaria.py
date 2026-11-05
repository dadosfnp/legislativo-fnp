"""Sentinela temática (CBS/IBS e reforma tributária).

Varre as fontes oficiais por palavras-chave (editáveis no admin), e para cada
proposição NOVA (ainda não vista e ainda não no painel):
  1) cria a proposição como PENDENTE (fila de curadoria), e
  2) alerta todo o time do Legislativo da FNP -- sino + e-mail (sempre).

Nunca publica sozinho (a curadoria é humana). Primeira execução deve usar
--baseline, que marca os itens atuais como vistos SEM alertar, para não
disparar um lote de proposições antigas.

Uso:
  python manage.py sentinela_tributaria --baseline   # 1ª vez
  python manage.py sentinela_tributaria               # execuções seguintes (cron)
  python manage.py sentinela_tributaria --dry-run --dias 30
"""
from __future__ import annotations

import logging

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q

from apps.comentarios.models import Notificacao
from apps.proposicoes.models import Macrotema, Proposicao
from apps.usuarios.models import Perfil, Usuario

from ... import fontes
from ...emails import enviar_email_sentinela
from ...models import ItemSentinelaVisto, TermoSentinela

logger = logging.getLogger(__name__)
MACROTEMA_NOME = 'Reforma Tributária'


class Command(BaseCommand):
    help = 'Varre as fontes oficiais por CBS/IBS (termos editáveis) e alerta o Leg da FNP.'

    def add_arguments(self, parser):
        parser.add_argument('--baseline', action='store_true', help='Marca os atuais como vistos sem alertar (1ª execução).')
        parser.add_argument('--dry-run', action='store_true', help='Não grava nem envia e-mail; só mostra.')
        parser.add_argument('--dias', type=int, default=15, help='Janela de apresentação a considerar (default 15).')

    def handle(self, *args, **options):
        baseline = options['baseline']
        dry = options['dry_run']
        dias = options['dias']

        termos = list(TermoSentinela.objects.filter(ativo=True).values_list('termo', flat=True))
        if not termos:
            self.stdout.write(self.style.WARNING('Nenhum termo ativo cadastrado — nada a varrer.'))
            return

        candidatos = fontes.buscar(termos, dias)
        self.stdout.write(f'{len(candidatos)} candidato(s) nas fontes para os termos: {", ".join(termos)}')

        equipe = list(
            Usuario.objects.filter(is_active=True)
            .filter(Q(is_staff=True) | Q(perfil__classe_usuario=Perfil.EQUIPE_FNP))
            .distinct()
        )

        novos = 0
        alertados = 0
        with transaction.atomic():
            macrotema = self._macrotema(dry)
            for cand in candidatos:
                if ItemSentinelaVisto.objects.filter(fonte=cand['fonte'], id_externo=cand['id_externo']).exists():
                    continue
                ja_no_painel = Proposicao.objects.filter(
                    Q(link=cand['link']) | Q(titulo=cand['titulo'])
                ).exists()

                visto = None
                if not dry:
                    visto = ItemSentinelaVisto.objects.create(
                        fonte=cand['fonte'], id_externo=cand['id_externo'],
                        titulo=cand['titulo'], termo=cand.get('termo', ''), alertado=False,
                    )
                novos += 1

                if baseline or ja_no_painel:
                    # baseline: só registra; já no painel: conhecido, não re-alerta.
                    continue

                prop = self._criar_pendente(cand, macrotema, dry)
                self._alertar(equipe, prop, cand, dry)
                if visto is not None:
                    visto.proposicao = prop
                    visto.alertado = True
                    visto.save(update_fields=['proposicao', 'alertado'])
                alertados += 1
                self.stdout.write(f'  • NOVO: {cand["titulo"]} ({cand["termo"]}) — pendente + alerta p/ {len(equipe)} do Leg')

            if dry:
                transaction.set_rollback(True)

        resumo = f'Vistos novos: {novos} · alertados: {alertados}'
        if baseline:
            resumo += ' · (baseline — nada alertado)'
        if dry:
            resumo += ' · (dry-run — nada gravado)'
        self.stdout.write(self.style.SUCCESS(resumo))

    def _macrotema(self, dry):
        if dry:
            return Macrotema.objects.filter(nome=MACROTEMA_NOME).first()
        macrotema, _ = Macrotema.objects.get_or_create(
            nome=MACROTEMA_NOME,
            defaults={'slug': 'reforma-tributaria', 'cor': '#1A4B8F'},
        )
        return macrotema

    def _criar_pendente(self, cand, macrotema, dry):
        if dry:
            return Proposicao(titulo=cand['titulo'], link=cand['link'])  # só para a mensagem
        return Proposicao.objects.create(
            titulo=cand['titulo'],
            casa=cand['casa'],
            ementa_resumida=cand['ementa'],
            link=cand['link'],
            macrotema=macrotema,
            status_publicacao=Proposicao.PENDENTE,
        )

    def _alertar(self, equipe, prop, cand, dry):
        mensagem = f'Radar tributário ({cand["termo"]}): {prop.titulo} — {cand["ementa"][:90]}'
        if dry:
            return
        Notificacao.objects.bulk_create([
            Notificacao(
                destinatario=u, proposicao=prop,
                tipo=Notificacao.TIPO_SENTINELA, mensagem=mensagem[:255],
            )
            for u in equipe
        ])
        try:
            enviar_email_sentinela(equipe, prop, cand['termo'])
            Notificacao.objects.filter(proposicao=prop, tipo=Notificacao.TIPO_SENTINELA).update(enviado_email=True)
        except Exception:
            logger.exception('Falha ao enviar e-mail do sentinela para %s', prop.titulo)

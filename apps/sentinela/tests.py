from unittest import mock

from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from apps.comentarios.models import Notificacao
from apps.proposicoes.models import Proposicao
from apps.usuarios.models import Perfil, Usuario

from .models import ItemSentinelaVisto, TermoSentinela

CAND = {
    'fonte': 'camara',
    'id_externo': '9999001',
    'titulo': 'PLP 300/2026',
    'casa': 'camara',
    'ementa': 'Altera a Lei Complementar nº 214, de 2025, sobre a CBS e o IBS.',
    'link': 'https://www.camara.leg.br/proposicoesWeb/fichadetramitacao?idProposicao=9999001',
    'termo': 'CBS',
}


def _patch_buscar(candidatos):
    return mock.patch('apps.sentinela.fontes.buscar', return_value=candidatos)


class SentinelaTest(TestCase):
    def setUp(self):
        # time do Legislativo que deve receber os alertas
        self.membro = Usuario.objects.create_user(username='leg', email='leg@fnp.org.br', password='x')
        self.membro.perfil.classe_usuario = Perfil.EQUIPE_FNP
        self.membro.perfil.save()
        self.admin = Usuario.objects.create_user(username='root', email='root@fnp.org.br', password='x', is_staff=True)
        # usuário público não deve receber
        self.publico = Usuario.objects.create_user(username='pub', email='pub@cidade.gov.br', password='x')
        self.publico.perfil.classe_usuario = Perfil.PREFEITO
        self.publico.perfil.save()
        mail.outbox = []

    def test_cria_pendente_e_alerta_o_leg(self):
        with _patch_buscar([CAND]):
            call_command('sentinela_tributaria')
        prop = Proposicao.objects.get(titulo='PLP 300/2026')
        self.assertEqual(prop.status_publicacao, Proposicao.PENDENTE)
        self.assertEqual(prop.link, CAND['link'])
        # sino para o membro e o admin, não para o público
        notifs = Notificacao.objects.filter(tipo=Notificacao.TIPO_SENTINELA, proposicao=prop)
        destinatarios = set(notifs.values_list('destinatario_id', flat=True))
        self.assertEqual(destinatarios, {self.membro.id, self.admin.id})
        # e-mail enviado (um envio para a lista do time)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('leg@fnp.org.br', mail.outbox[0].to)
        self.assertIn('root@fnp.org.br', mail.outbox[0].to)
        self.assertNotIn('pub@cidade.gov.br', mail.outbox[0].to)
        self.assertTrue(ItemSentinelaVisto.objects.filter(id_externo='9999001').exists())

    def test_baseline_nao_alerta(self):
        with _patch_buscar([CAND]):
            call_command('sentinela_tributaria', baseline=True)
        self.assertTrue(ItemSentinelaVisto.objects.filter(id_externo='9999001').exists())
        self.assertFalse(Proposicao.objects.filter(titulo='PLP 300/2026').exists())
        self.assertEqual(Notificacao.objects.filter(tipo=Notificacao.TIPO_SENTINELA).count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_nao_realerta_item_ja_visto(self):
        ItemSentinelaVisto.objects.create(fonte='camara', id_externo='9999001', titulo='PLP 300/2026')
        with _patch_buscar([CAND]):
            call_command('sentinela_tributaria')
        self.assertFalse(Proposicao.objects.filter(titulo='PLP 300/2026').exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_ja_no_painel_nao_duplica_nem_alerta(self):
        Proposicao.objects.create(titulo='PLP 300/2026', casa='camara', link=CAND['link'])
        with _patch_buscar([CAND]):
            call_command('sentinela_tributaria')
        self.assertEqual(Proposicao.objects.filter(titulo='PLP 300/2026').count(), 1)
        self.assertEqual(Notificacao.objects.filter(tipo=Notificacao.TIPO_SENTINELA).count(), 0)
        # marcou como visto para não reprocessar
        self.assertTrue(ItemSentinelaVisto.objects.filter(id_externo='9999001').exists())

    def test_dry_run_nao_grava(self):
        with _patch_buscar([CAND]):
            call_command('sentinela_tributaria', dry_run=True)
        self.assertFalse(Proposicao.objects.filter(titulo='PLP 300/2026').exists())
        self.assertFalse(ItemSentinelaVisto.objects.filter(id_externo='9999001').exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_sem_termos_ativos_nao_faz_nada(self):
        TermoSentinela.objects.update(ativo=False)
        with _patch_buscar([CAND]) as m:
            call_command('sentinela_tributaria')
        m.assert_not_called()
        self.assertFalse(Proposicao.objects.filter(titulo='PLP 300/2026').exists())

    def test_termos_seed_carregados(self):
        self.assertTrue(TermoSentinela.objects.filter(termo='CBS', ativo=True).exists())
        self.assertTrue(TermoSentinela.objects.filter(termo='IBS', ativo=True).exists())


class RadarTributarioViewTest(TestCase):
    def setUp(self):
        cache.clear()
        self.equipe = Usuario.objects.create_user(username='eq', email='eq@fnp.org.br', password='x')
        self.equipe.perfil.classe_usuario = Perfil.EQUIPE_FNP
        self.equipe.perfil.status_aprovacao = Perfil.APROVADO
        self.equipe.perfil.save()
        self.publico = Usuario.objects.create_user(username='pb', email='pb@cidade.gov.br', password='x')
        self.publico.perfil.classe_usuario = Perfil.PREFEITO
        self.publico.perfil.status_aprovacao = Perfil.APROVADO
        self.publico.perfil.save()
        self.url = reverse('legislativo:radar_tributario')

    def test_equipe_ve_a_aba_com_termos(self):
        self.client.force_login(self.equipe)
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Radar Tributário')
        self.assertContains(resp, 'CBS')  # termo do seed

    def test_publico_bloqueado(self):
        self.client.force_login(self.publico)
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 403)

    def test_cadastrar_tema(self):
        self.client.force_login(self.equipe)
        resp = self.client.post(self.url, {'acao': 'add', 'termo': 'ICMS partilha'})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(TermoSentinela.objects.filter(termo='ICMS partilha', ativo=True).exists())

    def test_remover_tema(self):
        t = TermoSentinela.objects.create(termo='tema para remover')
        self.client.force_login(self.equipe)
        self.client.post(self.url, {'acao': 'remove', 'termo_id': t.pk})
        self.assertFalse(TermoSentinela.objects.filter(pk=t.pk).exists())

    def test_ve_alertas_capturados(self):
        prop = Proposicao.objects.create(
            titulo='PLP 300/2026', casa='camara',
            link='https://www.camara.leg.br/x', status_publicacao=Proposicao.PENDENTE,
        )
        ItemSentinelaVisto.objects.create(
            fonte='camara', id_externo='9', titulo='PLP 300/2026',
            termo='CBS', proposicao=prop, alertado=True,
        )
        self.client.force_login(self.equipe)
        resp = self.client.get(self.url)
        self.assertContains(resp, 'PLP 300/2026')

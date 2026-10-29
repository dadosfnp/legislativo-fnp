import base64
import io
import json
import tempfile
from unittest import mock

from django.contrib import admin
from django.contrib.auth.models import Group
from django.contrib.sessions.middleware import SessionMiddleware
from django.core import mail
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.http import Http404, HttpResponse
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from PIL import Image

from apps.comentarios.models import Comentario, ComentarioLike, PalavraProibida
from apps.comentarios.moderacao import classificar_comentario, contem_palavra_proibida
from apps.proposicoes.models import AnexoProposicao, EdicaoMeritoHistorico, Macrotema, Noticia, Proposicao, Tema
from apps.usuarios.middleware import CadastroPendenteMiddleware, MFAObrigatorioStaffMiddleware
from apps.usuarios.models import Perfil, Usuario

from .forms import ComentarioForm
from .throttling import _client_ip, rate_limited
from .views import (
    PROPOSICOES_POR_PAGINA,
    HomeView,
    ParticipacaoListView,
    PerfilView,
    ProposicaoDetailView,
    curtir_comentario,
    denunciar_comentario,
    excluir_comentario,
    get_filtered_proposicoes,
    get_home_sections,
    solicitar_exclusao,
    usuario_publico,
)


def _request_with_session(path):
    request = RequestFactory().get(path)
    SessionMiddleware(lambda req: None).process_request(request)
    request.session.save()
    return request


def _post_request_with_session(path, data=None):
    request = RequestFactory().post(path, data=data or {})
    SessionMiddleware(lambda req: None).process_request(request)
    request.session.save()
    return request


class ProposicaoModelTest(TestCase):
    def test_criar_proposicao_basica(self):
        proposicao = Proposicao.objects.create(
            titulo='Teste de proposição',
            casa='camara',
            status_tramitacao='Em tramitação',
        )
        self.assertEqual(proposicao.prioridade_fnp, 'normal')
        self.assertEqual(str(proposicao), 'Teste de proposição')


class HomeViewRegressionTest(TestCase):
    def setUp(self):
        macrotema = Macrotema.objects.create(nome='Infraestrutura', slug='infraestrutura', cor='#1A4B8F')
        tema = Tema.objects.create(nome='Transporte', slug='transporte', macrotema=macrotema)
        proposicao1 = Proposicao.objects.create(
            titulo='PL 1234/2025 - Financiamento do Transporte Público',
            casa='camara',
            status_tramitacao='Em tramitação',
            pauta=True,
            urgente=True,
            prioridade_fnp='alta',
            macrotema=macrotema,
            ementa_resumida='Projeto de lei que trata do financiamento do transporte público.',
        )
        proposicao1.temas.set([tema])
        proposicao2 = Proposicao.objects.create(
            titulo='PL 5678/2025 - Fortalecimento do Sistema Municipal',
            casa='senado',
            status_tramitacao='Aguardando parecer',
            pauta=False,
            urgente=False,
            prioridade_fnp='media',
            macrotema=macrotema,
            ementa_resumida='Projeto que visa ampliar a atuação dos municípios.',
        )
        proposicao2.temas.set([tema])

    def test_home_view_renders_briefing_cards_with_count_stats(self):
        request = _request_with_session(reverse('legislativo:home'))
        response = HomeView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertIn('card-brief', content)
        self.assertIn('PL 1234/2025 - Financiamento do Transporte Público', content)
        self.assertIn('stats-total', content)
        self.assertIn('stats-pauta', content)


class UsuarioDisplayNameTest(TestCase):
    def test_usa_nome_completo_quando_disponivel(self):
        usuario = Usuario.objects.create(
            username='bruno', email='bruno.marra@fnp.org.br', first_name='Bruno', last_name='Marra',
        )
        self.assertEqual(usuario.get_display_name(), 'Bruno Marra')

    def test_deriva_do_email_quando_sem_nome_cadastrado(self):
        usuario = Usuario.objects.create(username='joana.silva', email='joana.silva@fnp.org.br')
        self.assertEqual(usuario.get_display_name(), 'Joana Silva')


class PerfilSignalTest(TestCase):
    def test_novo_usuario_ganha_perfil_automaticamente(self):
        usuario = Usuario.objects.create(username='ana', email='ana@fnp.org.br')
        self.assertIsNotNone(usuario.perfil)

    def test_cadastro_pendente_notifica_equipe_por_email(self):
        usuario = Usuario.objects.create(
            username='novo.cadastro', email='novo.cadastro@fnp.org.br', first_name='Novo', last_name='Cadastro',
        )
        self.assertEqual(len(mail.outbox), 1)
        enviado = mail.outbox[0]
        self.assertIn('ronan.castro@fnp.org.br', enviado.to)
        self.assertIn('nucleo.dados@fnp.org.br', enviado.to)
        self.assertIn('Novo Cadastro', enviado.body)
        self.assertIn(reverse('admin:usuarios_usuario_change', args=[usuario.pk]), enviado.body)

    def test_cadastro_de_staff_nao_dispara_email(self):
        Usuario.objects.create(username='equipe', email='equipe@fnp.org.br', is_staff=True)
        self.assertEqual(len(mail.outbox), 0)


class PerfilViewTest(TestCase):
    def test_perfil_view_renderiza_dados_de_usuario_e_de_perfil(self):
        usuario = Usuario.objects.create(
            username='bruno', email='bruno.marra@fnp.org.br', first_name='Bruno', last_name='Marra',
        )
        usuario.perfil.telefone = '(61) 99999-0000'
        usuario.perfil.cargo = 'Coordenador'
        usuario.perfil.save()

        request = _request_with_session(reverse('legislativo:perfil'))
        request.user = usuario
        response = PerfilView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertIn('(61) 99999-0000', content)
        self.assertIn('Coordenador', content)


class UsuarioGroupSignalTest(TestCase):
    def test_novo_usuario_entra_no_grupo_usuario(self):
        usuario = Usuario.objects.create(username='novo', email='novo@fnp.org.br')
        self.assertTrue(usuario.groups.filter(name='Usuário').exists())

    def test_superusuario_nao_entra_no_grupo_usuario(self):
        usuario = Usuario.objects.create(username='root2', email='root2@fnp.org.br', is_superuser=True)
        self.assertFalse(usuario.groups.filter(name='Usuário').exists())


class SetupRolesCommandTest(TestCase):
    def test_comando_e_idempotente_e_promove_root(self):
        Usuario.objects.create(username='bruno', email='bruno.marra@fnp.org.br')

        call_command('setup_roles')
        call_command('setup_roles')  # rodar duas vezes não deve duplicar grupo nem quebrar

        self.assertEqual(Group.objects.filter(name='Root').count(), 1)
        usuario = Usuario.objects.get(email='bruno.marra@fnp.org.br')
        self.assertTrue(usuario.is_superuser)
        self.assertTrue(usuario.is_staff)
        self.assertTrue(usuario.groups.filter(name='Root').exists())


class EdicaoMeritoHistoricoIngestTest(TestCase):
    def test_reingestao_com_campo_de_merito_alterado_grava_historico(self):
        registro = [{'Proposição': 'PL 999/2025', 'Casa': 'Camara', 'Posicionamento da FNP': 'Posição inicial'}]
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8') as arquivo:
            json.dump(registro, arquivo)
            caminho = arquivo.name

        call_command('ingest_legislativo', caminho)
        self.assertEqual(EdicaoMeritoHistorico.objects.count(), 0)  # criação inicial não gera histórico

        registro[0]['Posicionamento da FNP'] = 'Posição revisada'
        with open(caminho, 'w', encoding='utf-8') as arquivo:
            json.dump(registro, arquivo)

        call_command('ingest_legislativo', caminho)

        historico = EdicaoMeritoHistorico.objects.get()
        self.assertEqual(historico.campo, 'posicionamento_fnp')
        self.assertEqual(historico.valor_anterior, 'Posição inicial')
        self.assertEqual(historico.valor_novo, 'Posição revisada')
        self.assertIsNone(historico.autor)


class ModeracaoAutomaticaTest(TestCase):
    def test_comentario_normal_e_aprovado(self):
        self.assertEqual(classificar_comentario('Ótima iniciativa, parabéns!'), 'aprovado')

    def test_comentario_com_palavra_proibida_e_reprovado(self):
        PalavraProibida.objects.create(palavra='droga')
        self.assertEqual(classificar_comentario('Isso é uma droga de proposta'), 'rejeitado')

    def test_checagem_ignora_acento_e_caixa(self):
        PalavraProibida.objects.create(palavra='droga')
        self.assertTrue(contem_palavra_proibida('Isso é uma DRÓGA de proposta'))

    def test_nao_bloqueia_substring_dentro_de_outra_palavra(self):
        PalavraProibida.objects.create(palavra='droga')
        self.assertFalse(contem_palavra_proibida('Precisamos de uma drogaria no bairro'))

    def test_palavra_inativa_nao_bloqueia(self):
        PalavraProibida.objects.create(palavra='droga', ativa=False)
        self.assertEqual(classificar_comentario('isso é uma droga'), 'aprovado')


class CadastroPendenteMiddlewareTest(TestCase):
    def _passa_pela_middleware(self, usuario, path='/qualquer-pagina/'):
        request = RequestFactory().get(path)
        request.user = usuario
        middleware = CadastroPendenteMiddleware(lambda req: HttpResponse('ok'))
        return middleware(request)

    def test_usuario_pendente_e_redirecionado(self):
        usuario = Usuario.objects.create(username='pendente1', email='pendente1@fnp.org.br')
        response = self._passa_pela_middleware(usuario)
        self.assertEqual(response.status_code, 302)
        self.assertIn('cadastro-pendente', response.url)

    def test_usuario_aprovado_passa_direto(self):
        usuario = Usuario.objects.create(username='aprovado1', email='aprovado1@fnp.org.br')
        usuario.perfil.status_aprovacao = Perfil.APROVADO
        usuario.perfil.save()
        response = self._passa_pela_middleware(usuario)
        self.assertEqual(response.status_code, 200)

    def test_staff_passa_direto_mesmo_com_cadastro_pendente(self):
        usuario = Usuario.objects.create(username='staffx', email='staffx@fnp.org.br', is_staff=True)
        usuario.perfil.status_aprovacao = Perfil.PENDENTE
        usuario.perfil.save()
        response = self._passa_pela_middleware(usuario)
        self.assertEqual(response.status_code, 200)


class MFAObrigatorioStaffMiddlewareTest(TestCase):
    def _passa_pela_middleware(self, usuario, path='/qualquer-pagina/'):
        request = RequestFactory().get(path)
        request.user = usuario
        middleware = MFAObrigatorioStaffMiddleware(lambda req: HttpResponse('ok'))
        return middleware(request)

    def test_staff_sem_2fa_e_redirecionado_pra_ativacao(self):
        usuario = Usuario.objects.create(username='staffsem2fa', email='staffsem2fa@fnp.org.br', is_staff=True)
        response = self._passa_pela_middleware(usuario)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/2fa/', response.url)

    def test_staff_com_2fa_passa_direto(self):
        from allauth.mfa.models import Authenticator

        usuario = Usuario.objects.create(username='staffcom2fa', email='staffcom2fa@fnp.org.br', is_staff=True)
        Authenticator.objects.create(user=usuario, type=Authenticator.Type.TOTP, data={})
        response = self._passa_pela_middleware(usuario)
        self.assertEqual(response.status_code, 200)

    def test_usuario_comum_sem_2fa_nao_e_afetado(self):
        usuario = Usuario.objects.create(username='comumsem2fa', email='comumsem2fa@fnp.org.br', is_staff=False)
        response = self._passa_pela_middleware(usuario)
        self.assertEqual(response.status_code, 200)

    def test_pagina_de_ativacao_do_2fa_fica_liberada_pro_staff_sem_2fa(self):
        usuario = Usuario.objects.create(username='staffativando', email='staffativando@fnp.org.br', is_staff=True)
        response = self._passa_pela_middleware(usuario, path=reverse('mfa_activate_totp'))
        self.assertEqual(response.status_code, 200)


class DenunciaComentarioTest(TestCase):
    def setUp(self):
        self.proposicao = Proposicao.objects.create(titulo='PL teste denúncia', casa='camara')
        self.autor = Usuario.objects.create(username='autor_d', email='autor_d@fnp.org.br')
        self.comentario = Comentario.objects.create(
            proposicao=self.proposicao, texto='comentário', autor=self.autor, status_moderacao='aprovado',
        )

    def _denunciar(self, usuario):
        request = _post_request_with_session(f'/comentario/{self.comentario.pk}/denunciar/')
        request.user = usuario
        return denunciar_comentario(request, pk=self.comentario.pk)

    def test_denuncia_unica_nao_oculta_comentario(self):
        denunciante = Usuario.objects.create(username='d1', email='d1@fnp.org.br')
        self._denunciar(denunciante)
        self.comentario.refresh_from_db()
        self.assertEqual(self.comentario.status_moderacao, 'aprovado')
        self.assertEqual(self.comentario.denuncias.count(), 1)

    def test_atingir_limite_de_denuncias_oculta_comentario(self):
        for i in range(Comentario.DENUNCIAS_PARA_OCULTAR):
            denunciante = Usuario.objects.create(username=f'd{i}', email=f'd{i}@fnp.org.br')
            self._denunciar(denunciante)
        self.comentario.refresh_from_db()
        self.assertEqual(self.comentario.status_moderacao, 'pendente')

    def test_mesmo_usuario_nao_denuncia_duas_vezes(self):
        denunciante = Usuario.objects.create(username='drepetido', email='drepetido@fnp.org.br')
        self._denunciar(denunciante)
        self._denunciar(denunciante)
        self.comentario.refresh_from_db()
        self.assertEqual(self.comentario.denuncias.count(), 1)


class LGPDTest(TestCase):
    def test_solicitar_exclusao_marca_data_no_perfil(self):
        usuario = Usuario.objects.create(username='lgpd2', email='lgpd2@fnp.org.br')
        self.assertIsNone(usuario.perfil.exclusao_solicitada_em)

        request = _post_request_with_session(reverse('legislativo:solicitar_exclusao'))
        request.user = usuario
        response = solicitar_exclusao(request)

        self.assertEqual(response.status_code, 200)
        usuario.perfil.refresh_from_db()
        self.assertIsNotNone(usuario.perfil.exclusao_solicitada_em)


class ExportarDadosEngajamentoTest(TestCase):
    """Exportação em massa/por usuário pro Root -- diferente da exportação
    de LGPD (LGPDTest acima), que cada usuário faz só dos próprios dados."""

    def setUp(self):
        self.root = Usuario.objects.create(
            username='rootexp', email='rootexp@fnp.org.br', is_staff=True, is_superuser=True,
        )
        self.staff_comum = Usuario.objects.create(
            username='staffexp', email='staffexp@fnp.org.br', is_staff=True,
        )
        self.usuario = Usuario.objects.create(username='alvoexp', email='alvoexp@fnp.org.br', first_name='Alvo')

    def test_nao_superuser_recebe_permission_denied(self):
        from django.core.exceptions import PermissionDenied

        from apps.usuarios.admin_views import exportar_dados_view

        request = _request_with_session('/admin/exportar-dados/')
        request.user = self.staff_comum
        with self.assertRaises(PermissionDenied):
            exportar_dados_view(request)

    def test_superuser_exportacao_em_massa_inclui_todos_os_usuarios(self):
        from apps.usuarios.admin_views import exportar_dados_view

        request = _post_request_with_session('/admin/exportar-dados/', {'modo': 'massa'})
        request.user = self.root
        response = exportar_dados_view(request)

        self.assertEqual(response.status_code, 200)
        dados = json.loads(response.content)
        self.assertEqual(dados['total_usuarios'], Usuario.objects.count())
        self.assertIn('attachment', response['Content-Disposition'])

    def test_superuser_exportacao_de_um_usuario_especifico(self):
        from apps.usuarios.admin_views import exportar_dados_view

        request = _post_request_with_session(
            '/admin/exportar-dados/', {'modo': 'usuario', 'usuario_id': self.usuario.pk},
        )
        request.user = self.root
        response = exportar_dados_view(request)

        dados = json.loads(response.content)
        self.assertEqual(dados['total_usuarios'], 1)
        self.assertEqual(dados['usuarios'][0]['cadastro']['email'], 'alvoexp@fnp.org.br')


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class AvatarUrlTest(TestCase):
    def test_sem_foto_retorna_none(self):
        usuario = Usuario.objects.create(username='av1', email='av1@fnp.org.br')
        self.assertIsNone(usuario.get_avatar_url())

    def test_foto_google_e_usada_como_fallback(self):
        usuario = Usuario.objects.create(username='av2', email='av2@fnp.org.br')
        usuario.perfil.foto_google_url = 'https://exemplo.com/foto.jpg'
        usuario.perfil.save()
        self.assertEqual(usuario.get_avatar_url(), 'https://exemplo.com/foto.jpg')

    def test_foto_manual_tem_prioridade_sobre_a_do_google(self):
        usuario = Usuario.objects.create(username='av3', email='av3@fnp.org.br')
        usuario.perfil.foto_google_url = 'https://exemplo.com/foto.jpg'
        buffer = io.BytesIO()
        Image.new('RGB', (2, 2)).save(buffer, format='PNG')
        usuario.perfil.foto.save('avatar.png', SimpleUploadedFile('avatar.png', buffer.getvalue()), save=True)

        self.assertIn('avatar', usuario.get_avatar_url())
        self.assertNotEqual(usuario.get_avatar_url(), 'https://exemplo.com/foto.jpg')

    def test_foto_maior_que_o_limite_e_rejeitada(self):
        from django.core.exceptions import ValidationError

        from apps.usuarios.models import FOTO_PERFIL_TAMANHO_MAXIMO_MB, validar_tamanho_foto_perfil

        usuario = Usuario.objects.create(username='av4', email='av4@fnp.org.br')
        buffer = io.BytesIO()
        Image.new('RGB', (2, 2)).save(buffer, format='PNG')
        arquivo = SimpleUploadedFile('avatar.png', buffer.getvalue())
        arquivo.size = FOTO_PERFIL_TAMANHO_MAXIMO_MB * 1024 * 1024 + 1  # simula arquivo grande sem gerar bytes reais

        with self.assertRaises(ValidationError):
            validar_tamanho_foto_perfil(arquivo)

        usuario.perfil.foto = arquivo
        with self.assertRaises(ValidationError):
            usuario.perfil.full_clean()


class CustomSignupFormCaptchaTest(TestCase):
    def test_sem_chaves_configuradas_formulario_nao_tem_campo_captcha(self):
        from .forms import CustomSignupForm

        self.assertNotIn('captcha', CustomSignupForm().fields)

    @override_settings(RECAPTCHA_HABILITADO=True)
    def test_com_chaves_configuradas_formulario_ganha_campo_captcha(self):
        from .forms import CustomSignupForm

        self.assertIn('captcha', CustomSignupForm().fields)


class GoogleSignupDomainRestrictionTest(TestCase):
    """Cadastro novo via Google só é aberto pra e-mail @fnp.org.br -- login
    de quem já tem conta não passa por is_open_for_signup (ver docstring
    do adapter), então não é coberto/afetado por este teste."""

    class _ContaSocialFalsa:
        def __init__(self, email):
            self.user = type('UsuarioFalso', (), {'email': email})()
            self.account = type('SocialAccountFalso', (), {'extra_data': {'email': email}})()

    def test_email_institucional_pode_se_cadastrar(self):
        from apps.usuarios.adapters import GoogleAccountAdapter

        adapter = GoogleAccountAdapter()
        sociallogin = self._ContaSocialFalsa('funcionario@fnp.org.br')
        self.assertTrue(adapter.is_open_for_signup(None, sociallogin))

    def test_email_nao_institucional_nao_pode_se_cadastrar(self):
        from apps.usuarios.adapters import GoogleAccountAdapter

        adapter = GoogleAccountAdapter()
        sociallogin = self._ContaSocialFalsa('prefeito@algummunicipio.gov.br')
        self.assertFalse(adapter.is_open_for_signup(None, sociallogin))


class UsernamePadraoTest(TestCase):
    """Username de cadastro novo segue "nome.sobrenome" (ex.: Bruno Marra ->
    bruno.marra), mesmo padrão das contas já cadastradas manualmente --
    sem isso o allauth usaria só o primeiro nome."""

    def test_nome_e_sobrenome_viram_nome_ponto_sobrenome(self):
        from apps.usuarios.adapters import CustomAccountAdapter

        usuario = Usuario(first_name='Bruno', last_name='Marra', email='brunoteste@fnp.org.br')
        CustomAccountAdapter().populate_username(None, usuario)
        self.assertEqual(usuario.username, 'bruno.marra')

    def test_acentos_sao_removidos(self):
        from apps.usuarios.adapters import CustomAccountAdapter

        usuario = Usuario(first_name='José', last_name='Andrade', email='joseandrade@fnp.org.br')
        CustomAccountAdapter().populate_username(None, usuario)
        self.assertEqual(usuario.username, 'jose.andrade')

    def test_username_ja_existente_ganha_sufixo_unico(self):
        from apps.usuarios.adapters import CustomAccountAdapter

        Usuario.objects.create(username='bruno.marra', email='brunooriginal@fnp.org.br')
        usuario = Usuario(first_name='Bruno', last_name='Marra', email='brunosegundo@fnp.org.br')
        CustomAccountAdapter().populate_username(None, usuario)
        self.assertNotEqual(usuario.username, 'bruno.marra')
        self.assertTrue(usuario.username.startswith('bruno.marra'))

    def test_sem_sobrenome_cai_no_padrao_do_allauth(self):
        from apps.usuarios.adapters import CustomAccountAdapter

        usuario = Usuario(first_name='Nucleo', last_name='', email='nucleo2@fnp.org.br')
        CustomAccountAdapter().populate_username(None, usuario)
        self.assertEqual(usuario.username, 'nucleo')


class ClasseUsuarioTest(TestCase):
    """Classe do usuário (Equipe FNP/Prefeito/Indicado da prefeitura/Parlamentar)
    -- classificada automaticamente no cadastro a partir do cargo informado
    (nunca escolhida manualmente), estática em /perfil/ (só o Root/
    Administrador FNP altera, via Django Admin)."""

    def _dados_signup(self, **overrides):
        dados = {
            'first_name': 'Maria',
            'last_name': 'Silva',
            'municipio': 'Cidade Teste',
            'uf': 'SP',
            'setor_responsavel': 'Gabinete',
            'cargo': 'Prefeita',
            'telefone': '11999999999',
            'email': 'maria.silva@exemplo.com',
            'password1': 'senha-segura-123',
            'password2': 'senha-segura-123',
        }
        dados.update(overrides)
        return dados

    def test_signup_classifica_prefeito_pelo_cargo(self):
        response = self.client.post(reverse('account_signup'), self._dados_signup())
        self.assertEqual(response.status_code, 302)
        usuario = Usuario.objects.get(email='maria.silva@exemplo.com')
        self.assertEqual(usuario.perfil.classe_usuario, Perfil.PREFEITO)

    def test_signup_classifica_parlamentar_pelo_cargo(self):
        dados = self._dados_signup(
            email='joao.deputado@exemplo.com', cargo='Deputado Estadual',
        )
        response = self.client.post(reverse('account_signup'), dados)
        self.assertEqual(response.status_code, 302)
        usuario = Usuario.objects.get(email='joao.deputado@exemplo.com')
        self.assertEqual(usuario.perfil.classe_usuario, Perfil.PARLAMENTAR)

    def test_signup_classifica_indicado_da_prefeitura_por_padrao(self):
        dados = self._dados_signup(
            email='ana.secretaria@exemplo.com', cargo='Secretária de Educação',
        )
        response = self.client.post(reverse('account_signup'), dados)
        self.assertEqual(response.status_code, 302)
        usuario = Usuario.objects.get(email='ana.secretaria@exemplo.com')
        self.assertEqual(usuario.perfil.classe_usuario, Perfil.INDICADO_PREFEITURA)

    def test_classe_usuario_nao_e_campo_do_formulario_publico(self):
        from .forms import CustomSignupForm

        self.assertNotIn('classe_usuario', CustomSignupForm().fields)

    def test_usuario_nao_consegue_alterar_a_propria_classe_em_perfil(self):
        usuario = Usuario.objects.create_user(username='parla', email='parla@exemplo.com', password='x')
        usuario.perfil.classe_usuario = Perfil.INDICADO_PREFEITURA
        usuario.perfil.status_aprovacao = Perfil.APROVADO
        usuario.perfil.save()
        self.client.force_login(usuario)

        response = self.client.post(reverse('legislativo:perfil'), {
            'first_name': usuario.first_name,
            'last_name': usuario.last_name,
            'email': usuario.email,
            'classe_usuario': Perfil.PARLAMENTAR,
            'telefone': '',
            'cargo': '',
            'setor_responsavel': '',
            'municipio_nome': '',
            'municipio_uf': '',
        })
        self.assertEqual(response.status_code, 200)
        usuario.perfil.refresh_from_db()
        self.assertEqual(usuario.perfil.classe_usuario, Perfil.INDICADO_PREFEITURA)

    def test_perfil_de_acesso_aparece_estatico_no_template(self):
        usuario = Usuario.objects.create_user(username='estatico', email='estatico@exemplo.com', password='x')
        usuario.perfil.classe_usuario = Perfil.PREFEITO
        usuario.perfil.status_aprovacao = Perfil.APROVADO
        usuario.perfil.save()
        self.client.force_login(usuario)

        response = self.client.get(reverse('legislativo:perfil'))

        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertIn('Perfil de acesso', content)
        self.assertIn('field-static', content)
        self.assertNotIn('name="classe_usuario"', content)

    def test_admin_filtra_por_classe_de_usuario(self):
        from apps.usuarios.admin import UsuarioAdmin

        self.assertIn('perfil__classe_usuario', UsuarioAdmin.list_filter)


class ClassificarPorCargoTest(TestCase):
    """apps.usuarios.classificacao.classificar_por_cargo -- heurística de
    palavra-chave (fronteira de palavra, sem acento/caixa) que decide
    o Perfil de acesso a partir do texto livre do cargo."""

    def test_prefeito_e_prefeita(self):
        from apps.usuarios.classificacao import classificar_por_cargo

        self.assertEqual(classificar_por_cargo('Prefeito'), Perfil.PREFEITO)
        self.assertEqual(classificar_por_cargo('Prefeita Municipal'), Perfil.PREFEITO)

    def test_deputado_senador_vereador(self):
        from apps.usuarios.classificacao import classificar_por_cargo

        self.assertEqual(classificar_por_cargo('Deputado Federal'), Perfil.PARLAMENTAR)
        self.assertEqual(classificar_por_cargo('Senadora'), Perfil.PARLAMENTAR)
        self.assertEqual(classificar_por_cargo('Vereador'), Perfil.PARLAMENTAR)

    def test_cargo_generico_cai_no_padrao_indicado_da_prefeitura(self):
        from apps.usuarios.classificacao import classificar_por_cargo

        self.assertEqual(classificar_por_cargo('Secretário de Saúde'), Perfil.INDICADO_PREFEITURA)
        self.assertEqual(classificar_por_cargo(''), Perfil.INDICADO_PREFEITURA)
        self.assertEqual(classificar_por_cargo(None), Perfil.INDICADO_PREFEITURA)

    def test_nunca_classifica_como_equipe_fnp(self):
        from apps.usuarios.classificacao import classificar_por_cargo

        self.assertNotEqual(classificar_por_cargo('Equipe FNP'), Perfil.EQUIPE_FNP)


class RateLimitTest(TestCase):
    def setUp(self):
        cache.clear()

    def test_bloqueia_apos_atingir_o_limite(self):
        request = _post_request_with_session('/qualquer/')
        for _ in range(3):
            self.assertFalse(rate_limited('teste', request, limit=3, window_seconds=60))
        self.assertTrue(rate_limited('teste', request, limit=3, window_seconds=60))

    def test_prefixos_diferentes_nao_compartilham_limite(self):
        request = _post_request_with_session('/qualquer/')
        for _ in range(3):
            self.assertFalse(rate_limited('comentario', request, limit=3, window_seconds=60))
        self.assertFalse(rate_limited('participacao', request, limit=3, window_seconds=60))


class PaginacaoHomeTest(TestCase):
    def test_home_pagina_quando_mais_de_uma_pagina_de_resultado(self):
        for i in range(PROPOSICOES_POR_PAGINA + 5):
            Proposicao.objects.create(titulo=f'PL paginação {i}', casa='camara')

        sections = get_home_sections('', '', page_number=1)

        self.assertEqual(sections['page_obj'].paginator.num_pages, 2)
        self.assertEqual(len(sections['proposicoes']), PROPOSICOES_POR_PAGINA)


class AcessoAdminViaGrupoTest(TestCase):
    """Adicionar alguém a Administrador FNP/Root pelo widget de grupos do
    Admin precisa conceder is_staff sozinho -- sem isso a pessoa nem
    conseguia logar em /admin/, e a sidebar do site nunca mostrava o link
    "Painel Admin" (que checava só is_superuser antes desta correção)."""

    def test_adicionar_ao_grupo_administrador_fnp_concede_staff(self):
        call_command('setup_roles')
        usuario = Usuario.objects.create(username='novoadm', email='novoadm@fnp.org.br')
        self.assertFalse(usuario.is_staff)

        grupo = Group.objects.get(name='Administrador FNP')
        usuario.groups.add(grupo)

        usuario.refresh_from_db()
        self.assertTrue(usuario.is_staff)

    def test_usuario_comum_sem_grupo_admin_nao_ganha_staff(self):
        usuario = Usuario.objects.create(username='comumx', email='comumx@fnp.org.br')
        self.assertFalse(usuario.is_staff)


class HomeFiltroEstatisticaTest(TestCase):
    def setUp(self):
        self.pauta = Proposicao.objects.create(titulo='PL na pauta', casa='camara', pauta=True)
        Proposicao.objects.create(titulo='PL fora da pauta', casa='camara', pauta=False)

    def test_filtro_pauta_retorna_so_proposicoes_na_pauta(self):
        proposicoes = get_filtered_proposicoes('', '', filtro='pauta')
        self.assertEqual(list(proposicoes), [self.pauta])

    def test_home_view_marca_filtro_ativo_no_contexto(self):
        request = _request_with_session(reverse('legislativo:home') + '?filtro=pauta')
        response = HomeView.as_view()(request)
        content = response.content.decode('utf-8')
        self.assertIn('Na pauta (1)', content)
        self.assertIn('Limpar filtro', content)
        # Com filtro ativo, as seções fixas (Urgentes/Em alta) somem da página.
        self.assertNotIn('id="cards-urgentes"', content)


class HomeFiltroPorMacrotemaTest(TestCase):
    def test_tema_slug_casa_com_macrotema_tambem(self):
        macrotema = Macrotema.objects.create(nome='Meio Ambiente', slug='meio-ambiente', cor='#15803d')
        proposicao = Proposicao.objects.create(titulo='PL ambiental', casa='camara', macrotema=macrotema)
        Proposicao.objects.create(titulo='PL qualquer', casa='camara')

        proposicoes = get_filtered_proposicoes('', 'meio-ambiente')
        self.assertEqual(list(proposicoes), [proposicao])


class RespostaAninhadaTest(TestCase):
    """O model já suportava profundidade ilimitada via parent -- o que
    faltava era o template renderizar recursivamente e permitir responder
    a uma resposta (não só a comentários de primeiro nível)."""

    def test_resposta_de_resposta_aparece_na_pagina(self):
        proposicao = Proposicao.objects.create(titulo='PL teste aninhamento', casa='camara')
        autor = Usuario.objects.create(username='autoresq', email='autoresq@fnp.org.br')
        raiz = Comentario.objects.create(
            proposicao=proposicao, texto='comentário raiz', autor=autor, status_moderacao='aprovado',
        )
        resposta1 = Comentario.objects.create(
            proposicao=proposicao, texto='primeira resposta', autor=autor, parent=raiz, status_moderacao='aprovado',
        )
        Comentario.objects.create(
            proposicao=proposicao, texto='resposta da resposta', autor=autor, parent=resposta1,
            status_moderacao='aprovado',
        )

        request = _request_with_session(f'/proposicao/{proposicao.pk}/')
        request.user = autor
        response = ProposicaoDetailView.as_view()(request, pk=proposicao.pk)
        content = response.content.decode('utf-8')

        self.assertIn('primeira resposta', content)
        self.assertIn('resposta da resposta', content)
        # "Responder" precisa existir também dentro do bloco aninhado, não só
        # no comentário raiz -- checa que aparece mais de uma vez.
        self.assertGreater(content.count('data-parent-id="'), 1)


class NoticiasRelacionadasTest(TestCase):
    """Seção "Notícias relacionadas" na página da proposição -- reaproveita o
    model Noticia (já existente, sem migration nova) e o filtro `dominio`
    (apps.legislativo.templatetags.legislativo_extras)."""

    def test_noticia_aparece_com_link_clicavel_e_badge_de_dominio(self):
        proposicao = Proposicao.objects.create(titulo='PL com notícia', casa='camara')
        Noticia.objects.create(
            proposicao=proposicao,
            titulo='Câmara aprova projeto',
            url='https://www.folhavitoria.com.br/materia-x',
        )
        request = _request_with_session(f'/proposicao/{proposicao.pk}/')
        request.user = Usuario.objects.create(username='leitornoticia', email='leitornoticia@fnp.org.br')
        response = ProposicaoDetailView.as_view()(request, pk=proposicao.pk)
        content = response.content.decode('utf-8')

        self.assertIn('Câmara aprova projeto', content)
        self.assertIn('https://www.folhavitoria.com.br/materia-x', content)
        self.assertIn('FOLHAVITORIA.COM.BR', content)

    def test_sem_noticia_mostra_estado_vazio(self):
        proposicao = Proposicao.objects.create(titulo='PL sem notícia', casa='camara')
        request = _request_with_session(f'/proposicao/{proposicao.pk}/')
        request.user = Usuario.objects.create(username='semnoticia', email='semnoticia@fnp.org.br')
        response = ProposicaoDetailView.as_view()(request, pk=proposicao.pk)
        content = response.content.decode('utf-8')

        self.assertIn('Nenhuma notícia registrada para esta proposição.', content)


class DominioFilterTest(TestCase):
    def test_extrai_dominio_sem_www(self):
        from apps.legislativo.templatetags.legislativo_extras import dominio

        self.assertEqual(dominio('https://www.camara.leg.br/noticias/x'), 'CAMARA.LEG.BR')
        self.assertEqual(dominio('https://cmbh.mg.gov.br/x'), 'CMBH.MG.GOV.BR')
        self.assertEqual(dominio(''), '')


class AnexoProposicaoTest(TestCase):
    """Anexos de documentos/ementas na proposição -- qualquer usuário
    autenticado e aprovado envia; exclusão só pelo autor do envio ou staff
    (mesmo modelo de excluir_comentario)."""

    def setUp(self):
        cache.clear()
        self.proposicao = Proposicao.objects.create(titulo='PL com anexo', casa='camara')
        self.usuario = Usuario.objects.create_user(username='anexador', email='anexador@fnp.org.br', password='x')
        self.usuario.perfil.status_aprovacao = Perfil.APROVADO
        self.usuario.perfil.save()

    def test_upload_valido_cria_anexo(self):
        self.client.force_login(self.usuario)
        arquivo = SimpleUploadedFile('ementa.pdf', b'%PDF-1.4 conteudo falso', content_type='application/pdf')
        response = self.client.post(
            reverse('legislativo:enviar_anexo', args=[self.proposicao.pk]),
            {'arquivo': arquivo, 'titulo': 'Ementa oficial'},
        )
        self.assertEqual(response.status_code, 302)
        anexo = self.proposicao.anexos.get()
        self.assertEqual(anexo.titulo, 'Ementa oficial')
        self.assertEqual(anexo.enviado_por, self.usuario)
        self.assertEqual(anexo.status_moderacao, 'pendente')

    def test_extensao_nao_permitida_e_rejeitada(self):
        self.client.force_login(self.usuario)
        arquivo = SimpleUploadedFile('script.exe', b'conteudo', content_type='application/octet-stream')
        self.client.post(reverse('legislativo:enviar_anexo', args=[self.proposicao.pk]), {'arquivo': arquivo})
        self.assertEqual(self.proposicao.anexos.count(), 0)

    def test_arquivo_maior_que_o_limite_e_rejeitado(self):
        from django.core.exceptions import ValidationError

        from apps.proposicoes.models import ANEXO_TAMANHO_MAXIMO_MB, validar_tamanho_anexo

        arquivo = SimpleUploadedFile('grande.pdf', b'x')
        arquivo.size = ANEXO_TAMANHO_MAXIMO_MB * 1024 * 1024 + 1  # simula arquivo grande sem gerar bytes reais
        with self.assertRaises(ValidationError):
            validar_tamanho_anexo(arquivo)

    def test_exclusao_por_outro_usuario_nao_apaga(self):
        anexo = AnexoProposicao.objects.create(
            proposicao=self.proposicao, arquivo=SimpleUploadedFile('doc.pdf', b'x'), enviado_por=self.usuario,
        )
        outro = Usuario.objects.create_user(username='outroanexo', email='outroanexo@fnp.org.br', password='x')
        outro.perfil.status_aprovacao = Perfil.APROVADO
        outro.perfil.save()
        self.client.force_login(outro)
        self.client.post(reverse('legislativo:excluir_anexo', args=[anexo.pk]))
        self.assertTrue(AnexoProposicao.objects.filter(pk=anexo.pk).exists())

    def test_autor_exclui_o_proprio_anexo(self):
        anexo = AnexoProposicao.objects.create(
            proposicao=self.proposicao, arquivo=SimpleUploadedFile('doc.pdf', b'x'), enviado_por=self.usuario,
        )
        self.client.force_login(self.usuario)
        self.client.post(reverse('legislativo:excluir_anexo', args=[anexo.pk]))
        self.assertFalse(AnexoProposicao.objects.filter(pk=anexo.pk).exists())

    def test_staff_exclui_anexo_de_outro(self):
        anexo = AnexoProposicao.objects.create(
            proposicao=self.proposicao, arquivo=SimpleUploadedFile('doc.pdf', b'x'), enviado_por=self.usuario,
        )
        staff = Usuario.objects.create_user(
            username='staffanexo', email='staffanexo@fnp.org.br', password='x', is_staff=True,
        )
        self.client.force_login(staff)
        self.client.post(reverse('legislativo:excluir_anexo', args=[anexo.pk]))
        self.assertFalse(AnexoProposicao.objects.filter(pk=anexo.pk).exists())


class AnexoModeracaoTest(TestCase):
    """Anexo nasce sempre 'pendente' (sem checagem automática de conteúdo de
    arquivo, ao contrário do comentário) -- só aparece na página pública
    depois de aprovado pela equipe (Root/Administrador FNP)."""

    def setUp(self):
        self.proposicao = Proposicao.objects.create(titulo='PL com anexo pendente', casa='camara')
        self.autor = Usuario.objects.create(username='autoranexo', email='autoranexo@fnp.org.br')
        self.pendente = AnexoProposicao.objects.create(
            proposicao=self.proposicao, arquivo=SimpleUploadedFile('doc.pdf', b'x'),
            enviado_por=self.autor, titulo='Anexo pendente',
        )

    def _renderizar_como(self, usuario):
        request = _request_with_session(f'/proposicao/{self.proposicao.pk}/')
        request.user = usuario
        return ProposicaoDetailView.as_view()(request, pk=self.proposicao.pk).content.decode('utf-8')

    def test_anonimo_nao_ve_anexo_pendente(self):
        from django.contrib.auth.models import AnonymousUser

        content = self._renderizar_como(AnonymousUser())
        self.assertNotIn('Anexo pendente', content)

    def test_outro_usuario_nao_ve_anexo_pendente(self):
        outro = Usuario.objects.create(username='outroleitor', email='outroleitor@fnp.org.br')
        content = self._renderizar_como(outro)
        self.assertNotIn('Anexo pendente', content)

    def test_autor_ve_o_proprio_anexo_pendente(self):
        content = self._renderizar_como(self.autor)
        self.assertIn('Anexo pendente', content)
        self.assertIn('Aguardando aprovação', content)

    def test_staff_ve_qualquer_anexo_pendente(self):
        staff = Usuario.objects.create(username='staffmod', email='staffmod@fnp.org.br', is_staff=True)
        content = self._renderizar_como(staff)
        self.assertIn('Anexo pendente', content)

    def test_anexo_aprovado_aparece_pra_todo_mundo(self):
        self.pendente.status_moderacao = 'aprovado'
        self.pendente.save(update_fields=['status_moderacao'])
        from django.contrib.auth.models import AnonymousUser

        content = self._renderizar_como(AnonymousUser())
        self.assertIn('Anexo pendente', content)

    def test_admin_aprova_em_massa(self):
        from apps.proposicoes.admin import AnexoProposicaoAdmin

        self.assertIn('aprovar_selecionados', AnexoProposicaoAdmin.actions)
        self.assertIn('rejeitar_selecionados', AnexoProposicaoAdmin.actions)


class ComentarioMidiaTest(TestCase):
    """Foto/vídeo opcional no comentário do fórum -- 1 arquivo por comentário,
    sem moderação de conteúdo além do que já existe pro texto."""

    def setUp(self):
        cache.clear()
        self.proposicao = Proposicao.objects.create(titulo='PL com mídia no fórum', casa='camara')
        self.usuario = Usuario.objects.create_user(username='midiador', email='midiador@fnp.org.br', password='x')
        self.usuario.perfil.status_aprovacao = Perfil.APROVADO
        self.usuario.perfil.save()
        self.client.force_login(self.usuario)

    def _postar(self, midia):
        return self.client.post(
            reverse('legislativo:proposicao_detail', args=[self.proposicao.pk]),
            {'texto': 'Comentário com mídia', 'parent': '', 'midia': midia},
        )

    def test_comentario_com_imagem_e_publicado(self):
        buffer = io.BytesIO()
        Image.new('RGB', (2, 2)).save(buffer, format='PNG')
        arquivo = SimpleUploadedFile('foto.jpg', buffer.getvalue(), content_type='image/jpeg')
        response = self._postar(arquivo)
        self.assertEqual(response.status_code, 302)
        comentario = self.proposicao.comentarios.get()
        self.assertTrue(comentario.midia)
        self.assertEqual(comentario.tipo_midia, 'imagem')

    def test_comentario_com_video_e_publicado(self):
        arquivo = SimpleUploadedFile('registro.mp4', b'conteudo de video falso', content_type='video/mp4')
        response = self._postar(arquivo)
        self.assertEqual(response.status_code, 302)
        comentario = self.proposicao.comentarios.get()
        self.assertEqual(comentario.tipo_midia, 'video')

    def test_extensao_nao_permitida_e_rejeitada(self):
        arquivo = SimpleUploadedFile('arquivo.exe', b'conteudo', content_type='application/octet-stream')
        response = self._postar(arquivo)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.proposicao.comentarios.count(), 0)

    def test_video_maior_que_o_limite_e_rejeitado(self):
        from django.core.exceptions import ValidationError

        from apps.comentarios.models import COMENTARIO_VIDEO_TAMANHO_MAXIMO_MB, validar_tamanho_midia_comentario

        arquivo = SimpleUploadedFile('grande.mp4', b'x')
        arquivo.size = COMENTARIO_VIDEO_TAMANHO_MAXIMO_MB * 1024 * 1024 + 1
        with self.assertRaises(ValidationError):
            validar_tamanho_midia_comentario(arquivo)

    def test_comentario_sem_midia_continua_funcionando(self):
        response = self.client.post(
            reverse('legislativo:proposicao_detail', args=[self.proposicao.pk]),
            {'texto': 'Comentário só texto', 'parent': ''},
        )
        self.assertEqual(response.status_code, 302)
        comentario = self.proposicao.comentarios.get()
        self.assertFalse(comentario.midia)
        self.assertIsNone(comentario.tipo_midia)

    def test_comentario_com_documento_e_publicado(self):
        arquivo = SimpleUploadedFile('ata-reuniao.pdf', b'%PDF-1.4 conteudo falso', content_type='application/pdf')
        response = self._postar(arquivo)
        self.assertEqual(response.status_code, 302)
        comentario = self.proposicao.comentarios.get()
        self.assertEqual(comentario.tipo_midia, 'documento')
        self.assertEqual(comentario.midia_nome_arquivo.split('.')[-1], 'pdf')

    def test_comentario_com_audio_e_publicado(self):
        arquivo = SimpleUploadedFile('recado.mp3', b'conteudo de audio falso', content_type='audio/mpeg')
        response = self._postar(arquivo)
        self.assertEqual(response.status_code, 302)
        comentario = self.proposicao.comentarios.get()
        self.assertEqual(comentario.tipo_midia, 'audio')

    def test_audio_gravado_ao_vivo_em_webm_nao_e_confundido_com_video(self):
        """Gravação de áudio e de vídeo do fórum usam o mesmo contêiner
        .webm -- só o content_type que o navegador manda distingue as duas
        (ver _detectar_categoria_midia)."""
        arquivo = SimpleUploadedFile('audio-gravacao.webm', b'conteudo falso', content_type='audio/webm;codecs=opus')
        response = self._postar(arquivo)
        self.assertEqual(response.status_code, 302)
        comentario = self.proposicao.comentarios.get()
        self.assertEqual(comentario.tipo_midia, 'audio')

    def test_documento_maior_que_o_limite_e_rejeitado(self):
        from django.core.exceptions import ValidationError

        from apps.comentarios.models import COMENTARIO_DOCUMENTO_TAMANHO_MAXIMO_MB, validar_tamanho_midia_comentario

        arquivo = SimpleUploadedFile('grande.pdf', b'x', content_type='application/pdf')
        arquivo.size = COMENTARIO_DOCUMENTO_TAMANHO_MAXIMO_MB * 1024 * 1024 + 1
        with self.assertRaises(ValidationError):
            validar_tamanho_midia_comentario(arquivo)

    def test_audio_maior_que_o_limite_e_rejeitado(self):
        from django.core.exceptions import ValidationError

        from apps.comentarios.models import COMENTARIO_AUDIO_TAMANHO_MAXIMO_MB, validar_tamanho_midia_comentario

        arquivo = SimpleUploadedFile('grande.mp3', b'x', content_type='audio/mpeg')
        arquivo.size = COMENTARIO_AUDIO_TAMANHO_MAXIMO_MB * 1024 * 1024 + 1
        with self.assertRaises(ValidationError):
            validar_tamanho_midia_comentario(arquivo)

    def test_comentario_so_com_midia_sem_texto_e_publicado(self):
        arquivo = SimpleUploadedFile('recado.mp3', b'conteudo de audio falso', content_type='audio/mpeg')
        response = self.client.post(
            reverse('legislativo:proposicao_detail', args=[self.proposicao.pk]),
            {'texto': '', 'parent': '', 'midia': arquivo},
        )
        self.assertEqual(response.status_code, 302)
        comentario = self.proposicao.comentarios.get()
        self.assertEqual(comentario.texto, '')
        self.assertEqual(comentario.tipo_midia, 'audio')

    def test_comentario_totalmente_vazio_e_rejeitado(self):
        response = self.client.post(
            reverse('legislativo:proposicao_detail', args=[self.proposicao.pk]),
            {'texto': '', 'parent': ''},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.proposicao.comentarios.count(), 0)
        self.assertIn('Escreva um comentário ou anexe', response.content.decode('utf-8'))

    def test_comentario_so_com_espacos_e_rejeitado(self):
        response = self.client.post(
            reverse('legislativo:proposicao_detail', args=[self.proposicao.pk]),
            {'texto': '   ', 'parent': ''},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.proposicao.comentarios.count(), 0)


class UrlsImpressaoOficialTest(TestCase):
    """Links diretos de impressão na fonte oficial -- Câmara tem 3 formatos
    (reduzida/completa/personalizada), todos derivados só do idProposicao
    (aparece tanto em /propostas-legislativas/<id> quanto em
    ?idProposicao=<id>, formatos que coexistem nos dados reais importados
    de fontes diferentes); Senado só tem 1 (PDF único). Pedido do usuário:
    apontar direto pra versão de impressão de cada casa, não pra ficha de
    tramitação normal nem pra uma página de impressão nossa."""

    def test_camara_formato_id_proposicao_na_query(self):
        from apps.proposicoes.print_urls import urls_impressao_oficial

        urls = urls_impressao_oficial('https://www.camara.leg.br/proposicoesWeb/fichadetramitacao?idProposicao=2589656')
        self.assertEqual(
            urls['Impressão reduzida'],
            'https://www.camara.leg.br/proposicoesWeb/prop_imp?idProposicao=2589656&ord=1&tp=reduzida',
        )
        self.assertEqual(
            urls['Impressão completa'],
            'https://www.camara.leg.br/proposicoesWeb/prop_imp?idProposicao=2589656&ord=1&tp=completa',
        )
        self.assertEqual(
            urls['Impressão personalizada'],
            'https://www.camara.leg.br/proposicoesWeb/prop_visual_impress?idProposicao=2589656&ord=1',
        )

    def test_camara_formato_propostas_legislativas_no_path(self):
        from apps.proposicoes.print_urls import urls_impressao_oficial

        urls = urls_impressao_oficial('https://www.camara.leg.br/propostas-legislativas/2418774')
        self.assertEqual(
            urls['Impressão reduzida'],
            'https://www.camara.leg.br/proposicoesWeb/prop_imp?idProposicao=2418774&ord=1&tp=reduzida',
        )

    def test_senado_vira_pdf_unico(self):
        from apps.proposicoes.print_urls import urls_impressao_oficial

        urls = urls_impressao_oficial('https://www25.senado.leg.br/web/atividade/materias/-/materia/136079')
        self.assertEqual(
            urls, {'Imprimir (PDF)': 'https://www25.senado.leg.br/web/atividade/materias/-/materia/136079/pdf'},
        )

    def test_fonte_desconhecida_cai_no_link_direto(self):
        from apps.proposicoes.print_urls import urls_impressao_oficial

        urls = urls_impressao_oficial('https://exemplo.gov.br/algo')
        self.assertEqual(urls, {'Abrir na fonte oficial': 'https://exemplo.gov.br/algo'})

    def test_sem_link_nao_ha_opcao_nenhuma(self):
        from apps.proposicoes.print_urls import urls_impressao_oficial

        self.assertEqual(urls_impressao_oficial(''), {})
        self.assertEqual(urls_impressao_oficial(None), {})


class ImprimirProposicaoTest(TestCase):
    """Botão de impressão na página da proposição -- menu com as opções
    derivadas de urls_impressao_oficial(); sem link cadastrado, o botão
    nem aparece (não há o que imprimir na fonte)."""

    def test_menu_de_impressao_mostra_as_3_opcoes_da_camara(self):
        proposicao = Proposicao.objects.create(
            titulo='PL com fonte',
            casa='camara',
            link='https://www.camara.leg.br/propostas-legislativas/2589656',
        )
        request = _request_with_session(f'/proposicao/{proposicao.pk}/')
        request.user = Usuario.objects.create(username='vejoimpressao', email='vejoimpressao@fnp.org.br')
        response = ProposicaoDetailView.as_view()(request, pk=proposicao.pk)
        content = response.content.decode('utf-8')

        # Django escapa "&" pra "&amp;" no HTML -- checa por trecho sem "&".
        self.assertIn('prop_imp?idProposicao=2589656', content)
        self.assertIn('tp=reduzida', content)
        self.assertIn('tp=completa', content)
        self.assertIn('prop_visual_impress?idProposicao=2589656', content)
        self.assertIn('Imprimir na fonte oficial', content)

    def test_sem_link_da_fonte_botao_nao_aparece(self):
        proposicao = Proposicao.objects.create(titulo='PL sem fonte', casa='camara')
        request = _request_with_session(f'/proposicao/{proposicao.pk}/')
        request.user = Usuario.objects.create(username='semfonte', email='semfonte@fnp.org.br')
        response = ProposicaoDetailView.as_view()(request, pk=proposicao.pk)
        content = response.content.decode('utf-8')
        self.assertNotIn('Imprimir na fonte oficial', content)


class CompartilharProposicaoTest(TestCase):
    """Botão de compartilhar (Web Share API + fallback WhatsApp/X/Facebook/
    copiar link, 100% client-side) -- checa só que a marcação com os dados
    que o JS usa (share-trigger-btn) chega renderizada."""

    def test_botao_de_compartilhar_aparece_na_pagina_da_proposicao(self):
        proposicao = Proposicao.objects.create(titulo='PL para compartilhar', casa='camara')
        request = _request_with_session(f'/proposicao/{proposicao.pk}/')
        request.user = Usuario.objects.create(username='vejocompartilhar', email='vejocompartilhar@fnp.org.br')
        response = ProposicaoDetailView.as_view()(request, pk=proposicao.pk)
        content = response.content.decode('utf-8')

        self.assertIn('share-trigger-btn', content)
        self.assertIn(reverse('legislativo:proposicao_detail', args=[proposicao.pk]), content)
        self.assertIn('PL para compartilhar', content)


class EnviarParticipacaoRemovidaTest(TestCase):
    def test_pagina_da_proposicao_nao_tem_mais_o_formulario_avulso(self):
        proposicao = Proposicao.objects.create(titulo='PL sem participação avulsa', casa='camara')
        request = _request_with_session(f'/proposicao/{proposicao.pk}/')
        request.user = Usuario.objects.create(username='semparticip', email='semparticip@fnp.org.br')
        response = ProposicaoDetailView.as_view()(request, pk=proposicao.pk)
        content = response.content.decode('utf-8')
        self.assertNotIn('Enviar participação', content)


class ParticipacoesComoEngajamentoTest(TestCase):
    """"Participações" mostra só as proposições em que o próprio usuário
    logado comentou -- não é mais uma lista pública de Participacao."""

    def test_requer_login(self):
        request = _request_with_session(reverse('legislativo:participacao_list'))
        from django.contrib.auth.models import AnonymousUser

        request.user = AnonymousUser()
        response = ParticipacaoListView.as_view()(request)
        self.assertEqual(response.status_code, 302)

    def test_mostra_so_proposicoes_do_proprio_usuario(self):
        proposicao_minha = Proposicao.objects.create(titulo='PL que comentei', casa='camara')
        proposicao_outro = Proposicao.objects.create(titulo='PL de outra pessoa', casa='camara')
        eu = Usuario.objects.create(username='euparticipo', email='euparticipo@fnp.org.br')
        outro = Usuario.objects.create(username='outroparticipa', email='outroparticipa@fnp.org.br')
        Comentario.objects.create(proposicao=proposicao_minha, texto='comentei', autor=eu, status_moderacao='aprovado')
        Comentario.objects.create(proposicao=proposicao_outro, texto='comentou', autor=outro, status_moderacao='aprovado')

        request = _request_with_session(reverse('legislativo:participacao_list'))
        request.user = eu
        response = ParticipacaoListView.as_view()(request)
        content = response.content.decode('utf-8')

        self.assertIn('PL que comentei', content)
        self.assertNotIn('PL de outra pessoa', content)


class PerfilEmailEditavelTest(TestCase):
    def test_alterar_email_atualiza_usuario_e_email_address_do_allauth(self):
        from allauth.account.models import EmailAddress

        usuario = Usuario.objects.create(username='trocaemail', email='antigo@fnp.org.br', first_name='Troca')
        request = _post_request_with_session(
            reverse('legislativo:perfil'),
            {'first_name': 'Troca', 'last_name': '', 'email': 'novo@fnp.org.br'},
        )
        request.user = usuario
        response = PerfilView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        usuario.refresh_from_db()
        self.assertEqual(usuario.email, 'novo@fnp.org.br')
        self.assertTrue(EmailAddress.objects.filter(user=usuario, email='novo@fnp.org.br', verified=False).exists())

    def test_nao_permite_email_ja_usado_por_outro_usuario(self):
        Usuario.objects.create(username='dono', email='ocupado@fnp.org.br')
        usuario = Usuario.objects.create(username='querendo', email='livre@fnp.org.br', first_name='Q')
        request = _post_request_with_session(
            reverse('legislativo:perfil'),
            {'first_name': 'Q', 'last_name': '', 'email': 'ocupado@fnp.org.br'},
        )
        request.user = usuario
        PerfilView.as_view()(request)

        usuario.refresh_from_db()
        self.assertEqual(usuario.email, 'livre@fnp.org.br')  # não mudou


class ComentarioLikeTest(TestCase):
    def test_curtir_e_descurtir_alterna(self):
        proposicao = Proposicao.objects.create(titulo='PL curtido', casa='camara')
        autor = Usuario.objects.create(username='autorcurtido', email='autorcurtido@fnp.org.br')
        curtidor = Usuario.objects.create(username='curtidor', email='curtidor@fnp.org.br')
        comentario = Comentario.objects.create(
            proposicao=proposicao, texto='comentário curtido', autor=autor, status_moderacao='aprovado',
        )

        request = _post_request_with_session(f'/comentario/{comentario.pk}/curtir/')
        request.user = curtidor
        curtir_comentario(request, pk=comentario.pk)
        self.assertEqual(comentario.likes.count(), 1)

        request2 = _post_request_with_session(f'/comentario/{comentario.pk}/curtir/')
        request2.user = curtidor
        curtir_comentario(request2, pk=comentario.pk)
        self.assertEqual(comentario.likes.count(), 0)

    def test_likes_entram_no_ranking_em_alta(self):
        proposicao = Proposicao.objects.create(titulo='PL bombou de like', casa='camara')
        autor = Usuario.objects.create(username='autorbombou', email='autorbombou@fnp.org.br')
        comentario = Comentario.objects.create(
            proposicao=proposicao, texto='comentário', autor=autor, status_moderacao='aprovado',
        )
        for i in range(3):
            curtidor = Usuario.objects.create(username=f'curtidor{i}', email=f'curtidor{i}@fnp.org.br')
            ComentarioLike.objects.create(comentario=comentario, usuario=curtidor)

        sections = get_home_sections('', '')
        self.assertIn(proposicao, list(sections['em_alta']))


class ExcluirComentarioTest(TestCase):
    def setUp(self):
        self.proposicao = Proposicao.objects.create(titulo='PL excluível', casa='camara')
        self.autor = Usuario.objects.create(username='autorexclui', email='autorexclui@fnp.org.br')
        self.outro = Usuario.objects.create(username='outroexclui', email='outroexclui@fnp.org.br')

    def test_autor_exclui_proprio_comentario_sem_respostas(self):
        comentario = Comentario.objects.create(
            proposicao=self.proposicao, texto='vou apagar', autor=self.autor, status_moderacao='aprovado',
        )
        request = _post_request_with_session(f'/comentario/{comentario.pk}/excluir/')
        request.user = self.autor

        excluir_comentario(request, pk=comentario.pk)

        self.assertFalse(Comentario.objects.filter(pk=comentario.pk).exists())

    def test_nao_exclui_comentario_de_outro_usuario(self):
        comentario = Comentario.objects.create(
            proposicao=self.proposicao, texto='não é seu', autor=self.autor, status_moderacao='aprovado',
        )
        request = _post_request_with_session(f'/comentario/{comentario.pk}/excluir/')
        request.user = self.outro

        with self.assertRaises(Http404):
            excluir_comentario(request, pk=comentario.pk)

        self.assertTrue(Comentario.objects.filter(pk=comentario.pk).exists())

    def test_nao_exclui_comentario_com_resposta(self):
        comentario = Comentario.objects.create(
            proposicao=self.proposicao, texto='tem resposta', autor=self.autor, status_moderacao='aprovado',
        )
        Comentario.objects.create(
            proposicao=self.proposicao, texto='respondendo', autor=self.outro,
            parent=comentario, status_moderacao='aprovado',
        )
        request = _post_request_with_session(f'/comentario/{comentario.pk}/excluir/')
        request.user = self.autor

        excluir_comentario(request, pk=comentario.pk)

        self.assertTrue(Comentario.objects.filter(pk=comentario.pk).exists())


class DefinirSenhaSocialTest(TestCase):
    """Conta sem senha local (login só via Google) não pode criar uma senha
    direto a partir da sessão -- precisa confirmar pelo e-mail cadastrado,
    reaproveitando o fluxo de "Esqueci minha senha" do allauth."""

    def test_usuario_sem_senha_ve_tela_de_confirmacao_por_email(self):
        from apps.usuarios.views import definir_senha_social

        usuario = Usuario.objects.create(username='soogoogle', email='soogoogle@fnp.org.br')
        usuario.set_unusable_password()
        usuario.save()
        self.assertFalse(usuario.has_usable_password())

        request = _request_with_session('/contas/password/set/')
        request.user = usuario
        response = definir_senha_social(request)

        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertIn('ainda não tem uma senha', content)

    def test_post_envia_email_de_confirmacao(self):
        from apps.usuarios.views import definir_senha_social

        usuario = Usuario.objects.create(username='soogoogle2', email='soogoogle2@fnp.org.br')
        usuario.set_unusable_password()
        usuario.save()
        # A criação do usuário acima já dispara o aviso de cadastro pendente
        # (ver PerfilSignalTest) -- limpa a caixa antes do POST pra testar só
        # o e-mail de confirmação que o próprio endpoint manda.
        mail.outbox.clear()
        request = _post_request_with_session('/contas/password/set/')
        request.user = usuario
        response = definir_senha_social(request)

        self.assertEqual(response.status_code, 200)
        self.assertIn('Confira seu e-mail', response.content.decode('utf-8'))
        self.assertEqual(len(mail.outbox), 1)

    def test_usuario_com_senha_e_redirecionado_pra_alterar_senha(self):
        from apps.usuarios.views import definir_senha_social

        usuario = Usuario.objects.create(username='comsenha', email='comsenha@fnp.org.br')
        usuario.set_password('umasenhaqualquer123')
        usuario.save()

        request = _request_with_session('/contas/password/set/')
        request.user = usuario
        response = definir_senha_social(request)

        self.assertEqual(response.status_code, 302)
        self.assertIn('password/change', response.url)

    def test_falha_no_envio_de_email_mostra_erro_em_vez_de_500(self):
        """EMAIL_BACKEND de produção sem SMTP real configurado (pendência
        conhecida, ver CLAUDE.md) derrubava esta view com 500 -- agora a
        falha é tratada e mostrada como mensagem amigável na mesma página."""
        from unittest.mock import patch

        from apps.usuarios.views import definir_senha_social

        usuario = Usuario.objects.create(username='soogoogle3', email='soogoogle3@fnp.org.br')
        usuario.set_unusable_password()
        usuario.save()

        request = _post_request_with_session('/contas/password/set/')
        request.user = usuario

        with patch(
            'apps.usuarios.views.ResetPasswordForm.save',
            side_effect=ConnectionRefusedError('recusado'),
        ):
            response = definir_senha_social(request)

        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertIn('Não foi possível enviar o e-mail agora', content)
        self.assertNotIn('Confira seu e-mail', content)


class UsuarioPublicoTest(TestCase):
    def test_pagina_publica_mostra_proposicoes_comentadas(self):
        proposicao = Proposicao.objects.create(titulo='PL do fulano', casa='camara')
        fulano = Usuario.objects.create(username='fulano', email='fulano@fnp.org.br', first_name='Fulano')
        Comentario.objects.create(proposicao=proposicao, texto='oi', autor=fulano, status_moderacao='aprovado')

        request = _request_with_session(f'/usuario/{fulano.pk}/')
        from django.contrib.auth.models import AnonymousUser

        request.user = AnonymousUser()
        response = usuario_publico(request, pk=fulano.pk)

        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertIn('Fulano', content)
        self.assertIn('PL do fulano', content)


class ComentarioParentEscopadoTest(TestCase):
    """Auditoria de segurança 2026-08-11: 'parent' (campo escondido do form
    de comentário) não podia apontar pra um comentário de outra proposição --
    sem isso, um POST manual conseguia encaixar uma resposta na árvore de
    comentários de uma proposição diferente da que o formulário pertence."""

    def setUp(self):
        self.autor = Usuario.objects.create(username='autorparent', email='autorparent@fnp.org.br')
        self.proposicao_a = Proposicao.objects.create(titulo='PL A', casa='camara')
        self.proposicao_b = Proposicao.objects.create(titulo='PL B', casa='camara')
        self.comentario_raiz_b = Comentario.objects.create(
            proposicao=self.proposicao_b, texto='raiz em B', autor=self.autor, status_moderacao='aprovado',
        )

    def test_parent_de_outra_proposicao_e_rejeitado(self):
        form = ComentarioForm(
            data={'texto': 'tentando responder em B a partir de A', 'parent': self.comentario_raiz_b.pk},
            proposicao=self.proposicao_a,
        )
        self.assertFalse(form.is_valid())
        self.assertIn('parent', form.errors)

    def test_parent_da_mesma_proposicao_e_aceito(self):
        comentario_raiz_a = Comentario.objects.create(
            proposicao=self.proposicao_a, texto='raiz em A', autor=self.autor, status_moderacao='aprovado',
        )
        form = ComentarioForm(
            data={'texto': 'resposta legítima em A', 'parent': comentario_raiz_a.pk},
            proposicao=self.proposicao_a,
        )
        self.assertTrue(form.is_valid())

    def test_post_view_com_parent_de_outra_proposicao_nao_salva_comentario(self):
        request = _post_request_with_session(
            f'/proposicao/{self.proposicao_a.pk}/',
            {'texto': 'ataque via view', 'parent': self.comentario_raiz_b.pk},
        )
        request.user = self.autor
        ProposicaoDetailView.as_view()(request, pk=self.proposicao_a.pk)

        self.assertFalse(Comentario.objects.filter(texto='ataque via view').exists())


class EmailConfirmadoNoAdminTest(TestCase):
    """A coluna 'E-mail confirmado' do UsuarioAdmin dá ao Root o sinal que
    faltava pra recusar um cadastro suspeito antes de aprovar, já que o
    cadastro por e-mail/senha não exige confirmação pra navegar."""

    def test_email_nao_confirmado_aparece_como_false(self):
        from allauth.account.models import EmailAddress

        from apps.usuarios.admin import UsuarioAdmin

        usuario = Usuario.objects.create(username='naoconfirmado', email='naoconfirmado@fnp.org.br')
        EmailAddress.objects.create(user=usuario, email=usuario.email, verified=False, primary=True)

        admin_instance = UsuarioAdmin(Usuario, admin.site)
        request = _request_with_session('/admin/usuarios/usuario/')
        obj = admin_instance.get_queryset(request).get(pk=usuario.pk)

        self.assertFalse(admin_instance.email_confirmado(obj))

    def test_email_confirmado_aparece_como_true(self):
        from allauth.account.models import EmailAddress

        from apps.usuarios.admin import UsuarioAdmin

        usuario = Usuario.objects.create(username='confirmado', email='confirmado@fnp.org.br')
        EmailAddress.objects.create(user=usuario, email=usuario.email, verified=True, primary=True)

        admin_instance = UsuarioAdmin(Usuario, admin.site)
        request = _request_with_session('/admin/usuarios/usuario/')
        obj = admin_instance.get_queryset(request).get(pk=usuario.pk)

        self.assertTrue(admin_instance.email_confirmado(obj))


class AuditoriaAprovacaoCadastroTest(TestCase):
    """Ações em massa de aprovar/rejeitar cadastro usavam .update() direto,
    que nunca passa pelo LogEntry automático do Django Admin -- não ficava
    registrado quem aprovou/rejeitou. Corrigido registrando explicitamente."""

    def test_aprovar_cadastros_registra_log_entry(self):
        from django.contrib.admin.models import LogEntry

        from apps.usuarios.admin import UsuarioAdmin

        root = Usuario.objects.create(username='rootauditoria', email='rootauditoria@fnp.org.br', is_superuser=True)
        alvo = Usuario.objects.create(username='alvoauditoria', email='alvoauditoria@fnp.org.br')

        from django.contrib.messages.storage.fallback import FallbackStorage

        request = _post_request_with_session('/admin/usuarios/usuario/')
        request.user = root
        request._messages = FallbackStorage(request)
        admin_instance = UsuarioAdmin(Usuario, admin.site)
        admin_instance.aprovar_cadastros(request, Usuario.objects.filter(pk=alvo.pk))

        entrada = LogEntry.objects.get(object_id=str(alvo.pk))
        self.assertEqual(entrada.user_id, root.pk)
        self.assertIn('aprovado', entrada.change_message.lower())


class SenhaMinimaTest(TestCase):
    def test_senha_curta_e_rejeitada(self):
        from django.contrib.auth.password_validation import validate_password
        from django.core.exceptions import ValidationError

        with self.assertRaises(ValidationError):
            validate_password('curta123')  # 8 caracteres, abaixo do novo mínimo de 10

    def test_senha_com_dez_caracteres_e_aceita(self):
        from django.contrib.auth.password_validation import validate_password

        validate_password('umasenhagrande123')


class SessionCookieAgeTest(TestCase):
    def test_sessao_nao_e_eterna(self):
        from django.conf import settings

        self.assertEqual(settings.SESSION_COOKIE_AGE, 60 * 60 * 24 * 7)


@override_settings(ALLOWED_HOSTS=['testserver'])
class DenunciaRateLimitTest(TestCase):
    def setUp(self):
        cache.clear()
        self.proposicao = Proposicao.objects.create(titulo='PL denunciado muito', casa='camara')
        self.autor = Usuario.objects.create(username='autordenunciado', email='autordenunciado@fnp.org.br')
        self.denunciante = Usuario.objects.create(username='denuncianteativo', email='denuncianteativo@fnp.org.br')
        self.denunciante.perfil.status_aprovacao = Perfil.APROVADO
        self.denunciante.perfil.save()

    def test_bloqueia_apos_dez_denuncias_na_janela(self):
        # Client mantém o cookie de sessão entre chamadas (como um navegador
        # de verdade) -- com RequestFactory cru, cada chamada gerava uma
        # sessão nova, e o rate limit (sessão+IP) nunca acumulava contagem.
        self.client.force_login(self.denunciante)
        comentarios = [
            Comentario.objects.create(
                proposicao=self.proposicao, texto=f'comentário {i}', autor=self.autor, status_moderacao='aprovado',
            )
            for i in range(11)
        ]
        for comentario in comentarios:
            self.client.post(f'/comentario/{comentario.pk}/denunciar/')

        total_denuncias = sum(c.denuncias.count() for c in comentarios)
        self.assertEqual(total_denuncias, 10)  # a 11ª foi bloqueada pelo rate limit


class ClientIpTest(TestCase):
    """Atrás do Nginx, REMOTE_ADDR é sempre o IP do próprio Nginx -- o rate
    limit precisa ler X-Forwarded-For (o último valor da lista, que é o que
    o Nginx efetivamente viu, não um valor que o próprio cliente possa ter
    forjado antes dele)."""

    def test_usa_ultimo_valor_de_x_forwarded_for(self):
        request = RequestFactory().get('/', HTTP_X_FORWARDED_FOR='1.2.3.4, 10.0.0.5')
        self.assertEqual(_client_ip(request), '10.0.0.5')

    def test_sem_header_usa_remote_addr(self):
        request = RequestFactory().get('/', REMOTE_ADDR='203.0.113.9')
        self.assertEqual(_client_ip(request), '203.0.113.9')


class TentativaLoginTest(TestCase):
    """Auditoria de login (achado da varredura de segurança, 2026-08-11: não
    havia log consultável de tentativa de login, só o rate-limit interno do
    allauth, que conta mas não é consultável)."""

    def setUp(self):
        cache.clear()
        self.usuario = Usuario.objects.create(username='comauditoria', email='comauditoria@fnp.org.br')
        self.usuario.set_password('senhaboa123456')
        self.usuario.save()
        self.usuario.perfil.status_aprovacao = Perfil.APROVADO
        self.usuario.perfil.save()

    def test_login_bem_sucedido_registra_tentativa(self):
        from apps.usuarios.models import TentativaLogin

        response = self.client.post(reverse('account_login'), {
            'login': 'comauditoria@fnp.org.br',
            'password': 'senhaboa123456',
        })

        self.assertEqual(response.status_code, 302)
        tentativa = TentativaLogin.objects.get()
        self.assertTrue(tentativa.sucesso)
        self.assertEqual(tentativa.usuario, self.usuario)
        self.assertEqual(tentativa.email, 'comauditoria@fnp.org.br')

    def test_login_com_senha_errada_registra_falha(self):
        from apps.usuarios.models import TentativaLogin

        self.client.post(reverse('account_login'), {
            'login': 'comauditoria@fnp.org.br',
            'password': 'senhaerrada',
        })

        tentativa = TentativaLogin.objects.get()
        self.assertFalse(tentativa.sucesso)
        self.assertIsNone(tentativa.usuario)
        self.assertEqual(tentativa.email, 'comauditoria@fnp.org.br')


class GmailApiEmailBackendTest(TestCase):
    """Backend alternativo ao SMTP (bloqueado por padrão pela DigitalOcean
    nas portas 587/465, ver CLAUDE.md) -- envia via Gmail API sobre HTTPS.
    Testado com mock da API do Google, sem credenciais reais nem chamada
    de rede."""

    def _service_account_json_b64(self):
        payload = {
            'type': 'service_account',
            'client_email': 'legislativo-fnp-email-sender@teste.iam.gserviceaccount.com',
            'token_uri': 'https://oauth2.googleapis.com/token',
        }
        return base64.b64encode(json.dumps(payload).encode()).decode()

    def _backend(self, **kwargs):
        from apps.legislativo.email_backends import GmailApiEmailBackend

        return GmailApiEmailBackend(**kwargs)

    @override_settings(GMAIL_SENDER_EMAIL='naoresponda@fnp.org.br')
    def test_envia_mensagem_com_sucesso(self):
        from django.core.mail import EmailMessage

        with (
            override_settings(GMAIL_SERVICE_ACCOUNT_JSON_B64=self._service_account_json_b64()),
            mock.patch('apps.legislativo.email_backends.service_account.Credentials.from_service_account_info') as m_creds,
            mock.patch('apps.legislativo.email_backends.build') as m_build,
        ):
            m_creds.return_value.with_subject.return_value = mock.Mock()
            servico = m_build.return_value
            execute_mock = servico.users.return_value.messages.return_value.send.return_value.execute
            execute_mock.return_value = {'id': 'abc123'}

            mensagem = EmailMessage('Assunto', 'Corpo', 'naoresponda@fnp.org.br', ['destino@exemplo.com'])
            enviados = self._backend().send_messages([mensagem])

            self.assertEqual(enviados, 1)
            m_creds.return_value.with_subject.assert_called_once_with('naoresponda@fnp.org.br')
            servico.users.return_value.messages.return_value.send.assert_called_once()
            _, chamada_kwargs = servico.users.return_value.messages.return_value.send.call_args
            self.assertEqual(chamada_kwargs['userId'], 'me')
            self.assertIn('raw', chamada_kwargs['body'])
            execute_mock.assert_called_once()

    def test_lista_vazia_nao_monta_credenciais(self):
        with mock.patch('apps.legislativo.email_backends.build') as m_build:
            enviados = self._backend().send_messages([])

        self.assertEqual(enviados, 0)
        m_build.assert_not_called()

    @override_settings(GMAIL_SENDER_EMAIL='naoresponda@fnp.org.br')
    def test_falha_com_fail_silently_nao_propaga(self):
        from django.core.mail import EmailMessage

        with (
            override_settings(GMAIL_SERVICE_ACCOUNT_JSON_B64=self._service_account_json_b64()),
            mock.patch('apps.legislativo.email_backends.service_account.Credentials.from_service_account_info') as m_creds,
            mock.patch('apps.legislativo.email_backends.build') as m_build,
        ):
            m_creds.return_value.with_subject.return_value = mock.Mock()
            m_build.return_value.users.return_value.messages.return_value.send.return_value.execute.side_effect = (
                Exception('falha simulada da API')
            )

            mensagem = EmailMessage('Assunto', 'Corpo', 'naoresponda@fnp.org.br', ['destino@exemplo.com'])
            enviados = self._backend(fail_silently=True).send_messages([mensagem])

        self.assertEqual(enviados, 0)

    @override_settings(GMAIL_SENDER_EMAIL='naoresponda@fnp.org.br')
    def test_falha_sem_fail_silently_propaga(self):
        from django.core.mail import EmailMessage

        with (
            override_settings(GMAIL_SERVICE_ACCOUNT_JSON_B64=self._service_account_json_b64()),
            mock.patch('apps.legislativo.email_backends.service_account.Credentials.from_service_account_info') as m_creds,
            mock.patch('apps.legislativo.email_backends.build') as m_build,
        ):
            m_creds.return_value.with_subject.return_value = mock.Mock()
            m_build.return_value.users.return_value.messages.return_value.send.return_value.execute.side_effect = (
                Exception('falha simulada da API')
            )

            mensagem = EmailMessage('Assunto', 'Corpo', 'naoresponda@fnp.org.br', ['destino@exemplo.com'])
            with self.assertRaises(Exception):
                self._backend(fail_silently=False).send_messages([mensagem])


class SiteBrandingTest(TestCase):
    """django.contrib.sites cria o Site com domain/name='example.com' por
    padrão -- django-allauth usa esses dois campos pra montar o texto de
    todo e-mail transacional (assunto com prefixo "[example.com]", corpo
    "Olá da example.com!"). Migration legislativo.0007 corrige isso --
    achado real, reportado pelo usuário num e-mail de verdade."""

    def test_site_nao_e_mais_example_com(self):
        from django.conf import settings
        from django.contrib.sites.models import Site

        site = Site.objects.get(pk=settings.SITE_ID)
        self.assertNotEqual(site.domain, 'example.com')
        self.assertNotEqual(site.name, 'example.com')
        self.assertEqual(site.domain, 'legislativo.fnp.org.br')
        self.assertEqual(site.name, 'Painel Legislativo FNP')

    def test_email_de_redefinicao_de_senha_nao_menciona_example_com(self):
        from allauth.account.forms import ResetPasswordForm

        usuario = Usuario.objects.create_user(username='brandtest', email='brandtest@fnp.org.br', password='x')
        request = _post_request_with_session('/contas/senha/redefinir/')
        mail.outbox.clear()

        form = ResetPasswordForm(data={'email': usuario.email})
        self.assertTrue(form.is_valid())
        form.save(request)

        mensagem = mail.outbox[-1]
        self.assertNotIn('example.com', mensagem.subject)
        self.assertNotIn('example.com', mensagem.body)
        self.assertIn('Painel Legislativo FNP', mensagem.subject)


# --- Cadastro de proposicao (com aprovacao) ----------------------------------

from django.core.exceptions import PermissionDenied  # noqa: E402

from apps.proposicoes import fontes  # noqa: E402

from .views import (  # noqa: E402
    aprovar_proposicao,
    cadastrar_proposicao,
    get_proposicao_visivel,
    proposicoes_pendentes,
    rejeitar_proposicao,
)

_DADOS_FONTE = {
    'titulo': 'PL 123/2025',
    'casa': 'camara',
    'status_tramitacao': 'Em tramitacao',
    'local': 'CCJ',
    'ultima_movimentacao': 'mov',
    'ementa_resumida': 'ementa de teste',
    'link': 'https://www.camara.leg.br/ficha',
}


class CadastroProposicaoTest(TestCase):
    def setUp(self):
        cache.clear()
        self.colaborador = Usuario.objects.create_user(
            username='colab', email='colab@fnp.org.br', password='x'
        )
        self.colaborador.perfil.classe_usuario = Perfil.EQUIPE_FNP
        self.colaborador.perfil.status_aprovacao = Perfil.APROVADO
        self.colaborador.perfil.save()

        self.aprovador = Usuario.objects.create_user(
            username='aprov', email='aprov@fnp.org.br', password='x', is_staff=True
        )
        self.publico = Usuario.objects.create_user(
            username='pref', email='pref@cidade.gov.br', password='x'
        )

    def _cadastrar(self, user, dados, tipo='PL', numero=123, ano=2025):
        with mock.patch('apps.legislativo.views.buscar_proposicao', return_value=dados):
            request = _post_request_with_session(
                '/proposicoes/cadastrar/', {'tipo': tipo, 'numero': numero, 'ano': ano}
            )
            request.user = user
            return cadastrar_proposicao(request)

    def test_colaborador_cadastra_como_pendente(self):
        resp = self._cadastrar(self.colaborador, _DADOS_FONTE)
        self.assertEqual(resp.status_code, 200)
        p = Proposicao.objects.get(titulo='PL 123/2025')
        self.assertEqual(p.status_publicacao, Proposicao.PENDENTE)
        self.assertEqual(p.submetido_por_id, self.colaborador.id)
        self.assertEqual(p.ementa_resumida, 'ementa de teste')

    def test_aprovador_publica_direto(self):
        self._cadastrar(self.aprovador, _DADOS_FONTE)
        p = Proposicao.objects.get(titulo='PL 123/2025')
        self.assertEqual(p.status_publicacao, Proposicao.PUBLICADA)
        self.assertEqual(p.aprovado_por_id, self.aprovador.id)
        self.assertIsNotNone(p.aprovado_em)

    def test_nao_encontrada_nao_cria(self):
        resp = self._cadastrar(self.colaborador, None)
        self.assertFalse(Proposicao.objects.filter(titulo='PL 123/2025').exists())
        self.assertIn('encontrada', resp.content.decode().lower())

    def test_idempotente_nao_duplica_e_preserva_status(self):
        Proposicao.objects.create(
            titulo='PL 123/2025', casa='camara',
            status_publicacao=Proposicao.PUBLICADA, status_tramitacao='antigo',
        )
        self._cadastrar(self.colaborador, _DADOS_FONTE)
        self.assertEqual(Proposicao.objects.filter(titulo='PL 123/2025').count(), 1)
        p = Proposicao.objects.get(titulo='PL 123/2025')
        self.assertEqual(p.status_publicacao, Proposicao.PUBLICADA)
        self.assertEqual(p.status_tramitacao, 'Em tramitacao')

    def test_publico_externo_nao_cadastra(self):
        with self.assertRaises(PermissionDenied):
            self._cadastrar(self.publico, _DADOS_FONTE)

    def test_views_publicas_escondem_pendente(self):
        Proposicao.objects.create(titulo='PL 1/2025', casa='camara', status_publicacao=Proposicao.PUBLICADA)
        Proposicao.objects.create(titulo='PL 2/2025', casa='camara', status_publicacao=Proposicao.PENDENTE)
        titulos = set(get_filtered_proposicoes('', '').values_list('titulo', flat=True))
        self.assertIn('PL 1/2025', titulos)
        self.assertNotIn('PL 2/2025', titulos)

    def test_detalhe_pendente_404_para_publico(self):
        p = Proposicao.objects.create(titulo='PL 2/2025', casa='camara', status_publicacao=Proposicao.PENDENTE)
        request = _request_with_session('/proposicao/%d/' % p.pk)
        request.user = self.publico
        with self.assertRaises(Http404):
            get_proposicao_visivel(request, p.pk)

    def test_detalhe_pendente_visivel_para_aprovador(self):
        p = Proposicao.objects.create(titulo='PL 2/2025', casa='camara', status_publicacao=Proposicao.PENDENTE)
        request = _request_with_session('/proposicao/%d/' % p.pk)
        request.user = self.aprovador
        self.assertEqual(get_proposicao_visivel(request, p.pk).pk, p.pk)

    def test_aprovar_publica(self):
        p = Proposicao.objects.create(titulo='PL 2/2025', casa='camara', status_publicacao=Proposicao.PENDENTE)
        request = _post_request_with_session('/proposicao/%d/aprovar/' % p.pk)
        request.user = self.aprovador
        resp = aprovar_proposicao(request, p.pk)
        self.assertEqual(resp.status_code, 302)
        p.refresh_from_db()
        self.assertEqual(p.status_publicacao, Proposicao.PUBLICADA)
        self.assertEqual(p.aprovado_por_id, self.aprovador.id)

    def test_rejeitar_registra_motivo(self):
        p = Proposicao.objects.create(titulo='PL 2/2025', casa='camara', status_publicacao=Proposicao.PENDENTE)
        request = _post_request_with_session('/proposicao/%d/rejeitar/' % p.pk, {'motivo': 'fora de escopo'})
        request.user = self.aprovador
        rejeitar_proposicao(request, p.pk)
        p.refresh_from_db()
        self.assertEqual(p.status_publicacao, Proposicao.REJEITADA)
        self.assertEqual(p.motivo_rejeicao, 'fora de escopo')

    def test_nao_aprovador_nao_aprova(self):
        p = Proposicao.objects.create(titulo='PL 2/2025', casa='camara', status_publicacao=Proposicao.PENDENTE)
        request = _post_request_with_session('/proposicao/%d/aprovar/' % p.pk)
        request.user = self.colaborador
        with self.assertRaises(PermissionDenied):
            aprovar_proposicao(request, p.pk)

    def test_pendentes_lista_so_para_aprovador(self):
        request = _request_with_session('/proposicoes/pendentes/')
        request.user = self.colaborador
        with self.assertRaises(PermissionDenied):
            proposicoes_pendentes(request)


class FontesTest(TestCase):
    def test_buscar_camara_normaliza(self):
        lista = mock.Mock()
        lista.json.return_value = {'dados': [{'id': 99, 'ementa': 'ementa X'}]}
        lista.raise_for_status = mock.Mock()
        det = mock.Mock()
        det.json.return_value = {'dados': {'statusProposicao': {
            'descricaoSituacao': 'Pronta', 'siglaOrgao': 'CCJ',
            'descricaoTramitacao': 'tram', 'dataHora': '2026-01-02T10:00:00',
        }}}
        det.raise_for_status = mock.Mock()
        with mock.patch('apps.proposicoes.fontes.requests.get', side_effect=[lista, det]):
            dados = fontes.buscar_camara('PL', 1, 2025)
        self.assertEqual(dados['titulo'], 'PL 1/2025')
        self.assertEqual(dados['casa'], 'camara')
        self.assertIn('fichadetramitacao', dados['link'])
        self.assertEqual(dados['ementa_resumida'], 'ementa X')

    def test_buscar_camara_nao_encontrada(self):
        vazio = mock.Mock()
        vazio.json.return_value = {'dados': []}
        vazio.raise_for_status = mock.Mock()
        with mock.patch('apps.proposicoes.fontes.requests.get', return_value=vazio):
            self.assertIsNone(fontes.buscar_camara('PL', 1, 2025))

    def test_buscar_proposicao_fallback_senado(self):
        with mock.patch('apps.proposicoes.fontes.buscar_camara', return_value=None), \
             mock.patch('apps.proposicoes.fontes.buscar_senado', return_value={'titulo': 'PL 1/2025', 'casa': 'senado'}):
            dados = fontes.buscar_proposicao('PL', 1, 2025)
        self.assertEqual(dados['casa'], 'senado')


# --- Alerta individual (seguir + notificacao + e-mail) -----------------------

from apps.comentarios.models import Notificacao as _Notificacao  # noqa: E402
from apps.proposicoes.models import ProposicaoSeguida as _ProposicaoSeguida  # noqa: E402

from .views import notificar_participantes_da_discussao as _notificar_discussao  # noqa: E402
from .views import toggle_seguir as _toggle_seguir  # noqa: E402


class AlertaIndividualTest(TestCase):
    def setUp(self):
        cache.clear()
        self.seguidor = Usuario.objects.create_user(username='seg', email='seg@fnp.org.br', password='x')
        self.prop = Proposicao.objects.create(
            titulo='PL 500/2025', casa='camara', status_tramitacao='A',
            status_publicacao=Proposicao.PUBLICADA,
        )

    def _seguir(self):
        _ProposicaoSeguida.objects.create(usuario=self.seguidor, proposicao=self.prop)

    def test_toggle_seguir_cria_e_remove(self):
        req = _post_request_with_session('/proposicao/%d/seguir/' % self.prop.pk)
        req.user = self.seguidor
        _toggle_seguir(req, self.prop.pk)
        self.assertTrue(_ProposicaoSeguida.objects.filter(usuario=self.seguidor, proposicao=self.prop).exists())
        req2 = _post_request_with_session('/proposicao/%d/seguir/' % self.prop.pk)
        req2.user = self.seguidor
        _toggle_seguir(req2, self.prop.pk)
        self.assertFalse(_ProposicaoSeguida.objects.filter(usuario=self.seguidor, proposicao=self.prop).exists())

    def test_movimentacao_gera_notificacao(self):
        self._seguir()
        self.prop.status_tramitacao = 'Novo status'
        self.prop.save()
        notifs = _Notificacao.objects.filter(
            destinatario=self.seguidor, proposicao=self.prop, tipo=_Notificacao.TIPO_MOVIMENTACAO
        )
        self.assertEqual(notifs.count(), 1)

    def test_mudanca_fora_de_alerta_nao_notifica(self):
        self._seguir()
        self.prop.ementa_resumida = 'mudou so a ementa'
        self.prop.save()
        self.assertEqual(_Notificacao.objects.filter(tipo=_Notificacao.TIPO_MOVIMENTACAO).count(), 0)

    def test_proposicao_nova_nao_notifica(self):
        self._seguir()
        Proposicao.objects.create(titulo='PL 999/2025', casa='camara', urgente=True)
        self.assertEqual(_Notificacao.objects.filter(tipo=_Notificacao.TIPO_MOVIMENTACAO).count(), 0)

    def test_email_so_para_optante(self):
        self.seguidor.perfil.receber_email_atualizacoes = True
        self.seguidor.perfil.save()
        self._seguir()
        mail.outbox = []
        self.prop.pauta = True
        self.prop.save()
        self.assertEqual(len(mail.outbox), 1)
        n = _Notificacao.objects.get(tipo=_Notificacao.TIPO_MOVIMENTACAO, destinatario=self.seguidor)
        self.assertTrue(n.enviado_email)

    def test_email_desligado_nao_envia(self):
        self._seguir()
        mail.outbox = []
        self.prop.pauta = True
        self.prop.save()
        self.assertEqual(len(mail.outbox), 0)

    def test_falha_email_nao_quebra_save(self):
        self.seguidor.perfil.receber_email_atualizacoes = True
        self.seguidor.perfil.save()
        self._seguir()
        with mock.patch('apps.comentarios.emails.enviar_email_alerta', side_effect=Exception('smtp down')):
            self.prop.urgente = True
            self.prop.save()
        n = _Notificacao.objects.get(tipo=_Notificacao.TIPO_MOVIMENTACAO, destinatario=self.seguidor)
        self.assertFalse(n.enviado_email)

    def test_opt_in_no_perfil_persiste(self):
        from .forms import PerfilDadosForm
        form = PerfilDadosForm(
            {'telefone': '', 'cargo': '', 'setor_responsavel': '', 'receber_email_atualizacoes': True},
            instance=self.seguidor.perfil,
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.seguidor.perfil.refresh_from_db()
        self.assertTrue(self.seguidor.perfil.receber_email_atualizacoes)

    def test_notificacao_comentario_continua_comentario_e_sem_email(self):
        outro = Usuario.objects.create_user(username='c1', email='c1@cidade.gov.br', password='x')
        Comentario.objects.create(proposicao=self.prop, autor=outro, texto='oi', status_moderacao='aprovado')
        novo = Comentario.objects.create(proposicao=self.prop, autor=self.seguidor, texto='resp', status_moderacao='aprovado')
        mail.outbox = []
        _notificar_discussao(self.prop, novo)
        n = _Notificacao.objects.filter(destinatario=outro).first()
        self.assertIsNotNone(n)
        self.assertEqual(n.tipo, _Notificacao.TIPO_COMENTARIO)
        self.assertEqual(len(mail.outbox), 0)

    def test_seguindo_list_mostra_so_seguidas(self):
        from .views import SeguindoListView
        Proposicao.objects.create(titulo='PL 600/2025', casa='camara', status_publicacao=Proposicao.PUBLICADA)
        self._seguir()  # segue self.prop (PL 500/2025), nao a PL 600/2025
        req = _request_with_session('/seguindo/')
        req.user = self.seguidor
        resp = SeguindoListView.as_view()(req)
        conteudo = resp.content.decode()
        self.assertIn('PL 500/2025', conteudo)
        self.assertNotIn('PL 600/2025', conteudo)

    def test_seguindo_list_exige_login(self):
        from django.contrib.auth.models import AnonymousUser
        from .views import SeguindoListView
        req = _request_with_session('/seguindo/')
        req.user = AnonymousUser()
        resp = SeguindoListView.as_view()(req)
        self.assertEqual(resp.status_code, 302)

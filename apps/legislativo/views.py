import json
import time

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import BooleanField, Count, Exists, F, Max, OuterRef, Prefetch, Q, Value
from django.http import Http404, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.decorators.http import require_GET, require_POST

from apps.comentarios.models import Comentario, ComentarioLike, DenunciaComentario, Notificacao
from apps.comentarios.moderacao import classificar_comentario
from apps.proposicoes.fontes import buscar_proposicao
from apps.proposicoes.models import AnexoProposicao, Macrotema, Proposicao, ProposicaoSeguida, Tema
from apps.usuarios.models import Perfil, Usuario

from .forms import AnexoProposicaoForm, CadastroProposicaoForm, ComentarioForm, PerfilDadosForm, PerfilForm
from .throttling import rate_limited

FAVORITOS_SESSION_KEY = 'favoritos'
RECENTES_SESSION_KEY = 'recentes'
RECENTES_MAX = 8


def get_favoritos_ids(request):
    return set(request.session.get(FAVORITOS_SESSION_KEY, []))


def get_recentes_ids(request):
    return request.session.get(RECENTES_SESSION_KEY, [])


def pode_cadastrar(user):
    """Quem pode cadastrar proposição pelo painel: Equipe FNP ou aprovador
    (Root/Administrador FNP, via is_staff — cobre o consultor externo que
    esteja no grupo Administrador FNP)."""
    if not user.is_authenticated:
        return False
    if user.is_staff:
        return True
    perfil = getattr(user, 'perfil', None)
    return bool(perfil and perfil.classe_usuario == Perfil.EQUIPE_FNP)


def eh_aprovador(user):
    """Coordenação do Legislativo (Root/Administrador FNP) — aprova/rejeita e
    publica direto ao cadastrar."""
    return user.is_authenticated and user.is_staff


def get_proposicao_visivel(request, pk):
    """Proposição acessível ao público só se publicada; pendente/rejeitada só
    para aprovadores (que precisam pré-visualizar)."""
    proposicao = get_object_or_404(Proposicao, pk=pk)
    if proposicao.status_publicacao != Proposicao.PUBLICADA and not eh_aprovador(request.user):
        raise Http404()
    return proposicao


def notificar_participantes_da_discussao(proposicao, comentario):
    participantes_ids = (
        proposicao.comentarios
        .exclude(autor__isnull=True)
        .exclude(autor=comentario.autor)
        .values_list('autor_id', flat=True)
        .distinct()
    )
    Notificacao.objects.bulk_create(
        Notificacao(
            destinatario_id=destinatario_id,
            proposicao=proposicao,
            comentario=comentario,
            mensagem=f'Novo comentário de {comentario.autor} em "{proposicao.titulo}"',
        )
        for destinatario_id in participantes_ids
    )


def registrar_visualizacao(request, proposicao):
    Proposicao.objects.filter(pk=proposicao.pk).update(visualizacoes=F('visualizacoes') + 1)
    recentes = [pk for pk in get_recentes_ids(request) if pk != proposicao.pk]
    recentes.insert(0, proposicao.pk)
    request.session[RECENTES_SESSION_KEY] = recentes[:RECENTES_MAX]


FILTROS_ESTATISTICA = {
    'pauta': {'pauta': True},
    'urgentes': {'urgente': True},
    'alta_prioridade': {'prioridade_fnp': 'alta'},
    'com_relator': {'interlocutores__icontains': 'relator'},
}

FILTRO_LABELS = {
    'pauta': 'Na pauta',
    'urgentes': 'Urgentes',
    'alta_prioridade': 'Alta prioridade',
    'com_relator': 'Com relator',
}


def get_filtered_proposicoes(query, tema_slug, filtro=None):
    proposicoes = Proposicao.objects.publicadas().select_related('macrotema').prefetch_related('temas')
    if tema_slug:
        # tema_slug pode ser o slug de um Tema (subcategoria) ou de um
        # Macrotema (classificação editorial ampla) -- o chip exibido no
        # card/detalhe mostra o macrotema quando existe, então o link do
        # chip precisa casar com qualquer um dos dois.
        proposicoes = proposicoes.filter(Q(temas__slug=tema_slug) | Q(macrotema__slug=tema_slug))
    if filtro in FILTROS_ESTATISTICA:
        proposicoes = proposicoes.filter(**FILTROS_ESTATISTICA[filtro])
    if query:
        proposicoes = proposicoes.filter(
            Q(titulo__icontains=query)
            | Q(ementa_resumida__icontains=query)
            | Q(status_tramitacao__icontains=query)
            | Q(local__icontains=query)
            | Q(temas__nome__icontains=query)
            | Q(macrotema__nome__icontains=query)
        )
    if tema_slug or query:
        proposicoes = proposicoes.distinct()
    return proposicoes


PROPOSICOES_POR_PAGINA = 24


def get_home_sections(query, tema_slug, page_number=1, filtro=None):
    """Querysets das 3 seções da home que são globais (não dependem da sessão
    de quem acessa) — compartilhado entre HomeView e api_proposicoes_cards
    para o live-update via SSE não duplicar a lógica de filtro/ranking."""
    filtered = get_filtered_proposicoes(query, tema_slug, filtro)
    ordenadas = filtered.order_by('-urgente', '-pauta', '-aprovada', 'parada', 'prioridade_fnp', '-criado_em')
    page_obj = Paginator(ordenadas, PROPOSICOES_POR_PAGINA).get_page(page_number)

    base = Proposicao.objects.publicadas().select_related('macrotema').prefetch_related('temas')

    urgentes = base.filter(urgente=True).order_by('-criado_em')[:6]

    em_alta = (
        base.annotate(
            comentarios_count=Count(
                'comentarios', distinct=True, filter=Q(comentarios__status_moderacao='aprovado')
            ),
            likes_count=Count('comentarios__likes', distinct=True),
        )
        .annotate(relevancia=F('visualizacoes') + F('comentarios_count') * 5 + F('likes_count') * 2)
        .filter(Q(visualizacoes__gt=0) | Q(comentarios_count__gt=0) | Q(likes_count__gt=0))
        .order_by('-relevancia')[:4]
    )

    return {
        'filtered': filtered,
        'page_obj': page_obj,
        'proposicoes': page_obj.object_list,
        'urgentes': urgentes,
        'em_alta': em_alta,
    }


class HomeView(View):
    template_name = 'legislativo/home.html'

    def get(self, request):
        query = request.GET.get('q', '').strip()
        tema_slug = request.GET.get('tema', '').strip()
        filtro = request.GET.get('filtro', '').strip()
        page_number = request.GET.get('page', 1)
        temas = Tema.objects.order_by('nome')
        active_tema_obj = temas.filter(slug=tema_slug).first() if tema_slug else None
        if tema_slug and not active_tema_obj:
            # O chip de macrotema (mostrado no card/detalhe quando existe) também
            # linka pra cá -- o slug pode ser de um Macrotema, não de um Tema.
            active_tema_obj = Macrotema.objects.filter(slug=tema_slug).first()

        sections = get_home_sections(query, tema_slug, page_number, filtro)
        # Contagem dos cards de estatística reflete busca/tema (facetas de
        # contexto — "de quantos resultados da minha busca X são urgentes"),
        # mas nunca o próprio filtro de card já selecionado -- senão clicar
        # em "Na pauta" (0 resultados) zerava Total/Urgentes/Alta prioridade/
        # Com relator também, já que o cálculo de todos passava a partir do
        # conjunto já filtrado só por "na pauta" (achado via captura de tela
        # real, 2026-08-14).
        stats = compute_counts(get_filtered_proposicoes(query, tema_slug))
        page_obj = sections['page_obj']
        urgentes = sections['urgentes']
        em_alta = sections['em_alta']

        base = Proposicao.objects.publicadas().select_related('macrotema').prefetch_related('temas')

        recentes_ids = get_recentes_ids(request)
        recentes_map = {p.pk: p for p in base.filter(pk__in=recentes_ids)}
        recentes = [recentes_map[pk] for pk in recentes_ids if pk in recentes_map]

        areas_interesse = Tema.objects.filter(proposicoes__pk__in=recentes_ids).distinct()[:8] if recentes_ids else []
        if not areas_interesse:
            areas_interesse = Tema.objects.annotate(num=Count('proposicoes')).filter(num__gt=0).order_by('-num')[:8]

        # Qualquer filtro ativo (tema/área de interesse, busca ou card de
        # estatística) vira uma única lista consolidada em "Todas as
        # proposições", no topo -- em vez de deixar Urgentes/Em alta com
        # cards que não respeitam o filtro (era a origem do "filtro parece
        # quebrado": só a grade "Todas" reagia à busca/tema).
        filtro_ativo = bool(query or tema_slug or filtro)
        if filtro in FILTRO_LABELS:
            filtro_label = FILTRO_LABELS[filtro]
        elif active_tema_obj:
            filtro_label = active_tema_obj.nome
        elif query:
            filtro_label = f'Busca por “{query}”'
        else:
            filtro_label = None

        return render(
            request,
            self.template_name,
            {
                'proposicoes': page_obj.object_list,
                'page_obj': page_obj,
                'urgentes': urgentes,
                'em_alta': em_alta,
                'recentes': recentes,
                'areas_interesse': areas_interesse,
                'temas': temas,
                'active_tema': tema_slug,
                'active_tema_obj': active_tema_obj,
                'active_filtro': filtro,
                'filtro_ativo': filtro_ativo,
                'filtro_label': filtro_label,
                'query': query,
                'stats': stats,
                'favoritos_ids': get_favoritos_ids(request),
            },
        )


def compute_counts(proposicoes):
    return {
        'total': proposicoes.count(),
        'pauta': proposicoes.filter(pauta=True).count(),
        'urgentes': proposicoes.filter(urgente=True).count(),
        'alta_prioridade': proposicoes.filter(prioridade_fnp='alta').count(),
        'com_relator': proposicoes.filter(interlocutores__icontains='relator').count(),
    }


@require_GET
def api_proposicoes(request):
    query = request.GET.get('q', '').strip()
    tema_slug = request.GET.get('tema', '').strip()
    filtro = request.GET.get('filtro', '').strip()
    proposicoes = get_filtered_proposicoes(query, tema_slug, filtro)
    return JsonResponse({'counts': compute_counts(proposicoes)})


BUSCA_SUGESTOES_MIN_CHARS = 2
BUSCA_SUGESTOES_LIMITE = 8


@require_GET
def api_busca_sugestoes(request):
    """Sugestões da busca da home conforme o usuário digita — HTML pronto
    (mesmo princípio de api_proposicoes_cards: servidor monta, JS só exibe)."""
    query = request.GET.get('q', '').strip()
    html = ''
    if len(query) >= BUSCA_SUGESTOES_MIN_CHARS:
        proposicoes = get_filtered_proposicoes(query, '').order_by('-urgente', '-criado_em')[:BUSCA_SUGESTOES_LIMITE]
        html = render_to_string(
            'legislativo/_busca_sugestoes.html',
            {'proposicoes': proposicoes},
            request=request,
        )
    return JsonResponse({'html': html})


@require_GET
def api_proposicoes_cards(request):
    """Renderiza os cards (HTML pronto, mesmos templates da home) para o
    live-update via SSE — o JS só troca o innerHTML, nenhuma lógica de
    montagem de card duplicada em JS."""
    query = request.GET.get('q', '').strip()
    tema_slug = request.GET.get('tema', '').strip()
    filtro = request.GET.get('filtro', '').strip()
    page_number = request.GET.get('page', 1)
    sections = get_home_sections(query, tema_slug, page_number, filtro)
    favoritos_ids = get_favoritos_ids(request)

    def render_cards(proposicoes):
        return render_to_string(
            'legislativo/_cards_grid.html',
            {'proposicoes': proposicoes, 'favoritos_ids': favoritos_ids},
            request=request,
        )

    return JsonResponse({
        # Mesmo raciocínio do HomeView -- não usa sections['filtered'] (que
        # inclui o filtro de card já selecionado) pra não zerar os outros
        # cards quando o filtro ativo tiver poucos/nenhum resultado.
        'counts': compute_counts(get_filtered_proposicoes(query, tema_slug)),
        'sections': {
            'urgentes': render_cards(sections['urgentes']),
            'em_alta': render_cards(sections['em_alta']),
            'todas': render_cards(sections['proposicoes']),
        },
    })


@require_GET
def api_proposicao_detail(request, pk):
    proposicao = get_proposicao_visivel(request, pk)
    data = {
        'id': proposicao.pk,
        'titulo': proposicao.titulo,
        'casa': proposicao.get_casa_display() if hasattr(proposicao, 'get_casa_display') else proposicao.casa,
        'status_tramitacao': proposicao.status_tramitacao,
        'macrotema': {
            'nome': proposicao.macrotema.nome if proposicao.macrotema else None,
            'cor': proposicao.macrotema.cor if proposicao.macrotema else None,
        },
        'prioridade_fnp': proposicao.get_prioridade_fnp_display(),
        'urgente': proposicao.urgente,
        'aprovada': proposicao.aprovada,
        'pauta': proposicao.pauta,
        'temas': [tema.nome for tema in proposicao.temas.all()],
        'ementa_resumida': proposicao.ementa_resumida,
        'proximos_eventos': proposicao.proximos_eventos,
        'interlocutores': proposicao.interlocutores,
        'ultima_movimentacao': proposicao.ultima_movimentacao,
        'posicionamento_fnp': proposicao.posicionamento_fnp,
        'acoes_incidencia': proposicao.acoes_incidencia,
        'riscos_oportunidades': proposicao.riscos_oportunidades,
    }
    return JsonResponse(data)


SSE_POLL_INTERVAL_SECONDS = 5
SSE_MAX_ITERATIONS = 120  # ~10 min por conexão; o EventSource do navegador reconecta sozinho


def _sse_stream(query, tema_slug, filtro):
    last_snapshot = None
    for _ in range(SSE_MAX_ITERATIONS):
        proposicoes = get_filtered_proposicoes(query, tema_slug, filtro)
        latest = proposicoes.aggregate(Max('atualizado_em'))['atualizado_em__max']
        # Contagem dos cards de estatística não usa o filtro de card em si
        # (mesmo raciocínio de HomeView/api_proposicoes_cards) -- senão o
        # primeiro push do SSE, que sempre acontece por causa do
        # last_snapshot=None inicial, sobrescrevia os números certos do
        # primeiro render com os zerados (achado via captura de tela real,
        # 2026-08-14).
        counts = compute_counts(get_filtered_proposicoes(query, tema_slug))
        snapshot = (latest, tuple(sorted(counts.items())))

        if snapshot != last_snapshot:
            payload = {'counts': counts, 'updated': last_snapshot is not None}
            yield f'data: {json.dumps(payload, default=str)}\n\n'
            last_snapshot = snapshot
        else:
            yield ': heartbeat\n\n'

        time.sleep(SSE_POLL_INTERVAL_SECONDS)


@require_GET
def api_proposicao_sse(request):
    query = request.GET.get('q', '').strip()
    tema_slug = request.GET.get('tema', '').strip()
    filtro = request.GET.get('filtro', '').strip()

    response = StreamingHttpResponse(
        _sse_stream(query, tema_slug, filtro),
        content_type='text/event-stream',
    )
    response['Cache-Control'] = 'no-cache'
    response['X-Accel-Buffering'] = 'no'
    return response


@require_POST
def toggle_favorito(request, pk):
    proposicao = get_object_or_404(Proposicao.objects.publicadas(), pk=pk)
    favoritos = request.session.get(FAVORITOS_SESSION_KEY, [])
    if proposicao.pk in favoritos:
        favoritos.remove(proposicao.pk)
    else:
        favoritos.append(proposicao.pk)
    request.session[FAVORITOS_SESSION_KEY] = favoritos

    next_url = request.POST.get('next', '')
    if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        return redirect(next_url)
    return redirect('legislativo:home')


@login_required
@require_POST
def toggle_seguir(request, pk):
    """Segue/deixa de seguir uma proposição (watchlist por conta, base do
    alerta individual) -- separado do favoritar por sessão."""
    proposicao = get_object_or_404(Proposicao.objects.publicadas(), pk=pk)
    seguida, criada = ProposicaoSeguida.objects.get_or_create(usuario=request.user, proposicao=proposicao)
    if not criada:
        seguida.delete()

    next_url = request.POST.get('next', '')
    if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        return redirect(next_url)
    return redirect('legislativo:proposicao_detail', pk=pk)


@login_required
@require_POST
def denunciar_comentario(request, pk):
    comentario = get_object_or_404(Comentario, pk=pk)
    if not rate_limited('denuncia', request, limit=10, window_seconds=600):
        _, criada = DenunciaComentario.objects.get_or_create(comentario=comentario, denunciante=request.user)

        if criada and comentario.status_moderacao == 'aprovado':
            if comentario.denuncias.count() >= Comentario.DENUNCIAS_PARA_OCULTAR:
                comentario.status_moderacao = 'pendente'
                comentario.save(update_fields=['status_moderacao'])

    next_url = request.POST.get('next', '')
    if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        return redirect(next_url)
    return redirect('legislativo:proposicao_detail', pk=comentario.proposicao_id)


@login_required
@require_POST
def excluir_comentario(request, pk):
    """Só o próprio autor exclui, e só se ninguém respondeu ainda --
    Comentario.parent é on_delete=CASCADE, então excluir um comentário com
    respostas apagaria também respostas de outras pessoas junto."""
    comentario = get_object_or_404(Comentario, pk=pk, autor=request.user)
    proposicao_id = comentario.proposicao_id
    if not comentario.respostas.exists():
        comentario.delete()

    next_url = request.POST.get('next', '')
    if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        return redirect(next_url)
    return redirect('legislativo:proposicao_detail', pk=proposicao_id)


@login_required
@require_POST
def enviar_anexo(request, pk):
    proposicao = get_object_or_404(Proposicao, pk=pk)
    if not rate_limited('anexo', request, limit=5, window_seconds=600):
        form = AnexoProposicaoForm(request.POST, request.FILES)
        if form.is_valid():
            anexo = form.save(commit=False)
            anexo.proposicao = proposicao
            anexo.enviado_por = request.user
            anexo.save()

    next_url = request.POST.get('next', '')
    if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        return redirect(next_url)
    return redirect('legislativo:proposicao_detail', pk=proposicao.pk)


@login_required
@require_POST
def excluir_anexo(request, pk):
    """Só o autor do envio ou staff (Root/Administrador FNP) excluem --
    mesmo modelo de excluir_comentario."""
    anexo = get_object_or_404(AnexoProposicao, pk=pk)
    proposicao_id = anexo.proposicao_id
    if anexo.enviado_por_id == request.user.id or request.user.is_staff:
        anexo.delete()

    next_url = request.POST.get('next', '')
    if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        return redirect(next_url)
    return redirect('legislativo:proposicao_detail', pk=proposicao_id)


@login_required
@require_POST
def curtir_comentario(request, pk):
    comentario = get_object_or_404(Comentario, pk=pk)
    like, criado = ComentarioLike.objects.get_or_create(comentario=comentario, usuario=request.user)
    if not criado:
        like.delete()

    next_url = request.POST.get('next', '')
    if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        return redirect(next_url)
    return redirect('legislativo:proposicao_detail', pk=comentario.proposicao_id)


class FavoritosListView(View):
    template_name = 'legislativo/favoritos_list.html'

    def get(self, request):
        favoritos_ids = get_favoritos_ids(request)
        favoritos_map = {
            p.pk: p
            for p in Proposicao.objects.publicadas().select_related('macrotema').prefetch_related('temas').filter(pk__in=favoritos_ids)
        }
        proposicoes = [favoritos_map[pk] for pk in favoritos_ids if pk in favoritos_map]
        return render(
            request,
            self.template_name,
            {
                'proposicoes': proposicoes,
                'favoritos_ids': favoritos_ids,
                'page_title': 'Favoritos',
                'mostrar_sidebar': True,
            },
        )


class SeguindoListView(View):
    """Monitoramento individual: lista as proposições que o usuário segue
    (ProposicaoSeguida), base dos alertas no sistema e por e-mail. Diferente de
    Favoritos (sessão/anônimo), seguir exige conta -- por isso login_required."""

    template_name = 'legislativo/seguindo_list.html'

    @method_decorator(login_required)
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        proposicoes = (
            Proposicao.objects.publicadas()
            .select_related('macrotema')
            .prefetch_related('temas')
            .filter(seguidores__usuario=request.user)
            .order_by('-seguidores__criado_em')
        )
        return render(
            request,
            self.template_name,
            {
                'proposicoes': proposicoes,
                'favoritos_ids': get_favoritos_ids(request),
                'recebe_email': getattr(getattr(request.user, 'perfil', None), 'receber_email_atualizacoes', False),
                'page_title': 'Seguindo',
                'mostrar_sidebar': True,
            },
        )


@login_required
def ler_notificacao(request, pk):
    notificacao = get_object_or_404(Notificacao, pk=pk, destinatario=request.user)
    notificacao.lida = True
    notificacao.save(update_fields=['lida'])
    return redirect('legislativo:proposicao_detail', pk=notificacao.proposicao_id)


@login_required
@require_POST
def marcar_notificacoes_lidas(request):
    request.user.notificacoes.filter(lida=False).update(lida=True)
    next_url = request.POST.get('next', '')
    if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        return redirect(next_url)
    return redirect('legislativo:home')


NIVEIS_RESPOSTA_PREFETCH = 4  # respostas mais profundas que isso ainda funcionam, só sem prefetch (N+1)


class ProposicaoDetailView(View):
    template_name = 'legislativo/proposicao_detail.html'

    def _forum_context(self, proposicao, usuario):
        aprovados = proposicao.comentarios.filter(status_moderacao='aprovado')
        if usuario.is_authenticated:
            curtido_por_mim = Exists(ComentarioLike.objects.filter(comentario=OuterRef('pk'), usuario=usuario))
        else:
            curtido_por_mim = Value(False, output_field=BooleanField())

        def com_anotacoes(queryset):
            return queryset.annotate(likes_count=Count('likes', distinct=True), curtido_por_mim=curtido_por_mim)

        respostas_qs = com_anotacoes(Comentario.objects.filter(status_moderacao='aprovado').select_related('autor'))
        prefetches = []
        caminho = 'respostas'
        for _ in range(NIVEIS_RESPOSTA_PREFETCH):
            prefetches.append(Prefetch(caminho, queryset=respostas_qs))
            caminho += '__respostas'
        comentarios = com_anotacoes(
            aprovados.filter(parent__isnull=True).select_related('autor')
        ).prefetch_related(*prefetches)
        return {
            'comentarios': comentarios,
            'comentarios_count': aprovados.count(),
            'participantes_count': aprovados.exclude(autor__isnull=True).values('autor_id').distinct().count(),
        }

    def _anexos_context(self, proposicao, usuario):
        """Anexo nasce sempre 'pendente' (sem checagem automática de
        conteúdo de arquivo) -- só aparece pra quem não é staff depois de
        aprovado. O próprio autor do envio continua vendo o dele enquanto
        pendente (senão pareceria que o upload sumiu), mas não um rejeitado
        -- mesmo princípio de Comentario (rejeitado nunca fica visível)."""
        anexos = proposicao.anexos.select_related('enviado_por')
        if not (usuario.is_authenticated and usuario.is_staff):
            filtro = Q(status_moderacao='aprovado')
            if usuario.is_authenticated:
                filtro |= Q(enviado_por=usuario)
            anexos = anexos.filter(filtro)
        return anexos

    def _breadcrumb(self):
        return {
            'breadcrumb_parent_label': 'Proposições',
            'breadcrumb_parent_url': reverse('legislativo:home'),
        }

    def get(self, request, pk):
        proposicao = get_proposicao_visivel(request, pk)
        registrar_visualizacao(request, proposicao)
        comentario_form = ComentarioForm(initial={'parent': None}, proposicao=proposicao)
        return render(
            request,
            self.template_name,
            {
                'proposicao': proposicao,
                'page_title': proposicao.titulo,
                'comentario_form': comentario_form,
                'anexo_form': AnexoProposicaoForm(),
                'anexos': self._anexos_context(proposicao, request.user),
                'favoritos_ids': get_favoritos_ids(request),
                **self._breadcrumb(),
                **self._forum_context(proposicao, request.user),
            },
        )

    def post(self, request, pk):
        proposicao = get_proposicao_visivel(request, pk)
        comentario_form = ComentarioForm(initial={'parent': None}, proposicao=proposicao)
        success_message = None

        if rate_limited('comentario', request, limit=5, window_seconds=300):
            success_message = 'Você enviou comentários rápido demais. Aguarde alguns minutos e tente novamente.'
        else:
            comentario_form = ComentarioForm(request.POST, request.FILES, proposicao=proposicao)
            if comentario_form.is_valid():
                comentario = comentario_form.save(commit=False)
                comentario.proposicao = proposicao
                if request.user.is_authenticated:
                    comentario.autor = request.user
                comentario.status_moderacao = classificar_comentario(comentario.texto)
                comentario.save()
                if comentario.status_moderacao == 'aprovado':
                    if comentario.autor_id:
                        notificar_participantes_da_discussao(proposicao, comentario)
                    return redirect('legislativo:proposicao_detail', pk=pk)
                success_message = 'Seu comentário não foi publicado por conter um termo não permitido nesta plataforma.'
                comentario_form = ComentarioForm(initial={'parent': None}, proposicao=proposicao)
            elif comentario_form.non_field_errors():
                success_message = ' '.join(comentario_form.non_field_errors())

        return render(
            request,
            self.template_name,
            {
                'proposicao': proposicao,
                'page_title': proposicao.titulo,
                'comentario_form': comentario_form,
                'anexo_form': AnexoProposicaoForm(),
                'anexos': self._anexos_context(proposicao, request.user),
                'favoritos_ids': get_favoritos_ids(request),
                'success_message': success_message,
                **self._breadcrumb(),
                **self._forum_context(proposicao, request.user),
            },
        )


class ParticipacaoListView(View):
    """A página Participações mostra o engajamento de fato do usuário logado no fórum
    (proposições em que comentou) -- o formulário avulso "Enviar participação"
    foi removido da página da proposição a pedido do usuário (Participacao
    segue existindo pra dado histórico/exportação via Admin)."""

    template_name = 'legislativo/participacao_list.html'

    @method_decorator(login_required)
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        proposicoes = (
            Proposicao.objects.publicadas().select_related('macrotema')
            .prefetch_related('temas')
            .filter(comentarios__autor=request.user, comentarios__status_moderacao='aprovado')
            .distinct()
            .order_by('-comentarios__criado_em')
        )
        return render(
            request,
            self.template_name,
            {
                'proposicoes': proposicoes,
                'favoritos_ids': get_favoritos_ids(request),
                'page_title': 'Participações',
                'mostrar_sidebar': True,
            },
        )


def usuario_publico(request, pk):
    usuario = get_object_or_404(Usuario.objects.select_related('perfil__municipio'), pk=pk)
    proposicoes = (
        Proposicao.objects.publicadas().select_related('macrotema')
        .prefetch_related('temas')
        .filter(comentarios__autor=usuario, comentarios__status_moderacao='aprovado')
        .distinct()
        .order_by('-comentarios__criado_em')
    )
    return render(
        request,
        'legislativo/usuario_publico.html',
        {
            'usuario_perfil': usuario,
            'proposicoes': proposicoes,
            'favoritos_ids': get_favoritos_ids(request),
            'page_title': usuario.get_display_name(),
        },
    )


CAMPOS_TECNICOS_FONTE = ('casa', 'status_tramitacao', 'local', 'ultima_movimentacao', 'ementa_resumida', 'link')


@login_required
def cadastrar_proposicao(request):
    if not pode_cadastrar(request.user):
        raise PermissionDenied()

    form = CadastroProposicaoForm(request.POST or None)
    resultado = None
    erro = None

    if request.method == 'POST':
        if rate_limited('cadastro_proposicao', request, limit=20, window_seconds=600):
            erro = 'Muitos cadastros em sequência. Aguarde alguns minutos e tente novamente.'
        elif form.is_valid():
            dados = buscar_proposicao(
                form.cleaned_data['tipo'],
                form.cleaned_data['numero'],
                form.cleaned_data['ano'],
            )
            if not dados:
                erro = 'Proposição não encontrada na Câmara nem no Senado. Confira o tipo, o número e o ano.'
            else:
                campos = {campo: dados[campo] for campo in CAMPOS_TECNICOS_FONTE}
                existente = Proposicao.objects.filter(titulo=dados['titulo']).first()
                if existente:
                    # Já está no painel -- só atualiza a ficha técnica, nunca
                    # mexe no status de publicação nem na curadoria da FNP.
                    for campo, valor in campos.items():
                        if valor:
                            setattr(existente, campo, valor)
                    existente.save()
                    resultado = {
                        'proposicao': existente,
                        'ja_existia': True,
                        'visivel': existente.status_publicacao == Proposicao.PUBLICADA,
                    }
                else:
                    aprovador = eh_aprovador(request.user)
                    proposicao = Proposicao(
                        titulo=dados['titulo'],
                        submetido_por=request.user,
                        status_publicacao=Proposicao.PUBLICADA if aprovador else Proposicao.PENDENTE,
                        **campos,
                    )
                    if aprovador:
                        proposicao.aprovado_por = request.user
                        proposicao.aprovado_em = timezone.now()
                    proposicao.save()
                    resultado = {
                        'proposicao': proposicao,
                        'ja_existia': False,
                        'visivel': aprovador,
                    }
                form = CadastroProposicaoForm()

    return render(
        request,
        'legislativo/cadastrar_proposicao.html',
        {
            'form': form,
            'resultado': resultado,
            'erro': erro,
            'page_title': 'Cadastrar proposição',
            'mostrar_sidebar': True,
        },
    )


@login_required
def proposicoes_pendentes(request):
    if not eh_aprovador(request.user):
        raise PermissionDenied()
    pendentes = (
        Proposicao.objects.pendentes()
        .select_related('submetido_por', 'macrotema')
        .order_by('-criado_em')
    )
    return render(
        request,
        'legislativo/proposicoes_pendentes.html',
        {
            'pendentes': pendentes,
            'page_title': 'Pendentes de aprovação',
            'mostrar_sidebar': True,
        },
    )


@login_required
@require_POST
def aprovar_proposicao(request, pk):
    if not eh_aprovador(request.user):
        raise PermissionDenied()
    proposicao = get_object_or_404(Proposicao, pk=pk)
    proposicao.status_publicacao = Proposicao.PUBLICADA
    proposicao.aprovado_por = request.user
    proposicao.aprovado_em = timezone.now()
    proposicao.motivo_rejeicao = ''
    proposicao.save()
    return redirect('legislativo:proposicoes_pendentes')


@login_required
@require_POST
def rejeitar_proposicao(request, pk):
    if not eh_aprovador(request.user):
        raise PermissionDenied()
    proposicao = get_object_or_404(Proposicao, pk=pk)
    proposicao.status_publicacao = Proposicao.REJEITADA
    proposicao.aprovado_por = request.user
    proposicao.aprovado_em = timezone.now()
    proposicao.motivo_rejeicao = request.POST.get('motivo', '').strip()
    proposicao.save()
    return redirect('legislativo:proposicoes_pendentes')


@login_required
def cadastro_pendente(request):
    return render(
        request,
        'legislativo/cadastro_pendente.html',
        {
            'perfil': request.user.perfil,
            'page_title': 'Cadastro em análise',
        },
    )


def politica_privacidade(request):
    return render(request, 'legislativo/politica_privacidade.html', {'page_title': 'Política de Privacidade'})


@login_required
def solicitar_exclusao(request):
    perfil = request.user.perfil
    if request.method == 'POST':
        perfil.exclusao_solicitada_em = timezone.now()
        perfil.save(update_fields=['exclusao_solicitada_em'])
        return render(
            request,
            'legislativo/solicitar_exclusao.html',
            {'page_title': 'Solicitar exclusão da conta', 'enviado': True, 'mostrar_sidebar': True},
        )
    return render(
        request,
        'legislativo/solicitar_exclusao.html',
        {'page_title': 'Solicitar exclusão da conta', 'perfil': perfil, 'mostrar_sidebar': True},
    )


class PerfilView(View):
    template_name = 'legislativo/perfil.html'

    @method_decorator(login_required)
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        form = PerfilForm(instance=request.user)
        dados_form = PerfilDadosForm(instance=request.user.perfil)
        return self._render(request, form, dados_form)

    def post(self, request):
        form = PerfilForm(request.POST, instance=request.user)
        dados_form = PerfilDadosForm(request.POST, request.FILES, instance=request.user.perfil)
        success_message = None
        if form.is_valid() and dados_form.is_valid():
            form.save()
            dados_form.save()
            success_message = 'Perfil atualizado com sucesso.'
        return self._render(request, form, dados_form, success_message)

    def _render(self, request, form, dados_form, success_message=None):
        return render(
            request,
            self.template_name,
            {
                'form': form,
                'dados_form': dados_form,
                'success_message': success_message,
                'page_title': 'Meu perfil',
                'mostrar_sidebar': True,
            },
        )

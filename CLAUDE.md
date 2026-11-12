# CLAUDE.md — Contexto do Projeto Legislativo FNP

> Arquivo de contexto para sessões com Claude Code. Atualizado automaticamente a cada 3h enquanto há sessão ativa (mantém este arquivo fiel ao código para evitar redescoberta/gasto de tokens em sessões futuras).
> Diário datado das sessões: `docs/historico-sessoes.md` (registrar sessões novas lá, não aqui).

---

## Visão Geral

**Legislativo FNP** é uma plataforma Django para acompanhamento legislativo voltada ao monitoramento de proposições em tramitação no Congresso Nacional e ao impacto para municípios. A proposta é reunir um painel institucional, visual profissional e fluxo de participação colaborativa para a Frente Nacional de Prefeitas e Prefeitos.

URL de produção: `legislativo.fnp.org.br` (droplet `fnp-web` na DigitalOcean, via branch `main` do remoto `production`) — **no ar desde 2026-08-05**, HTTPS via Let's Encrypt/certbot, confirmado com `curl` retornando 200 e HTML real da home
Branch de desenvolvimento ativo: `next`

---

## Stack Tecnológica

| Camada | Tecnologia |
|---|---|
| Backend | Django 5.2 LTS (suporte até abril/2028) · Python 3.12 local (via `.venv/`) / 3.11 no CI |
| Auth | django-allauth 65.x (login por e-mail + Google OAuth, cadastro manual ou Google) |
| Banco (dev) | SQLite |
| Banco (prod) | PostgreSQL 18 (DigitalOcean Managed Database, `fnp-database`), via `DATABASE_URL` |
| Templates | Django Templates + CSS/JS vanilla |
| Estáticos | WhiteNoise |
| Dados | Modelos Django + management commands (`ingest_legislativo`, `sync_legado_firestore`, `sync_camara --watch`) |
| Mídia | Pillow (`ImageField` de `Perfil.foto`), `MEDIA_URL`/`MEDIA_ROOT` (`media/`, gitignored; servido via `runserver` em DEBUG) |
| UI | HTML semântico, CSS customizado (dark mode via `[data-theme]`), JavaScript vanilla |
| Testes | Django TestCase (via `RequestFactory`, não `self.client` — ver Observações) + pytest |

**Observação sobre Python 3.14:** documentado quando o projeto ainda estava no Django 4.2 — havia um bug real do próprio Django (`copy.copy()` sobre `RequestContext`, `django/template/context.py`) que quebrava `self.client.get(...)` nos testes E as telas de listagem/adicionar do Admin em runtime normal no 3.14. Com o upgrade pra Django 5.2 (2026-08-06), esse bug pode já ter sido corrigido nas versões mais novas — **não testado/reverificado ainda** (o `.venv/` local continua em Python 3.12 por segurança). Os testes continuam usando `RequestFactory` + `SessionMiddleware` manual (funciona em qualquer versão, mantido por robustez), independente disso.

---

## Estrutura de Arquivos

```
apps/
  usuarios/                 # Usuario (auth nativo), Perfil (FK p/ Municipio), Municipio
    models.py                # Perfil: status_aprovacao, foto, foto_google_url, setor_responsavel, exclusao_solicitada_em
    admin.py                # UsuarioAdmin (UserAdmin nativo) + PerfilInline + status de cadastro/exclusão
    signals.py               # cria Perfil (status pendente p/ novo usuário, aprovado p/ staff); atualiza foto do Google em login social
    middleware.py            # CadastroPendenteMiddleware — bloqueia navegação até perfil ser aprovado
    adapters.py              # GoogleAccountAdapter — importa foto de perfil do Google no signup social
  proposicoes/               # Proposicao, Macrotema, Tema, Noticia, EdicaoMeritoHistorico
    models.py
    admin.py                 # ProposicaoAdmin.save_model grava EdicaoMeritoHistorico; badge de macrotema; inlines
  comentarios/                # Comentario, Participacao, Notificacao, PalavraProibida, DenunciaComentario
    models.py                  # Comentario.DENUNCIAS_PARA_OCULTAR = 3
    admin.py                 # ações em massa de moderação de comentário; PalavraProibida e DenunciaComentario registradas aqui
    moderacao.py              # classificar_comentario() — aprova/rejeita automaticamente por palavra proibida
  legislativo/                # camada de views/urls/forms que orquestra os 3 apps acima
    admin.py                  # vazio (registros vivem nos apps de domínio)
    admin_site.py              # FNPAdminSite/FNPAdminConfig — AdminSite customizado (dashboard, index_template)
    models.py                 # vazio (models vivem nos apps de domínio)
    views.py                 # get_home_sections() pagina "Todas as proposições" (24/página); denunciar_comentario()
    urls.py
    forms.py                # CustomSignupForm (município/UF/setor/cargo/telefone), PerfilForm, PerfilDadosForm, ComentarioForm, ParticipacaoForm
    context_processors.py   # notificacoes + usuario_display_name + usuario_avatar_url
    data_utils.py            # split_temas() — separa temas compostos "A, B/C"
    throttling.py             # rate_limited() — throttle simples via cache (sessão+IP), sem dependência externa
    templatetags/
      admin_icons.py           # ícones SVG por app/model do Django Admin (barra lateral)
    tests.py                  # ~134 testes — RequestFactory/middleware direto, não self.client (ver Observação Python 3.14)
    management/
      commands/
        ingest_legislativo.py
        sync_legado_firestore.py  # importa as 104 proposições reais do Firestore legado (legislativo-fnp.web.app)
        sync_camara.py             # sync ao vivo via API Dados Abertos da Câmara, com --watch
        setup_roles.py              # cria grupos Root/Administrador FNP/Usuário, promove um e-mail a Root
static/
  css/
    style.css               # app-shell, dark mode, acessibilidade, cards, auth
    admin-custom.css         # reskin completo do Django Admin (identidade "Território": tema sempre claro, sidebar clara igual ao site público)
  js/
    main.js
  img/
    logo-FNP.png
  favicon.svg
templates/
  base.html                  # rodapé (_footer.html) só renderiza na home (url_name == 'home')
  _sidebar.html             # menu lateral, só renderiza na página de perfil (mostrar_sidebar=True)
  _topbar.html              # barra superior (breadcrumb "Voltar | Título", busca, tema, notificações, avatar)
  _footer.html               # rodapé com links de LGPD (Política de Privacidade/Exportar/Excluir) + DPO
  _search_modal.html        # busca via Ctrl+K
  socialaccount/login.html  # confirmação de login Google, traduzida
  allauth/layouts/base.html # override raiz de TODAS as páginas do allauth (login/signup/logout/etc.)
  admin/
    base_site.html            # dark mode do Django Admin desativado; header/breadcrumb sempre claros
    fnp_index.html             # dashboard com métricas acionáveis (pendências de moderação/aprovação/exclusão)
    nav_sidebar.html           # sidebar do Admin reescrita (sem tabela padrão do Django, com ícones e grupos recolhíveis)
  legislativo/
    home.html
    perfil.html                # + upload de foto, município/UF/setor/telefone, links de conta/privacidade
    proposicao_detail.html
    participacao_list.html
    favoritos_list.html
    cadastro_pendente.html      # tela de bloqueio para cadastro ainda não aprovado
    politica_privacidade.html
    solicitar_exclusao.html
    _proposicao_card.html
setup/
  settings.py                 # LOGIN_URL, MEDIA_URL/ROOT, SOCIALACCOUNT_ADAPTER, cabeçalhos de segurança (if not DEBUG)
  urls.py
  wsgi.py
  asgi.py
docs/
  README.md
  runbook.md
  adr/
    0001-initial-architecture.md
```

---

## Arquitetura do Projeto

### Domínio principal

Os models são divididos por domínio em `apps.usuarios`, `apps.proposicoes` e `apps.comentarios` (ver Estrutura de Arquivos). O app `legislativo` continua sendo a camada de views/urls/forms/templates que orquestra os três — todo `{% url 'legislativo:...' %}` nos templates continua válido, só os models/admins mudaram de app. Responsabilidades:
- modelar proposições (104 reais, migradas do Firestore legado), macrotemas, temas (M2M), comentários, participação, notificações e usuários
- renderizar a homepage com cards de briefing compactos (Urgentes, Áreas de interesse, Em alta, Últimos acessados, Todas)
- exibir detalhes de proposições e suportar fórum de comentários com notificação aos participantes
- autenticação opcional (navegação pública não exige login) via e-mail/senha ou Google OAuth

### Identidade visual "Território" (FNP, desde 2026-11-12)

Paleta e tipografia únicas da FNP, trazidas de `sistema-fnp` (guia de estilo
compartilhado pelo usuário) pro legislativo-fnp inteiro — site público e
Django Admin: marinho `#103758` / azul `#1e4784` / amarelo `#eeaf19` como
cores de marca, tipografia Outfit (títulos/botões) + Work Sans (texto),
ícones Phosphor Icons 2.1.1 variante duotone (baixados via CDN público
`cdn.jsdelivr.net/npm/@phosphor-icons/core`, vendorizados inline nos
templates — MIT, sem precisar do repositório privado `sistema-fnp` pra
isso). Tokens de cor/raio/sombra em `:root` de `static/css/style.css`; as
variáveis antigas (`--bg-surface`, `--text-primary`...) viram alias das
novas, pra não quebrar o CSS já existente. Selos/tags são cor chapada, nunca
degradê ("cor pelo significado, nunca por enfeite", regra do guia de
estilo). `apps/legislativo/templatetags/admin_icons.py` é a fonte única dos
ícones tanto do site público quanto do Admin.

**Pendente**: os 3 SVGs de pattern decorativo (bastões) e qualquer variante
de logo exclusiva do `sistema-fnp` — aguardando acesso ao repositório (ver
Pendências).

### Padrão atual de UI

- **Pública (não logado):** header simples só com logo (link externo para fnp.org.br) + botão "Entrar". Sem sidebar.
- **Autenticada:** topbar estilo breadcrumb (`← Voltar | Título`, oculto na própria home, fundo marinho) + busca Ctrl+K, tamanho de fonte, dark mode, notificações, avatar (foto real se houver, senão inicial em círculo amarelo) com nome "Nome Sobrenome" + sidebar lateral colapsável clara (fundo céu, item ativo em pílula amarela) — exibida sempre que a view define `mostrar_sidebar=True` no contexto (perfil, favoritos, seguindo, participações, monitoramento interno, radar tributário, cadastro/pendentes de proposição, solicitar exclusão — não só `/perfil/`). Logo da FNP (link externo) aparece no topbar nas páginas sem sidebar.
- Cards compactos (redesenhados segundo justinmind.com/ui-design/cards): badges de prioridade/urgência, chip de tema, meta-linha com ícones (Casa/Status/Municípios), estrela de favorito sem círculo.
- "Áreas de interesse" e filtro de tema em dropdown pesquisável (mesmo componente, `.tema-dropdown`).
- Rodapé (`_footer.html`) só renderiza na home — demais páginas não o incluem (`base.html` checa `request.resolver_match.url_name == 'home'`); tem links de LGPD (Política de Privacidade, Exportar meus dados, Solicitar exclusão) + contato do DPO.
- Acessibilidade: skip links (Alt+1/Alt+2), `:focus-visible` global, `prefers-reduced-motion`, `role="search"`, `aria-hidden` em ícones decorativos — ver `docs/adr` / commit `8efad51`.
- Dark mode via `[data-theme="dark"]` + `localStorage['fnp-theme']`; tamanho de fonte via `localStorage['fnp-font-size']`; sidebar colapsada via `localStorage['fnp-sidebar-collapsed']`. A sidebar (pública e do Admin) **não muda** com o dark mode — fundo céu claro é parte fixa da marca, independente do tema da página.

### Django Admin — identidade visual e navegação própria

O Admin não usa mais o tema padrão do Django nem o modo escuro nativo (removido em `templates/admin/base_site.html` — `dark-mode-vars` vazio, sem toggle): cabeçalho, breadcrumb e conteúdo são **sempre claros**. A barra lateral **também é clara** desde 2026-11-12 (pedido explícito do usuário, reabrindo a decisão antiga de "sidebar do Admin sempre escura, mesma paleta do site público porém invertida") — hoje usa a mesma paleta clara (céu/marinho/pílula amarela) da sidebar pública, ver "Identidade Território" acima. `AdminSite` customizado (`apps/legislativo/admin_site.py`, ligado via `FNPAdminConfig` no lugar de `django.contrib.admin` em `INSTALLED_APPS`) adiciona um dashboard na página inicial (`templates/admin/fnp_index.html`) com métricas clicáveis: comentários pendentes, cadastros pendentes, solicitações de exclusão, proposições urgentes/na pauta.

A barra lateral (`templates/admin/nav_sidebar.html`) **não reaproveita** `admin/app_list.html` (a versão do Django sempre renderiza o link "Adicionar" de cada model, show_changelinks só controla "Modificar") — é um loop próprio: apps com 1 model viram link direto, apps com mais de um viram grupo recolhível (`<details>/<summary>` nativos, sem JS próprio), cada um com ícone (`apps/legislativo/templatetags/admin_icons.py`, mapeamento por `app_label`/`object_name` com fallback). Cuidado ao mexer aqui: Django define `a:link, a:visited { color: var(--link-fg) }` globalmente com especificidade maior que uma classe simples — qualquer novo link na sidebar precisa repetir os pseudo-seletores (`#nav-sidebar .algo:link, #nav-sidebar .algo:visited`) ou fica ilegível nos links já visitados.

### Funcionalidades já implementadas

- Home com listagem, busca, filtro por tema (M2M), estatísticas (total, pauta, urgentes, alta prioridade, com relator)
- Favoritos e "últimos acessados" (funcionam mesmo sem login, via sessão)
- "Em alta" (ranking por visualizações + comentários) e "Áreas de interesse" (derivado dos temas mais acessados)
- Cadastro e login (e-mail/senha próprio ou Google OAuth via django-allauth); credenciais reais do Google já criadas (2026-08-04) e preenchidas no `.env` local — tela de consentimento OAuth ainda em modo "teste" (só e-mails listados como usuário de teste no Google Cloud Console conseguem logar), ver pendências
- Cadastro coleta município/UF (vira `Municipio` via `get_or_create`, `Perfil.municipio` é `ForeignKey` — vários usuários podem apontar pro mesmo município), setor responsável, cargo e telefone; mesmos campos editáveis depois em `/perfil/`
- **Aprovação de cadastro:** todo cadastro novo nasce `Perfil.status_aprovacao='pendente'` (staff nasce `'aprovado'`); `CadastroPendenteMiddleware` redireciona usuário pendente para `cadastro_pendente.html` até um Root/Administrador FNP aprovar ou rejeitar (ações em massa simétricas `aprovar_cadastros`/`rejeitar_cadastros` no `UsuarioAdmin`) — tela de aviso já distingue mensagem para pendente vs. rejeitado
- **Fotos de perfil:** upload manual (`Perfil.foto`) ou importação automática da foto do Google no login social (`GoogleAccountAdapter.save_user` + signal `pre_social_login` para manter atualizada); exibida no topbar e nos comentários do fórum, com fallback pra inicial do nome
- **LGPD:** página de Política de Privacidade e solicitação de exclusão de conta (`solicitar_exclusao` — só marca `Perfil.exclusao_solicitada_em`, exclusão real é manual pelo Root via Admin, sem autoexclusão instantânea). `UsuarioAdmin` tem ações simétricas `aprovar_exclusoes` (reaproveita a tela de confirmação nativa do `delete_selected` — exclui de fato) e `rejeitar_exclusoes` (limpa `exclusao_solicitada_em`, mantém a conta), no mesmo padrão de `aprovar_cadastros`/`rejeitar_cadastros`. Exportação de dados **não é mais self-service** (removido em 2026-08-07, a pedido do usuário) — só o Root exporta, via `/admin/exportar-dados/` (ver "Exportar dados de engajamento" no Estado Atual); pedido de portabilidade de dados de um usuário específico é atendido por lá, selecionando a pessoa.
- Página de perfil (`PerfilView`) com edição de nome/foto/telefone/cargo/município/UF/setor responsável, link para trocar senha (allauth) e para as ações de LGPD
- Fórum de comentários por proposição com notificação aos demais participantes da discussão (só dispara se o comentário for aprovado)
- **Moderação automática de comentários** (`apps/comentarios/moderacao.py`): comentário nasce aprovado por padrão (sem fila manual); é reprovado automaticamente só se contiver alguma `PalavraProibida` ativa (lista editável via Admin, nunca hardcoded). Checagem por fronteira de palavra (`\b`), sem acento/caixa — evita falso positivo tipo "droga" bloquear "drogaria" (Scunthorpe problem). Autor vê mensagem explicando a reprovação. Comentários "pendente" anteriores a essa mudança continuam precisando de revisão manual (ação em massa no Admin) — o auto-approve só vale pra novos envios.
- **Denúncia de comentário** (`DenunciaComentario`, botão "Denunciar" no fórum, `denunciar_comentario` em views.py): complemento à lista de palavras proibidas para pegar assédio/sarcasmo sem palavrão. Usuário logado denuncia (não pode denunciar 2x o mesmo comentário — `UniqueConstraint`); ao atingir `Comentario.DENUNCIAS_PARA_OCULTAR` (3) denúncias distintas, o comentário volta sozinho para `'pendente'` até revisão manual. Visível no Admin (`total_denuncias` na listagem de Comentario).
- **Rate limiting** nos formulários públicos (`apps/legislativo/throttling.py`): máx. 5 comentários/5min e 3 participações/10min por sessão+IP, via cache padrão do Django (sem dependência nova — trocar para Redis se o app crescer para múltiplos workers).
- **Paginação** na home: "Todas as proposições" pagina de 24 em 24 (`PROPOSICOES_POR_PAGINA` em views.py) via `Paginator`; "Urgentes"/"Em alta" continuam como destaques fixos (6/4 itens), não paginam.
- **Cabeçalhos de segurança de produção**: `SECURE_SSL_REDIRECT`, HSTS, `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE` — só ativam com `DEBUG=False`, assumindo que o Nginx do droplet repassa `X-Forwarded-Proto` (confirmar antes de habilitar em produção pela primeira vez, ver comentário em `settings.py`).
- Endpoint SSE/polling para atualização de dados em tempo real; cards de Urgentes/Em alta/Todas se atualizam sozinhos (sem F5) via `api_proposicoes_cards`, que renderiza o HTML pronto (mesmos templates) em vez de duplicar lógica em JS
- Acessibilidade WCAG 2.1-aligned (skip links, foco visível, redução de movimento)

---

## Modelos principais

### Proposicao

Campos principais: `titulo`, `casa`, `status_tramitacao`, `local`, `pauta`, `urgente`, `aprovada`, `parada`, `prioridade_fnp`, `macrotema`, `ementa_resumida`, `proximos_eventos`, `interlocutores`, `ultima_movimentacao`, `link`, `posicionamento_fnp`, `acoes_incidencia`, `riscos_oportunidades`, `visualizacoes` (contador para "Em alta").

**`temas`** é `ManyToManyField` para `Tema` (migrado de FK única — ver migração `0002_replace_tema_with_m2m.py`, que separa nomes compostos "A, B/C" via `split_temas()`).

### Usuario / Perfil

`Usuario` é `AUTH_USER_MODEL`, estende `AbstractUser` (só dados de autenticação). `Perfil` guarda `municipio` (**ForeignKey**, não mais 1-para-1 — vários usuários podem ser do mesmo município), `telefone`, `cargo`, `setor_responsavel`, `foto` (upload), `foto_google_url` (importada no login social), `status_aprovacao` (pendente/aprovado/rejeitado) e `exclusao_solicitada_em`; criado automaticamente por signal (`signals.py`) a todo cadastro novo — inline no Admin de Usuario. Nome de exibição é `Usuario.get_display_name()` (nome completo ou derivado do e-mail) — nunca o e-mail cru na UI. Avatar é `Usuario.get_avatar_url()` (foto manual tem prioridade sobre a do Google). Hierarquia de acesso via grupos nativos do Django: **Root** (superusuário), **Administrador FNP**, **Usuário** (padrão); ver `python manage.py setup_roles`.

### Macrotema / Tema

- `Macrotema` organiza a classificação editorial das proposições
- `Tema` representa subcategorias mais específicas (M2M com Proposicao)

### Participacao / Comentario / Notificacao / PalavraProibida / DenunciaComentario

- `Participacao` permite registrar contribuições, sugestões, dúvidas ou indicações — campos `municipio`, `uf`, `setor_responsavel`, `cargo`, `email`, `telefone`, `mensagem` (mesmo vocabulário do cadastro de usuário; `setor_responsavel`/`telefone` foram renomeados de `responsavel`/`whatsapp`)
- `Comentario` é a base do fórum por proposição; `status_moderacao` é calculado automaticamente no envio via `apps.comentarios.moderacao.classificar_comentario()` — não fica mais pendente por padrão. `DENUNCIAS_PARA_OCULTAR = 3` (classe constante)
- `PalavraProibida` (`palavra`, `ativa`) é a lista, editável só via Admin, que aciona a reprovação automática
- `DenunciaComentario` (`comentario` FK, `denunciante` FK, `UniqueConstraint` por par) registra denúncias de usuários; ao atingir o limite, oculta o comentário automaticamente (`denunciar_comentario` em views.py)
- `Notificacao` é criada para todo comentarista anterior quando alguém novo comenta na mesma discussão (`notificar_participantes_da_discussao`), só quando o comentário é aprovado

---

## Fluxo de dados e importação

As proposições podem ser importadas via management command:

```powershell
python manage.py ingest_legislativo <caminho-do-json>
```

O comando aceita payloads no formato de lista de registros com campos como:
- `Proposição`
- `Casa`
- `Status da Tramitação`
- `Tema`
- `Macrotema`
- `Ementa Resumida`
- `Próximos Eventos/Ações Esperadas`
- `Interlocutores Estratégicos...`
- `Última Movimentação`
- `Posicionamento da FNP`
- `Ações de Incidência da FNP...`
- `Riscos e Oportunidades`

O comando cria ou atualiza proposições, temas, macrotemas e notícias associadas.

---

## UI e UX — Convenções adotadas

### Estrutura visual

- Hero com mensagem institucional
- Estatísticas em cards
- Busca e filtros em painel dedicado
- Cards de briefing com leitura rápida
- Modal de detalhe para aprofundamento

### Responsividade

Toda alteração deve respeitar compatibilidade com dispositivos móveis:
- layouts responsivos
- legibilidade em telas pequenas
- espaço confortável para toque
- navegação estável em telas estreitas
- evitar dependência excessiva de hover como mecanismo principal

### Diretrizes de design

- visual institucional, sério e profissional
- leitura rápida, editorial e executiva
- prioridade ao entendimento imediato da proposição
- linguagem clara para usuários técnicos e gestores

### Acessibilidade (WCAG 2.1)

- skip links no topo (`#conteudo-principal`, `#menu-principal`, `#rodape`), atalhos Alt+1/Alt+2
- `:focus-visible` visível em todos os elementos interativos (light e dark mode)
- `@media (prefers-reduced-motion: reduce)` respeitado
- `role="search"` + `<label>` nos campos de busca; `aria-hidden="true"` em ícones decorativos
- zoom nativo do navegador (sem controle de zoom customizado)

---

## Git — Remotos e fluxo

O projeto usa dois remotos com papeis distintos:

| Remoto | URL | Uso |
|---|---|---|
| `origin` | `https://github.com/brunofnp/legislativo-fnp.git` | Repositório pessoal — desenvolvimento |
| `production` | `https://github.com/dadosfnp/legislativo-fnp.git` | Repositório da organização — produção |

### Branches

- `next` → branch principal de desenvolvimento
- `main` → branch de produção

### Fluxo diário

```powershell
git checkout next
git pull origin next
git push origin next
```

Para produção:

```powershell
git checkout main
git pull production main
git push production main
```

### Deploy no droplet via SSH direto

Desde 2026-08-20, Claude Code tem acesso SSH direto ao droplet `fnp-web`
(`ssh -i ~/.ssh/id_ed25519_fnp_web root@142.93.205.222`, chave já
configurada na máquina local) — **autorizado explicitamente pelo
usuário** ("Pode fazer" / "Pode continuar assim", 2026-08-20/21). Uso
esse acesso pra rodar o deploy de verdade (`git pull` + `docker compose
build` + `up -d` + conferência de log/`curl`/`docker compose ps`) sempre
que a promoção pra produção/droplet for autorizada — sem precisar passar
comando por comando pro usuário rodar no console web da DigitalOcean.
Continua valendo a regra de nunca promover `next`→`main`/produção sem
pedido explícito a cada vez; o que mudou foi só *quem* executa os
comandos depois de autorizado. Nunca expor conteúdo de credencial (senha,
chave de API, `.env`) na saída de comando — quando precisar levar um
segredo pro `.env` do servidor, usar `ssh ... "cat >> .env" < arquivo_local`
(o valor nunca aparece no stdout/stderr de nenhum lado).

---

## CI e validação

### Validações locais recomendadas

- `python manage.py check`
- `python manage.py test apps.legislativo`
- `python manage.py collectstatic --noinput`

### Regras de qualidade

- manter o projeto funcional em Django e com templates renderizando corretamente
- preservar responsividade e estabilidade visual
- testar mudanças de UI e fluxo antes de publicar

---

## Diretrizes de Engenharia (padrão fixo — não revisitar sem pedido explícito)

Estas são decisões arquiteturais fechadas para o projeto. Se uma sugestão minha reabrir alguma delas (banco, SSE, Admin, auth, etc.), eu devo sinalizar isso explicitamente antes de agir, não decidir sozinho.

**Estrutura:** modelo Radar Brasil — `apps/` (um app por domínio), `base_templates/` (layout compartilhado), `templates/` por app, `static/`, `setup/` (settings/urls raiz), `locale/`, `docs/`. Um app = uma responsabilidade; nada de app genérico "core" virando depósito. `requirements.txt` (prod) e `requirements-dev.txt` (dev/lint/teste) separados; `.env.example` versionado, `.env` nunca. Models já divididos: `apps.usuarios`/`apps.proposicoes`/`apps.comentarios`; `apps.legislativo` é a camada de views/urls/forms que orquestra os três (não um app "core" de despejo — só não tem models próprios).

**Models/banco:** status/urgência/categoria é sempre coluna real calculada na ingestão, nunca string-matching em template/JS (`urgente`, `aprovada`, `parada`, `prioridade_fnp` já são assim). Edição de campo de mérito nunca sobrescreve — grava linha de histórico (autor, campo, valor anterior, novo, data); `EdicaoMeritoHistorico` é gravado automaticamente pelo `ProposicaoAdmin.save_model` e por `ingest_legislativo` sempre que `posicionamento_fnp`/`acoes_incidencia`/`riscos_oportunidades` mudam (campos listados em `Proposicao.CAMPOS_MERITO`). FK de thread sempre com `related_name` explícito (`Comentario.parent` → `related_name='respostas'`, já correto). Migrations sempre revisadas antes de aplicar, nunca schema editado direto em produção. Ingestão idempotente via `update_or_create` (já é o padrão em `ingest_legislativo.py`).

**Views/templates:** server-side rendering por padrão; JS só para interatividade puramente client-side sobre dado já carregado (filtro/busca). Nunca SPA client-side recalculando dado que já deveria vir pronto do servidor. Django Admin para telas administrativas (edição de mérito, gestão de macrotema, moderação de comentários, hierarquia de usuários via grupos/permissões nativas) em vez de CRUD customizado, a menos que a necessidade seja genuinamente pública-facing. Customização visual do Admin é feita sobrescrevendo templates/CSS do próprio Django (`AdminSite` customizado, `nav_sidebar.html` próprio) — nunca reescrever o Admin do zero como CRUD à parte.

**Autenticação:** auth nativo do Django, nunca senha única/`if pass == X`. Permissão via `django.contrib.auth` (permissions/groups) — grupos **Root** (superusuário, bypassa checagem de permissão), **Administrador FNP** (moderação/edição de conteúdo) e **Usuário** (padrão, atribuído automaticamente por signal a todo cadastro novo); ver `python manage.py setup_roles`. `Usuario` (auth) e `Perfil` (município/telefone/cargo/setor/foto, via signal) já são separados como o padrão User+Profile pede; `Perfil.municipio` é `ForeignKey` (não 1-para-1 — vários usuários podem ser do mesmo município). Cadastro novo exige aprovação (`Perfil.status_aprovacao`) antes de liberar navegação (`CadastroPendenteMiddleware`); staff já nasce aprovado.

**Moderação de comentários:** publicação é automática por padrão (não pré-moderação total) — só é bloqueada por lista de palavras proibidas gerenciada via Admin (`PalavraProibida`), nunca hardcoded no código. Checagem por fronteira de palavra, sem acento/caixa. Ver `apps/comentarios/moderacao.py`. Complementado por denúncia de usuários (`DenunciaComentario` — 3 denúncias distintas ocultam o comentário sozinho). Não introduzir fila de aprovação manual por padrão de novo sem decisão revista — o ponto desta mudança foi justamente tirar a equipe pequena desse gargalo.

**Rate limiting/segurança:** throttle de formulário público via cache do Django (`apps/legislativo/throttling.py`), não via pacote externo — trocar por Redis só se o app crescer para múltiplos workers/servidores (não fazer isso preventivamente). Cabeçalhos de segurança de produção (HSTS/SSL redirect/cookies seguros) só ativam com `DEBUG=False` — nunca testar/depurar com eles ligados em ambiente local sem TLS.

**Tempo real:** SSE é a solução fechada para notificação (já implementado). Não introduzir WebSocket/Channels/Redis sem decisão revista explicitamente.

**Ingestão:** management command via cron. Não sugerir Celery/fila sem pedido, dado 1 dev só operando.

**Testes/lint:** seguir `pytest.ini`/`.flake8`/`pyproject.toml` já existentes (padrão Radar Brasil), sem ferramenta concorrente. Testar regra de negócio (cálculo de urgência, idempotência de carga, thread de comentário), não perseguir cobertura em código trivial.

**Infraestrutura:** Docker + Nginx no Droplet `fnp-web`, 1 database + 1 role dedicados no Postgres Managed, segredos em `.env` do servidor + Bitwarden, nunca no git. Mudança em Nginx compartilhado (ex.: SSE/upgrade de conexão) é mudança de infra compartilhada — sinalizar como tal e testar por túnel SSH antes de publicar.

**Processo:** entregas sequenciais e demonstráveis (1 dev só) — cada etapa roda sozinha antes da próxima começar, sem empilhar trabalho não testável.

---

## Postura de segurança (referência rápida)

> Histórico completo, com o raciocínio por trás de cada item (o que foi
> corrigido, o que foi decisão consciente, o que ainda está pendente):
> `docs/adr/0003-seguranca-auditoria-hardening.md`. Esta seção é só o
> resumo do estado atual — atualizar aqui quando algo mudar, mas deixar a
> narrativa completa no ADR.

O projeto é tratado como alvo plausível de ataque (fórum de discussão
política pública, cadastro aberto) — não como sistema interno de baixo
risco. Duas rodadas de auditoria já feitas: hardening geral em 2026-08-06
(junto do upgrade Django 4.2→5.2) e pentest completo de 48 itens em
2026-08-11 (relatório → correção → revarredura, no mesmo dia).

| Área | Estado |
|---|---|
| Framework | Django 5.2 LTS (suporte até abril/2028), `django-allauth` 65.x |
| `DEBUG`/`SECRET_KEY` | Fail-closed — produção sem `SECRET_KEY` não sobe |
| HTTPS/HSTS | `SECURE_SSL_REDIRECT`, HSTS 1 ano + subdomínios + preload, cookies `Secure` (só com `DEBUG=False`) |
| CSP | Ativo, `script-src` com nonce, sem `'unsafe-inline'` em nenhuma diretiva |
| Rate limit | Comentário/participação/denúncia + login (allauth nativo), IP real via `X-Forwarded-For` (não `REMOTE_ADDR` cru) |
| Senha | Mínimo 10 caracteres (`MinimumLengthValidator`), sem regra extra de complexidade (orientação NIST/OWASP atual) |
| Sessão | Expira em 7 dias (`SESSION_COOKIE_AGE`) |
| 2FA | Disponível em `/contas/2fa/`, **opcional** — não obrigatório pra staff (decisão consciente, ver Diretrizes) |
| Dependências | `requirements.lock` com hash; `pip-audit` no CI a cada push/PR |
| Monitoramento | Sentry ativo em produção (`SENTRY_DSN`) |
| Log de auditoria | `TentativaLogin` (sucesso/falha de login) + `LogEntry` explícito em aprovação/rejeição de cadastro/exclusão em massa |
| Infraestrutura | Cloud Firewall DO + `ufw` (só 22/80/443), SSH key-only, `fail2ban` ativo, Trusted Sources do banco restrito ao droplet + 1 IP |

**Pendências de segurança em aberto** (lista completa e atualizada em
"Pendências e próximos passos" mais abaixo): rotacionar `SECRET_KEY` +
senha da role `legislativo` + `GOOGLE_CLIENT_SECRET` (expostos num print
do `.env`); desativar o Client Secret antigo do Google no Console;
decidir se `acoes_incidencia`/`riscos_oportunidades` deveriam exigir
login; configurar `EMAIL_BACKEND` real em produção; atualizações de
SO/Docker com reboot pendente (adiado, droplet compartilhado).

**Regra permanente**: qualquer credencial exposta em captura de tela ou
terminal colado no chat (mesmo já rotacionada antes) merece rotação —
já aconteceu duas vezes com a mesma credencial (`doadmin`) nesta sessão
de auditoria. Nunca colar comando com senha visível sem necessidade.

---

## Regras de colaboração

> Atribuição de commit (sem coautoria do Claude) e modo ponytail estão no
> CLAUDE.md global do usuário — não repetir aqui.

1. O autor dos commits deve ser `brunofnp`
2. Manter documentação atualizada quando houver mudanças significativas
3. Priorizar compatibilidade mobile em todas as alterações
4. Usar `next` para desenvolvimento e `main` para produção

---

## Skills (Agent Skills) mais úteis aqui

| Skill | Quando usar neste projeto |
|---|---|
| `git-workflow-and-versioning` | Todo commit; promoção `next` → `main` (remoto `production`) só com pedido explícito a cada vez |
| `security-and-hardening` | Qualquer mudança em form público, auth/allauth, upload de mídia, CSP/nonce ou `settings.py` — fórum político aberto é alvo plausível (ver ADR 0003) |
| `code-review-and-quality` | Antes de promover pra `main`; o CI só roda em push/PR para `main` |
| `frontend-ui-engineering` | Templates/CSS/JS: mobile-first, dark mode, WCAG (skip links, `:focus-visible`, `prefers-reduced-motion`) |
| `test-driven-development` | Regra de negócio (moderação, denúncia, aprovação de cadastro, idempotência de ingestão) |
| `performance-optimization` | Só com sintoma real (N+1 na home/listagens, `api_proposicoes_cards`); não otimizar preventivamente |
| `documentation-and-adrs` | Decisão que reabra uma das "Diretrizes de Engenharia" → novo ADR em `docs/adr/` |
| `graphify` | Perguntas de arquitetura/relação entre arquivos — consultar `graphify-out/` antes de varrer o código |

## Pegadinhas para agentes

- **Leia `CONSTRAINTS.md` antes de escrever código.** Não enfraqueça esse arquivo para fazer uma mudança passar.
- **Testes**: todos em `apps/legislativo/tests.py` (~134 testes, 51 classes), com `RequestFactory` + middleware manual — não trocar por `self.client`. Rodar com `pytest` (config em `pytest.ini`) ou `python manage.py test apps.legislativo`.
- **Dependências**: fonte da verdade é `requirements.txt` / `requirements.lock` (Django 5.2). O bloco `dependencies` do `pyproject.toml` está defasado (`Django<5.0`) — não usar como referência; `pyproject.toml` vale só pela config de black/isort/ruff.
- **CI** (`.github/workflows/ci.yml`): Python 3.11, `DEBUG=True`, `pytest` + `pip-audit -r requirements.lock`; não roda em `next` — validar localmente antes.
- `.env` e `db.sqlite3` existem localmente com dados reais — nunca imprimir o conteúdo do `.env`.

---

## Estado Atual do Projeto

> Histórico completo de cada sessão em `docs/historico-sessoes.md`.

### Pendências e próximos passos

**Mais urgente agora:**

- **Acesso ao repositório `dadosfnp/sistema-fnp`** (convite de colaborador
  pendente, 2026-11-12) — necessário só pros 3 SVGs de pattern decorativo
  (bastões) e qualquer variante de logo exclusiva da identidade
  "Território"; cores/fontes/ícones/sidebar/topbar já foram trazidos sem
  precisar desse acesso (ícones via CDN público do Phosphor Icons, MIT).
- ~~Concluir a configuração de SMTP real~~ — **resolvido de vez em
  2026-08-20, por um caminho diferente do planejado**: a DigitalOcean
  bloqueia por padrão as portas de SMTP de saída (587/465) em toda conta
  nova (achado só testando de verdade — `ufw`/Cloud Firewall liberam
  tudo, o bloqueio é numa camada acima). Chamado de suporte aberto com a
  DO pra liberar (ainda sem resposta, **não é mais bloqueante**), mas a
  solução efetiva foi trocar pra **Gmail API** (fala HTTPS/443, sempre
  liberado) — `apps/legislativo/email_backends.py::GmailApiEmailBackend`,
  testado local e em produção de verdade, e-mail confirmado chegando.
  Ver seção datada "SMTP bloqueado pela DigitalOcean — resolvido via
  Gmail API" acima pro histórico completo.
- ~~Promover a sessão de 2026-08-19 pra `main`/produção/droplet~~ e
  ~~Confirmar o deploy do redesenho do upload de anexo~~ — **ambos
  resolvidos**, várias promoções feitas ao longo de 2026-08-19/20 (ver
  seções datadas "Segunda/Terceira promoção do dia" e as de 2026-08-20
  acima) — tudo que estava represado já está em produção confirmada
  saudável.
- ~~Rotacionar a senha do `doadmin` mais uma vez~~ — **resolvido em
  2026-08-20**, mesmo dia do achado. Apareceu em texto puro no chat
  (query de diagnóstico colada sem querer) e foi rotacionada pelo painel
  DO ainda na sessão — não exigiu mudança em nenhum serviço nosso, só a
  role `legislativo` é usada em produção.
- ~~Aumentar `client_max_body_size` no Nginx do droplet~~ — **resolvido
  em 2026-08-19**: linha alterada à mão de `6M` pra `30M` direto em
  `/etc/nginx/sites-enabled/legislativo.conf` (`sed` de uma linha só,
  nunca `cp` do arquivo inteiro — preserva o bloco SSL do certbot),
  `nginx -t` limpo, `reload` sem erro, `curl` confirmando `200 OK`
  depois. `deploy/nginx-legislativo.conf` (referência local) atualizado
  junto. Vídeo em comentário do fórum (até 25MB) já funciona em
  produção.
- ~~Promover a auditoria de UX mobile (2026-08-13) + os fixes de
  produção do mesmo dia pra `main`/produção~~ — **feito em 2026-08-13**,
  ver seção datada "Promoção completa `next` → `main` → produção →
  droplet" logo abaixo. Ainda pendente: testar em dispositivo físico de
  verdade (Safari iOS + Chrome Android) os 5 itens que emulador não
  cobre da auditoria original (teclado virtual cobrindo campo, rodapé —
  home tem 30.000+px de altura, não rolado até o fim —, contraste de
  cor dos badges, SSE se recuperando de troca de rede, tempo de carga
  em 4G real) **mais** o "card Resumo parecendo sobrepor" reportado no
  celular — não reproduzido em Chromium mesmo depois do fix de
  overflow, vale confirmar se sumiu de verdade agora que está em
  produção.
- **Rotacionar `SECRET_KEY`, senha da role `legislativo` e
  `GOOGLE_CLIENT_SECRET`** — os três apareceram em texto puro num print
  do `.env` de produção nesta sessão de chat (2026-08-11). `SECRET_KEY`
  é o mais barato de trocar (invalida sessões ativas, sem downtime
  real); os outros dois seguem o mesmo roteiro já usado hoje pro
  `doadmin` e pro Google Client Secret anterior.
- **Decidir se o mérito interno da proposição deveria ser público** —
  `posicionamento_fnp` continua fazendo sentido público, mas
  `acoes_incidencia`/`riscos_oportunidades` (estratégia de lobby)
  aparecem pra qualquer visitante anônimo em `proposicao_detail.html`,
  sem exigir login. Achado da revarredura de segurança de 2026-08-11 —
  decisão do usuário antes de eu mexer em código (pode ser intencional).
- ~~Configurar `EMAIL_BACKEND` real em produção~~ — **resolvido em
  2026-08-20** via Gmail API (ver item no topo desta lista). Com isso,
  dá pra reconsiderar `ACCOUNT_EMAIL_VERIFICATION='mandatory'` (o fix
  mais robusto pro achado da fusão de conta, ver auditoria de segurança
  acima) — decisão de produto ainda não tomada, mas o pré-requisito
  técnico não é mais um bloqueio.
- **Desativar/excluir o Client Secret antigo do Google OAuth** no Google
  Cloud Console (o criado em 2026-08-04) — o novo já está em produção e
  validado, mas o antigo continua uma credencial ativa em paralelo até
  ser desligado por lá.
- **Itens de infraestrutura já checados em 2026-08-11**: SSH key-only ✅,
  `ufw` ✅, `PermitRootLogin yes` ainda ligado mas baixa prioridade,
  `media/` chown ✅, limite de upload no Nginx ✅, **Trusted Sources do
  `fnp-database` confirmado restrito** ✅ (painel DO → Network Access: só
  2 origens — `fnp-web` e um IP fixo rotulado "Sistema-FNP", nada de
  "Allow all"). **Só falta**: atualizações de SO + Docker pendentes com
  reboot — **avaliado e adiado de propósito em 2026-08-11**: o droplet
  `fnp-web` hospeda outros sistemas da FNP também (Nginx configurado
  pra `ifem`/`fnp`/`fnp-homolog` além do `legislativo`), então um
  reboot derruba todos juntos, não só o nosso; usuário optou por não
  mexer agora pra não impactar os outros sistemas sem coordenar antes.
  Precisa de uma janela combinada com quem administra os demais.
- ~~Conferir visualmente no navegador a rodada de 23 itens acima~~ —
  **resolvido em 2026-08-14**, ver seção datada "Verificação visual
  pendente de duas rodadas antigas" logo abaixo. Tudo confirmado
  funcionando via Playwright contra o dev local.
- ~~Confirmar visualmente no navegador se os 5 ajustes finos da rodada
  anterior ficaram bons~~ — **resolvido em 2026-08-14**, mesma seção
  acima. Todos os 5 confirmados corretos.
- ~~Decidir o que fazer com as proposições não-curadas já em produção~~ —
  **resolvido em 2026-08-11**: `sync_legado_firestore --keep-json`
  rodado em produção pra restaurar/corrigir os 104 registros curados
  (nenhum "Criada", confirma que a base curada estava intacta), depois
  as 68 proposições fora do legado apagadas via
  `Proposicao.objects.exclude(titulo__in=titulos_104).delete()`
  (uma delas tinha 12 comentários — confirmado pelo usuário que eram só
  de teste, sem perda real). Banco de produção conferido em 104/104
  depois. **Correção same-day**: `--paginas 1` sozinho não foi
  suficiente — voltou a 165 em menos de um dia; `sync-camara` parado e
  desativado por `profiles` (ver seção datada mais abaixo, mesmo dia).

**Seguem em aberto (sem mudança nesta sessão):**

- ~~`SENTRY_DSN` sem conta criada~~ — **resolvido em 2026-08-11**, ver
  seção datada acima.
- Conta externa que falta ser criada pra ativar o que já está
  implementado (tudo desligado até lá, zero risco):
  `RECAPTCHA_PUBLIC_KEY`/`RECAPTCHA_PRIVATE_KEY`
  (google.com/recaptcha/admin).
- `EMAIL_BACKEND` de produção ainda não configurado com um backend real
  (SMTP/SES/etc.) via env var — `ACCOUNT_EMAIL_VERIFICATION=optional`
  manda e-mail de confirmação mas não entrega de verdade lá. (O bug de
  dev, cadastro local derrubando sem `EMAIL_BACKEND`, já foi corrigido —
  isso aqui é só sobre produção ter um backend real configurado.)
  **Ficou mais urgente em 2026-08-14**: o aviso de cadastro pendente pra
  `ronan.castro@fnp.org.br`/`nucleo.dados@fnp.org.br` (ver seção datada
  acima) também depende disso pra chegar de verdade em produção.
- Cache do Django é `LocMemCache` (por processo) — com Gunicorn
  `--workers 3`, rate limit de login e `throttling.py` de
  comentário/participação têm limite efetivo até 3x mais permissivo do
  que o configurado. Trocar por Redis é reabrir a diretriz "só trocar se
  crescer pra múltiplos workers" — decisão ainda não tomada.
- ~~`deploy/nginx-legislativo.conf` com limite de upload 6MB não copiado
  pro droplet~~ — **resolvido em 2026-08-11, sem repetir o incidente de
  2026-08-05**: em vez de `cp` do arquivo inteiro (que apaga o bloco SSL
  do certbot), só a linha `client_max_body_size 6M;` foi inserida à mão
  no arquivo já em produção (`/etc/nginx/sites-enabled/legislativo.conf`),
  validada com `nginx -t` antes do `reload`. No caminho, achado e
  removido um arquivo `legislativo.conf.save` (sobra de uma edição
  anterior do `nano`, não um symlink como os configs de verdade) que
  também declarava `server_name legislativo.fnp.org.br` e causava um
  aviso de "conflicting server name" — não afetava outros sistemas do
  droplet (`fnp`, `ifem`, `fnp-homolog`), só duplicava o nosso.
- Droplet `fnp-web` sem backup próprio (só o `fnp-database` tem).
- Reverificar se o bug do Python 3.14 (`copy.copy()` em `RequestContext`)
  ainda ocorre agora que o projeto está no Django 5.2 — não testado,
  `.venv/` local continua em 3.12 por segurança.
- ~~Conferir/chown o volume de `media/` no droplet~~ — **resolvido em
  2026-08-11**: estava `root:root`, mesmo risco de permissão que o
  `staticfiles/` teve em 2026-08-05. `chown -R 1000:1000` aplicado
  (aparece como dono `phillippi` no host — coincidência de UID 1000 com
  o usuário de deploy do IFEM, não é erro; o container roda como
  `appuser` também UID 1000, e Linux checa por número, não por nome).
- ~~Client Secret do Google OAuth exposto numa captura de tela em sessão
  anterior~~ — **decisão do usuário em 2026-08-11: não rotacionar**
  (risco aceito, baixo — app ainda em modo "teste" no Google Cloud
  Console). Não é mais pendência de segurança.
- Falta preencher `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` no `.env` do
  servidor de produção (não é o mesmo `.env` local) e adicionar e-mails
  de teste na tela de consentimento OAuth até o app sair do modo
  "teste" — item funcional separado do risco de exposição acima. Nota:
  o login Google em produção já foi testado e confirmado funcionando
  em 2026-08-11 (ver seção datada acima), então o `.env` do servidor já
  tem `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` preenchidos de fato —
  falta só adicionar e-mails de teste na tela de consentimento se
  alguém novo (fora quem já testou) precisar logar via Google antes do
  app sair do modo "teste".
- Comentários "pendente" de antes da moderação automática continuam
  precisando de revisão manual (ação em massa no Admin).
- ~~Container `legislativo` aparece `(unhealthy)` no `docker compose ps`~~
  — **causa raiz encontrada e corrigida em 2026-08-11, direto pelo
  Sentry** (ver seção datada abaixo): o healthcheck do
  `docker-compose.yml` rodava `curl http://localhost:8004/`, que manda
  `Host: localhost:8004` — o Django rejeitava com `DisallowedHost` (só
  `legislativo.fnp.org.br` está em `ALLOWED_HOSTS`), então o healthcheck
  falhava sempre, mesmo com o site respondendo `200 OK` de verdade via
  Nginx (que usa o Host certo). Fix: `-H "Host: legislativo.fnp.org.br"`
  adicionado ao `curl` do healthcheck, sem tocar em `ALLOWED_HOSTS`.
- Integração com o Senado (hoje só Câmara via `sync_camara`).
- Redesign visual incremental a partir de referências que o usuário vai
  mandando aos poucos (sidebar/topbar do site público e do Admin já
  alinhados; mais capturas de tela podem vir e pedir mais ajuste).

---

## Documentação Técnica

| Arquivo | Conteúdo |
|---|---|
| `README.md` | Visão geral do projeto e fluxo de repositórios |
| `docs/runbook.md` | Operação e procedimentos de execução |
| `docs/adr/0001-initial-architecture.md` | Arquitetura inicial do projeto |
| `docs/adr/0002-deploy-producao-dominio.md` | Como o projeto foi pro ar em `legislativo.fnp.org.br` — infraestrutura, passo a passo e cronologia real do primeiro deploy (2026-08-03/05) |
| `docs/adr/0003-seguranca-auditoria-hardening.md` | Auditorias de segurança, hardening, decisões conscientes e pendências — histórico completo (ver também "Postura de segurança" acima, no corpo deste arquivo) |
| `CONTRIBUTING.md` | Padrões de contribuição |
| `CONSTRAINTS.md` | Piso de qualidade (testes, supressões, WCAG, travas de métrica) |
| `docs/historico-sessoes.md` | Diário datado de todas as sessões (antes ficava neste arquivo) |

# Reconciliação bicameral — a matéria segue a Casa ativa — 2026-11-24

## Problema (vital)
O painel guarda **um único link** por proposição e o `atualizar_tramitacao` só
lê fichas da **Câmara**. Quando uma matéria **bicameral** troca de Casa, o
rastreamento automático deixa de alcançá-la. Caso: **PLP 299/2026** (ajuste
fiscal) — remetida do Senado à Câmara, lá aprovada e enviada à sanção; o painel
continuava no Senado e não acusou a aprovação.

Exigência do usuário: o sistema tem de **se atualizar sempre, sozinho**, com
essa inteligência no desenho.

## Decisão
Comando `reconciliar_casas` (cron diário): para cada proposição publicada,
consulta **as duas Casas** pelo título (`TIPO N/ANO`) e decide a **Casa ativa**:

1. **Terminal** — a Casa que já aprovou/sancionou/promulgou/arquivou manda
   (usa `is_aprovada`, que reconhece inclusive "sanção").
2. **Handoff** — "remetida à Câmara / ao Senado" aponta a Casa que recebeu.
3. **Recência** — senão, a de movimentação mais recente.

Quando a Casa ativa **difere** da Casa do link atual, troca **link + casa +
status + local + última movimentação** e recomputa o selo **APROVADA** (só
False→True). Pauta/parada seguem com seus próprios comandos.

## Segurança (o "cuidado")
- **Trava anticolisão**: número igual entre Casas pode ser matéria diferente.
  Só troca se a **ementa bater** (`ementa_compativel`, Jaccard ≥ 0,3). Ementa
  curada vazia → exige que as duas Casas batam entre si; senão não troca.
- **Nunca toca em curadoria**: posicionamento, tag FNP, prioridade, temas,
  apensação ficam intactos.
- **aprovada só avança** (False→True): nunca regride.
- `--dry-run` e `--quiet` (baseline sem alerta). Falha de rede em uma Casa →
  trata como "não encontrada" (sem exceção).

## Verificação
`CasaAtivaLogicTest`, `EmentaCompativelTest`, `ReconciliarCasasCommandTest`
(segue p/ Câmara + marca aprovada; preserva curadoria; colisão não troca; já
na Casa certa não mexe; dry-run; quiet sem alerta; não encontrada). Smoke real
24/11: PLP 299/2026 → casa_ativa=camara, ementa compatível, aprovada=True. 268
testes OK.

## Nota de integração
Empilhado sobre PR #22 (que ensina `is_aprovada` a reconhecer "sanção"). Merge
#22 antes.

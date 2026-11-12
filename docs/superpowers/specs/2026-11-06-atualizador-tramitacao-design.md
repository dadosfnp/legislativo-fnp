# Atualizador dirigido de tramitação — design (2026-11-06)

## Problema
As proposições curadas no painel não atualizam o status sozinhas. O
`sync_camara` foi desativado de propósito (varria a Câmara por palavra-chave e
inundava a base com registros não curados). Resultado: quando uma proposição
se move (ex.: PEC 18/2025 aprovada em 03/11/2026), o painel continua mostrando
o texto antigo até alguém editar à mão. A equipe pediu que "as proposições
atualizem" e que o alerta (sino + e-mail) funcione para as listadas.

## Decisão
Comando dirigido `atualizar_tramitacao` que percorre **somente as proposições
já publicadas** e refresca a **última movimentação** de cada uma a partir da
**ficha oficial da Câmara** (pelo `idProposicao` do link curado). Não varre por
palavra-chave, não cria proposições, não toca em campos editoriais
(posicionamento, status redigido à mão) — só a última movimentação, que é
factual. Quando muda, grava e dispara o alerta individual reusando o signal de
`comentarios/alertas.py`.

## Escopo v1: só Câmara
A API de Dados Abertos do **Senado** está em migração (`materia/situacaoatual`
com descontinuação marcada; substituto `/processo/{idProcesso}`), e o
`fontes.buscar_senado` atual não extrai a situação de forma confiável. Para não
gravar status errado em produção, o Senado fica **de fora na v1** (atualização
manual) e entra numa v2 quando a migração estabilizar. As proposições do Senado
foram corrigidas à mão na auditoria de 06/11/2026.

## Segurança contra flood de alertas
`save()` dispara o alerta; `.update()` não. O primeiro disparo em produção deve
usar `--quiet` (baseline silencioso), senão alertaria todo mundo sobre
movimentações já conhecidas. Depois disso, a execução diária roda em modo normal
e só alerta sobre movimentação nova de verdade.

## Interface
- `atualizar_tramitacao` — atualiza e alerta os seguidores das que mudaram.
- `--quiet` — atualiza sem alertar (baseline).
- `--dry-run` — só mostra o que mudaria.
- `--limit N` / `--titulo "X"` — restringe o alcance.

## Fora de escopo (v2+)
- Senado (quando a API `/processo` estabilizar).
- Atualizar `status_tramitacao`/flags automaticamente (hoje é editorial/curado).
- Agendamento: cron diário no deploy (Bruno) chamando o modo normal.

## Testes
`apps/legislativo/tests.py::AtualizarTramitacaoTest` (8 casos, fonte mockada):
atualiza, alerta seguidor, `--quiet` não alerta, não alerta quando igual,
`--dry-run` não grava, pula Senado, filtra por título, fonte sem resultado não
quebra. Smoke test real contra a API da Câmara confirmado em 06/11/2026.

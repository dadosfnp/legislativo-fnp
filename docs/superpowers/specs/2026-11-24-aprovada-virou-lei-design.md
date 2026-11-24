# A2 — "Virou lei" (selo APROVADA para normas) — 2026-11-24

## Problema
Matérias que já viraram lei/norma continuavam exibidas "em acompanhamento".
O `is_aprovada` só reconhecia `aprovad+plenário` e `promulgad`; não pegava
"Transformado em Norma Jurídica", "Sancionado" nem "Convertida na Lei" (MPV).

## Decisão
1. **Heurístico** (`data_utils.is_aprovada`): adiciona sinais TERMINAIS —
   promulgada, sancionada, transformada em lei/norma, convertida em lei. Esses
   valem **mesmo que o texto cite comissão** (uma lei é o fim da linha). A
   regra antiga (aprovação só em comissão não conta) permanece abaixo.
2. **Atualização automática** (`atualizar_tramitacao`): recomputa, pela
   situação fresca da Câmara, se a matéria virou lei e, em caso positivo, marca
   `aprovada`. Flip **só False→True**: nunca regride uma aprovação já curada
   (ex.: PEC promulgada) que o retorno do momento não reflita. `aprovada` está
   em `CAMPOS_ALERTA`, então o "virou lei" alerta os seguidores (silencioso em
   `--quiet`).

## Fora de escopo
Reescrever o `status_tramitacao` curado (texto editorial fica intocado; o selo
e a última movimentação já comunicam a virada). Senado continua manual na v1.

## Testes
`IsAprovadaTest` (transformado/sancionado/convertido/promulgado/plenário ✓;
comissão-só e aguardando ✗; terminal vale citando comissão). `Atualizar
TramitacaoTest`: vira-lei marca aprovada; não regride aprovada curada. Suíte
cheia: 237 testes OK.

## Nota de integração
Empilhado sobre A1 (`feat/sincronizado-em`): ambos reescrevem o mesmo bloco de
`atualizar_tramitacao`. Mesclar A1 antes.

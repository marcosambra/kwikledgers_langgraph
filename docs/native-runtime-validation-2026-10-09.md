# Validação Nativa de 2026-10-09

## Escopo

Fechamento diário somente de prévia, hook sem imports legados, transporte
limitado, recibos independentes, emissor de execução vinculado ao payload,
contexto nativo, criação governada e retomadas de fluxos mutáveis.

Os resultados abaixo foram executados localmente sobre a implementação desta
rodada. Testes focados estão contidos na suíte completa; não somar as contagens.

| Diretório | Comando | Resultado |
|---|---|---|
| `kwikledgers_langgraph` | `poetry run python -m unittest discover -s tests` | 408 testes aprovados. |
| `kwikledgers_fastmcp` | `poetry run python -m unittest discover -s tests` | 139 testes aprovados. |
| Ambos | `poetry check --lock` | Aprovado. |
| `kwikledgers_langgraph` | `poetry install` | Instalação pelo lockfile aprovada. |
| `kwikledgers_langgraph` | `PYTHONPATH=tests poetry run python -m unittest test_lifecycle_runtime test_lifecycle_adapters` | 52 testes aprovados. |
| `kwikledgers_langgraph` | `PYTHONPATH=tests poetry run python -m unittest test_atomic_runtime test_atomic_cli test_technical_debt_preparation_cli` | 67 testes aprovados. |
| `kwikledgers_langgraph` | `PYTHONPATH=tests poetry run python -m unittest test_work_item_creation test_work_item_creation_cli test_fastmcp_execution_capability` | 39 testes aprovados. |
| `kwikledgers_langgraph` | `PYTHONPATH=tests poetry run python -m unittest test_day_end_closure test_backend_factory` | 33 testes aprovados, incluindo gerador de cron em sandbox. |
| `kwikledgers_langgraph` | `bash -n scripts/register-day-end-closure.sh` | Sintaxe aprovada; não instala cron. |

Os entrypoints `kwikledgers-langgraph`, `kwikledgers-langgraph-lifecycle`,
`kwikledgers-langgraph-atomic`, `kwikledgers-langgraph-day-end-closure` e
`kwikledgers-langgraph-story-pr` passaram no smoke `poetry run <comando> --help`.

## Garantias Exercitadas

- Nenhum gatilho de fechamento escreve Azure ou fabrica aprovação.
- Imports da fábrica, CLI e hook nativos não carregam `legacy_adapters`.
- Recibos e tokens são vinculados à ação e à prévia técnica exata.
- O emissor e o verificador FastMCP rejeitam horários inválidos independentemente.
- Mudanças de revisão ou área do operador invalidam execução pendente.
- O início local é registrado antes de bloquear ativação por área ausente.
- Resultado desconhecido ou journal incompleto exige reconciliação, não retry automático.
- Timeout de transporte é finito; HTTP sem autenticação é restrito a loopback.
- O gerador de cron usa FastMCP e `--dry-run`, sem ambiente legado implícito.
- A notificação pode carregar a chave do arquivo nativo sem importar o servidor.

## Observabilidade e Limites

O fluxo conserva `correlation_id`, `active_run_id` e a timeline iniciada e
concluída da coleta. A emissão existente em `AI_Tracking/Metrics/ai-usage-log.md`
continua compatível com o leitor do `kl_observatory`, sem alteração de schema
ou dependência da API/banco do observatório. A raiz explícita de tracking tem
precedência sobre `KWIKLEDGERS_ROOT_DIR/AI_Tracking`.

Esta rodada não executou Azure autenticado, mutações Azure, calendário Windows,
aquisição de recibo pelo host real, instalação de especialistas, pipelines,
registro de cron, retirada de consumidores legados nem push. As chaves e
recibos dos testes são fixtures; não representam aprovação operacional.

Diagnósticos do editor nos módulos de produção alterados foram conferidos;
a CLI de fechamento ficou sem erros. Quatro diagnósticos de atributos dinâmicos
de `ModuleType` em fixtures legadas preexistentes não foram alterados.

Para evolução do observatório, conservar nomes e estrutura dos eventos atuais
ou versionar explicitamente qualquer mudança futura; a timeline não comprova
que houve escrita externa. A retirada operacional continua sujeita aos gates
do [guia de desativação](../../docs/validation/fastmcp-langgraph-legacy-sunset.md).

## Nota de Contrato de 2026-10-13

Esta nota registra o novo contrato documental, sem alterar os comandos,
resultados ou limites históricos de 2026-10-09. As contagens acima não são
resultados finais do novo contrato.

O contrato exige recibo assinado independente, conforme escolha explícita do usuário.
A prévia permanece separada da aprovação humana: a CLI inicial salva o pedido,
e a retomada usa `--receipt` somente após a revisão humana. Um request ID sozinho
não autoriza execução. `SignedReceiptApproval.verify` vincula o recibo à ação,
ao digest da prévia, ao run e ao request; a revisão nativa continua
revalidada. `unknown_outcome` continua exigindo reconciliação, sem retry automático.

Estados, journals e checkpoints locais funcionam sem chave; formatos antigos
assinados exigem nova prévia. Tokens `.local-v1` são persistidos em ledger compartilhado
entre LangGraph e FastMCP, com TTL máximo de cinco minutos e anti-replay. Não são
aceitos por HTTP, inclusive em loopback. A aprovação usa HMAC e chave independente
de pelo menos 32 bytes; não é opcional nas mutações não autônomas. O token não
ultrapassa a validade do recibo, cujo TTL máximo é de 600 segundos.

A fronteira de confiança é a conta e suas permissões locais. Este contrato não
comprova identidade externa nem protege contra processos da mesma conta com
escrita no ledger. Não há nesta nota nova declaração de suíte completa aprovada
nem de validação operacional externa.
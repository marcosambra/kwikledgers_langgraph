# Security Policy

## English

### Scope

This policy covers the standalone `kwikledgers_langgraph` runtime repository.

It includes:
- runtime graph orchestration code
- FastMCP adapters
- local guardrails and observability logic
- repository packaging and CI configuration

Issues inside the `kwikledgers_fastmcp` package must be handled in that owning
repository or internal process.

### Reporting

Do not use public issues for sensitive disclosures, including:
- leaked credentials or tokens
- insecure adapter access to the FastMCP backend
- approval-bypass paths for mutating flows
- exploitable observability or logging exposure

Report security concerns through the private KwikLedgers engineering or
incident channel and include:
- affected repository and path
- impact and exposure level
- reproduction details
- temporary mitigation if known

### Sensitive Areas

- `kwikledgers_langgraph/fastmcp_adapters.py`
- guardrail and approval logic under `kwikledgers_langgraph/`
- runtime observability and timeline state
- GitHub Actions workflows and packaging metadata

## Português

### Escopo

Esta política cobre o repositório standalone do runtime
`kwikledgers_langgraph`.

Ela inclui:
- código de orquestração do runtime em grafo
- adapters do FastMCP
- lógica local de guardrails e observabilidade
- configuração de empacotamento e CI do repositório

Problemas dentro do pacote `kwikledgers_fastmcp` devem ser tratados no
repositório responsável ou no processo interno correspondente.

### Reporte

Não use issues públicos para divulgações sensíveis, incluindo:
- vazamento de credenciais ou tokens
- acesso inseguro dos adapters ao backend FastMCP
- caminhos que contornem aprovação em fluxos mutáveis
- exposição explorável de observabilidade ou logs

Reporte preocupações de segurança pelo canal privado de engenharia ou
incidentes da KwikLedgers e inclua:
- repositório e caminho afetados
- impacto e nível de exposição
- detalhes de reprodução
- mitigação temporária, se conhecida

### Áreas Sensíveis

- `kwikledgers_langgraph/fastmcp_adapters.py`
- lógica de guardrails e aprovação sob `kwikledgers_langgraph/`
- observabilidade do runtime e timeline no estado compartilhado
- workflows do GitHub Actions e metadata de empacotamento
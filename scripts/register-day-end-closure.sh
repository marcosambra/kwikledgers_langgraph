#!/usr/bin/env bash
set -euo pipefail

project_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
workspace_root=$(cd "$project_root/.." && pwd)
python_path="$project_root/.venv/bin/python"
fastmcp_python="$workspace_root/kwikledgers_fastmcp/.venv/bin/python"
env_file="${KWIKLEDGERS_ENV_FILE:-$workspace_root/kwikledgers_fastmcp/.env}"
audit_dir="$workspace_root/AI_Tracking/Audit"
marker='# kwikledgers-day-end-closure'

command -v crontab >/dev/null
[[ -x "$python_path" ]] || { printf 'Runtime Python indisponivel: %s\n' "$python_path" >&2; exit 1; }
[[ -x "$fastmcp_python" ]] || { printf 'Runtime FastMCP indisponivel: %s\n' "$fastmcp_python" >&2; exit 1; }
[[ "$env_file" == /* && -f "$env_file" && -r "$env_file" ]] || {
    printf 'O arquivo de ambiente deve ser absoluto, existente e legivel.\n' >&2
    exit 1
}
cmp -s /etc/localtime /usr/share/zoneinfo/America/Sao_Paulo || {
    printf 'O fuso do cron deve corresponder a America/Sao_Paulo.\n' >&2
    exit 1
}
[[ "$workspace_root$env_file" != *'%'* && "$workspace_root$env_file" != *$'\n'* && "$workspace_root$env_file" != *$'\r'* ]] || {
    printf 'Caminho com %% ou quebra de linha nao e suportado pelo registro cron.\n' >&2
    exit 1
}

existing=$(mktemp)
errors=$(mktemp)
updated=$(mktemp)
trap 'rm -f "$existing" "$errors" "$updated"' EXIT
if ! LC_ALL=C crontab -l >"$existing" 2>"$errors"; then
    if ! grep -q '^no crontab for ' "$errors"; then
        cat "$errors" >&2
        exit 1
    fi
fi
mkdir -p "$audit_dir"
awk -v marker="$marker" 'index($0, marker) == 0' "$existing" >"$updated"
printf '0 18 * * 1-5 /usr/bin/env SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt KWIKLEDGERS_ROOT_DIR=%q KWIKLEDGERS_ENV_FILE=%q KWIKLEDGERS_FASTMCP_COMMAND=%q %q -m kwikledgers_langgraph.day_end_closure_cli --trigger scheduled --backend fastmcp --timezone America/Sao_Paulo --notify >>%q 2>&1 %s\n' \
    "$workspace_root" "$env_file" "$fastmcp_python" "$python_path" "$audit_dir/day-end-closure.log" "$marker" >>"$updated"
crontab "$updated"
printf 'Fechamento automatico agendado: segunda a sexta, 18:00 (America/Sao_Paulo), com escrita no Azure (horas, Closed e continuacao).\n'
printf 'Log local: %s/day-end-closure.log\n' "$audit_dir"
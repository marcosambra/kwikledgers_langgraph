#!/usr/bin/env bash
set -euo pipefail

project_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
workspace_root=$(cd "$project_root/.." && pwd)
python_path="$project_root/.venv/bin/python"
audit_dir="$workspace_root/AI_Tracking/Audit"
marker='# kwikledgers-day-end-closure'

command -v crontab >/dev/null
[[ -x "$python_path" ]] || { printf 'Runtime Python indisponivel: %s\n' "$python_path" >&2; exit 1; }
[[ $(date +%z) == $(TZ=America/Sao_Paulo date +%z) ]] || {
    printf 'O fuso do cron deve corresponder a America/Sao_Paulo.\n' >&2
    exit 1
}
[[ "$workspace_root" != *'%'* ]] || { printf 'Caminho com %% nao e suportado pelo registro cron.\n' >&2; exit 1; }

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
printf '0 18 * * 1-5 /usr/bin/env SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt KWIKLEDGERS_ENV_FILE=%q %q -m kwikledgers_langgraph.day_end_closure_cli --trigger scheduled --backend legacy --timezone America/Sao_Paulo --notify >>%q 2>&1 %s\n' \
    "$workspace_root/agent/.env" "$python_path" "$audit_dir/day-end-closure.log" "$marker" >>"$updated"
crontab "$updated"
printf 'Fechamento agendado: segunda a sexta, 18:00 (America/Sao_Paulo).\n'
printf 'Log local: %s/day-end-closure.log\n' "$audit_dir"
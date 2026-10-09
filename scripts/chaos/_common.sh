# shellcheck shell=bash
# Shared setup for chaos scripts: scratch dir, flaky proxy, config pointing at it.
# Requires OMNIROUTE_API_KEY in env. Never prints it.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CORE="$HERE/../../core"
UPSTREAM="${UPSTREAM:-localhost:8080}"  # set UPSTREAM to your own OpenAI-compatible endpoint
PORT="${PROXY_PORT:-18080}"
MODEL="${CHAOS_MODEL:-auto/coding-cheap}"
# OmniRoute is paused by the owner (2026-10-07): the suite runs against the deterministic fake
# upstream unless K3_ALLOW_OMNIROUTE=1 is set. The proxy needs *a* key value; "x" works for the fake.
if [ "${K3_ALLOW_OMNIROUTE:-0}" = 1 ]; then
  : "${OMNIROUTE_API_KEY:?OMNIROUTE_API_KEY not set}"
else
  OMNIROUTE_API_KEY=x; export OMNIROUTE_API_KEY
  case "${CHAOS_UPSTREAM:-auto}" in real) echo "[chaos] CHAOS_UPSTREAM=real refused: OmniRoute is paused (set K3_ALLOW_OMNIROUTE=1)" >&2; exit 2;; esac
  CHAOS_UPSTREAM=fake
fi
WORK="$(mktemp -d /tmp/k3code-chaos.XXXXXX)"
export K3CODE_HOME="$WORK/home"
mkdir -p "$K3CODE_HOME" "$WORK/proj/.k3code"
MODE="$WORK/mode"
cat > "$WORK/proj/.k3code/config.yaml" <<YAML
providers:
  - name: omniroute
    kind: openai
    base_url: http://127.0.0.1:$PORT/v1
    api_key_env: OMNIROUTE_API_KEY
    models: {default: "$MODEL"}
default_model: default
max_turns: 12
headless_permission: yolo
reliability:
  netwatch:
    http_probe_url: http://127.0.0.1:$PORT/v1/models
    tcp_probe_host: 127.0.0.1
    tcp_probe_port: $PORT
    base_interval: 1.0
    max_interval: 3.0
    provider_fail_threshold: 1
YAML
# Upstream: real OmniRoute if it answers a chat completion, else the deterministic fake
# (OmniRoute keys can be quota-exhausted). Force with CHAOS_UPSTREAM=real|fake.
FAKE_PORT=$((PORT + 1))
if [ "${CHAOS_UPSTREAM:-auto}" = auto ]; then
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 60 -H "Authorization: Bearer $OMNIROUTE_API_KEY" \
    -H 'content-type: application/json' "http://$UPSTREAM/v1/chat/completions" \
    -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":5}" || true)
  [ "$code" = 200 ] && CHAOS_UPSTREAM=real || CHAOS_UPSTREAM=fake
fi
echo "[chaos] upstream: $CHAOS_UPSTREAM"
if [ "$CHAOS_UPSTREAM" = fake ]; then
  python3 "$HERE/fake_upstream.py" "$FAKE_PORT" &
  FAKE_PID=$!
  UPSTREAM="127.0.0.1:$FAKE_PORT"
fi
python3 "$HERE/flaky_proxy.py" --listen "127.0.0.1:$PORT" --upstream "$UPSTREAM" --mode-file "$MODE" &
PROXY_PID=$!
cleanup() { kill "$PROXY_PID" ${FAKE_PID:+"$FAKE_PID"} 2>/dev/null || true; }
trap cleanup EXIT
sleep 1
k3code() { (cd "$WORK/proj" && uv run --project "$CORE" k3code "$@"); }

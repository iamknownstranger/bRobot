#!/usr/bin/env bash
# Thin shim: logic lives in the freebie_agent package.
set -euo pipefail
cd "$(dirname "$0")/../../.."
exec uv run freebie-skill ledger-query --config config.toml

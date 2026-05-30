#!/bin/bash
# Open WebUI entrypoint wrapper. Runs on every container start, applies a
# small surgical fix that the upstream image doesn't ship, then execs the
# image's normal entrypoint.
#
# All operations here are idempotent — they no-op on warm restarts and
# only do real work after a fresh container creation.
#
# Why this wrapper exists:
#
# WEB-SEARCH BUGFIX. Open WebUI's SafeWebBaseLoader._fetch (in
# /app/backend/open_webui/retrieval/web/utils.py) calls
#
#     session.get(url, **(self.requests_kwargs | kwargs),
#                 allow_redirects=AIOHTTP_CLIENT_ALLOW_REDIRECTS)
#
# but requests_kwargs ALREADY contains allow_redirects (set in __init__).
# Python raises:
#     TypeError: ClientSession.get() got multiple values for keyword
#     argument 'allow_redirects'
#
# The broad `except Exception` in _fetch_with_rate_limit swallows it and
# every URL fetch returns "" silently. Web Search appears to "search ok"
# then drops every result with "no sources found".
#
# Fix: strip the duplicate explicit kwarg. The merged dict supplies the
# same value, so behaviour is preserved.
#
# Removal criterion: when upstream Open WebUI fixes this in web/utils.py,
# this patch becomes a no-op (the substitution finds no match and logs a
# notice) and we can delete the wrapper. Track:
#   https://github.com/open-webui/open-webui  (search "allow_redirects")
#
# Playwright Chromium install is NOT done here — Open WebUI's own
# start.sh runs `playwright install chromium` when WEB_LOADER_ENGINE=
# playwright. We just persist the cache via a host-mounted volume in
# docker-compose.yml so the install only happens on a truly-fresh cache.

set -euo pipefail

log() { printf '[open-webui-entrypoint] %s\n' "$*"; }

WEB_UTILS=/app/backend/open_webui/retrieval/web/utils.py
if [ -f "$WEB_UTILS" ]; then
  if grep -q 'allow_redirects=AIOHTTP_CLIENT_ALLOW_REDIRECTS,$' "$WEB_UTILS"; then
    log "patching SafeWebBaseLoader._fetch (drop duplicate allow_redirects kwarg)"
    python3 - <<'PY'
import pathlib
p = pathlib.Path("/app/backend/open_webui/retrieval/web/utils.py")
src = p.read_text()
old = """async with session.get(
                        url,
                        **(self.requests_kwargs | kwargs),
                        allow_redirects=AIOHTTP_CLIENT_ALLOW_REDIRECTS,
                    ) as response:"""
new = """async with session.get(
                        url,
                        **(self.requests_kwargs | kwargs),
                    ) as response:"""
if old in src:
    p.write_text(src.replace(old, new))
    print("  -> patched")
else:
    print("  -> pattern not found (upstream may have fixed it; check)")
PY
  else
    log "SafeWebBaseLoader._fetch already patched (or upstream is fixed) - skipping"
  fi
else
  log "WARNING: $WEB_UTILS not found (Open WebUI layout changed?). Skipping patch."
fi

log "handing off to Open WebUI start.sh"
exec bash /app/backend/start.sh "$@"

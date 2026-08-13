#!/bin/sh
set -eu

if [ "${AUTOPROMPT_BOOTSTRAP:-false}" = "true" ]; then
  # Settings import already rejects the critical production misconfigurations.
  # Keep Django's complete deployment report visible, while leaving optional
  # HSTS preload/include-subdomains decisions to the verified domain rollout.
  python manage.py check --deploy --fail-level ERROR
  python manage.py migrate --noinput
  python manage.py seed_data
  python manage.py import_asset_library --manifest /app/assets/library/manifest.json
  python manage.py collectstatic --noinput
fi

exec "$@"

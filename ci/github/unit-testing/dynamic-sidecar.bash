#!/bin/bash
# http://redsymbol.net/articles/unofficial-bash-strict-mode/
set -o errexit  # abort on nonzero exitstatus
set -o nounset  # abort on unbound variable
set -o pipefail # don't hide errors within pipes
IFS=$'\n\t'

install() {
  bash ci/helpers/ensure_python_pip.bash
  pushd services/dynamic-sidecar
  pip3 install -r requirements/ci.txt -r requirements/_tools.txt
  popd
  pip list -v
}

codestyle() {
  pushd services/dynamic-sidecar
  make codestyle-ci
  popd
}

test() {
  pytest \
    --asyncio-mode=auto \
    --color=yes \
    --cov-append \
    --cov-config=.coveragerc \
    --cov-report=term-missing \
    --cov-report=xml \
    --cov=simcore_service_dynamic_sidecar \
    --durations=10 \
    --log-date-format="%Y-%m-%d %H:%M:%S" \
    --log-format="%(asctime)s %(levelname)s %(message)s" \
    --verbose \
    -m "not heavy_load" \
    services/dynamic-sidecar/tests/unit
}

# Check if the function exists (bash specific)
if declare -f "$1" >/dev/null; then
  # call arguments verbatim
  "$@"
else
  # Show a helpful error
  echo "'$1' is not a known function name" >&2
  exit 1
fi

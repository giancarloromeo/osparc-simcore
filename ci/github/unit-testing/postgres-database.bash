#!/bin/bash
# http://redsymbol.net/articles/unofficial-bash-strict-mode/
set -o errexit   # abort on nonzero exitstatus
set -o nounset   # abort on unbound variable
set -o pipefail  # don't hide errors within pipes
IFS=$'\n\t'

install() {
    bash ci/helpers/ensure_python_pip.bash
    pushd packages/postgres-database; pip3 install -r requirements/ci.txt; popd;
    pip list -v
}

test() {
    pytest \
      --asyncio-mode=auto \
      --color=yes \
      --cov-append \
      --cov-config=.coveragerc \
      --cov-report=term-missing \
      --cov-report=xml \
      --cov=simcore_postgres_database \
      --log-date-format="%Y-%m-%d %H:%M:%S" \
      --log-format="%(asctime)s %(levelname)s %(message)s" \
      --durations=10 \
      --verbose \
      -m "not heavy_load" \
      packages/postgres-database/tests
}

# Check if the function exists (bash specific)
if declare -f "$1" > /dev/null
then
  # call arguments verbatim
  "$@"
else
  # Show a helpful error
  echo "'$1' is not a known function name" >&2
  exit 1
fi

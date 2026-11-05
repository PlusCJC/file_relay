#!/bin/sh
# The bundled runtime performs installation; system Python is not required.
set -eu
script_dir=${0%/*}
[ "$script_dir" != "$0" ] || script_dir=.
package_dir=$(CDPATH= cd -- "$script_dir" && pwd)
exec "$package_dir/file-relay" install "$@"

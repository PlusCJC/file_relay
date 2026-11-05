#!/bin/sh
# Kept beside the installed executable for one-command removal.
set -eu
script_dir=${0%/*}
[ "$script_dir" != "$0" ] || script_dir=.
install_dir=$(CDPATH= cd -- "$script_dir" && pwd)
exec "$install_dir/file-relay" uninstall "$@"

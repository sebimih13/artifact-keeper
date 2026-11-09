#!/bin/sh
# Publish 1.1.0-1 first, then run in each demo client.
set -eu
expected=${EXPECTED_VERSION:-1.1.0-1}
before=$(dpkg-query -W -f='${Version}' ak-devtools-hello)
apt-get -o Dir::Etc::sourcelist=sources.list.d/apt-local-devtools.sources \
    -o Dir::Etc::sourceparts=- -o APT::Get::List-Cleanup=0 \
    -o APT::Update::Error-Mode=any update
after_update=$(dpkg-query -W -f='${Version}' ak-devtools-hello)
test "$before" = "$after_update"
printf 'apt update left the installed version unchanged: %s\n' "$after_update"
apt-cache policy ak-devtools-hello ak-devtools-info
apt-get install -y --only-upgrade ak-devtools-hello ak-devtools-info
for package in ak-devtools-hello ak-devtools-info; do
    actual=$(dpkg-query -W -f='${Version}' "$package")
    test "$actual" = "$expected"
done
devtools-hello
devtools-info

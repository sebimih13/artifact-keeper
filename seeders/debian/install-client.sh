#!/bin/sh
# Run as root in a disposable Ubuntu client. Public read access; signed metadata.
set -eu
: "${APT_REPO_URL:?Set the reachable repository URL}"
arch=$(dpkg --print-architecture)
install -d -m 0755 /etc/apt/keyrings
install -m 0644 /demo/repo-key.asc /etc/apt/keyrings/apt-local-devtools.asc
cat > /etc/apt/sources.list.d/apt-local-devtools.sources <<EOF
Types: deb
URIs: $APT_REPO_URL
Suites: stable
Components: main
Architectures: $arch
Signed-By: /etc/apt/keyrings/apt-local-devtools.asc
EOF
# Isolate demo operations to this source; Ubuntu's normal sources stay available.
apt-get -o Dir::Etc::sourcelist=sources.list.d/apt-local-devtools.sources \
    -o Dir::Etc::sourceparts=- -o APT::Get::List-Cleanup=0 \
    -o APT::Update::Error-Mode=any update
version=${APT_PACKAGE_VERSION:-1.0.0-1}
apt-get install -y --no-install-recommends \
    "ak-devtools-hello=$version" "ak-devtools-info=$version"
dpkg-query -W -f='${Package} ${Version} ${Architecture}\n' ak-devtools-hello ak-devtools-info
devtools-hello
devtools-info

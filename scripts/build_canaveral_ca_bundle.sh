#!/usr/bin/env bash
#
# Build a PEM CA bundle that the prism-mcp server can pass to httpx as
# verify=<path>. Canaveral Artifactory presents a chain rooted at the
# internal "Canaveral - Root CA" certificate, which is installed in the
# macOS System keychain on Nutanix laptops but is NOT in certifi's
# Mozilla NSS bundle — so without this file Python rejects the TLS
# handshake with SELF_SIGNED_CERT_IN_CHAIN.
#
# Usage:
#   scripts/build_canaveral_ca_bundle.sh
#   # then in your .env (or shell):
#   export PRISM_MCP_CA_BUNDLE=~/.cache/prism-mcp/canaveral-ca-bundle.pem
#
# Idempotent: overwrites the destination file on every run so a refreshed
# certificate in the keychain shows up here without an extra step.

set -euo pipefail

DEST="${PRISM_MCP_CA_BUNDLE:-$HOME/.cache/prism-mcp/canaveral-ca-bundle.pem}"
mkdir -p "$(dirname "$DEST")"

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "error: this script reads the macOS System keychain and is" >&2
    echo "       Darwin-only. On Linux, point PRISM_MCP_CA_BUNDLE at" >&2
    echo "       /etc/ssl/certs/ca-certificates.crt or a path that has" >&2
    echo "       the Canaveral roots installed via update-ca-certificates." >&2
    exit 1
fi

KEYCHAIN="/Library/Keychains/System.keychain"
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

for cn in "Canaveral - Root CA" "Canaveral - Intermediate CA"; do
    if ! security find-certificate -c "$cn" -p "$KEYCHAIN" \
            >>"$TMP" 2>/dev/null ; then
        echo "warn: '$cn' not found in $KEYCHAIN" >&2
    fi
done

if [[ ! -s "$TMP" ]] ; then
    echo "error: no Canaveral certs extracted; aborting." >&2
    echo "       Install the Nutanix dev tools or import the Canaveral" >&2
    echo "       root certificate into the System keychain first." >&2
    exit 2
fi

mv "$TMP" "$DEST"

printf 'wrote %s\n' "$DEST"
openssl crl2pkcs7 -nocrl -certfile "$DEST" 2>/dev/null \
    | openssl pkcs7 -print_certs -noout 2>/dev/null \
    | grep -E "^(subject|issuer)=" \
    | sed 's/^/  /'

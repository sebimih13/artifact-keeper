#!/usr/bin/env python3
"""Generate fresh deployment secrets and a dedicated PostgreSQL CA offline.

Never overwrites existing .env or PostgreSQL certificates. Does not generate
the HTTPS certificate: supply your existing certificate for artifacts.ceva.internal.
"""
import base64
import os
from pathlib import Path
import secrets
import subprocess

ROOT = Path(__file__).resolve().parent


def main():
    os.umask(0o077)
    env_path = ROOT / ".env"
    if env_path.exists():
        print("Keeping existing .env (no credential rotation).")
    else:
        values = {
            "POSTGRES_PASSWORD": secrets.token_hex(32),
            "POSTGRES_APP_PASSWORD": secrets.token_hex(32),
            "JWT_SECRET": secrets.token_hex(32),
            "SSO_ENCRYPTION_KEY": secrets.token_hex(32),
            "AK_WEBHOOK_SECRET_KEY": base64.b64encode(secrets.token_bytes(32)).decode(),
            "INITIAL_ADMIN_PASSWORD": "Ak1!" + secrets.token_hex(24),
        }
        template = (ROOT / ".env.example").read_text()
        for key, value in values.items():
            template = template.replace(f"{key}=GENERATE", f"{key}={value}")
        with env_path.open("x") as output:
            output.write(template)
        env_path.chmod(0o600)
        print("Created .env with independent random secrets (mode 0600).")

    pg = ROOT / "certs/postgres"
    ca = ROOT / "certs/authority"
    https = ROOT / "certs/https"
    for directory in (pg, ca, https):
        directory.mkdir(parents=True, exist_ok=True)
    expected = [pg / "postgres-server.crt", pg / "postgres-server.key", pg / "postgres-ca.crt"]
    if all(path.exists() for path in expected):
        print("Keeping existing PostgreSQL certificates.")
    else:
        if any(path.exists() for path in expected) or (ca / "postgres-ca.key").exists():
            raise SystemExit("Incomplete PostgreSQL PKI exists; inspect it before regenerating.")
        def openssl(*args):
            subprocess.run(["openssl", *map(str, args)], check=True, capture_output=True)

        openssl("req", "-x509", "-newkey", "rsa:3072", "-nodes", "-sha256", "-days", "3650",
                "-subj", "/CN=CEVA Artifact Keeper PostgreSQL CA",
                "-addext", "basicConstraints=critical,CA:TRUE",
                "-addext", "keyUsage=critical,keyCertSign,cRLSign",
                "-keyout", ca / "postgres-ca.key", "-out", pg / "postgres-ca.crt")
        openssl("req", "-new", "-newkey", "rsa:2048", "-nodes",
                "-subj", "/CN=postgres", "-keyout", pg / "postgres-server.key",
                "-out", ca / "postgres-server.csr")
        extensions = ca / "postgres-server.ext"
        extensions.write_text("basicConstraints=critical,CA:FALSE\n"
                              "keyUsage=critical,digitalSignature,keyEncipherment\n"
                              "extendedKeyUsage=serverAuth\nsubjectAltName=DNS:postgres\n")
        openssl("x509", "-req", "-in", ca / "postgres-server.csr",
                "-CA", pg / "postgres-ca.crt", "-CAkey", ca / "postgres-ca.key",
                "-CAcreateserial", "-days", "397", "-sha256", "-extfile", extensions,
                "-out", pg / "postgres-server.crt")
        for certificate in pg.glob("*.crt"):
            certificate.chmod(0o644)
        print("Created dedicated PostgreSQL CA and server certificate (397 days).")
    print("Supply certs/https/fullchain.pem and certs/https/privkey.pem, then run ./check.sh.")


if __name__ == "__main__":
    main()

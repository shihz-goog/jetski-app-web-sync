#!/usr/bin/env python3
"""
Dynamically extracts the embedded TLS certificate and private key from the user's local
~/.jetski-server/bin/*/extensions/antigravity/ installation into ~/.gemini/jetski/bin/.
Ensures zero private keys or certificates are ever stored in the Git repository.
"""

import glob
import os
import re
import sys

HOME = os.path.expanduser("~")
OUT_DIR = os.path.join(HOME, ".gemini", "jetski", "bin")


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    bins = sorted(
        glob.glob(
            os.path.join(
                HOME,
                ".jetski-server",
                "bin",
                "*",
                "extensions",
                "antigravity",
                "bin",
                "language_server_linux_*",
            )
        )
    )
    bins = [b for b in bins if not b.endswith(".real")]
    if not bins:
        print("ERROR: No language_server_linux_* binary found under ~/.jetski-server/bin/", file=sys.stderr)
        sys.exit(1)

    with open(bins[-1], "rb") as f:
        data = f.read()

    keys = re.findall(rb"-----BEGIN PRIVATE KEY-----.*?-----END PRIVATE KEY-----", data, re.S)
    if not keys:
        print("ERROR: Could not find embedded TLS private key in language_server binary", file=sys.stderr)
        sys.exit(1)

    key_out = os.path.join(OUT_DIR, "ls_bridge_key.pem")
    with open(key_out, "wb") as f:
        f.write(keys[0] + b"\n")
    os.chmod(key_out, 0o600)

    certs = sorted(
        glob.glob(
            os.path.join(
                HOME,
                ".jetski-server",
                "bin",
                "*",
                "extensions",
                "antigravity",
                "dist",
                "languageServer",
                "cert.pem",
            )
        )
    )
    if not certs:
        print("ERROR: Could not find cert.pem under ~/.jetski-server/bin/", file=sys.stderr)
        sys.exit(1)

    cert_out = os.path.join(OUT_DIR, "ls_bridge_cert.pem")
    with open(certs[-1], "rb") as f:
        cert_data = f.read()
    with open(cert_out, "wb") as f:
        f.write(cert_data)

    print(f"Successfully extracted local TLS certificate and key to {OUT_DIR}")


if __name__ == "__main__":
    main()

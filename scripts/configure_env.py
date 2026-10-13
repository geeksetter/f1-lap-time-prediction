"""Create private local settings without printing or overwriting credentials."""

import argparse
import os
import secrets
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=55432)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Choose a port from 1024 to 65535")
    target = Path(__file__).resolve().parents[1] / ".env"
    if target.exists():
        parser.error(".env already exists; edit it locally rather than overwriting it")
    password = secrets.token_urlsafe(24)
    # token_urlsafe uses only URL-safe characters, so no password escaping is needed.
    contents = (
        "POSTGRES_USER=f1\nPOSTGRES_DB=f1_project\n"
        f"POSTGRES_PASSWORD={password}\nPOSTGRES_PORT={args.port}\n"
        f"DATABASE_URL=postgresql://f1:{password}@127.0.0.1:{args.port}/f1_project\n"
        "OPENF1_BASE_URL=https://api.openf1.org/v1\nOPENF1_TIMEOUT=60\n"
        "OPENF1_ATTEMPTS=4\nOPENF1_MIN_INTERVAL=2.1\n"
    )
    # Exclusive creation and restricted permissions even before the first write.
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(contents)
    print(f"Created private .env for localhost:{args.port}; credentials are not printed")


if __name__ == "__main__":
    main()

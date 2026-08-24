from __future__ import annotations

import argparse
import secrets

from cryptography.fernet import Fernet


def generate_secrets() -> None:
    print("SESSION_SECRET=" + secrets.token_urlsafe(48))
    print("CONFIG_ENCRYPTION_KEY=" + Fernet.generate_key().decode("ascii"))


def main() -> None:
    parser = argparse.ArgumentParser(description="mobo 运维工具")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("generate-secrets", help="生成会话和配置加密密钥")
    args = parser.parse_args()
    if args.command == "generate-secrets":
        generate_secrets()


if __name__ == "__main__":
    main()

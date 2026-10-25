"""Operator CLI: python -m app.cli create-admin EMAIL"""

from __future__ import annotations

import getpass
import sys

from sqlalchemy import select

from app.config import get_settings
from app.db import db_session
from app.models import User
from app.security import hash_password


def create_admin(email: str, password: str) -> None:
    s = get_settings()
    if len(password) < s.min_password_length:
        raise SystemExit(f"Password must be at least {s.min_password_length} characters")
    with db_session() as db:
        u = db.scalar(select(User).where(User.email == email.lower()))
        if u is None:
            u = User(email=email.lower(), password_hash=hash_password(password), is_admin=True)
            db.add(u)
        else:
            u.is_admin = True
        print(f"Admin ready: {email}")


def main(argv: list[str]) -> None:
    if len(argv) == 3 and argv[1] == "create-admin":
        create_admin(argv[2], getpass.getpass("Password: "))
    else:
        raise SystemExit("usage: python -m app.cli create-admin EMAIL")


if __name__ == "__main__":
    main(sys.argv)

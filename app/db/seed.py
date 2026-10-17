"""Seed the owner and two sample contacts. Safe to run repeatedly.

Usage: python -m app.db.seed
"""

from sqlalchemy import select

from app.config import get_settings
from app.db.models import Contact, User
from app.db.session import session_scope

SAMPLE_CONTACTS = [
    {
        "name": "علی رضایی",
        "aliases": ["علی", "آقای رضایی", "Ali Rezaei", "Ali"],
        "email": "ali.rezaei@example.com",
    },
    {
        "name": "سارا محمدی",
        "aliases": ["سارا", "خانم محمدی", "Sara Mohammadi", "Sara"],
        "email": "sara.mohammadi@example.com",
    },
]


def seed() -> None:
    settings = get_settings()
    owner_id = settings.owner_telegram_id
    if owner_id is None:
        print("seed: ALLOWED_USER_IDS is empty; skipping")
        return

    with session_scope() as session:
        owner = session.scalar(select(User).where(User.telegram_user_id == owner_id))
        if owner is None:
            owner = User(telegram_user_id=owner_id)
            session.add(owner)
        owner.name = settings.owner_name or owner.name or "Owner"
        owner.email = settings.owner_email or owner.email
        session.flush()

        existing = set(session.scalars(select(Contact.email).where(Contact.user_id == owner.id)))
        added = 0
        for data in SAMPLE_CONTACTS:
            if data["email"] not in existing:
                session.add(Contact(user_id=owner.id, **data))
                added += 1

    print(f"seed: owner ready, {added} contact(s) added")


if __name__ == "__main__":
    seed()

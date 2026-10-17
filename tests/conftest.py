"""Shared fixtures. Tests use a separate <db>_test database on the compose Postgres server."""

import datetime as dt
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.agent.fa import TEHRAN
from app.config import get_settings
from app.db.models import Base, Contact, User
from app.tools.registry import ToolContext

# Sunday 26 Mehr 1405, 10:00 in Tehran.
NOW = dt.datetime(2026, 10, 18, 10, 0, tzinfo=TEHRAN)


@pytest.fixture(scope="session")
def engine():
    url = make_url(get_settings().database_url)
    test_url = url.set(database=f"{url.database}_test")
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        exists = conn.scalar(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": test_url.database})
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{test_url.database}"'))
    admin.dispose()

    engine = create_engine(test_url)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine) -> Iterator[Session]:
    with engine.begin() as conn:
        tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()


@pytest.fixture
def owner(session: Session) -> User:
    user = User(telegram_user_id=1001, name="مالک", email="owner@example.com")
    session.add(user)
    session.flush()
    session.add_all(
        [
            Contact(user_id=user.id, name="علی رضایی", aliases=["علی", "Ali Rezaei"], email="ali.rezaei@example.com"),
            Contact(user_id=user.id, name="سارا محمدی", aliases=["سارا", "Sara"], email="sara.mohammadi@example.com"),
        ]
    )
    session.commit()
    return user


class FakeCalendar:
    def __init__(self) -> None:
        self.created: dict[str, dict] = {}

    def list_events(self, start, end):
        return [
            {"title": e["title"], "start": e["start"].isoformat(), "end": e["end"].isoformat()}
            for e in self.created.values()
            if start <= e["start"] < end
        ]

    def create_event(self, event_id, start, end, title, description, guests):
        self.created.setdefault(
            event_id, {"start": start, "end": end, "title": title, "description": description, "guests": guests}
        )
        return {"event_id": event_id, "html_link": f"https://calendar.test/{event_id}"}


class FakeEmail:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def __call__(self, to, subject, text, html=None, idempotency_key=None) -> str:
        self.sent.append({"to": to, "subject": subject, "text": text, "key": idempotency_key})
        return f"msg-{len(self.sent)}"


@pytest.fixture
def calendar() -> FakeCalendar:
    return FakeCalendar()


@pytest.fixture
def email() -> FakeEmail:
    return FakeEmail()


@pytest.fixture
def ctx(session, owner, calendar, email) -> ToolContext:
    return ToolContext(session=session, user_id=owner.id, now=lambda: NOW, calendar=calendar, send_email=email)

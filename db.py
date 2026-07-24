import os
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


engine: Engine | None = None
SessionLocal: sessionmaker[Session] | None = None


def normalize_database_url(database_url: str) -> str:
    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", "postgresql+psycopg://", 1)

    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+psycopg://", 1)

    return database_url


def create_db_engine(database_url: str) -> Engine:
    return create_engine(
        normalize_database_url(database_url),
        pool_pre_ping=True,
    )


def init_db(database_url: str) -> None:
    global engine, SessionLocal
    engine = create_db_engine(database_url)
    SessionLocal = sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
    )


def init_db_from_env() -> None:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("Missing required environment variable(s): DATABASE_URL")

    init_db(database_url)


def close_db() -> None:
    global engine, SessionLocal
    if engine is not None:
        engine.dispose()
    engine = None
    SessionLocal = None


def get_db_session() -> Iterator[Session]:
    if SessionLocal is None:
        raise RuntimeError("Database session factory was not initialized.")

    with SessionLocal() as session:
        yield session

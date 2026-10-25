from __future__ import annotations

import os

# TEST_DATABASE_URL=postgresql+psycopg://... runs the whole suite against PostgreSQL.
TEST_DB = os.environ.get("TEST_DATABASE_URL", "sqlite://")
os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = TEST_DB
os.environ["SECRET_KEY"] = "test-secret-key-test-secret-key-0123456789"
os.environ["RUN_WORKER"] = "false"
os.environ["PUBLIC_BASE_URL"] = "http://testserver"

import pytest  # noqa: E402

from app import db as dbmod  # noqa: E402
from app.channels.clients import set_client  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.llm.providers import set_provider_override  # noqa: E402
from app.models import Business, User  # noqa: E402
from app.security import hash_password, limiter, new_token  # noqa: E402
from app.services import retrieval  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_db():
    get_settings.cache_clear()
    engine = dbmod.init_engine(TEST_DB)
    dbmod.Base.metadata.drop_all(engine)
    dbmod.Base.metadata.create_all(engine)
    limiter.reset()
    retrieval.clear_cache()
    set_provider_override(None)
    set_client(None)
    yield
    engine.dispose()


@pytest.fixture
def db():
    s = dbmod.session_factory()()
    yield s
    s.close()


def make_business(db, email="owner@example.com", name="Тест", site="", verified=True, **kw) -> Business:
    u = User(email=email, password_hash=hash_password("correct horse battery"))
    db.add(u)
    db.flush()
    b = Business(
        owner_id=u.id,
        name=name,
        site_url=site,
        site_verified=verified,
        verify_token=new_token(12),
        public_key=new_token(16),
        channels_enabled=True,
        **kw,
    )
    db.add(b)
    db.commit()
    return b


@pytest.fixture
def biz(db):
    return make_business(db)


KB_TEXTS = [
    ("Режим работы", "Мы работаем ежедневно с 10:00 до 22:00. Без выходных."),
    ("Адрес", "Адрес: Уфа, улица Ленина, 15. Парковка бесплатная для гостей."),
    ("Заезд на карте", "Заезд на электрокартинге длится 10 минут и стоит 800 ₽. Дети с 7 лет."),
    ("Телефон", "Телефон для связи: +7 (347) 123-45-67."),
]


@pytest.fixture
def kb_biz(db, biz):
    from app.services import kb

    for title, text in KB_TEXTS:
        kb.add_text(db, biz, text, title=title, kind="faq", published=True)
    db.commit()
    return biz

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from uuid import UUID

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

from recruitment.core.config import Settings


class Database:
    def __init__(self, settings: Settings) -> None:
        self.api = self._engine(settings.database_url.get_secret_value(), "recruitment_api")
        self.auth = self._engine(settings.auth_database_url.get_secret_value(), "recruitment_auth")
        self.worker = self._engine(
            settings.worker_database_url.get_secret_value(), "recruitment_worker"
        )
        self.dispatcher = self._engine(
            settings.dispatcher_database_url.get_secret_value(), "recruitment_dispatcher"
        )

    @staticmethod
    def _engine(url: str, role: str) -> Engine:
        from sqlalchemy import event

        engine = create_engine(
            url, pool_pre_ping=True, hide_parameters=True, connect_args={"connect_timeout": 10}
        )

        @event.listens_for(engine, "connect")
        def verify_role(connection: object, _: object) -> None:
            # DBAPI connection is deliberately checked before any application query.
            with connection.cursor() as cursor:  # type: ignore[attr-defined]
                cursor.execute(
                    "SELECT current_user, rolsuper, rolbypassrls FROM pg_roles "
                    "WHERE rolname = current_user"
                )
                actual, superuser, bypass = cursor.fetchone()
                if actual != role or superuser or bypass:
                    raise RuntimeError(f"Database connection must use the restricted {role} role.")
            connection.rollback()  # type: ignore[attr-defined]

        return engine

    @contextmanager
    def transaction(
        self, engine: Engine, tenant_id: UUID | None = None, user_id: UUID | None = None
    ) -> Iterator[Session]:
        with Session(engine) as session, session.begin():
            session.execute(
                text(
                    "SELECT set_config('TimeZone', 'UTC', true), "
                    "set_config('app.tenant_id', :tenant, true), "
                    "set_config('app.user_id', :user, true)"
                ),
                {
                    "tenant": str(tenant_id) if tenant_id else "",
                    "user": str(user_id) if user_id else "",
                },
            )
            yield session

    def dispose(self) -> None:
        for engine in (self.api, self.auth, self.worker, self.dispatcher):
            engine.dispose()

    def warm(self) -> None:
        def check(engine: Engine) -> None:
            with self.transaction(engine) as session:
                session.execute(text("SELECT 1"))

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(check, (self.api, self.auth)))

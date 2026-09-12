from alembic import context
from emer.settings import settings
from emer.storage.models import Base
from sqlalchemy import create_engine, pool

if context.is_offline_mode():
    context.configure(url=settings.database_url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(settings.database_url, poolclass=pool.NullPool)
    # All revisions share one transaction and lock, including simultaneous free-host startups.
    with engine.begin() as connection:
        connection.exec_driver_sql("SET LOCAL lock_timeout = '30s'")
        connection.exec_driver_sql("SELECT pg_advisory_xact_lock(7346203)")
        context.configure(connection=connection, target_metadata=Base.metadata)
        with context.begin_transaction():
            context.run_migrations()

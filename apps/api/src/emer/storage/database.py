from emer.settings import settings
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

engine = create_async_engine(settings.database_url, pool_size=4, max_overflow=2, pool_pre_ping=True)
Session = async_sessionmaker(engine, expire_on_commit=False)

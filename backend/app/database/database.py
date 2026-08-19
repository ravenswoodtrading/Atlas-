from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from pathlib import Path

# Store the database in the backend folder
BASE_DIR = Path(__file__).resolve().parents[2]
DATABASE_URL = f"sqlite:///{BASE_DIR / 'atlas.db'}"

# timeout=30: sqlite3's own busy-wait BEFORE it raises "database is
# locked" (default is 5s) -- a backstop for genuine writer-vs-writer
# contention. Belt-and-braces with the WAL pragma below, which is what
# actually prevents most contention in the first place.
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 30}
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, connection_record):
    """
    Default SQLite journal mode (DELETE/rollback) takes an exclusive
    lock on the WHOLE file for the duration of any write -- with the
    background scheduler (scan queue, competitor checks, weekly
    recheck) writing on its own schedule while the app also serves
    page requests, that meant a page load could hang or time out
    behind an in-flight background write (confirmed live -- "/" took
    anywhere from 1s to a full timeout depending on what the
    scheduler was doing). WAL mode lets readers proceed concurrently
    with a writer instead of blocking on it -- this is the standard
    fix for exactly this "one writer, many readers, all in the same
    process" shape, not a workaround specific to this app.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.close()


SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)
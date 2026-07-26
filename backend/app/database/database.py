from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from pathlib import Path

# Store the database in the backend folder
BASE_DIR = Path(__file__).resolve().parents[2]
DATABASE_URL = f"sqlite:///{BASE_DIR / 'atlas.db'}"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False}
)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)
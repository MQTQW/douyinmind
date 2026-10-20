"""Add analysis fields to existing local databases without replacing saved data."""
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine


def ensure_video_analysis_columns(engine: Engine) -> None:
    additions = {
        "visual_analysis_text": "TEXT NOT NULL DEFAULT ''",
        "analysis_json": "TEXT NOT NULL DEFAULT '{}'",
        "analysis_model": "VARCHAR(64) NOT NULL DEFAULT ''",
        "analysis_duration_seconds": "FLOAT NOT NULL DEFAULT 0",
    }
    with engine.begin() as connection:
        inspector = inspect(connection)
        if "video_cache" not in inspector.get_table_names():
            return
        columns = {column["name"] for column in inspector.get_columns("video_cache")}
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(text(f"ALTER TABLE video_cache ADD COLUMN {name} {definition}"))


def ensure_chat_knowledge_base_column(engine: Engine) -> None:
    """Keep saved conversations while adding their optional knowledge scope."""
    with engine.begin() as connection:
        inspector = inspect(connection)
        if "chat_sessions" not in inspector.get_table_names():
            return
        columns = {column["name"] for column in inspector.get_columns("chat_sessions")}
        if "knowledge_base_id" not in columns:
            connection.execute(text("ALTER TABLE chat_sessions ADD COLUMN knowledge_base_id INTEGER REFERENCES knowledge_bases(id)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_chat_sessions_knowledge_base_id ON chat_sessions (knowledge_base_id)"))

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


def ensure_knowledge_base_tombstones(engine: Engine) -> None:
    """Retain library IDs and stop legacy writers assigning deleted scopes."""
    with engine.begin() as connection:
        inspector = inspect(connection)
        tables = inspector.get_table_names()
        if "knowledge_bases" not in tables:
            return
        columns = {column["name"] for column in inspector.get_columns("knowledge_bases")}
        if "deleted_at" not in columns:
            connection.execute(text("ALTER TABLE knowledge_bases ADD COLUMN deleted_at DATETIME"))
        if connection.dialect.name != "sqlite" or "knowledge_base_videos" not in tables:
            return
        for operation, suffix in (("INSERT", "insert"), ("UPDATE OF knowledge_base_id", "update")):
            connection.execute(text(f"""CREATE TRIGGER IF NOT EXISTS prevent_deleted_library_membership_{suffix}
                BEFORE {operation} ON knowledge_base_videos
                WHEN EXISTS (SELECT 1 FROM knowledge_bases
                             WHERE id = NEW.knowledge_base_id AND deleted_at IS NOT NULL)
                BEGIN SELECT RAISE(ABORT, 'knowledge_base_deleted'); END"""))

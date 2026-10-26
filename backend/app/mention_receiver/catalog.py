"""Read the existing library catalogue without opening the application or worker."""
from pathlib import Path
from contextlib import closing
import sqlite3


def configured_database(url: str, backend: Path) -> Path:
    from sqlalchemy.engine import make_url
    parsed = make_url(url)
    if parsed.drivername not in {"sqlite", "sqlite+pysqlite"} or not parsed.database or parsed.database == ":memory:" or parsed.query:
        raise ValueError("接收原型只支持显式的本机 SQLite 文件目录")
    path = Path(parsed.database)
    return (path if path.is_absolute() else backend / path).resolve(strict=True)


def read_libraries(database: Path) -> list[dict]:
    database = database.resolve(strict=True)
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.execute("PRAGMA query_only=ON")
        return [{"id": row[0], "name": row[1]} for row in conn.execute(
            "SELECT id, name FROM knowledge_bases ORDER BY id"
        )]

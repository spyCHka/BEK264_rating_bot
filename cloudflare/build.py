"""Bundle only the public bot modules and the current empty journal schema."""
import sys
from pathlib import Path
from shutil import copyfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
from bot import connect

# Parents precede children for an import with foreign keys enabled.
TABLES = ("settings", "viewers", "students", "accounts", "identities", "votes",
          "invitations", "pickers", "grade_views", "outbox")


def schema():
    db = connect(":memory:")
    try:
        ddl = [r[0] for r in db.execute("""SELECT sql FROM sqlite_schema
            WHERE name NOT LIKE 'sqlite_%' AND sql IS NOT NULL
            ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 ELSE 2 END, name""")]
        columns = {t: [r[1] for r in db.execute(f"PRAGMA table_info({t})")] for t in TABLES}
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
        if set(columns) != tables - {"sqlite_sequence"}:
            raise ValueError("Update the migration table list to match the new journal schema.")
        return ddl, columns
    finally:
        db.close()


def build():
    directory = HERE / "dist"
    directory.mkdir(exist_ok=True)
    ddl, columns = schema()
    (directory / "schema.py").write_text(f"DDL = {ddl!r}\nCOLUMNS = {columns!r}\n")
    for path in (ROOT / "bot.py", ROOT / "delivery.py", HERE / "entry.py", HERE / "journal_store.py", HERE / "protocol.py"):
        copyfile(path, directory / path.name)


if __name__ == "__main__":
    build()

"""SQLite-compatible access to local storage and hosted libSQL."""

import sqlite3
from importlib import import_module
from pathlib import Path

class Row(dict):
    """Support both column names and positional access, like sqlite3.Row."""

    def __getitem__(self, key):
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)


class Cursor:
    """Expose dictionary-like rows from the libSQL DB-API cursor."""

    def __init__(self, result, rowcount: int | None = None):
        self.result = result
        self.columns = [column[0] for column in result.description or ()]
        self._rowcount = rowcount

    @property
    def lastrowid(self):
        return self.result.lastrowid

    @property
    def rowcount(self):
        return self._rowcount if self._rowcount is not None else self.result.rowcount

    def fetchone(self):
        row = self.result.fetchone()
        return Row(zip(self.columns, row)) if row is not None else None

    def fetchall(self):
        return [Row(zip(self.columns, row)) for row in self.result.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


class RemoteConnection:
    """Normalize libSQL cursors and preserve transaction boundaries."""

    def __init__(self, client):
        self.client = client
        self.depth = 0

    def execute(self, sql, parameters=()):
        return Cursor(self.client.execute(sql, parameters))

    def batch(self, statements):
        if self.depth:
            raise RuntimeError("Batch writes must not be nested in an interactive transaction")
        groups = {}
        for index, (sql, parameters) in enumerate(statements):
            groups.setdefault(sql, []).append((index, parameters))
        results = {}
        with self.client:
            for sql, group in groups.items():
                indexes, parameters = zip(*group)
                if len(group) == 1:
                    results[indexes[0]] = Cursor(self.client.execute(sql, parameters[0]))
                    continue
                cursor = Cursor(self.client.executemany(sql, parameters))
                results[indexes[0]] = cursor
                for index in indexes[1:]:
                    results[index] = Cursor(cursor.result, rowcount=0)
        return [results[index] for index in range(len(statements))]

    def executescript(self, script):
        for statement in script.split(";"):
            if statement.strip() and "journal_mode" not in statement:
                self.execute(statement)

    def __enter__(self):
        if self.depth == 0:
            self.client.__enter__()
        self.depth += 1
        return self

    def __exit__(self, exception_type, _exception, _traceback):
        self.depth -= 1
        if self.depth == 0:
            return self.client.__exit__(exception_type, _exception, _traceback)

    def close(self):
        self.client.close()


def batch(connection, statements):
    """Commit a complete scope atomically."""
    if isinstance(connection, RemoteConnection):
        return connection.batch(statements)
    with connection:
        return [connection.execute(sql, parameters) for sql, parameters in statements]


def connect(path: str, auth_token: str | None = None):
    """Use SQLite locally or a directly connected hosted libSQL database."""
    if path.startswith("libsql://"):
        if not auth_token:
            raise ValueError("TURSO_AUTH_TOKEN is required with TURSO_DATABASE_URL")
        client = import_module("libsql").connect(database=path, auth_token=auth_token)
        return RemoteConnection(client)
    if auth_token:
        raise ValueError("TURSO_DATABASE_URL must start with libsql://")
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection

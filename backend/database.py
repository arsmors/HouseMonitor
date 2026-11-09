"""SQLite-compatible access to local storage and hosted libSQL."""

import sqlite3
from pathlib import Path

from libsql_client import create_client_sync


class Row(dict):
    """Support both column names and positional access, like sqlite3.Row."""

    def __getitem__(self, key):
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)


class Cursor:
    """Normalize remote results without changing SQL or transaction boundaries."""

    def __init__(self, result):
        self.result = result
        self.rows = iter(Row(zip(result.columns, row)) for row in result.rows)

    @property
    def lastrowid(self):
        return self.result.last_insert_rowid

    @property
    def rowcount(self):
        return self.result.rows_affected

    def fetchone(self):
        return next(self.rows, None)

    def fetchall(self):
        return list(self.rows)

    def __iter__(self):
        return iter(self.fetchall())


class RemoteConnection:
    """Keep transactions on a direct cloud connection, never an ephemeral replica."""

    def __init__(self, client, *, supports_transactions: bool = True):
        self.client = client
        self.supports_transactions = supports_transactions
        self.transaction = None
        self.depth = 0

    def execute(self, sql, parameters=()):
        executor = self.transaction if self.transaction is not None else self.client
        return Cursor(executor.execute(sql, parameters))

    def batch(self, statements):
        if self.transaction is not None:
            raise RuntimeError("Batch writes must not be nested in an interactive transaction")
        return [Cursor(result) for result in self.client.batch(statements)]

    def executescript(self, script):
        for statement in script.split(";"):
            if statement.strip() and "journal_mode" not in statement:
                self.execute(statement)

    def __enter__(self):
        if not self.supports_transactions:
            raise RuntimeError("Interactive transactions are not supported by the HTTP database connection")
        if self.depth == 0:
            self.transaction = self.client.transaction()
        self.depth += 1
        return self

    def __exit__(self, exception_type, _exception, _traceback):
        self.depth -= 1
        if self.depth == 0:
            transaction, self.transaction = self.transaction, None
            try:
                if exception_type is None:
                    transaction.commit()
                else:
                    transaction.rollback()
            finally:
                transaction.close()

    def close(self):
        self.client.close()


def batch(connection, statements):
    """Commit a complete scope in one server round trip, not a long transaction."""
    if isinstance(connection, RemoteConnection):
        return connection.batch(statements)
    with connection:
        return [connection.execute(sql, parameters) for sql, parameters in statements]


def connect(path: str, auth_token: str | None = None):
    """Use SQLite locally or a directly connected hosted libSQL database."""
    if path.startswith("libsql://"):
        if not auth_token:
            raise ValueError("TURSO_AUTH_TOKEN is required with TURSO_DATABASE_URL")
        http_url = path.replace("libsql://", "https://", 1)
        return RemoteConnection(create_client_sync(http_url, auth_token=auth_token), supports_transactions=False)
    if auth_token:
        raise ValueError("TURSO_DATABASE_URL must start with libsql://")
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection

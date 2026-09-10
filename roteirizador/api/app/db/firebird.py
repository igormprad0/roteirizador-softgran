from __future__ import annotations
import re
from typing import Any, Sequence

import firebird.driver as fb

from ..config import Profile, get_settings

_READ_ONLY = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)


class ReadOnlyViolation(RuntimeError):
    """Tentativa de executar SQL que não é leitura contra a base do cliente."""


class ErpConnection:
    def __init__(self, dsn: str, user: str, password: str):
        self._con = fb.connect(dsn, user=user, password=password, charset="WIN1252")

    @staticmethod
    def _assert_read_only(sql: str) -> None:
        if not _READ_ONLY.match(sql):
            raise ReadOnlyViolation(
                f"acesso ao ERP é somente leitura; recusado: {sql.strip()[:80]!r}")

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        self._assert_read_only(sql)
        cur = self._con.cursor()
        try:
            cur.execute(sql, params)
            cols = [d[0].strip() for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        finally:
            cur.close()

    def close(self) -> None:
        self._con.close()

    def __enter__(self) -> "ErpConnection":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def connect(profile: Profile) -> ErpConnection:
    s = get_settings()
    return ErpConnection(s.dsn(profile), s.fb_user, s.fb_password)

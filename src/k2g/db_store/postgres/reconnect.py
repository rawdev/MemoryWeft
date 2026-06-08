"""Self-healing psycopg2 connection mixin for managed / serverless Postgres.

Managed Postgres (Neon scale-to-zero, Supabase pooler, RDS Proxy) closes idle
connections server-side. A long-lived store that caches a single connection then
raises ``psycopg2.InterfaceError: connection already closed`` on the next use,
and stays broken until the process restarts. Local / self-hosted Postgres never
shows this — it keeps idle connections open — so the failure only surfaces
against managed backends (and intermittently, only after an idle gap).

``ReconnectingConnMixin`` turns ``_conn`` into a property that transparently
reopens the connection (via the subclass-supplied ``_new_connection``) whenever
the cached handle is missing or ``closed``. ``KEEPALIVE_KWARGS`` adds libpq TCP
keepalives so a server-dropped socket is detected promptly rather than on a
later blocking call.

Existing ``self._conn = <conn>`` assignments keep working through the property
setter, so call sites are unchanged; only ``close()`` must call ``_close_conn()``
to tear the connection down without the property resurrecting it.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# libpq TCP keepalives — passed straight through ``psycopg2.connect`` to libpq.
# Surface a server-dropped socket within ~30s idle + 5×10s probes instead of
# blocking on the next query.
KEEPALIVE_KWARGS: dict[str, int] = {
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 5,
}

# Instance-dict key holding the live connection. A distinct name from the
# ``_conn`` property avoids any descriptor recursion.
_BACKING = "_conn_backing"


class ReconnectingConnMixin:
    """Mixin providing a self-healing ``_conn`` property.

    Subclasses must implement ``_new_connection(self)`` returning a fresh,
    fully-initialised psycopg2 connection — i.e. with the same ``cursor_factory``,
    ``autocommit`` and any adapter registration (e.g. pgvector ``register_vector``)
    that the store relies on.
    """

    def _new_connection(self) -> Any:  # pragma: no cover - subclass responsibility
        raise NotImplementedError(
            f"{type(self).__name__} must implement _new_connection()"
        )

    @property
    def _conn(self) -> Any:
        conn = self.__dict__.get(_BACKING)
        if conn is None or conn.closed:
            if conn is not None:
                logger.warning(
                    "%s: Postgres connection was closed (managed-PG idle drop?) "
                    "— reconnecting",
                    type(self).__name__,
                )
            conn = self._new_connection()
            self.__dict__[_BACKING] = conn
        return conn

    @_conn.setter
    def _conn(self, value: Any) -> None:
        self.__dict__[_BACKING] = value

    def _close_conn(self) -> bool:
        """Close the cached connection without triggering a reconnect.

        Returns True when a live connection was actually closed. Safe in
        ``close()`` / ``__del__`` — it reads the backing field directly and never
        goes through the reconnecting property.
        """
        conn = self.__dict__.get(_BACKING)
        self.__dict__[_BACKING] = None
        if conn is not None and not conn.closed:
            conn.close()
            return True
        return False

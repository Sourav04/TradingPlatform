from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Boolean, Integer, String, create_engine, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column


class Base(DeclarativeBase):
    pass


class Control(Base):
    __tablename__ = "trading_control"

    id: Mapped[int] = mapped_column(primary_key=True)
    killed: Mapped[bool] = mapped_column(Boolean, nullable=False)


class Intent(Base):
    __tablename__ = "order_intents"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    strategy: Mapped[str] = mapped_column(String(64))
    symbol: Mapped[str] = mapped_column(String(64))
    target: Mapped[int] = mapped_column(Integer)
    quantity: Mapped[int] = mapped_column(Integer)
    filled: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20))


class BrokerOrder(Base):
    __tablename__ = "fake_broker_orders"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(64))
    quantity: Mapped[int] = mapped_column(Integer)
    filled: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20))
    visible_at: Mapped[str] = mapped_column(String(40))


class DesiredPosition(Base):
    __tablename__ = "desired_positions"

    symbol: Mapped[str] = mapped_column(String(64), primary_key=True)
    timestamp: Mapped[str] = mapped_column(String(40))
    target: Mapped[int] = mapped_column(Integer)


def net_positions(
    session: Session, model: type[Intent] | type[BrokerOrder]
) -> dict[str, int]:
    result: dict[str, int] = {}
    for symbol, filled in session.execute(select(model.symbol, model.filled)):
        result[symbol] = result.get(symbol, 0) + filled
    return {symbol: units for symbol, units in result.items() if units}


class Store:
    def __init__(self, url: str) -> None:
        self.engine: Engine = create_engine(url, pool_pre_ping=True)

    def initialize(self) -> None:
        Base.metadata.create_all(self.engine)
        with self.engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO trading_control (id, killed) VALUES (1, FALSE) "
                "ON CONFLICT (id) DO NOTHING"
            )

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        with Session(self.engine) as session, session.begin():
            # A write locks the same control row on PostgreSQL and serializes SQLite writers.
            session.execute(
                update(Control).where(Control.id == 1).values(killed=Control.killed)
            )
            if session.get(Control, 1) is None:
                raise RuntimeError("Trading control record missing")
            yield session

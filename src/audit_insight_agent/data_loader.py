from __future__ import annotations

from pathlib import Path
import re
from typing import Any

import pandas as pd

from .config import resolve_source_location
from .models import DataSource, SourceConfig



"""Загрузка структурированных и табличных данных.

Первая версия должна поддерживать CSV, Excel, Parquet, JSON и SQL, сохранять
сведения об источнике и применять единые правила типов. Чтение документов
здесь запрещено: это ответственность document_loader.py.
"""



SUPPORTED_EXTENSIONS = {
    ".csv": "csv",
    ".parquet": "parquet",
    ".xlsx": "excel",
    ".xls": "excel",
    ".json": "json",
}

FORBIDDEN_DIRECTORY_NAMES = {"private"}


def _is_forbidden(
    file_path: Path,
    root: Path,
) -> bool:
    relative_path = file_path.relative_to(
        root
    )

    path_parts = {
        part.lower()
        for part in relative_path.parts
    }

    if (
        path_parts
        & FORBIDDEN_DIRECTORY_NAMES
    ):
        return True

    return False


def discover_data_sources(
    data_root: str | Path,
) -> list[DataSource]:
    """
    Ищет только разрешенные публичные данные.
    """

    root = Path(
        data_root
    ).expanduser().resolve()

    if not root.exists():
        raise FileNotFoundError(
            f"Каталог данных не найден: {root}"
        )

    if not root.is_dir():
        raise NotADirectoryError(
            f"Ожидался каталог: {root}"
        )

    sources: list[DataSource] = []

    for file_path in root.rglob("*"):

        if not file_path.is_file():
            continue

        extension = (
            file_path.suffix.lower()
        )

        if extension not in (
            SUPPORTED_EXTENSIONS
        ):
            continue

        if _is_forbidden(
            file_path=file_path,
            root=root,
        ):
            continue

        relative_path = (
            file_path
            .relative_to(root)
            .as_posix()
        )

        sources.append(
            DataSource(
                source_id=relative_path,
                relative_path=relative_path,
                file_format=(
                    SUPPORTED_EXTENSIONS[
                        extension
                    ]
                ),
                size_bytes=(
                    file_path.stat().st_size
                ),
            )
        )

    return sorted(
        sources,
        key=lambda source: (
            source.relative_path
        ),
    )


def resolve_source_path(
    data_root: str | Path,
    source: DataSource,
) -> Path:
    """
    Безопасно восстанавливает полный путь.
    """

    root = Path(
        data_root
    ).expanduser().resolve()

    path = (
        root
        / source.relative_path
    ).resolve()

    if (
        path != root
        and root not in path.parents
    ):
        raise PermissionError(
            "Попытка выхода за каталог данных"
        )

    if _is_forbidden(
        file_path=path,
        root=root,
    ):
        raise PermissionError(
            f"Запрещенный источник: {path}"
        )

    return path


def load_table(
    data_root: str | Path,
    source: DataSource,
    columns: list[str] | None = None,
    limit: int | None = None,
) -> pd.DataFrame:
    """
    Загружает таблицу для конкретной проверки.
    """

    path = resolve_source_path(
        data_root=data_root,
        source=source,
    )

    if source.file_format == "csv":
        return pd.read_csv(
            path,
            usecols=columns,
            nrows=limit,
            low_memory=False,
        )

    if source.file_format == "parquet":

        dataframe = pd.read_parquet(
            path,
            columns=columns,
        )

        if limit is not None:
            dataframe = dataframe.head(
                limit
            )

        return dataframe

    if source.file_format == "excel":
        return pd.read_excel(
            path,
            usecols=columns,
            nrows=limit,
        )

    if source.file_format == "json":

        try:
            dataframe = pd.read_json(
                path,
                lines=True,
            )
        except ValueError:
            dataframe = pd.read_json(
                path,
            )

        if columns is not None:
            dataframe = dataframe[
                columns
            ]

        if limit is not None:
            dataframe = dataframe.head(
                limit
            )

        return dataframe

    raise ValueError(
        f"Формат не поддерживается: "
        f"{source.file_format}"
    )


def find_source(
    sources: tuple[DataSource, ...],
    filename: str,
) -> DataSource | None:
    """
    Ищет источник по имени файла.
    """

    for source in sources:

        if Path(
            source.relative_path
        ).name == filename:
            return source

    return None


def infer_table_format(path: Path, configured_format: str = "auto") -> str:
    if configured_format != "auto":
        return configured_format.lower()
    try:
        return SUPPORTED_EXTENSIONS[path.suffix.lower()]
    except KeyError as error:
        raise ValueError(f"Unsupported table extension: {path.suffix}") from error


def load_configured_table(
    source: SourceConfig,
    config_path: str | Path,
    limit: int | None = None,
) -> pd.DataFrame:
    """Load a configured table without relying on source names or schemas."""

    if source.source_type != "table":
        raise ValueError(f"Source {source.source_id!r} is not a table")
    path = resolve_source_location(source, config_path)
    if not path.is_file():
        raise FileNotFoundError(f"Table source not found: {path}")
    file_format = infer_table_format(path, source.format)
    options = dict(source.options)

    if file_format == "csv":
        frame = pd.read_csv(path, encoding=source.encoding, nrows=limit, **options)
    elif file_format == "excel":
        frame = pd.read_excel(path, nrows=limit, **options)
    elif file_format == "parquet":
        frame = pd.read_parquet(path, **options)
    elif file_format == "json":
        try:
            frame = pd.read_json(path, **options)
        except ValueError:
            frame = pd.read_json(path, lines=True, **options)
    else:
        raise ValueError(f"Unsupported configured table format: {file_format}")

    return frame.head(limit) if limit is not None else frame


def safe_table_name(source: SourceConfig) -> str:
    """Return an SQL-safe physical table name independent from business meaning."""

    candidate = source.table_name or source.source_id
    normalized = re.sub(r"[^A-Za-z0-9_]", "_", candidate).strip("_")
    if not normalized:
        raise ValueError(f"Cannot derive table name from {candidate!r}")
    if normalized[0].isdigit():
        normalized = f"source_{normalized}"
    return normalized


def quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


class DuckDBTableStore:
    """Persistent analytical store shared by ingestion, profiling and checks."""

    def __init__(self, database: str | Path = ":memory:") -> None:
        try:
            import duckdb
        except ImportError as error:
            raise RuntimeError("DuckDB support requires the 'duckdb' package") from error
        self.connection = duckdb.connect(str(database))

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> DuckDBTableStore:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def ingest(
        self,
        source: SourceConfig,
        config_path: str | Path,
        replace: bool = True,
    ) -> str:
        frame = load_configured_table(source, config_path)
        table_name = safe_table_name(source)
        quoted = quote_identifier(table_name)
        temporary_name = "_audit_ingest_frame"
        self.connection.register(temporary_name, frame)
        try:
            clause = "OR REPLACE " if replace else ""
            self.connection.execute(
                f"CREATE {clause}TABLE {quoted} AS SELECT * FROM {temporary_name}"
            )
        finally:
            self.connection.unregister(temporary_name)
        return table_name

    def query(self, sql: str, parameters: list[Any] | None = None) -> pd.DataFrame:
        """Execute calculations in DuckDB and return their tabular result."""

        return self.connection.execute(sql, parameters or []).fetchdf()

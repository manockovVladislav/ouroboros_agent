from __future__ import annotations

from pathlib import Path

import pandas as pd

from .models import DataSource



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

FORBIDDEN_DIRECTORY_NAMES = {
    "private",
    "error_injection_preview",
}

FORBIDDEN_FILE_FRAGMENTS = {
    "ground_truth",
    "error_injection_manifest",
    "hidden_error_business_logic",
    "participant_3_hidden_error",
}


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

    filename = (
        file_path.name.lower()
    )

    return any(
        fragment in filename
        for fragment
        in FORBIDDEN_FILE_FRAGMENTS
    )


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
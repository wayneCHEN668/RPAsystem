"""
data/file_reader.py
--------------------
从 Excel / CSV / JSON 文件读取业务数据。

职责：
  - 读取文件，返回统一的 list[dict] 格式
  - 支持列名映射（重命名、过滤）
  - 支持基础数据清洗（去空行、去空白、类型转换）
  - 同步接口（文件 IO 不需要异步）

依赖：pandas + openpyxl（Excel），标准库（CSV / JSON）
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from loguru import logger


# ─────────────────────────────────────────────
# 数据结构
# ─────────────────────────────────────────────

ReadOptions = dict[str, Any]   # 传给 pandas read_excel / read_csv 的额外参数


# ─────────────────────────────────────────────
# Excel 读取
# ─────────────────────────────────────────────

def read_excel(
    path: str,
    sheet: str | int = 0,
    *,
    col_map: dict[str, str] | None = None,
    usecols: list[str] | None = None,
    drop_empty_rows: bool = True,
    dtype: dict[str, type] | None = None,
    skiprows: int = 0,
    nrows: int | None = None,
) -> list[dict[str, Any]]:
    """
    读取 Excel 文件，返回 list[dict]。

    Parameters
    ----------
    path : str
        .xlsx / .xls 文件路径。
    sheet : str | int
        Sheet 名或索引，默认第一个 Sheet。
    col_map : dict
        列名映射，如 {"客户名称": "customer", "热力站编号": "station_code"}。
        只映射指定的列，其余列名保持不变。
    usecols : list[str]
        只读取这些列（映射前的原始列名）。
    drop_empty_rows : bool
        是否丢弃全空行。
    dtype : dict
        列类型强制转换，如 {"heat_load": float}。
    skiprows : int
        跳过文件头部 N 行（非表头行，如合并单元格标题）。
    nrows : int
        最多读取的数据行数（不含表头）。
    """
    try:
        import pandas as pd
    except ImportError:
        raise ImportError("Excel 读取需要安装: pip install pandas openpyxl")

    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Excel 文件不存在: {path}")

    logger.debug(f"[file] 读取 Excel: {path} sheet={sheet}")

    kwargs: ReadOptions = {
        "sheet_name": sheet,
        "skiprows": skiprows,
        "dtype": dtype or {},
    }
    if usecols:
        kwargs["usecols"] = usecols
    if nrows is not None:
        kwargs["nrows"] = nrows

    df = pd.read_excel(p, **kwargs)

    if drop_empty_rows:
        df = df.dropna(how="all")

    # 列名映射
    if col_map:
        df = df.rename(columns=col_map)

    # 去除列名和字符串值的首尾空白
    df.columns = [str(c).strip() for c in df.columns]
    for col in df.select_dtypes(include="object").columns:
        df[col] = df[col].astype(str).str.strip().replace("nan", "")

    records = df.to_dict(orient="records")
    logger.info(f"[file] Excel 读取完成: {len(records)} 行")
    return records


def read_excel_sheets(path: str) -> dict[str, list[dict[str, Any]]]:
    """
    读取 Excel 所有 Sheet，返回 {sheet_name: list[dict]}。
    适合多 Sheet 的配置表文件。
    """
    try:
        import pandas as pd
    except ImportError:
        raise ImportError("Excel 读取需要安装: pip install pandas openpyxl")

    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Excel 文件不存在: {path}")

    xl = pd.ExcelFile(p)
    result: dict[str, list[dict[str, Any]]] = {}
    for sheet in xl.sheet_names:
        df = xl.parse(sheet).dropna(how="all")
        result[sheet] = df.to_dict(orient="records")
    logger.info(f"[file] Excel 多 Sheet 读取: {list(result.keys())}")
    return result


# ─────────────────────────────────────────────
# CSV 读取
# ─────────────────────────────────────────────

def read_csv(
    path: str,
    *,
    encoding: str = "utf-8-sig",    # utf-8-sig 自动去除 BOM
    delimiter: str = ",",
    col_map: dict[str, str] | None = None,
    usecols: list[str] | None = None,
    drop_empty_rows: bool = True,
    skip_header_rows: int = 0,
) -> list[dict[str, Any]]:
    """
    读取 CSV 文件，返回 list[dict]。

    Parameters
    ----------
    encoding : str
        文件编码，默认 utf-8-sig（自动去除 BOM，兼容 Excel 导出的 CSV）。
    delimiter : str
        分隔符，默认逗号。
    col_map : dict
        列名映射。
    usecols : list[str]
        只保留这些列（映射后的列名）。
    skip_header_rows : int
        跳过表头前的说明行数。
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"CSV 文件不存在: {path}")

    logger.debug(f"[file] 读取 CSV: {path}")

    rows: list[dict[str, Any]] = []
    with open(p, encoding=encoding, newline="") as f:
        # 跳过说明行
        for _ in range(skip_header_rows):
            next(f)
        reader = csv.DictReader(f, delimiter=delimiter)
        for row in reader:
            # 去首尾空白
            cleaned = {k.strip(): v.strip() for k, v in row.items() if k}
            if drop_empty_rows and all(not v for v in cleaned.values()):
                continue
            rows.append(cleaned)

    # 列名映射
    if col_map:
        rows = [
            {col_map.get(k, k): v for k, v in r.items()}
            for r in rows
        ]

    # 过滤列
    if usecols:
        cols_set = set(usecols)
        rows = [{k: v for k, v in r.items() if k in cols_set} for r in rows]

    logger.info(f"[file] CSV 读取完成: {len(rows)} 行")
    return rows


# ─────────────────────────────────────────────
# JSON 读取
# ─────────────────────────────────────────────

def read_json(
    path: str,
    *,
    encoding: str = "utf-8",
    root_key: str = "",
) -> list[dict[str, Any]]:
    """
    读取 JSON 文件，返回 list[dict]。

    Parameters
    ----------
    root_key : str
        若 JSON 顶层是 dict，指定哪个 key 下是列表，
        如 {"data": [...], "total": 100} 时传 root_key="data"。
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"JSON 文件不存在: {path}")

    logger.debug(f"[file] 读取 JSON: {path}")

    with open(p, encoding=encoding) as f:
        data = json.load(f)

    if root_key:
        if not isinstance(data, dict) or root_key not in data:
            raise KeyError(f"JSON 文件中不存在 key '{root_key}'")
        data = data[root_key]

    if not isinstance(data, list):
        raise TypeError(f"期望 JSON 顶层为 list，实际为 {type(data).__name__}")

    logger.info(f"[file] JSON 读取完成: {len(data)} 条")
    return data


# ─────────────────────────────────────────────
# 通用路由函数
# ─────────────────────────────────────────────

def read_file(
    path: str,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """
    根据文件扩展名自动路由到对应读取函数。

    支持 .xlsx .xls .csv .json
    额外参数透传给对应的读取函数。

    示例：
        rows = read_file("orders.xlsx", sheet="待处理", col_map={"客户名": "customer"})
        rows = read_file("config.csv", delimiter=";")
        rows = read_file("result.json", root_key="items")
    """
    suffix = Path(path).suffix.lower()
    match suffix:
        case ".xlsx" | ".xls":
            return read_excel(path, **kwargs)
        case ".csv":
            return read_csv(path, **kwargs)
        case ".json":
            return read_json(path, **kwargs)
        case _:
            raise ValueError(f"不支持的文件格式: {suffix}，支持 .xlsx .xls .csv .json")

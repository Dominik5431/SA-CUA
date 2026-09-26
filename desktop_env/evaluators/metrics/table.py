import functools
import itertools
import logging
import os.path

from numbers import Number
from typing import Any, Union, cast, Callable, Iterable
from typing import Dict, List, Tuple, Set

import openpyxl
import pandas as pd
from openpyxl import Workbook
from openpyxl.cell.cell import Cell
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.cell_range import MultiCellRange
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.worksheet import Worksheet
from rapidfuzz import fuzz

from .utils import (
    _match_value_to_rule,
    _read_cell_style,
    read_cell_value,
)
from .utils import (
    load_charts,
    load_sparklines,
    load_rows_or_cols,
    load_xlsx_styles,
    load_filters,
    load_pivot_tables,
)


logger = logging.getLogger("desktopenv.metric.table")

BOOK = Union[pd.ExcelFile, Workbook, str]


def _parse_sheet_idx(
    sheet_idx: Union[int, str],
    result: BOOK,
    expected: BOOK,
    result_sheet_names: List[str],
    expected_sheet_names: List[str],
) -> Tuple[BOOK, str]:
    if isinstance(sheet_idx, int):
        try:
            if not result_sheet_names or sheet_idx >= len(result_sheet_names):
                logger.error(
                    f"Sheet index {sheet_idx} out of range. Available sheets: {result_sheet_names}"
                )
                index = ""
            else:
                index: str = result_sheet_names[sheet_idx]
                logger.debug(f"Sheet index {sheet_idx} resolved to sheet: {index}")
        except Exception as e:
            logger.error(f"Error resolving sheet index {sheet_idx}: {e}")
            index = ""
        book: BOOK = result
    elif sheet_idx.startswith("RI"):
        try:
            index: str = result_sheet_names[int(sheet_idx[2:])]
        except:
            index = ""
        book: BOOK = result
    elif sheet_idx.startswith("RN"):
        index: str = sheet_idx[2:]
        book: BOOK = result
    elif sheet_idx.startswith("EI"):
        try:
            index: str = expected_sheet_names[int(sheet_idx[2:])]
        except:
            index = ""
        book: BOOK = expected
    elif sheet_idx.startswith("EN"):
        index: str = sheet_idx[2:]
        book: BOOK = expected
    else:
        logger.error("Unrecognized sheet index")
        raise ValueError("Unrecognized sheet index")
    return book, index


SHEET = Union[pd.DataFrame, Worksheet, List[str]]


def _load_sheet(book: BOOK, index: str) -> SHEET:
    try:
        if isinstance(book, str):
            book: str = cast(str, book)
            csv_name: str = "{:}-{:}.csv".format(os.path.splitext(book)[0], index)

            try:
                all_lines: List[str] = _safe_read_file(csv_name)
                csv_lines: List[str] = list(
                    itertools.dropwhile(
                        lambda l: len(l) == 0,
                        map(lambda l: l.strip(), reversed(all_lines)),
                    )
                )
                return csv_lines
            except (FileNotFoundError, IOError) as e:
                logger.error(f"Failed to read CSV file {csv_name}: {e}")
                return None
        if isinstance(book, pd.ExcelFile):
            return pd.read_excel(book, index)
        if isinstance(book, Workbook):
            return book[index]
        logger.error("Not supported workbook format")
        raise NotImplementedError("Not supported workbook format")
    except NotImplementedError as e:
        raise e
    except:
        return None


def _safe_read_file(file_path: str) -> List[str]:
    """
    Safely read a file with multiple encoding attempts.

    Args:
        file_path: Path to the file to read

    Returns:
        List of lines from the file

    Raises:
        FileNotFoundError: If file doesn't exist
        IOError: If file cannot be read with any encoding
    """
    encodings = [
        "utf-8",
        "utf-8-sig",
        "latin-1",
        "windows-1252",
        "gbk",
        "cp1251",
        "iso-8859-1",
    ]

    last_error = None

    for encoding in encodings:
        try:
            with open(file_path, "r", encoding=encoding) as f:
                lines = f.read().splitlines()
                logger.debug(
                    f"Successfully read file {file_path} with encoding {encoding}"
                )
                return lines
        except UnicodeDecodeError as e:
            last_error = e
            logger.debug(f"Failed to read {file_path} with encoding {encoding}: {e}")
            continue
        except (FileNotFoundError, IOError) as e:
            raise e

    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
            logger.warning(f"Read file {file_path} with UTF-8 and error replacement")
            return lines
    except Exception as e:
        logger.error(
            f"Failed to read file {file_path} with any encoding. Last error: {last_error}"
        )
        raise IOError(
            f"Cannot read file {file_path} with any supported encoding"
        ) from last_error


def compare_csv(result: str, expected: Union[str, List[str]], **options) -> float:
    """
    Compare CSV files. If expected is a list, returns 1.0 if result matches any of the expected files.

    Args:
        result: Path to result CSV file
        expected: Path to expected CSV file or list of paths to expected CSV files
        options: Additional options (strict, ignore_case)

    Returns:
        1.0 if result matches expected (or any file in expected list), 0.0 otherwise
    """
    if result is None:
        return 0.0

    try:
        result_lines: List[str] = _safe_read_file(result)
    except (FileNotFoundError, IOError) as e:
        logger.error(f"Failed to read result file {result}: {e}")
        return 0.0

    if isinstance(expected, str):
        expected_files = [expected]
    else:
        expected_files = expected

    for expected_file in expected_files:
        try:
            expected_lines: List[str] = _safe_read_file(expected_file)

            current_result_lines = result_lines
            current_expected_lines = expected_lines

            if not options.get("strict", True):
                current_result_lines = map(str.strip, current_result_lines)
                current_expected_lines = map(str.strip, current_expected_lines)
            if options.get("ignore_case", False):
                current_result_lines = map(str.lower, current_result_lines)
                current_expected_lines = map(str.lower, current_expected_lines)

            if list(current_result_lines) == list(current_expected_lines):
                return 1.0

        except (FileNotFoundError, IOError):
            continue

    return 0.0


def compare_table(result: str, expected: str = None, **options) -> float:
    """
    Args:
        result (str): path to result xlsx
        expected (str): path to golden xlsx
        rules (List[Dict[str, Any]]): list of dict like
          {
            "type": str,
            <str as parameters>: anything
          }
          as sequential rules

    Returns:
        float: the score
    """

    if result is None:
        logger.error("Result file path is None")
        return 0.0

    if not os.path.exists(result):
        logger.error(f"Result file not found: {result}")
        return 0.0

    try:
        logger.info(f"Loading result file: {result}")
        xlworkbookr: Workbook = openpyxl.load_workbook(filename=result)
        pdworkbookr = pd.ExcelFile(result)
        logger.info(
            f"Successfully loaded result file with sheets: {pdworkbookr.sheet_names}"
        )
    except Exception as e:
        logger.error(f"Failed to load result file {result}: {e}")
        return 0.0
    worksheetr_names: List[str] = pdworkbookr.sheet_names

    if expected is not None:
        xlworkbooke: Workbook = openpyxl.load_workbook(filename=expected)
        pdworkbooke = pd.ExcelFile(expected)
        worksheete_names: List[str] = pdworkbooke.sheet_names
    else:
        xlworkbooke: Workbook = None
        pdworkbooke = None
        worksheete_names: List[str] = None

    parse_idx: Callable[[Union[str, int], BOOK, BOOK], Tuple[BOOK, str]] = (
        functools.partial(
            _parse_sheet_idx,
            result_sheet_names=worksheetr_names,
            expected_sheet_names=worksheete_names,
        )
    )

    passes = True
    for r in options["rules"]:
        if r["type"] == "sheet_name":
            metric: bool = worksheetr_names == worksheete_names
            logger.debug(
                "Assertion: %s.sheet_names == %s.sheet_names - %s",
                result,
                expected,
                metric,
            )

        elif r["type"] == "sheet_data":

            error_limit: int = r.get("precision", 4)
            sheet1: pd.DataFrame = _load_sheet(
                *parse_idx(r["sheet_idx0"], pdworkbookr, pdworkbooke)
            )
            if sheet1 is None:
                return 0.0
            sheet2: pd.DataFrame = _load_sheet(
                *parse_idx(r["sheet_idx1"], pdworkbookr, pdworkbooke)
            )

            sheet1 = sheet1.round(error_limit)
            sheet2 = sheet2.round(error_limit)
            metric: bool = sheet1.equals(sheet2)
            logger.debug("Sheet1: \n%s", str(sheet1))
            logger.debug("Sheet2: \n%s", str(sheet2))
            try:
                logger.debug("Sheet1 =v= Sheet2: \n%s", str(sheet1 == sheet2))
            except:
                logger.debug("Sheet1 =/v= Sheet2")
            logger.debug(
                "Assertion: %s =v= %s - %s", r["sheet_idx0"], r["sheet_idx1"], metric
            )

        elif r["type"] == "sheet_print":

            sheet1: List[str] = _load_sheet(
                *parse_idx(r["sheet_idx0"], result, expected)
            )
            if sheet1 is None:
                return 0.0
            sheet2: List[str] = _load_sheet(
                *parse_idx(r["sheet_idx1"], result, expected)
            )
            if r.get("ignore_case", False):
                sheet1 = [l.lower() for l in sheet1]
                sheet2 = [l.lower() for l in sheet2]
            metric: bool = sheet1 == sheet2
            logger.debug(
                "Assertion: %s =p= %s - %s", r["sheet_idx0"], r["sheet_idx1"], metric
            )

        elif r["type"] == "sheet_fuzzy":

            sheet1: Tuple[BOOK, str] = parse_idx(r["sheet_idx0"], result, expected)
            sheet2: Tuple[BOOK, str] = parse_idx(r["sheet_idx1"], result, expected)
            total_metric = True
            for rl in r["rules"]:
                for rng in MultiCellRange(rl["range"]):
                    for cdn in rng.cells:
                        coordinate: str = "{:}{:d}".format(
                            get_column_letter(cdn[1]), cdn[0]
                        )
                        value1: str = str(read_cell_value(*sheet1, coordinate))
                        value2: str = str(read_cell_value(*sheet2, coordinate))
                        logger.debug("%s: %s vs %s", cdn, value1, value2)

                        for rplc in rl.get("normalization", []):
                            value1 = value1.replace(rplc[0], rplc[1])
                            value2 = value2.replace(rplc[0], rplc[1])
                        if "trim_leadings" in rl:
                            value1 = value1.lstrip(rl["trim_leadings"])
                            value2 = value2.lstrip(rl["trim_leadings"])
                        if "trim_trailings" in rl:
                            value1 = value1.rstrip(rl["trim_trailings"])
                            value2 = value2.rstrip(rl["trim_trailings"])
                        if "ignore_chars" in rl:
                            ignore_chars: Set[str] = set(rl["ignore_chars"])
                            value1 = "".join(
                                filter(lambda ch: ch not in ignore_chars, value1)
                            )
                            value2 = "".join(
                                filter(lambda ch: ch not in ignore_chars, value2)
                            )
                        if rl.get("ignore_case", False):
                            value1 = value1.lower()
                            value2 = value2.lower()

                        if rl["type"] == "includes":
                            metric: bool = value2 in value1
                        elif rl["type"] == "included_by":
                            metric: bool = value1 in value2
                        elif rl["type"] == "fuzzy_match":
                            metric: bool = fuzz.ratio(value1, value2) >= rl.get(
                                "threshold", 85.0
                            )
                        elif rl["type"] == "exact_match":
                            metric: bool = value1 == value2
                        total_metric = total_metric and metric

            metric: bool = total_metric
            logger.debug(
                "Assertion: %s =~= %s - %s", r["sheet_idx0"], r["sheet_idx1"], metric
            )

        elif r["type"] == "sparkline":

            sparkline1: Dict[str, str] = load_sparklines(
                *parse_idx(r["sheet_idx0"], result, expected)
            )
            sparkline2: Dict[str, str] = load_sparklines(
                *parse_idx(r["sheet_idx1"], result, expected)
            )
            metric: bool = sparkline1 == sparkline2
            logger.debug(
                "Assertion: %s.sp == %.sp - %s",
                r["sheet_idx0"],
                r["sheet_idx1"],
                metric,
            )

        elif r["type"] == "chart":

            charts1: Dict[str, Any] = load_charts(
                *parse_idx(r["sheet_idx0"], xlworkbookr, xlworkbooke), **r
            )
            charts2: Dict[str, Any] = load_charts(
                *parse_idx(r["sheet_idx1"], xlworkbookr, xlworkbooke), **r
            )
            metric: bool = charts1 == charts2
            logger.debug(
                "Assertion: %s[chart] == %s[chart] - %s",
                r["sheet_idx0"],
                r["sheet_idx1"],
                metric,
            )

        elif r["type"] == "style":

            sheet_idx1: Tuple[BOOK, str] = parse_idx(
                r["sheet_idx0"], xlworkbookr, xlworkbooke
            )
            book_name1: str = parse_idx(r["sheet_idx0"], result, expected)[0]
            styles1: Dict[str, List[Any]] = load_xlsx_styles(
                *sheet_idx1, book_name1, **r
            )

            sheet_idx2: Tuple[BOOK, str] = parse_idx(
                r["sheet_idx1"], xlworkbookr, xlworkbooke
            )
            book_name2: str = parse_idx(r["sheet_idx1"], result, expected)[0]
            styles2: Dict[str, List[Any]] = load_xlsx_styles(
                *sheet_idx2, book_name2, **r
            )
            metric: bool = styles1 == styles2
            logger.debug(
                "Assertion: %s.style == %s.style - %s",
                r["sheet_idx0"],
                r["sheet_idx1"],
                metric,
            )

        elif r["type"] == "freeze":

            sheet1: Worksheet = _load_sheet(
                *parse_idx(r["sheet_idx0"], xlworkbookr, xlworkbooke)
            )
            if sheet1 is None:
                return 0.0
            sheet2: Worksheet = _load_sheet(
                *parse_idx(r["sheet_idx1"], xlworkbookr, xlworkbooke)
            )
            metric: bool = sheet1.freeze_panes == sheet2.freeze_panes
            logger.debug(
                "Assertion: %s.freeze(%s) == %s.freeze(%s) - %s",
                r["sheet_idx0"],
                sheet1.freeze_panes,
                r["sheet_idx1"],
                sheet2.freeze_panes,
                metric,
            )

        elif r["type"] == "zoom":

            sheet: Worksheet = _load_sheet(
                *parse_idx(r["sheet_idx"], xlworkbookr, xlworkbooke)
            )
            if sheet is None:
                return 0.0
            zoom_scale: Number = sheet.sheet_view.zoomScale or 100.0
            metric: bool = _match_value_to_rule(zoom_scale, r)
            logger.debug(
                "Assertion: %s.zoom(%.1f) %s %.1f - %s",
                r["sheet_idx"],
                zoom_scale,
                r["method"],
                r["ref"],
                metric,
            )

        elif r["type"] == "data_validation":

            sheet: Worksheet = _load_sheet(
                *parse_idx(r["sheet_idx"], xlworkbookr, xlworkbooke)
            )
            if sheet is None:
                return 0.0
            data_validators: List[DataValidation] = (
                sheet.data_validations.dataValidation
            )

            total_metric = len(data_validators) >= len(r["dv_props"])
            for dat_vldt in data_validators:
                metric = False
                for prpt in r["dv_props"]:
                    metric = metric or all(
                        _match_value_to_rule(getattr(dat_vldt, attrbt), mr)
                        for attrbt, mr in prpt.items()
                    )
                    if metric:
                        break
                total_metric = total_metric and metric
                if not total_metric:
                    break

            logger.debug(
                "Assertion: %s.data_validation - %s", r["sheet_idx"], total_metric
            )
            metric: bool = total_metric

        elif r["type"] == "row_props":

            rows1: Dict[str, Any] = load_rows_or_cols(
                *parse_idx(r["sheet_idx0"], xlworkbookr, xlworkbooke), obj="row", **r
            )
            rows2: Dict[str, Any] = load_rows_or_cols(
                *parse_idx(r["sheet_idx1"], xlworkbookr, xlworkbooke), obj="row", **r
            )
            logger.debug("Rows1: %s", repr(rows1))
            logger.debug("Rows2: %s", repr(rows2))
            metric: bool = rows1 == rows2
            logger.debug(
                "Assertion: %s[rows] == %s[rows] - %s",
                r["sheet_idx0"],
                r["sheet_idx1"],
                metric,
            )

        elif r["type"] == "col_props":

            cols1: Dict[str, Any] = load_rows_or_cols(
                *parse_idx(r["sheet_idx0"], xlworkbookr, xlworkbooke), obj="column", **r
            )
            cols2: Dict[str, Any] = load_rows_or_cols(
                *parse_idx(r["sheet_idx1"], xlworkbookr, xlworkbooke), obj="column", **r
            )
            metric: bool = cols1 == cols2
            logger.debug(
                "Assertion: %s[cols] == %s[cols] - %s",
                r["sheet_idx0"],
                r["sheet_idx1"],
                metric,
            )

        elif r["type"] == "filter":

            filters1: Dict[str, Any] = load_filters(
                *parse_idx(r["sheet_idx0"], xlworkbookr, xlworkbooke), **r
            )
            filters2: Dict[str, Any] = load_filters(
                *parse_idx(r["sheet_idx1"], xlworkbookr, xlworkbooke), **r
            )
            metric: bool = filters1 == filters2
            logger.debug(
                "Assertion: %s[filter] == %s[filter] - %s",
                r["sheet_idx0"],
                r["sheet_idx1"],
                metric,
            )

        elif r["type"] == "pivot_table":

            pivots1: Dict[str, Any] = load_pivot_tables(
                *parse_idx(r["sheet_idx0"], xlworkbookr, xlworkbooke), **r
            )
            pivots2: Dict[str, Any] = load_pivot_tables(
                *parse_idx(r["sheet_idx1"], xlworkbookr, xlworkbooke), **r
            )
            metric: bool = pivots1 == pivots2
            logger.debug(
                "Assertion: %s[pivot]==%s[pivot] - %s",
                r["sheet_idx0"],
                r["sheet_idx1"],
                metric,
            )

        elif r["type"] == "check_cell":

            try:
                sheet: Worksheet = _load_sheet(
                    *parse_idx(r["sheet_idx"], xlworkbookr, xlworkbooke)
                )
                if sheet is None:
                    logger.error(
                        f"Failed to load sheet for sheet_idx: {r['sheet_idx']}"
                    )
                    return 0.0
                cell: Cell = sheet[r["coordinate"]]
                metric: bool = True
                for prpt, rule in r["props"].items():
                    if prpt == "value":
                        try:
                            parsed_result = parse_idx(r["sheet_idx"], result, expected)
                            logger.debug(f"parse_idx result: {parsed_result}")
                            val = read_cell_value(*parsed_result, r["coordinate"])
                            logger.debug(f"Cell {r['coordinate']} value: {val}")
                        except Exception as e:
                            logger.error(
                                f"Failed to read cell value at {r['coordinate']}: {e}"
                            )
                            val = None
                    else:
                        try:
                            val = _read_cell_style(prpt, cell)
                        except Exception as e:
                            logger.error(
                                f"Failed to read cell style {prpt} at {r['coordinate']}: {e}"
                            )
                            val = None

                    metric = metric and _match_value_to_rule(val, rule)
            except Exception as e:
                logger.error(f"Error in check_cell processing: {e}")
                return 0.0

            logger.debug(
                "Assertion: %s[%s] :%s - %s",
                r["sheet_idx"],
                r["coordinate"],
                repr(r["props"]),
                metric,
            )

        else:
            raise NotImplementedError(
                "Unimplemented sheet check: {:}".format(r["type"])
            )

        passes = passes and metric
        if not passes:
            break

    return float(passes)


def compare_conference_city_in_order(actual_city_list_path, expected_city):
    expected_city_list = expected_city["expected"]
    wb = openpyxl.load_workbook(actual_city_list_path)
    sheet = wb.active
    actual_city_list = []
    for row in sheet["C2:C22"]:
        for cell in row:
            actual_city_list.append(cell.value)
    try:
        for i in range(len(actual_city_list)):
            if isinstance(expected_city_list[i], str):
                if expected_city_list[i] not in actual_city_list[i]:
                    logger.debug(
                        f"Expected city {expected_city_list[i]}; Actual city {actual_city_list[i]}"
                    )
                    print(
                        f"Expected city {expected_city_list[i]}; Actual city {actual_city_list[i]}"
                    )
                    return 0.0

            elif isinstance(expected_city_list[i], List):
                if not any(
                    possible_str in actual_city_list[i]
                    for possible_str in expected_city_list[i]
                ):
                    logger.debug(
                        f"Expected city {expected_city_list[i]}; Actual city {actual_city_list[i]}"
                    )
                    print(
                        f"Expected city {expected_city_list[i]}; Actual city {actual_city_list[i]}"
                    )
                    return 0.0

            else:
                raise TypeError("Expected city should be a string or a list of strings")

    except:
        return 0.0

    return 1.0

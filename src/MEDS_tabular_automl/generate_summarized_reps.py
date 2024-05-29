from collections.abc import Callable

import pandas as pd
from scipy.sparse import vstack

pd.set_option("compute.use_numba", True)
import polars as pl
from loguru import logger
from scipy.sparse import coo_matrix, csr_matrix
from tqdm import tqdm

from MEDS_tabular_automl.generate_ts_features import get_ts_columns

CODE_AGGREGATIONS = [
    "code/count",
]

VALUE_AGGREGATIONS = [
    "value/count",
    "value/has_values_count",
    "value/sum",
    "value/sum_sqd",
    "value/min",
    "value/max",
]

VALID_AGGREGATIONS = CODE_AGGREGATIONS + VALUE_AGGREGATIONS


def time_aggd_col_alias_fntr(window_size: str, agg: str) -> Callable[[str], str]:
    if agg is None:
        raise ValueError("Aggregation type 'agg' must be provided")

    def f(c: str) -> str:
        if c in ["patient_id", "timestamp"]:
            return c
        else:
            return "/".join([window_size] + c.split("/") + [agg])

    return f


# def sparse_groupby_sum(df):
#     id_cols = ["patient_id", "timestamp"]
#     ohe = OneHotEncoder(sparse_output=True)
#     # Get all other columns we are not grouping by
#     other_columns = [col for col in df.columns if col not in id_cols]
#     # Get a 607875 x nDistinctIDs matrix in sparse row format with exactly
#     # 1 nonzero entry per row
#     onehot = ohe.fit_transform(df[id_cols].values.reshape(-1, 1))
#     # Transpose it. then convert from sparse column back to sparse row, as
#     # dot products of two sparse row matrices are faster than sparse col with
#     # sparse row
#     onehot = onehot.T.tocsr()
#     # Dot the transposed matrix with the other columns of the df, converted to sparse row
#     # format, then convert the resulting matrix back into a sparse
#     # dataframe with the same column names
#     out = pd.DataFrame.sparse.from_spmatrix(
#         onehot.dot(df[other_columns].sparse.to_coo().tocsr()),
#         columns=other_columns)
#     # Add in the groupby column to this resulting dataframe with the proper class labels
#     out[groupby] = ohe.categories_[0]
#     # This final groupby sum simply ensures the result is in the format you would expect
#     # for a regular pandas groupby and sum, but you can just return out if this is going to be
#     # a performance penalty. Note in that case that the groupby column may have changed index
#     return out.groupby(groupby).sum()


def sparse_rolling(df, sparse_matrix, timedelta, agg):
    """Iterates through rolling windows while maintaining sparsity.

    Example:

    >>> df = pd.DataFrame({'patient_id': {0: 1, 1: 1, 2: 1},
    ...  'timestamp': {0: pd.Timestamp('2021-01-01 00:00:00'),
    ...  1: pd.Timestamp('2021-01-01 00:00:00'), 2: pd.Timestamp('2020-01-01 00:00:00')},
    ...  'A/code': {0: 1, 1: 1, 2: 0}, 'B/code': {0: 0, 1: 0, 2: 1}, 'C/code': {0: 0, 1: 0, 2: 0}})
    >>> for col in ["A/code", "B/code", "C/code"]: df[col] = pd.arrays.SparseArray(df[col])
    >>> sparse_rolling(df, pd.Timedelta("1d"), "sum").dtypes
    A/code       Sparse[int64, 0]
    B/code       Sparse[int64, 0]
    C/code       Sparse[int64, 0]
    timestamp      datetime64[ns]
    dtype: object
    """
    patient_id = df.iloc[0].patient_id
    df = df.drop(columns="patient_id").reset_index(drop=True).reset_index()
    timestamps = []
    logger.info("rolling for patient_id")
    out_sparse_matrix = coo_matrix((0, sparse_matrix.shape[1]), dtype=sparse_matrix.dtype)
    for each in df[["index", "timestamp"]].rolling(on="timestamp", window=timedelta):
        subset_matrix = sparse_matrix[each["index"]]

        # TODO this is where we would apply the aggregation
        timestamps.append(each.index.max())
        agg_subset_matrix = subset_matrix.sum(axis=0)
        out_sparse_matrix = vstack([out_sparse_matrix, agg_subset_matrix])
    out_df = pd.DataFrame({"patient_id": [patient_id] * len(timestamps), "timestamp": timestamps})
    # out_df = pd.concat([out_df, pd.DataFrame.sparse.from_spmatrix(out_sparse_matrix)], axis=1)
    # out_df.columns = df.columns[1:]
    return out_df, out_sparse_matrix


def compute_agg(df, window_size: str, agg: str):
    """Applies aggreagtion to dataframe.

    Dataframe is expected to only have the relevant columns for aggregating
    It should have the patient_id and timestamp columns, and then only code columns
    if agg is a code aggregation or only value columns if it is a value aggreagation.

    Example:
    >>> from MEDS_tabular_automl.generate_ts_features import get_flat_ts_rep
    >>> feature_columns = ['A/value/sum', 'A/code/count', 'B/value/sum', 'B/code/count',
    ...                    "C/value/sum", "C/code/count", "A/static/present"]
    >>> data = {'patient_id': [1, 1, 1, 2, 2, 2],
    ...         'code': ['A', 'A', 'B', 'B', 'C', 'C'],
    ...         'timestamp': ['2021-01-01', '2021-01-01', '2020-01-01', '2021-01-04', None, None],
    ...         'numerical_value': [1, 2, 2, 2, 3, 4]}
    >>> df = pl.DataFrame(data).lazy()
    >>> df = get_flat_ts_rep(feature_columns, df)
    >>> df
       patient_id   timestamp  A/value  B/value  C/value  A/code  B/code  C/code
    0           1  2021-01-01        1        0        0       1       0       0
    1           1  2021-01-01        2        0        0       1       0       0
    2           1  2020-01-01        0        2        0       0       1       0
    3           2  2021-01-04        0        2        0       0       1       0
    >>> df['timestamp'] = pd.to_datetime(df['timestamp'])
    >>> df.dtypes
    patient_id               int64
    timestamp       datetime64[ns]
    A/value       Sparse[int64, 0]
    B/value       Sparse[int64, 0]
    C/value       Sparse[int64, 0]
    A/code        Sparse[int64, 0]
    B/code        Sparse[int64, 0]
    C/code        Sparse[int64, 0]
    dtype: object
    >>> output = compute_agg(df[['patient_id', 'timestamp', 'A/code', 'B/code', 'C/code']],
    ...  "1d", "code/count")
    >>> output
       1d/A/code/count  1d/B/code/count  1d/C/code/count  timestamp  patient_id
    0                1                0                0 2021-01-01           1
    1                2                0                0 2021-01-01           1
    2                0                1                0 2020-01-01           1
    0                0                1                0 2021-01-04           2
    >>> output.dtypes
    1d/A/code/count    Sparse[int64, 0]
    1d/B/code/count    Sparse[int64, 0]
    1d/C/code/count    Sparse[int64, 0]
    timestamp            datetime64[ns]
    patient_id                    int64
    dtype: object
    """
    if window_size == "full":
        timedelta = df["timestamp"].max() - df["timestamp"].min() + pd.Timedelta(days=1)
    else:
        timedelta = pd.Timedelta(window_size)
    logger.info("grouping by patient_id")
    group = dict(list(df[["patient_id", "timestamp"]].groupby("patient_id")))
    sparse_matrix = df[df.columns[2:]].sparse.to_coo()
    sparse_matrix = csr_matrix(sparse_matrix)
    logger.info("done grouping")
    out_sparse_matrix = coo_matrix((0, sparse_matrix.shape[1]), dtype=sparse_matrix.dtype)
    match agg:
        case "code/count" | "value/sum":
            agg = "sum"
            out_dfs = []
            for patient_id, subset_df in tqdm(group.items(), total=len(group)):
                logger.info("sparse rolling setup")
                subset_sparse_matrix = sparse_matrix[subset_df.index]
                patient_df = subset_df[
                    ["patient_id", "timestamp"]
                ]  # pd.concat([subset_df[["patient_id", "timestamp"]], sparse_df], axis=1)
                assert patient_df.timestamp.isnull().sum() == 0, "timestamp cannot be null"
                logger.info("sparse rolling start")
                patient_df, out_sparse = sparse_rolling(patient_df, subset_sparse_matrix, timedelta, agg)
                logger.info("sparse rolling complete")
                # patient_df["patient_id"] = patient_id
                out_dfs.append(patient_df)
                out_sparse_matrix = vstack([out_sparse_matrix, out_sparse])
            out_df = pd.concat(out_dfs, axis=0)
            out_df = pd.concat(
                [out_df.reset_index(drop=True), pd.DataFrame.sparse.from_spmatrix(out_sparse_matrix)], axis=1
            )
            out_df.columns = df.columns
            out_df.rename(columns=time_aggd_col_alias_fntr(window_size, "count"))

        case _:
            raise ValueError(f"Invalid aggregation `{agg}` for window_size `{window_size}`")

    id_cols = ["patient_id", "timestamp"]
    out_df = out_df.loc[:, id_cols + list(df.columns[2:])]
    return out_df


def _generate_summary(df: pd.DataFrame, window_size: str, agg: str) -> pl.LazyFrame:
    """Generate a summary of the data frame for a given window size and aggregation.

    Args:
    - df (DF_T): The data frame to summarize.
    - window_size (str): The window size to use for the summary.
    - agg (str): The aggregation to apply to the data frame.

    Returns:
    - pl.LazyFrame: The summarized data frame.

    Expect:
        >>> from MEDS_tabular_automl.generate_ts_features import get_flat_ts_rep
        >>> feature_columns = ['A/value/sum', 'A/code/count', 'B/value/sum', 'B/code/count',
        ...                    "C/value/sum", "C/code/count", "A/static/present"]
        >>> data = {'patient_id': [1, 1, 1, 2, 2, 2],
        ...         'code': ['A', 'A', 'B', 'B', 'C', 'C'],
        ...         'timestamp': ['2021-01-01', '2021-01-01', '2020-01-01', '2021-01-04', None, None],
        ...         'numerical_value': [1, 2, 2, 2, 3, 4]}
        >>> df = pl.DataFrame(data).lazy()
        >>> pivot_df = get_flat_ts_rep(feature_columns, df)
        >>> pivot_df['timestamp'] = pd.to_datetime(pivot_df['timestamp'])
        >>> pivot_df
           patient_id  timestamp  A/value  B/value  C/value  A/code  B/code  C/code
        0           1 2021-01-01        1        0        0       1       0       0
        1           1 2021-01-01        2        0        0       1       0       0
        2           1 2020-01-01        0        2        0       0       1       0
        3           2 2021-01-04        0        2        0       0       1       0
        >>> _generate_summary(pivot_df, "full", "value/sum")
           full/A/value/count  full/B/value/count  full/C/value/count  timestamp  patient_id
        0                   1                   0                   0 2021-01-01           1
        1                   3                   0                   0 2021-01-01           1
        2                   3                   2                   0 2021-01-01           1
        0                   0                   2                   0 2021-01-04           2
    """
    if agg not in VALID_AGGREGATIONS:
        raise ValueError(f"Invalid aggregation: {agg}. Valid options are: {VALID_AGGREGATIONS}")
    code_cols = [c for c in df.columns if c.endswith("code")]
    value_cols = [c for c in df.columns if c.endswith("value")]
    if agg in CODE_AGGREGATIONS:
        cols = code_cols
    else:
        cols = value_cols
    id_cols = ["patient_id", "timestamp"]
    df = df.loc[:, id_cols + cols]
    out_df = compute_agg(df, window_size, agg)
    return out_df


def generate_summary(
    feature_columns: list[str], df: pd.DataFrame, window_sizes: list[str], aggregations: list[str]
) -> pl.LazyFrame:
    """Generate a summary of the data frame for given window sizes and aggregations.

    This function processes a dataframe to apply specified aggregations over defined window sizes.
    It then joins the resulting frames on 'patient_id' and 'timestamp', and ensures all specified
    feature columns exist in the final output, adding missing ones with default values.

    Args:
        feature_columns (list[str]): List of all feature columns that must exist in the final output.
        df (list[pl.LazyFrame]): The input dataframes to process, expected to be length 2 list with code_df
            (pivoted shard with binary presence of codes) and value_df (pivoted shard with numerical values
            for each code).
        window_sizes (list[str]): List of window sizes to apply for summarization.
        aggregations (list[str]): List of aggregations to perform within each window size.

    Returns:
        pl.LazyFrame: A LazyFrame containing the summarized data with all required features present.

    Expect:
        >>> from datetime import date
        >>> wide_df = pd.DataFrame({"patient_id": [1, 1, 1, 2],
        ...     "A/code": [1, 1, 0, 0],
        ...     "B/code": [0, 0, 1, 1],
        ...     "A/value": [1, 2, 3, None],
        ...     "B/value": [None, None, None, 4.0],
        ...     "timestamp": [date(2021, 1, 1), date(2021, 1, 1),date(2020, 1, 3), date(2021, 1, 4)],
        ...     })
        >>> wide_df['timestamp'] = pd.to_datetime(wide_df['timestamp'])
        >>> for col in ["A/code", "B/code", "A/value", "B/value"]:
        ...     wide_df[col] = pd.arrays.SparseArray(wide_df[col])
        >>> feature_columns = ["A/code/count", "B/code/count", "A/value/sum", "B/value/sum"]
        >>> aggregations = ["code/count", "value/sum"]
        >>> window_sizes = ["full", "1d"]
        >>> generate_summary(feature_columns, wide_df, window_sizes, aggregations)[
        ...    ["1d/A/code/count", "full/B/code/count", "full/B/value/sum"]]
           1d/A/code/count  full/B/code/count  full/B/value/sum
        0              NaN                1.0                 0
        1              NaN                1.0                 0
        2              NaN                1.0                 0
        0              NaN                1.0                 0
        0              NaN                NaN                 0
        1              NaN                NaN                 0
        2              NaN                NaN                 0
        0              NaN                NaN                 0
        0                0                NaN                 0
        1              1.0                NaN                 0
        2              2.0                NaN                 0
        0                0                NaN                 0
        0              NaN                NaN                 0
        1              NaN                NaN                 0
        2              NaN                NaN                 0
        0              NaN                NaN                 0
    """
    logger.info("Sorting sparse dataframe by patient_id and timestamp")
    df = df.sort_values(["patient_id", "timestamp"]).reset_index(drop=True)
    assert len(feature_columns), "feature_columns must be a non-empty list"
    ts_columns = get_ts_columns(feature_columns)
    code_value_ts_columns = [f"{c}/code" for c in ts_columns] + [f"{c}/value" for c in ts_columns]
    final_columns = []
    out_dfs = []
    # Generate summaries for each window size and aggregation
    for window_size in window_sizes:
        for agg in aggregations:
            code_type, agg_name = agg.split("/")
            final_columns.extend(
                [f"{window_size}/{c}/{agg_name}" for c in code_value_ts_columns if c.endswith(code_type)]
            )
            # only iterate through code_types that exist in the dataframe columns
            if any([c.endswith(code_type) for c in df.columns]):
                logger.info(f"Generating aggregation {agg} for window_size {window_size}")
                # timestamp_dtype = df.dtypes[df.columns.index("timestamp")]
                # assert timestamp_dtype in [
                #     pl.Datetime,
                #     pl.Date,
                # ], f"timestamp must be of type Date, but is {timestamp_dtype}"
                out_df = _generate_summary(df, window_size, agg)
                out_dfs.append(out_df)

    final_columns = sorted(final_columns)
    # Combine all dataframes using successive joins
    result_df = pd.concat(out_dfs)
    # Add in missing feature columns with default values
    missing_columns = [col for col in final_columns if col not in result_df.columns]

    result_df[missing_columns] = pd.DataFrame.sparse.from_spmatrix(
        coo_matrix((result_df.shape[0], len(missing_columns)))
    )
    result_df = result_df[["patient_id", "timestamp"] + final_columns]
    return result_df

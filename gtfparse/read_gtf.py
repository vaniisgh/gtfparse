# Copyright (c) 2015-2018. Mount Sinai School of Medicine
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import print_function, division, absolute_import
import logging
from os.path import exists

from six import string_types
from six.moves import intern
import numpy as np
import pandas as pd

from .util import memory_usage
from .attribute_parsing import expand_attribute_strings
from .parsing_error import ParsingError
from .required_columns import REQUIRED_COLUMNS

def parse_gtf(filepath_or_buffer, chunksize=1024 * 1024):
    """
    Parameters
    ----------

    filepath_or_buffer : str or buffer object

    chunksize : int
    """

    logging.debug("Memory usage before GTF parsing: %0.4f MB" % memory_usage())
    dataframes = []

    def intern_str(s):
        return intern(str(s))

    def intern_frame(s):
        if s == ".":
            return 0
        else:
            return int(s)

    def fix_attribute_quotes(attr):
        # Catch mistaken semicolons by replacing "xyz;" with "xyz"
        # Required to do this since the Ensembl GTF for Ensembl release 78 has
        # gene_name = "PRAMEF6;"
        # transcript_name = "PRAMEF6;-201"
        return attr.replace(';\"', '\"').replace(";-", "-")

    # GTF columns:
    # 1) seqname: str ("1", "X", "chrX", etc...)
    # 2) source : str
    #      Different versions of GTF use second column as of:
    #      (a) gene biotype
    #      (b) transcript biotype
    #      (c) the annotation source
    #      See: https://www.biostars.org/p/120306/#120321
    # 3) feature : str ("gene", "transcript", &c)
    # 4) start : int
    # 5) end : int
    # 6) score : float or "."
    # 7) strand : "+", "-", or "."
    # 8) frame : 0, 1, 2 or "."
    # 9) attribute : key-value pairs separated by semicolons
    # (see more complete description in docstring at top of file)

    chunk_iterator = pd.read_csv(
        filepath_or_buffer,
        sep="\t",
        comment="#",
        names=REQUIRED_COLUMNS,
        skipinitialspace=True,
        skip_blank_lines=True,
        error_bad_lines=True,
        warn_bad_lines=True,
        chunksize=chunksize,
        engine="c",
        dtype={
            "start": np.int64,
            "end": np.int64,
            "score": np.float32,
        },
        na_values=".",
        converters={
            "seqname": intern_str,
            "source": intern_str,
            "feature": intern_str,
            "strand": intern_str,
            "attribute": fix_attribute_quotes,
            "frame": intern_frame,
        })
    dataframes = []
    try:
        for df in chunk_iterator:
            dataframes.append(df)
    except Exception as e:
        raise ParsingError(str(e))
    logging.debug("Memory usage after GTF parsing: %0.4f MB" % memory_usage())
    df = pd.concat(dataframes)
    logging.debug("Memory usage after concatenating final result: %0.4f MB" % memory_usage())
    return df


def parse_gtf_and_expand_attributes(
        filepath_or_buffer,
        chunksize=1024 * 1024,
        restrict_attribute_columns=None):
    """
    Parse lines into column->values dictionary and then expand
    the 'attribute' column into multiple columns. This expansion happens
    by replacing strings of semi-colon separated key-value values in the
    'attribute' column with one column per distinct key, with a list of
    values for each row (using None for rows where key didn't occur).

    Parameters
    ----------
    filepath_or_buffer : str or buffer object

    chunksize : int

    restrict_attribute_columns : list/set of str or None
        If given, then only usese attribute columns.
    """
    result = parse_gtf(filepath_or_buffer, chunksize=chunksize)
    attribute_values = result["attribute"]
    del result["attribute"]
    for column_name, values in expand_attribute_strings(
            attribute_values, usecols=restrict_attribute_columns).items():
        result[column_name] = values
    return result


def read_gtf(
        filepath_or_buffer,
        expand_attribute_column=True,
        infer_biotype_column=False,
        column_converters={},
        usecols=None,
        chunksize=1024 * 1024):
    """
    Parse a GTF into a dictionary mapping column names to sequences of values.

    Parameters
    ----------
    filepath_or_buffer : str or buffer object
        Path to GTF file (may be gzip compressed) or buffer object
        such as StringIO

    expand_attribute_column : bool
        Replace strings of semi-colon separated key-value values in the
        'attribute' column with one column per distinct key, with a list of
        values for each row (using None for rows where key didn't occur).

    infer_biotype_column : bool
        Due to the annoying ambiguity of the second GTF column across multiple
        Ensembl releases, figure out if an older GTF's source column is actually
        the gene_biotype or transcript_biotype.

    column_converters : dict, optional
        Dictionary mapping column names to conversion functions. Will replace
        empty strings with None and otherwise passes them to given conversion
        function.

    usecols : list of str or None
        Restrict which columns are loaded to the give set. If None, then
        load all columns.

    chunksize : int
    """
    if isinstance(filepath_or_buffer, string_types) and not exists(filepath_or_buffer):
        raise ValueError("GTF file does not exist: %s" % filepath_or_buffer)

    if expand_attribute_column:
        result_df = parse_gtf_and_expand_attributes(
            filepath_or_buffer,
            chunksize=chunksize,
            restrict_attribute_columns=usecols)
    else:
        result_df = parse_gtf(result_df)

    if usecols is not None:
        available_columns = result_df.columns
        valid_columns = [c for c in usecols if c in available_columns]
        result_df = result_df[valid_columns]

    for column_name, column_type in list(column_converters.items()):
        result_df[column_name] = [
            column_type(string_value) if len(string_value) > 0 else None
            for string_value
            in result_df[column_name]
        ]
    # Hackishly infer whether the values in the 'source' column of this GTF
    # are actually representing a biotype by checking for the most common
    # gene_biotype and transcript_biotype value 'protein_coding'
    if infer_biotype_column and "protein_coding" in result_df["source"]:
        # Disambiguate between the two biotypes by checking if
        # gene_biotype is already present in another column. If it is,
        # the 2nd column is the transcript_biotype (otherwise, it's the
        # gene_biotype)
        column_names = set(result_df.columns)
        if "gene_biotype" not in column_names:
            result_df["gene_biotype"] = result_df["source"]
        if "transcript_biotype" not in column_names:
            result_df["transcript_biotype"] = result_df["source"]

    return result_df

#!/usr/bin/env python

### load libs
import argparse
import gzip
import logging
import pandas as pd
import numpy as np
import os
from scipy.optimize import curve_fit
from uncertainties import ufloat
import matplotlib.pylab as plt
import bisect
import random
from collections import Counter
from collections.abc import Sequence

import polars as pl


__author__ = "Swan Floc’Hlay"
__contributors__ = "Gert Hulselmans"
__version__ = "v0.2.0"


FORMAT = '%(asctime)-15s %(message)s'
logging.basicConfig(format=FORMAT, level=logging.INFO)
logger = logging.getLogger('calculate_saturation_from_fragments')


### initialise function and classes


def read_bc_and_counts_from_fragments_file(fragments_bed_filename: str) -> pl.DataFrame:
    """
    Read cell barcode (column 4) and counts per fragment (column 5) from fragments BED file.
    Cell barcodes will appear more than once as they have counts per fragment, but as
    the fragment locations are not needed, they are not returned.

    Parameters
    ----------
    fragments_bed_filename: Fragments BED filename.

    Returns
    -------
    Polars dataframe with cell barcode and count per fragment (column 4 and 5 of BED file).
    """

    bed_column_names = (
        "Chromosome", "Start", "End", "Name", "Score", "Strand", "ThickStart", "ThickEnd", "ItemRGB", "BlockCount",
        "BlockSizes", "BlockStarts"
    )

    # Set the correct open function depending if the fragments BED file is gzip compressed or not.
    open_fn = gzip.open if fragments_bed_filename.endswith('.gz') else open

    skip_rows = 0
    nbr_columns = 0

    with open_fn(fragments_bed_filename, 'rt') as fragments_bed_fh:
        for line in fragments_bed_fh:
            # Remove newlines and spaces.
            line = line.strip()

            if not line or line.startswith('#'):
                # Count number of empty lines and lines which start with a comment before the actual data.
                skip_rows += 1
            else:
                # Get number of columns from the first real BED entry.
                nbr_columns = len(line.split('\t'))

                # Stop reading the BED file.
                break

    if nbr_columns < 5:
        raise ValueError(
            f'Fragments BED file needs to have at least 5 columns. "{fragments_bed_filename}" contains only '
            f'{nbr_columns} columns.'
        )

    # Read cell barcode (column 4) and counts (column 5) per fragemnt from fragments BED file.
    fragments_df = pl.read_csv(
        fragments_bed_filename,
        has_headers=False,
        skip_rows=skip_rows,
        sep='\t',
        use_pyarrow=True,
        columns=["column_1", "column_2", "column_3", "column_4", "column_5"],
        new_columns=["Chromosome", "Start", "End", "CellBarcode", "FragmentCount"],
    ).with_columns(
        [
            pl.col("Chromosome").cast(pl.Categorical),
            pl.col("Start").cast(pl.Int32),
            pl.col("End").cast(pl.Int32),
            pl.col("CellBarcode").cast(pl.Categorical),
            pl.col("FragmentCount").cast(pl.Int32),
        ]
    )

    return fragments_df


def MM(x, Vmax, Km):
    """
    Define the Michaelis-Menten Kinetics model that will be used for the model fitting.
    """
    if Vmax > 0 and Km > 0:
        y = (Vmax * x) / (Km + x)
    else:
        y = 1e10
    return y


# sub-sampling function
def sub_sample_fragments(
    fragments_df,
    min_uniq_frag=200,
    n_chunk=10,
    outfile="sampling_stats.tab",
    whitelist=None,
):
    # init stats bucket
    stats_bucket = {
        "mean_frag_per_bc": {"chunk 0": 0},  # mean read per cell
        "median_uniq_frag_per_bc": {"chunk 0": 0},  # median uniq fragment per cell
        "total_frag_count": {"chunk 0": 0},  # total read count (all barcodes)
        #"total_frag_count_bc_filtered": {"chunk 0": 0},
        "cell_barcode_count": {
            "chunk 0": 0
        },  # number of barcodes with n_reads > min_uniq_frag
    }

    # Get all cell barcodes which have more than min_uniq_frag fragments.
    good_cell_barcodes = fragments_df.groupby("CellBarcode").agg(
        pl.col("FragmentCount").count().alias('nbr_frags_per_CBs')
    ).filter(
        pl.col("nbr_frags_per_CBs") > min_uniq_frag
    )

    # Count all good cell barcodes.
    nbr_good_cell_barcodes = good_cell_barcodes.height

    # Create dataframe where each row contains one fragment:
    #   - Original dataframe has a count per fragment with the same cell barcode.
    #   - Create a row for each count, so we can sample fairly afterwards.
    fragments_all_df = fragments_df.with_column(
        pl.col("FragmentCount").repeat_by(
            pl.col("FragmentCount")
        )
    ).explode("FragmentCount")

    for fraction in np.arange(0.1, 1.1, 0.1):
        chunk = "chunk " + str(int(fraction * 10))

        logger.info(f"Random sample: {fraction}")

        # Sample x% from all fragments (with duplicates) and keep fragments which have good barcodes.
        logger.info(f"Sample {fraction * 100.0}% from all fragments and keep fragments with good barcodes.")
        fragments_sampled_for_good_bc_df = good_cell_barcodes.join(
            fragments_all_df.sample(frac=fraction),
            left_on="CellBarcode",
            right_on="CellBarcode",
            how="left"
        )

        # Get number of sampled fragments (with possible duplicate fragments) which have good barcodes.
        stats_bucket["total_frag_count"][chunk] = fragments_sampled_for_good_bc_df.height

        logger.info("Calculate mean number of fragments per barcode.")
        stats_bucket["mean_frag_per_bc"][chunk] = fragments_sampled_for_good_bc_df.select(
            [pl.col('CellBarcode'), pl.col('FragmentCount')]
        ).groupby("CellBarcode").agg(
            [pl.count("FragmentCount").alias("FragmentsPerCB")]
        ).select(
            [pl.col("FragmentsPerCB").mean().alias("MeanFragmentsPerCB")]
        )["MeanFragmentsPerCB"][0]

        logger.info("Calculate median number of unique fragments per barcode.")
        stats_bucket["median_uniq_frag_per_bc"][chunk] = fragments_sampled_for_good_bc_df.groupby(
            ["CellBarcode", "Chromosome", "Start", "End"]
        ).agg(
            [pl.col("FragmentCount").first().alias("FragmentCount")]
        ).select(
            [pl.col("CellBarcode"), pl.col("FragmentCount")]
        ).groupby("CellBarcode").agg(
            pl.col("FragmentCount").count().alias("UniqueFragmentsPerCB")
        ).select(
            pl.col("UniqueFragmentsPerCB").median()
        )["UniqueFragmentsPerCB"][0]

        logger.info("Calculate median number of unique fragments per barcode finished.")

        stats_bucket["cell_barcode_count"][chunk] = nbr_good_cell_barcodes

    logger.info("Save data as tab file.")

    # Save data as tab file
    stats_bucket = pd.DataFrame(stats_bucket)
    stats_bucket.to_csv(outfile, sep="\t")
    return stats_bucket


# MM-fit function
def fit_MM(
    stat_bucket,
    percentages=[0.3, 0.6, 0.9],
    path_to_fig="./",
    x_axis="total_frag_count",
    y_axis="median_uniq_frag_per_bc",
):
    # select x/y data fro MM fit from subsampling stats
    x_data = np.array(stat_bucket.loc[:, x_axis])
    y_data = np.array(stat_bucket.loc[:, y_axis])
    # fit to MM function
    best_fit_ab, covar = curve_fit(MM, x_data, y_data, bounds=(0, +np.inf))
    # expand fit space
    x_fit = np.linspace(0, int(np.max(x_data) * 100), num=500)
    y_fit = MM(x_fit, *(best_fit_ab))
    # impute maximum saturation to plot as 95% of y_max
    y_val = best_fit_ab[0] * 0.95
    # subset x_fit space if bigger then y_val
    if y_val < max(y_fit):
        x_coef = np.where(y_fit >= y_val)[0][0]
        x_fit = x_fit[0:x_coef]
        y_fit = y_fit[0:x_coef]
    # plot model
    plt.plot(x_fit, MM(x_fit, *best_fit_ab), label="fitted", c="black", linewidth=1)
    # plot raw data
    plt.scatter(x=x_data, y=y_data, c="crimson", s=10)
    # mark curent saturation
    x_idx = np.where(y_fit >= max(y_data))[0][0]
    x_coef = x_fit[x_idx]
    y_coef = y_fit[x_idx]
    plt.plot([x_coef, x_coef], [0, y_coef], linestyle="--", c="crimson")
    plt.plot([0, x_coef], [y_coef, y_coef], linestyle="--", c="crimson")
    plt.text(
        x=x_fit[-1],
        y=y_coef,
        s=str(round(100 * max(y_data) / best_fit_ab[0]))
        + "% {:.2e}".format(round(x_coef)),
        c="crimson",
        ha="right",
        va="bottom",
    )
    # plot percentaged values
    for perc in percentages:
        # Find read count for percent saturation
        y_val = best_fit_ab[0] * perc
        # Find closest match in fit
        if max(y_fit) > y_val:
            x_idx = np.where(y_fit >= y_val)[0][0]
            x_coef = x_fit[x_idx]
            y_coef = y_fit[x_idx]
            # Draw vline
            plt.plot([x_coef, x_coef], [0, y_coef], linestyle="--", c="grey")
            # Draw hline
            plt.plot([0, x_coef], [y_coef, y_coef], linestyle="--", c="grey")
            # Plot imputed read count
            plt.text(
                x=x_fit[-1],
                y=y_coef,
                s=str(round(100 * perc)) + "% {:.2e}".format(round(x_coef)),
                c="grey",
                ha="right",
                va="bottom",
            )
    # save figure
    plt.xlabel(x_axis)
    plt.ylabel(y_axis)
    plt.savefig(path_to_fig)
    plt.close()


def main():
    parser = argparse.ArgumentParser(
        description="Infer saturation of scATAC from fragments file."
    )
    parser.add_argument(
        "-i",
        "--input",
        dest="fragments_input_bed_filename",
        action="store",
        type=str,
        required=True,
        help="Fragment input BED filename.",
    )
    parser.add_argument(
        "-o",
        "--output",
        dest="output_prefix",
        action="store",
        type=str,
        required=True,
        help="Output prefix, which will contain PNG file with saturation curve and TSV file with summary of "
        "reads and additional reads needed to reach saturation specified by percentages.",
    )
    parser.add_argument(
        "-p",
        "--percentages",
        dest="percentages",
        type=str,
        help='Comma separated list of decimal percentages to predict. Default: "0.3,0.6,0.9"',
        default="0.3,0.6,0.9",
    )
    parser.add_argument(
        "-m",
        "--min_frags_per_cb",
        dest="min_frags_per_cb",
        type=int,
        help="Minimum number of unique fragments per cell barcodes",
        default=200,
    )
    parser.add_argument(
        "-s",
        "--subsamplings",
        dest="subsamplings",
        type=int,
        help="Number of sub-samplings to perform.",
        default=10,
    )
    parser.add_argument(
        "-w",
        "--whitelist",
        dest="whitelist",
        type=str,
        help="Barcode whitelist filename.",
        default=None,
    )

    parser.add_argument("-V", "--version", action="version", version=f"{__version__}")

    args = parser.parse_args()

    percentages = [float(x) for x in args.percentages.split(",")]


    # Enable global string cache.
    pl.frame.toggle_string_cache(True)

    # Load fragments BED file.
    logger.info("Loading fragments BED file started.")
    fragments_df = read_bc_and_counts_from_fragments_file(args.fragments_input_bed_filename)
    logger.info("Loading fragments BED file finished.")

    # Sub-sample.
    stats_bucket = sub_sample_fragments(
        fragments_df,
        min_uniq_frag=args.min_frags_per_cb,
        n_chunk=args.subsamplings,
        outfile=args.output_prefix + ".sampling_stats.tsv",
        whitelist=args.whitelist,
    )

    logger.info("fit_MM.")
    # Fit'n'plot for total count.
    fit_MM(
        stats_bucket,
        percentages=percentages,
        path_to_fig=args.output_prefix + ".saturation.png",
        x_axis="total_frag_count",
        y_axis="median_uniq_frag_per_bc",
    )
    logger.info("Finished.")


if __name__ == "__main__":
    main()

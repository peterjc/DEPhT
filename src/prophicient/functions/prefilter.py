from Bio.SeqFeature import (FeatureLocation, SeqFeature)
from Bio.SeqRecord import SeqRecord

from numpy import (average, std)
from Prophicient.functions import (att, gene_density)


# GLOBAL VARIABLES
# -----------------------------------------------------------------------------
DEFAULTS = {"bin_width": gene_density.DEFAULTS["bin_width"],
            "window_size": gene_density.DEFAULTS["window_size"],
            "F": -0.5, "C": 2}


def extract_naive_prophages(record):
    gene_dense_records_and_regions = prefilter_genome(record)

    phage_regions = list()
    for region_record, region_feature in gene_dense_records_and_regions:
        sequence = str(region_record.seq)

        half = len(sequence) // 2
        l_region = sequence[:half-1]
        r_region = sequence[half+1:]

        attL_feature, attR_feature = att.find_attatchment_site(
                                                        l_region, r_region)

        if attL_feature is None or attR_feature is None:
            continue

        region_start = region_feature.location.start
        prophage_start = (region_start + attL_feature.location.start)
        prophage_end = (region_start + half + 1 + attR_feature.location.end)

        phage_regions.append((prophage_start, prophage_end))

    return phage_regions


def prefilter_genome(record, bin_width=DEFAULTS["bin_width"],
                     window_size=DEFAULTS["window_size"], F=DEFAULTS["F"],
                     C=DEFAULTS["C"]):
    gene_dense_features = get_gene_dense_regions(
                                            record, window_size=window_size,
                                            F=F, C=C)

    gene_dense_records_and_features = []
    for i in range(len(gene_dense_features)):
        gene_dense_feature = gene_dense_features[i]
        region_seq = gene_dense_feature.extract(record.seq)

        region_record = SeqRecord(region_seq)
        region_record.id = "_".join([record.id, "region", str(i)])
        for feature in record.features:
            if (feature.location.start < gene_dense_feature.location.start or
                    feature.location.end > gene_dense_feature.location.end):
                continue

            region_start = gene_dense_feature.location.start
            feature_start = (feature.location.start - region_start)
            feature_end = (feature.location.end - region_start)
            feature_location = FeatureLocation(feature_start, feature_end)

            new_feature = SeqFeature(feature_location, strand=feature.strand,
                                     type=feature.type,
                                     qualifiers=feature.qualifiers)
            region_record.features.append(new_feature)

        region_record.features.sort(key=lambda x: x.location.start)
        gene_dense_records_and_features.append((region_record,
                                                gene_dense_feature))

    return gene_dense_records_and_features


def get_gene_dense_regions(record, bin_width=DEFAULTS["bin_width"],
                           window_size=DEFAULTS["window_size"],
                           F=DEFAULTS["F"], C=DEFAULTS["C"]):
    pos_nu_map = gene_density.count_genes_per_interval(
                                                    record, interval=bin_width)

    index_rho_map = gene_density.calculate_density_map(
                                        len(record), pos_nu_map, window_size,
                                        bin_width)

    rho_data = [rho for rho in index_rho_map.values()]

    rho_avg = average(rho_data)
    rho_std = std(rho_data)
    eps = rho_avg - (rho_std * F)

    region_contigs = gene_density.compile_region_contigs(index_rho_map, eps)

    ceiling = rho_avg + (rho_std * C)

    alpha_region_contigs = list()
    for region_contig in region_contigs:
        peak = max(region_contig, key=lambda x: x[1])
        if peak[1] >= ceiling:
            region_contig.sort(key=lambda x: x[0])
            alpha_region_contigs.append(region_contig)

    gene_dense_coords = list()
    for contig in alpha_region_contigs:
        start = contig[0][0] - window_size // 2
        end = contig[-1][0] + window_size // 2

        gene_dense_coords.append((start, end))

    gene_dense_regions = list()
    for coords in gene_dense_coords:
        feature = SeqFeature(FeatureLocation(coords[0], coords[1]),
                             strand=1)

        gene_dense_regions.append(feature)

    return gene_dense_regions

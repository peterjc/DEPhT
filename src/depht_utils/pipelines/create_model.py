"""Pipeline to compile the complete set of databases and data structures
required to run DEPhT from bacterial and phage sequences provided.
"""
import argparse
import json
import pathlib
import shutil

from Bio import SeqIO

from depht.__main__ import DEPHT_DIR
from depht.functions.annotation import (annotate_record,
                                        cleanup_flatfile_records)
from depht.functions.multiprocess import CPUS, parallelize
from depht.functions.sniff_format import sniff_format
from depht_utils import PACKAGE_DIR
from depht_utils.data.defaults import (HHSUITEDB_DEFAULTS,
                                       MODEL_SCHEMA_DEFAULTS,
                                       SHELL_DB_DEFAULTS)
from depht_utils.functions import fileio
from depht_utils.pipelines.build_HMM_db import build_HMM_db
from depht_utils.pipelines.build_reference_db import build_reference_db
from depht_utils.pipelines.curate_gene_clusters import (
    curate_gene_clusters,
    DEFAULTS as CURATION_DEFAULTS)
from depht_utils.pipelines.index_sequences import index_sequences
from depht_utils.pipelines.phamerate import execute_phamerate_pipeline
from depht_utils.pipelines.screen_conserved_phams import (
    screen_conserved_phams, REP_THRESHOLD)
from depht_utils.pipelines.train_model import train_model, WINDOW

MODELS_DIR = DEPHT_DIR.joinpath("models")
DEFAULT_CONFIG = PACKAGE_DIR.joinpath("data/defaults.json")


def parse_args(unparsed_args):
    """Function to parse command line arguments for running the create_model
    pipeline.

    :param unparsed_args: List of command line arguments
    :type unparsed_args: list
    """
    parser = argparse.ArgumentParser()

    parser.add_argument("-n", "--model_name", type=str, required=True)
    parser.add_argument("-p", "--phage_sequences", type=pathlib.Path,
                        required=True)
    parser.add_argument("-b", "--bacterial_sequences", type=pathlib.Path,
                        required=True)

    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--cpus", type=int, default=CPUS)
    parser.add_argument("-c", "--config", type=pathlib.Path,
                        default=DEFAULT_CONFIG)
    parser.add_argument("-f", "--force", action="store_true")
    parser.add_argument("-a", "--auto_annotate", action="store_true")

    args = parser.parse_args(unparsed_args)
    return args


def main(unparsed_args):
    """Main function for the command-line interface of the create_model
    pipeline.
    """
    args = parse_args(unparsed_args)

    create_model(args.model_name,
                 args.phage_sequences, args.bacterial_sequences,
                 verbose=args.verbose, config_file=args.config,
                 force=args.force, annotate=args.auto_annotate, cpus=args.cpus)


def load_config(config_file):
    """Function to load the master configuration file for the create_model
    pipeline and to verify its structure/contents.
    """
    with open(config_file, "r") as filehandle:
        config = json.load(filehandle)

    # Validate phameration configuration files
    phameration_config = config.get("phameration")
    if phameration_config is None or not isinstance(phameration_config, dict):
        return None

    # Validate bacterial phameration configuration file
    bacterial_phamerate_config = phameration_config.get("bacteria")
    if bacterial_phamerate_config is None:
        return None

    bacterial_phamerate_config_path = pathlib.Path(bacterial_phamerate_config)
    if not bacterial_phamerate_config_path.is_file():
        bacterial_phamerate_config_path = PACKAGE_DIR.joinpath(
                                                   bacterial_phamerate_config)

        if not bacterial_phamerate_config_path.is_file():
            return None

    phameration_config["bacteria"] = bacterial_phamerate_config_path

    # Validate phage phameration configuration file
    phage_phamerate_config = phameration_config.get("phage")
    if phage_phamerate_config is None:
        return None

    phage_phamerate_config_path = pathlib.Path(phage_phamerate_config)
    if not phage_phamerate_config_path.is_file():
        phage_phamerate_config_path = PACKAGE_DIR.joinpath(
                                                   phage_phamerate_config)

        if not phage_phamerate_config_path.is_file():
            return None

    phameration_config["phage"] = phage_phamerate_config_path

    # Validate homolog function configuration files
    function_config = config.get("functions")
    if function_config is None or not isinstance(function_config, dict):
        return None

    # Validate phage essential homolog function file
    essential_config = function_config.get("essential")
    essential_config_path = pathlib.Path(essential_config)
    if not essential_config_path.is_file():
        essential_config_path = PACKAGE_DIR.joinpath(essential_config)

        if not essential_config_path.is_file():
            return None

    function_config["essential"] = essential_config_path

    # Validate phage extended homolog function file
    extended_config = function_config.get("extended")
    extended_config_path = pathlib.Path(extended_config)
    if not extended_config_path.is_file():
        extended_config_path = PACKAGE_DIR.joinpath(extended_config)

        if not extended_config_path.is_file():
            return None

    function_config["extended"] = extended_config_path

    # Populate model building parameters
    parameter_config = config.get("parameters", dict())

    # Poopulate classifier parameters
    classifier_parameters = parameter_config.get("classifier", dict())
    classifier_window_size = classifier_parameters.get("window", WINDOW)
    classifier_parameters["window"] = classifier_window_size
    parameter_config["classifier"] = classifier_parameters

    # Populate shell genome database parameters
    shell_db_parameters = parameter_config.get("shell", dict())
    shell_db_rep_threshold = shell_db_parameters.get("rep_threshold",
                                                     REP_THRESHOLD)
    shell_db_parameters["rep_threshold"] = shell_db_rep_threshold
    parameter_config["shell"] = shell_db_parameters

    # Populate phage homology database parameters
    homolog_db_parameters = parameter_config.get("phage_homologs", dict())
    homolog_db_min_HMM_count = shell_db_parameters.get(
                                                "min_HMM_count",
                                                CURATION_DEFAULTS["min_size"])
    homolog_db_parameters["min_HMM_count"] = homolog_db_min_HMM_count
    parameter_config["phage_homologs"] = homolog_db_parameters

    return config


def create_model(model_name, phage_sequences, bacterial_sequences,
                 verbose=False, config_file=DEFAULT_CONFIG, force=False,
                 cpus=1, annotate=False):
    # Load master configuration file, which contains paths for
    # configuration of sub-pipelines
    config = load_config(config_file)
    if config is None:
        print(f"Specified configuration file at {config_file} "
              "is incorrectly formatted or contains invalid information.\n"
              "Please check the formatting/contents of this file before "
              "continuing.")
        return

    # Proactively create local model directory structure
    dir_map = create_model_structure(model_name, force=force)
    if dir_map is None:
        print("There already exists a DEPHT model with the name "
              f"'{model_name}'.\n"
              "Please rename your model or use the -f flag to forcibly "
              "overwrite the already existing model.")
        return

    # Annotate bacterial sequences and write fasta and genbank files
    if verbose:
        print("Collecting/annotating bacterial sequences...")
    bacterial_fasta_files, bacterial_gb_files, num_bacteria = clean_sequences(
                                          bacterial_sequences,
                                          dir_map["bacterial_tmp"],
                                          annotate=True, verbose=verbose,
                                          cpus=cpus)

    if num_bacteria == 0:
        print("Could not recognize any bacterial sequence files "
              "in the specified directory.\n"
              "Please check the formatting and contents of your files.")
        shutil.rmtree(dir_map["model_dir"])
        return

    # Collect phage sequences and write fasta and genbank files
    if verbose:
        print("Collecting/annotating phage sequences...")
    phage_fasta_files, phage_gb_files, num_phages = clean_sequences(
                                                phage_sequences,
                                                dir_map["phage_tmp"],
                                                verbose=verbose,
                                                cpus=cpus)

    if num_phages == 0:
        print("Could not recognize any phage sequence files "
              "in the specified directory.\n"
              "Please check the formatting and contents of your files.")
        shutil.rmtree(dir_map["model_dir"])
        return

    if verbose:
        print("Training phage/bacterial classifier...")
    train_model(model_name, phage_gb_files, bacterial_gb_files,
                cpus=cpus)

    if verbose:
        print("\nBuilding bacterial reference database...")
    build_reference_db(bacterial_fasta_files, dir_map["reference_db_dir"])

    if verbose:
        print("\nBuilding shell genome content database...")
    create_shell_db(bacterial_gb_files, dir_map["shell_db_dir"], config,
                    dir_map["shell_db_tmp"],
                    verbose=verbose)

    if verbose:
        print("\nBuilding phage homolog profile database...")
    create_phage_homologs_db(phage_sequences, dir_map["phage_homologs_dir"],
                             config, dir_map["phage_homologs_tmp"],
                             verbose=verbose, cpus=cpus)

    shutil.rmtree(dir_map["tmp_dir"])


def create_model_structure(model_name, force=False):
    dir_map = dict()

    # Create local model directory
    model_dir = MODELS_DIR.joinpath(model_name)
    dir_map["model_dir"] = model_dir
    if model_dir.is_dir():
        if force:
            shutil.rmtree(model_dir)
        else:
            return None

    model_dir.mkdir()

    # Create temporary working directory
    tmp_dir = model_dir.joinpath("tmp")
    dir_map["tmp_dir"] = tmp_dir
    tmp_dir.mkdir()

    # Create temporary bacterial sequence directory
    bacterial_tmp = tmp_dir.joinpath("bacteria")
    dir_map["bacterial_tmp"] = bacterial_tmp
    bacterial_tmp.mkdir()

    # Create temporary phage sequence directory
    phage_tmp = tmp_dir.joinpath("phage")
    dir_map["phage_tmp"] = phage_tmp
    phage_tmp.mkdir()

    # Create model reference blast database directory
    reference_db_dir = model_dir.joinpath(
                                    MODEL_SCHEMA_DEFAULTS["reference_db"])
    dir_map["reference_db_dir"] = reference_db_dir
    reference_db_dir.mkdir()

    # Create model shell database directory and temporary directory
    shell_db_tmp = tmp_dir.joinpath(MODEL_SCHEMA_DEFAULTS["shell_db"])
    dir_map["shell_db_tmp"] = shell_db_tmp
    shell_db_tmp.mkdir()
    shell_db_dir = model_dir.joinpath(
                                    MODEL_SCHEMA_DEFAULTS["shell_db"])
    dir_map["shell_db_dir"] = shell_db_dir
    shell_db_dir.mkdir()

    # Create model phage homolog HMM profile directory and temporary directory
    phage_homologs_tmp = tmp_dir.joinpath(
                                    MODEL_SCHEMA_DEFAULTS["phage_homologs_db"])
    dir_map["phage_homologs_tmp"] = phage_homologs_tmp
    phage_homologs_tmp.mkdir()
    phage_homologs_dir = model_dir.joinpath(
                                    MODEL_SCHEMA_DEFAULTS["phage_homologs_db"])
    dir_map["phage_homologs_dir"] = phage_homologs_dir
    phage_homologs_dir.mkdir()
    return dir_map


def clean_sequences(input_dir, output_dir, annotate=False, verbose=False,
                    cpus=1, trna=False):
    fasta_dir = output_dir.joinpath("fasta")
    fasta_dir.mkdir()

    gb_dir = output_dir.joinpath("gb")
    gb_dir.mkdir()

    work_items = list()
    for input_file in input_dir.iterdir():
        work_items.append((input_file, output_dir, fasta_dir, gb_dir,
                           annotate, trna))

    seq_count = sum(parallelize(work_items, cpus, clean_sequence,
                                verbose=verbose))

    return (fasta_dir, gb_dir, seq_count)


def clean_sequence(input_file, output_dir, fasta_dir, gb_dir, annotate=False,
                   trna=False):
    file_fmt = sniff_format(input_file)
    if file_fmt not in ("fasta", "genbank"):
        return 0

    records = [record for record in SeqIO.parse(input_file, file_fmt)]

    if file_fmt == "fasta" or annotate:
        for record in records:
            record.features = list()
            annotate_record(record, output_dir, trna=trna)

    cleanup_flatfile_records(records)

    fasta_file = fasta_dir.joinpath(".".join([input_file.stem, "fasta"]))
    SeqIO.write(records, fasta_file, "fasta")

    gb_file = gb_dir.joinpath(".".join([input_file.stem, "gb"]))
    SeqIO.write(records, gb_file, "gb")

    return 1


def create_shell_db(bacterial_sequences, output_dir, config, tmp_dir,
                    verbose=False):
    if verbose:
        print("...indexing bacterial protein sequences...")
    # Create a simple fasta-based database from the given bacterial sequences
    fasta_file, index_file, cluster_file = index_sequences(
                                                bacterial_sequences, tmp_dir,
                                                name=SHELL_DB_DEFAULTS["name"])

    gene_clusters_dir = tmp_dir.joinpath("phams")
    gene_clusters_dir.mkdir()
    # Phamerate bacterial sequences

    if verbose:
        print("...clustering bacterial protein sequences...")
    phamerate_config = config["phameration"]["bacteria"]
    execute_phamerate_pipeline(fasta_file, gene_clusters_dir, phamerate_config)

    if cluster_file is None:
        cluster_file = create_cluster_schema(index_file, gene_clusters_dir,
                                             tmp_dir)

    if verbose:
        print("...screening for shell genome content...")
    rep_threshold = config["parameters"]["shell"]["rep_threshold"]
    screen_conserved_phams(gene_clusters_dir, output_dir, index_file,
                           cluster_file, rep_threshold=rep_threshold)

    fasta_file.replace(output_dir.joinpath(fasta_file.name))


def create_phage_homologs_db(phage_sequences, output_dir, config, tmp_dir,
                             cpus=1, verbose=False):
    if verbose:
        print("...indexing phage protein sequences...")
    # Create a simple fasta-based database from the given phage sequences
    fasta_file, index_file, cluster_file = index_sequences(
                                                phage_sequences, tmp_dir)

    gene_clusters_dir = tmp_dir.joinpath("phams")
    gene_clusters_dir.mkdir()

    if verbose:
        print("...clustering phage protein sequences...")
    # Phamerate bacterial sequences
    phamerate_config = PACKAGE_DIR.joinpath(config["phameration"]["bacteria"])
    execute_phamerate_pipeline(fasta_file, gene_clusters_dir, phamerate_config)

    if verbose:
        print("...curating for essential phage protein clusters...")
    curated_clusters_dir = tmp_dir.joinpath("curated_phams")
    curated_clusters_dir.mkdir()
    functions_config = config["functions"]["essential"]
    curate_gene_clusters(gene_clusters_dir, index_file, functions_config,
                         curated_clusters_dir, verbose=verbose, cores=cpus)

    if verbose:
        print("...creating database of essential phage protein profiles...")
    build_HMM_db(curated_clusters_dir, output_dir, name="essential")

    if verbose:
        print("...curating for accessory phage protein clusters...")
    extended_curated_clusters_dir = tmp_dir.joinpath("extended_curated_phams")
    extended_curated_clusters_dir.mkdir()
    functions_config = config["functions"]["extended"]
    min_HMM_count = config["parameters"]["phage_homologs"]["min_HMM_count"]
    curate_gene_clusters(gene_clusters_dir, index_file, functions_config,
                         curated_clusters_dir, verbose=verbose,
                         accept_all=True, cores=cpus, min_size=min_HMM_count)

    if verbose:
        print("...creating database of accessory phage protein profiles...")
    build_HMM_db(curated_clusters_dir, output_dir, name="extended")


def create_cluster_schema(index_file, gene_clusters_dir, tmp_dir):
    gene_index = fileio.read_gene_index_file(index_file)

    clustered_ids = list()
    ids = list()
    for data_dict in list(gene_index.values()):
        ids.append(data_dict["parent"])

    clustered_ids.append(ids)

    cluster_file = tmp_dir.joinpath(".".join([HHSUITEDB_DEFAULTS["name"],
                                              "ci"]))
    fileio.write_cluster_file(clustered_ids, cluster_file)

    return cluster_file

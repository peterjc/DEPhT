"""
Prophicient scans bacterial genomes looking for prophages. Regions
identified as prophage candidates are further scrutinized, and
attachment sites identified as accurately as possible before
prophage extraction and generating the final report.
"""
import argparse
import pathlib
import shutil
import sys
from datetime import date, datetime

from Bio import SeqIO
from Bio.SeqFeature import FeatureLocation, SeqFeature

from prophicient import PACKAGE_DIR
from prophicient.classes.prophage import Prophage
from prophicient.functions import blastn, gene_prediction
from prophicient.functions.fasta import write_fasta
from prophicient.functions.att import find_attachment_site
from prophicient.functions.find_homologs import find_homologs
from prophicient.functions.multiprocess import CPUS
from prophicient.functions.prophage_prediction import (
                                contig_to_dataframe, predict_prophage_genes,
                                predict_prophage_coords)
from prophicient.functions.visualization import prophage_diagram

# GLOBAL VARIABLES
# -----------------------------------------------------------------------------
TEMP_DIR = pathlib.Path("/tmp/Prophicient/")
DATABASES_DIR = PACKAGE_DIR.joinpath("data/databases")
REFERENCES_DB = DATABASES_DIR.joinpath("Mycobacteria")
FUNCTIONS_DB = DATABASES_DIR.joinpath("functions")

DATE = date.today().strftime("%d-%b-%Y").upper()

MIN_LENGTH = 20000      # Don't annotate short contigs
META_LENGTH = 100000    # Medium-length contigs -> use metagenomic mode
EXTENSION = 10000
MIN_KMER_SCORE = 5


def parse_args(arguments):
    """
    Parse command line arguments.

    :param arguments: command line arguments that program was invoked with
    :type arguments: list
    :return: parsed_args
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("infile", type=pathlib.Path,
                        help="path to a FASTA nucleotide sequence file to "
                             "scan for prophages")
    parser.add_argument("outdir", type=pathlib.Path,
                        help="path where output files should be written")
    parser.add_argument("--gff3", type=pathlib.Path,
                        help="path to a GFF3 file, bypasses auto-annotation")
    parser.add_argument("--verbose", action="store_true",
                        help="toggles verbosity of pipeline")
    parser.add_argument("--no-graphics", action="store_true",
                        help="don't output genome map PDFs for identified "
                             "prophages")
    parser.add_argument("--cpus", type=int, default=CPUS,
                        help=f"number of processors to use [default: {CPUS}]")
    return parser.parse_args(arguments)


def main(arguments):
    """
    Main function that interfaces with command line args and the
    program workflow.

    :param arguments: command line arguments
    :type arguments: list
    """
    args = parse_args(arguments)

    # Verify that the input filepath is valid
    infile = args.infile
    if not infile.is_file():
        print(f"'{str(infile)}' is not a valid input file - exiting...")
        sys.exit(1)

    outdir = args.outdir
    if not outdir.is_dir():
        print(f"'{str(outdir)}' does not exist - creating it...")
        outdir.mkdir(parents=True)

    gff3 = args.gff3
    if gff3 and not gff3.is_file():
        print(f"GFF3 file '{str(gff3)}' does not exist - rollback to "
              f"auto-annotate...")
        gff3 = None

    cpus = args.cpus
    verbose = args.verbose
    diagram = not args.no_graphics

    # Mark program start time
    mark = datetime.now()
    find_prophages(infile, outdir, gff3, cpus, verbose, diagram)
    # execute_prophicient(args.infile, args.outdir, args.database,
    #                     cores=args.cpus, verbose=args.verbose)
    print(f"\nTotal runtime: {str(datetime.now() - mark)}")


def find_prophages(fasta, outdir, gff3=None, cpus=CPUS, verbose=False,
                   diagram=True, extension=EXTENSION):
    """
    Runs through all steps of prophage prediction:

    * auto-annotation with Pyrodigal (skip if gff3)
    * predict prophage genes using binary classifier
    * identify prophage regions
    * identify phage genes in prophage regions
    * detect attL/attR
    * extract final prophage sequences

    :param fasta: the path to a fasta nucleotide sequence file
    containing a mycobacterial genome to find prophages in
    :type fasta: pathlib.Path
    :param outdir: the path to a directory where output files should
    be written
    :type outdir: pathlib.Path
    :param gff3: (optional) the path to a GFF3 file with an annotation
    for the indicated fasta file
    :type gff3: pathlib.Path
    :param cpus: the maximum number of processors to use
    :type cpus: int
    :param verbose: should progress messages be printed along the way?
    :type verbose: bool
    :param diagram: should genome diagrams be created at the end?
    :type diagram: bool
    :param extend_by: number of basepairs to extend predicted prophage regions
    :type extend_by: int
    """
    if verbose:
        print("Loading FASTA file...")
    # Mark FASTA load start
    mark = datetime.now()
    # Parse FASTA file - only keep contigs longer than MIN_LENGTH
    contigs = [x for x in SeqIO.parse(fasta, "fasta") if len(x) >= MIN_LENGTH]
    # Print FASTA load time
    print(f"FASTA load: {str(datetime.now() - mark)}")

    if verbose:
        print("Beginning annotation...")
    # Mark annotation start
    mark = datetime.now()
    # TODO: add mechanism for skipping pyrodigal auto-annotation by
    #  associating records with gff3 file features
    if gff3:
        if verbose:
            print(f"\tUsing gff3 file '{str(gff3)}'...")
        # TODO: check each record's length against MIN_LENGTH - still
        #  skip these to be consistent
        pass
    else:
        if verbose:
            print("\tNo gff3 file - using Pyrodigal and Aragorn...")
        for contig in contigs:
            # Annotate record CDS & t(m)RNA features in-place
            gene_prediction.annotate_contig(contig, len(contig) < META_LENGTH)
    # Print annotation time
    print(f"Annotation: {str(datetime.now() - mark)}")

    if verbose:
        print("Looking for high-probability prophage regions...")
    # Mark prophage prediction start
    mark = datetime.now()
    # Get dataframes of CDS features for binary classification
    dataframes = [contig_to_dataframe(contig) for contig in contigs]
    # Perform binary classification of contig CDS features
    gene_predictions = [predict_prophage_genes(df) for df in dataframes]
    # Initial pass at prophage identification
    prophage_predictions = [predict_prophage_coords(x, y)
                            for x, y in zip(contigs, gene_predictions)]

    # Print prophage prediction time
    print(f"Prediction: {str(datetime.now() - mark)}")

    # Create temporary dir, if it doesn't exist.
    # If it does, destroy the existing first
    if TEMP_DIR.is_dir():
        shutil.rmtree(TEMP_DIR)
    TEMP_DIR.mkdir()

    # Search for phage gene remote homologs and annotate the bacterial sequence
    mark = datetime.now()
    search_for_prophage_region_homology(contigs, prophage_predictions,
                                        FUNCTIONS_DB, TEMP_DIR, cores=CPUS)
    print(f"Homology search: {str(datetime.now() - mark)}")

    prophages = load_initial_prophages(contigs, prophage_predictions)

    if len(prophages) == 0:
        print(f"No complete prophages found in {str(fasta)}. PHASTER may "
              f"be able to identify partial (dead) prophages.")
        return

    # Detect attachment sites, where possible, for the predicted prophage
    mark = datetime.now()
    detect_att_sites(prophages, REFERENCES_DB, extension*2, TEMP_DIR)
    print(f"Att search: {str(datetime.now() - mark)}")

    prophage_records = [prophage.record for prophage in prophages]
    # TODO: add parallel hhsearch to find essential phage functions:
    #  overwrite "hypothetical function" in cds.qualifiers["product"][0]
    # TODO: add attachment core detection
    # TODO: clean up final prophage annotations (add qualifiers for gene and
    #  locus_tag, and maybe gene features for each CDS/tRNA/tmRNA)

    if verbose:
        print("Generating final reports...")
    mark = datetime.now()
    for prophage in prophage_records:
        genbank_filename = outdir.joinpath(f"{prophage.id}.gbk")
        SeqIO.write(prophage, genbank_filename, "genbank")
        if diagram:
            diagram_filename = outdir.joinpath(f"{prophage.id}.pdf")
            prophage_diagram(prophage, diagram_filename)
    print(f"File Dumps: {str(datetime.now() - mark)}")


# HELPER FUNCTIONS
# -----------------------------------------------------------------------------
def load_initial_prophages(contigs, prophage_predictions):
    """Creates Prophage objects from initial prophage prediction coordinates
    and their respective parent SeqRecord objects.

    :param contigs: SeqRecord nucleotide sequence objects
    :type contigs: list[Bio.SeqRecord.SeqRecord]
    :param prophage_predictions: Coordinates for predicted prophages
    :type prophage_predictions: list[list]
    :return: Prophage objects that contain putative sequences and coordinates
    :rtype: list
    """
    prophages = []
    for contig_index, contig in enumerate(contigs):
        # Retrieve the contig seqrecord associated with the coordinates
        contig_predictions = prophage_predictions[contig_index]
        for prophage_index, prophage_coordinates in enumerate(
                                                    contig_predictions):
            # Create a prophage ID from the SeqRecord ID
            prophage_id = "".join(["prophi", contig.id,
                                   "-", str((prophage_index+1))])
            start = prophage_coordinates[0]
            end = prophage_coordinates[1]

            prophage = Prophage(contig, prophage_id, start=start, end=end)
            prophages.append(prophage)

    return prophages


def get_reference_map_from_sequence(sequence, sequence_name,
                                    reference_db_path, temp_dir):
    """Maps sequence BLASTn aligned reference genome IDs to their respective
    alignment result data.

    :param sequence: Query sequence to be aligned to the reference database
    :type sequence: str
    :param sequence_name: Name of the query sequence to be aligned
    :type sequence_name: str
    :param reference_db_path: Path to the database of references to search
    :type referrence: pathlib.Path
    :param temp_dir: Working directory to place BLASTn inputs and outputs
    :type temp_dir: pathlib.Path
    :return: A map of aligned reference genome IDs to alignment result data
    """
    sequence_path = temp_dir.joinpath(".".join([sequence_name, "fasta"]))

    # Write the sequence to a fasta file in the temp directory
    write_fasta(sequence_path, [sequence_name], [sequence])

    # Try to retrieve reference results for the sequence to the references
    try:
        blast_results = blastn.blast_references(
                                sequence_path, reference_db_path, temp_dir)
    # If there are no good alignments, return an empty list
    except blastn.SignificantAlignmentNotFound:
        blast_results = []

    reference_map = dict()
    for blast_result in blast_results:
        # Checks to see if the sequence refeernce ID has already been stored
        exists = reference_map.get(blast_result["sseqid"], None)

        # If it has been, continue
        if exists is not None:
            continue

        reference_map[blast_result["sseqid"]] = blast_result

    return reference_map


def search_for_prophage_region_homology(contigs, prophage_predictions,
                                        functions_db, temp_dir, cores=1,
                                        min_size=150):
    """In predicted prophage regions, annotate gene translations on the
    given bacterial sequence contig.

    :param contigs: Bacterial sequence contigs
    :type contigs: list
    :param prophage_predictions: Coordinates of predicted prophages
    :type prophage_predictions: list
    :param functions_db: Path to the database with prophage gene homologs.
    :type functions_db: pathlib.Path
    :param temp_dir: Path to place result files.
    :type temp_dir: pathlib.Path
    :param cores: Number of HHsearch process to create.
    :type cores: int
    :param min_size: Minimum length threshold of gene translations.
    :type min_size: int
    """
    translations_dir = temp_dir.joinpath("gene_translations")
    translations_dir.mkdir()

    gene_id_feature_map = dict()
    for contig_index, contig in enumerate(contigs):
        contig_predictions = prophage_predictions[contig_index]

        cds_num = 0
        for feature in contig.features:
            if feature.type != "CDS":
                continue
            gene_id = "_".join([contig.id, str(cds_num)])
            feature.qualifiers["locus_tag"] = [gene_id]

            trans = feature.translate(contig.seq,
                                      table=11, to_stop=True)
            feature.qualifiers["translation"] = [trans]

            feature.qualifiers["product"] = ["hypothetical protein"]

            cds_num += 1
            if len(trans) < min_size:
                continue

            for coordinates in contig_predictions:
                if (feature.location.end > coordinates[0] and
                        feature.location.end < coordinates[1]):
                    gene_id_feature_map[gene_id] = feature
                    file_path = translations_dir.joinpath(gene_id).with_suffix(
                                                                    ".fasta")

                    write_fasta(file_path, [gene_id], [trans])
                    break

    hhresults_dir = temp_dir.joinpath("hhresults")
    hhresults_dir.mkdir()

    homologs = find_homologs(translations_dir, hhresults_dir, functions_db,
                             cores=cores, verbose=True)

    for gene_id, product in homologs:
        feature = gene_id_feature_map[gene_id]

        feature.qualifiers["product"] = [product]


def detect_att_sites(prophages, reference_db_path, extension,
                     temp_dir, min_kmer_score=5):
    """Detect attachment sites demarcating predicted prophage regions from
    the bacterial contig.

    :param prophages: Predicted prophages
    :type prophages: list
    :param reference_db_path: Path to the database with reference sequences
    :type reference_db_path: pathlib.Path
    :param extension: Internal length of the prophage to check for att sites
    :type extension: int
    :param temp_dir: Path to place result files.
    :type temp_dir: pathlib.Path
    :param min_kmer_score: Minimum length threshold of attachment sites.
    :type min_kmer_score: int
    """
    for prophage in prophages:
        working_dir = temp_dir.joinpath(prophage.id) 
        working_dir.mkdir()

        l_sequence = str(prophage.seq[:extension])
        l_sequence_name = "_".join([prophage.id, "L", "extension"])
        l_reference_map = get_reference_map_from_sequence(
                                                l_sequence, l_sequence_name,
                                                reference_db_path, working_dir)

        r_sequence = str(prophage.seq[-1*(extension):])
        r_sequence_name = "_".join([prophage.id, "R", "extension"])
        r_reference_map = get_reference_map_from_sequence(
                                                r_sequence, r_sequence_name,
                                                reference_db_path, working_dir)

        reference_ids = list(set(l_reference_map.keys()).intersection(
                             set(r_reference_map.keys())))

        reference_data = [
                (l_reference_map[reference_id], r_reference_map[reference_id])
                for reference_id in reference_ids]
        reference_data.sort(key=lambda x: x[0]["evalue"] + x[1]["evalue"])

        new_coords = None
        for data_tuple in reference_data:
            left_ref_pos = int(data_tuple[0]["send"])
            right_ref_pos = int(data_tuple[1]["sstart"])

            if left_ref_pos > right_ref_pos:
                overlap_len = left_ref_pos - right_ref_pos
                if overlap_len < min_kmer_score:
                    continue

                new_start = (prophage.start +
                             int(data_tuple[0]["qend"]) - overlap_len)
                new_end = (prophage.end -
                           (extension - int(data_tuple[1]["qstart"])) +
                           overlap_len)
                new_coords = (new_start, new_end)

                break

        if not new_coords:
            l_origin = extension // 2
            r_origin = extension // 2
            if reference_data:
                data_tuple = reference_data[0]

                l_end = int(data_tuple[0]["qend"])
                if l_end < l_origin:
                    l_origin = l_end

                r_start = int(data_tuple[1]["qstart"])
                if r_start > r_origin:
                    r_origin = r_start

            kmer_data = find_attachment_site(l_sequence, r_sequence,
                                             l_origin, r_origin,
                                             k=min_kmer_score)
            if kmer_data[2] >= min_kmer_score:
                new_start = (prophage.start +
                             kmer_data[0].location.start)
                new_end = ((prophage.end - extension) +
                           kmer_data[1].location.end)
                new_coords = (new_start, new_end)
            else:
                new_coords = (prophage.start, prophage.end)

        prophage.set_coordinates(*new_coords)


def extract_prodigal_features(record, prodigal_output_path):
    with prodigal_output_path.open(mode="r") as filehandle:
        prodigal_output = "".join(filehandle.readlines())

    prodigal_genes = PRODIGAL_FORMAT.findall(prodigal_output)

    # Iterate over prodigal_genes (regex "hits" from Prodigal file...)
    for gene in prodigal_genes:
        gene_num = int(gene[0])
        start = int(gene[1]) - 1
        end = int(gene[2])
        strand = int(gene[3])
        partial = int(gene[4])
        start_codon = gene[5]
        rbs_type = gene[6]
        rbs_spacer = gene[7]
        gc_pct = gene[8]

        # Create SeqFeature from these data, and add it to record.features
        qualify = {"note": {"partial": partial, "start_codon": start_codon,
                            "rbs_type": rbs_type, "rbs_spacer": rbs_spacer,
                            "gc content": gc_pct},
                   "product": ["hypothetical protein"],
                   "locus_tag": ["_".join([record.id, "CDS",
                                 str(gene_num+1)])]}
        feature = SeqFeature(FeatureLocation(start, end), strand=strand,
                             qualifiers=qualify, type="CDS")

        feature_sequence = feature.extract(record.seq)
        feature_trans = feature_sequence.translate(to_stop=True, table=11)
        feature.qualifiers["translation"] = [str(feature_trans)]

        record.features.append(feature)


if __name__ == "__main__":
    if len(sys.argv) == 1:
        sys.argv.append("-h")
    main(sys.argv[1:])

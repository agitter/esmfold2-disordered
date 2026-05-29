#!/usr/bin/env python3
"""
Process a DisProt TSV release file into a simplified, deduplicated TSV
with UniProt sequences appended.

Usage:
    python process_disprot.py <input_disprot.tsv> <output.tsv>
"""

# Drafted by Claude Sonnet 4.6

import sys
import time
import argparse
import pandas as pd
import urllib.request
import urllib.error


# ── Column names as they appear in the DisProt file ──────────────────────────

KEEP_COLS = [
    "UniProt ACC",
    "DisProt ID",
    "Protein name",
    "Gene name",
    "Sequence length",
    "Organism",
    "NCBI Taxon ID",
    "Protein Disorder Content",
    "Alphafold Very Low confidence content",
]

SORT_COLS = [
    "Alphafold Very Low confidence content",
    "Protein Disorder Content",
]

MAX_SEQ_LEN = 700


# ── UniProt API ───────────────────────────────────────────────────────────────

def fetch_uniprot_sequence(accession: str, retries: int = 3, backoff: float = 2.0) -> str:
    """
    Fetch the canonical FASTA sequence for a UniProt accession.
    Returns the amino-acid sequence string, or "" on failure.
    """
    url = f"https://rest.uniprot.org/uniprotkb/{accession}.fasta"
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                fasta = resp.read().decode("utf-8")
            # FASTA: first line is header, rest is sequence (may be multi-line)
            lines = fasta.strip().splitlines()
            seq = "".join(line.strip() for line in lines if not line.startswith(">"))
            return seq
        except urllib.error.HTTPError as e:
            if e.code == 404:
                print(f"  [WARN] {accession}: not found in UniProt (404)", file=sys.stderr)
                return ""
            print(f"  [WARN] {accession}: HTTP {e.code} (attempt {attempt + 1}/{retries})", file=sys.stderr)
        except Exception as e:
            print(f"  [WARN] {accession}: {e} (attempt {attempt + 1}/{retries})", file=sys.stderr)
        if attempt < retries - 1:
            time.sleep(backoff * (attempt + 1))
    return ""


# ── Main processing ───────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Process a DisProt TSV release file.")
    parser.add_argument("input_file", help="Path to the DisProt .tsv input file")
    parser.add_argument("output_file", help="Path for the simplified output .tsv file")
    args = parser.parse_args()

    # ── 1. Load ───────────────────────────────────────────────────────────────
    print(f"Loading {args.input_file} …")
    df = pd.read_csv(args.input_file, sep="\t", low_memory=False)
    print(f"  {len(df):,} rows, {df['UniProt ACC'].nunique():,} unique UniProt ACCs")

    # ── 2. Remove obsolete rows ───────────────────────────────────────────────
    before = len(df)
    # The 'Obsolete' column may be boolean or the string "True"/"False"
    obsolete_mask = df["Obsolete"].astype(str).str.strip().str.lower() == "true"
    df = df[~obsolete_mask].copy()
    removed = before - len(df)
    print(f"  Removed {removed:,} obsolete rows → {len(df):,} remaining")

    # ── 3. Keep only needed columns (drop extras before dedup) ────────────────
    df = df[KEEP_COLS].copy()

    # ── 4. Deduplicate: one row per UniProt ACC ───────────────────────────────
    # Sort so that the row with the highest AlphaFold content (then highest
    # Disorder Content as tiebreaker) comes first within each group.
    df_sorted = df.sort_values(
        by=SORT_COLS,
        ascending=[False, False],
        na_position="last",          # NaN AlphaFold rows go to the bottom
    )
    df_dedup = df_sorted.drop_duplicates(subset=["UniProt ACC"], keep="first").copy()
    print(f"  After deduplication: {len(df_dedup):,} unique proteins")

    # ── 5. Sort output rows ───────────────────────────────────────────────────
    df_dedup = df_dedup.sort_values(
        by=SORT_COLS,
        ascending=[False, False],
        na_position="last",
    ).reset_index(drop=True)

    # ── 6. Fetch sequences from UniProt ──────────────────────────────────────
    accessions = df_dedup["UniProt ACC"].tolist()
    total = len(accessions)
    print(f"\nFetching sequences for {total:,} proteins from UniProt …")

    sequences = {}
    for i, acc in enumerate(accessions, 1):
        if i % 50 == 0 or i == 1 or i == total:
            print(f"  [{i}/{total}] {acc}")
        seq = fetch_uniprot_sequence(acc)
        sequences[acc] = seq
        # Be a polite API citizen: small delay between requests
        time.sleep(0.2)

    # ── 7. Add Sequence and Sequence Truncated columns ────────────────────────
    df_dedup["Sequence"] = df_dedup["UniProt ACC"].map(sequences)

    def truncate_seq(row):
        seq = row["Sequence"]
        if not isinstance(seq, str) or seq == "":
            return ""
        return seq[:MAX_SEQ_LEN]

    df_dedup["Sequence Truncated"] = df_dedup.apply(truncate_seq, axis=1)

    # ── 8. Write output ───────────────────────────────────────────────────────
    output_cols = KEEP_COLS + ["Sequence", "Sequence Truncated"]
    df_dedup[output_cols].to_csv(args.output_file, sep="\t", index=False)
    print(f"\nDone! Wrote {len(df_dedup):,} rows to {args.output_file}")

    # Quick sanity check
    missing_seq = (df_dedup["Sequence"] == "").sum()
    if missing_seq:
        print(f"  [NOTE] {missing_seq} protein(s) had no sequence returned from UniProt.")


if __name__ == "__main__":
    main()

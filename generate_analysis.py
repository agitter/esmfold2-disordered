#!/usr/bin/env python3
"""
generate_analysis.py

Parses AlphaFold2 (.pdb), AlphaFold3 (.cif inside .zip), and ESMFold2 (.cif)
structure files, aligns per-residue pLDDT to the full UniProt sequence, generates
one line plot per protein saved to ./analysis/, and writes a GitHub Markdown
document (analysis_results.md) in the working directory.

Usage:
    python generate_analysis.py [--tsv PATH] [--workdir PATH]

Defaults:
    --tsv     DisProt_release_2025_12_seqs.tsv  (in workdir)
    --workdir .  (current directory)
"""

# Drafted by Claude Sonnet 4.6

import argparse
import io
import os
import re
import zipfile
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import numpy as np
import pandas as pd
from Bio.Align import PairwiseAligner
from Bio.PDB import MMCIFParser, PDBParser


# ── Constants ─────────────────────────────────────────────────────────────────

METHODS = ["alphafold2", "alphafold3", "esmfold2"]
METHOD_LABELS = {
    "alphafold2": "AlphaFold2",
    "alphafold3": "AlphaFold3",
    "esmfold2":   "ESMFold2",
}
METHOD_COLORS = {
    "alphafold2": "#2166ac",   # blue
    "alphafold3": "#d6604d",   # red-orange
    "esmfold2":   "#4dac26",   # green
}

# pLDDT thresholds for background shading (AlphaFold convention)
PLDDT_BANDS = [
    (90, 100, "#d9f0d3", "Very high (>90)"),
    (70,  90, "#e8f4f8", "Confident (70–90)"),
    (50,  70, "#fff3cd", "Low (50–70)"),
    (0,   50, "#fde8e8", "Very low (<50)"),
]

def _make_aligner() -> PairwiseAligner:
    """
    Semiglobal aligner: the structure sequence (query) must align fully,
    but free gaps are allowed at both ends of the reference (target) so that:
      - C-terminal truncation (ESMFold2/AF3 stopping at residue 700) costs nothing
      - N-terminal truncation or partial AF2 database models also align cleanly
    Internal gaps in the structure (missing loops) are penalised normally.
    """
    aln = PairwiseAligner()
    aln.mode = "global"
    aln.match_score = 2
    aln.mismatch_score = -1
    aln.open_gap_score = -10
    aln.extend_gap_score = -0.5
    # Free end gaps on the reference (target) only
    aln.open_left_insertion_score    = 0
    aln.extend_left_insertion_score  = 0
    aln.open_right_insertion_score   = 0
    aln.extend_right_insertion_score = 0
    return aln

ALIGNER = _make_aligner()


# ── Three-letter → one-letter amino acid conversion ───────────────────────────

THREE_TO_ONE = {
    "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C",
    "GLN": "Q", "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I",
    "LEU": "L", "LYS": "K", "MET": "M", "PHE": "F", "PRO": "P",
    "SER": "S", "THR": "T", "TRP": "W", "TYR": "Y", "VAL": "V",
    "SEC": "U", "PYL": "O", "MSE": "M",   # selenomethionine → M
}


# ── PDB / CIF parsing ─────────────────────────────────────────────────────────

def _residues_from_structure(structure) -> list[tuple[str, float]]:
    """
    Return [(one_letter_aa, bfactor), ...] for every standard amino-acid
    ATOM residue in the first model, first chain, in residue sequence order.
    Hetero residues (HETATM) are skipped.
    """
    model = next(structure.get_models())
    # Collect all chains; for single-chain predictions just take the first.
    chains = list(model.get_chains())
    chain = chains[0]

    residues = []
    for res in chain.get_residues():
        het, _, _ = res.get_id()
        if het.strip():          # skip HETATM / water
            continue
        resname = res.get_resname().strip()
        aa = THREE_TO_ONE.get(resname)
        if aa is None:
            continue
        # Use CA B-factor as pLDDT; fall back to first atom if no CA
        if "CA" in res:
            bfac = res["CA"].get_bfactor()
        else:
            bfac = next(res.get_atoms()).get_bfactor()
        residues.append((aa, bfac))
    return residues


def parse_pdb(path: Path) -> list[tuple[str, float]]:
    parser = PDBParser(QUIET=True)
    structure = parser.get_structure("s", str(path))
    return _residues_from_structure(structure)


def parse_cif(path_or_handle, name: str = "s") -> list[tuple[str, float]]:
    parser = MMCIFParser(QUIET=True)
    if isinstance(path_or_handle, (str, Path)):
        structure = parser.get_structure(name, str(path_or_handle))
    else:
        structure = parser.get_structure(name, path_or_handle)
    return _residues_from_structure(structure)


def parse_af3_zip(zip_path: Path, acc: str) -> list[tuple[str, float]]:
    """Extract the model CIF from an AlphaFold3 zip and parse it."""
    cif_name = f"fold_{acc.lower()}_model_0.cif"
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        # Try exact match first, then fallback to any .cif
        match = cif_name if cif_name in names else next(
            (n for n in names if n.endswith(".cif")), None
        )
        if match is None:
            raise FileNotFoundError(
                f"No .cif found in {zip_path}. Contents: {names}"
            )
        with zf.open(match) as fh:
            # MMCIFParser needs a file-path-like object; wrap bytes in StringIO
            text = io.TextIOWrapper(fh, encoding="utf-8")
            return parse_cif(text, name=acc)


# ── Sequence alignment ────────────────────────────────────────────────────────

def align_plddt_to_reference(
    ref_seq: str,
    struct_residues: list[tuple[str, float]],
) -> np.ndarray:
    """
    Align the sequence extracted from a structure to the full UniProt reference
    sequence using a semiglobal aligner (free end gaps on the reference), then
    project per-residue pLDDT values onto the reference coordinate system.

    Returns a float array of length len(ref_seq) with NaN where the structure
    has no corresponding residue (gap, truncation, or not modelled).
    """
    struct_seq = "".join(aa for aa, _ in struct_residues)
    plddt_vals = [b for _, b in struct_residues]
    plddt_on_ref = np.full(len(ref_seq), np.nan)

    if not struct_seq:
        return plddt_on_ref

    alignments = ALIGNER.align(ref_seq, struct_seq)
    try:
        aln = next(iter(alignments))
    except StopIteration:
        return plddt_on_ref

    aln_ref    = aln[0]   # reference with gap characters
    aln_struct = aln[1]   # structure sequence with gap characters

    ref_pos    = 0   # position in original ref_seq
    struct_pos = 0   # position in plddt_vals

    for r_char, s_char in zip(aln_ref, aln_struct):
        if r_char == "-" and s_char == "-":
            continue
        elif r_char == "-":
            # insertion in structure relative to reference
            struct_pos += 1
        elif s_char == "-":
            # deletion in structure (or free end-gap on reference)
            ref_pos += 1
        else:
            # aligned columns (match or mismatch)
            if struct_pos < len(plddt_vals):
                plddt_on_ref[ref_pos] = plddt_vals[struct_pos]
            ref_pos    += 1
            struct_pos += 1

    return plddt_on_ref


# ── File discovery ────────────────────────────────────────────────────────────

def find_af2_pdb(workdir: Path, acc: str) -> Path | None:
    p = workdir / "alphafold2" / f"AF-{acc}-F1-model_v6.pdb"
    return p if p.exists() else None


def find_af3_zip(workdir: Path, acc: str) -> Path | None:
    p = workdir / "alphafold3" / f"{acc}.zip"
    return p if p.exists() else None


def find_esm_cif(workdir: Path, acc: str) -> Path | None:
    p = workdir / "esmfold2" / f"{acc}.cif"
    return p if p.exists() else None


def screenshot_rel_path(method: str, acc: str) -> str:
    """Relative path from the working directory to a screenshot PNG."""
    return f"{method}/{acc}.png"


# ── Plot generation ───────────────────────────────────────────────────────────

def generate_plot(
    acc: str,
    ref_seq: str,
    plddt_arrays: dict[str, np.ndarray],
    disorder_content: float,
    af_conf_content: float,
    out_path: Path,
) -> None:
    """
    Draw the per-residue pLDDT line plot for one protein and save to out_path.
    """
    n = len(ref_seq)
    positions = np.arange(1, n + 1)

    fig, ax = plt.subplots(figsize=(12, 4))

    # pLDDT confidence band shading (background)
    for lo, hi, color, _ in PLDDT_BANDS:
        ax.axhspan(lo, hi, color=color, alpha=0.45, zorder=0)

    # Band boundary lines (subtle)
    for threshold in (50, 70, 90):
        ax.axhline(threshold, color="grey", linewidth=0.4, linestyle="--", zorder=1)

    # pLDDT lines, one per method
    for method in METHODS:
        arr = plddt_arrays.get(method)
        if arr is None:
            continue
        label = METHOD_LABELS[method]
        color = METHOD_COLORS[method]
        # Use masked array so NaN segments are shown as gaps, not connected lines
        masked = np.ma.masked_invalid(arr)
        ax.plot(positions, masked, label=label, color=color,
                linewidth=1.4, alpha=0.9, zorder=3)

    # Axes formatting
    ax.set_xlim(1, n)
    ax.set_ylim(0, 100)
    ax.set_xlabel("Residue position", fontsize=11)
    ax.set_ylabel("pLDDT", fontsize=11)

    disorder_str = f"{disorder_content:.3f}" if not np.isnan(disorder_content) else "N/A"
    af_conf_str  = f"{af_conf_content:.3f}"  if not np.isnan(af_conf_content)  else "N/A"
    ax.set_title(
        f"{acc}  |  disorder content: {disorder_str}  |  AF very low conf: {af_conf_str}",
        fontsize=12, fontweight="bold", pad=10,
    )

    # Legend outside the plot to the right
    ax.legend(
        loc="upper left",
        bbox_to_anchor=(1.01, 1),
        borderaxespad=0,
        frameon=True,
        fontsize=10,
    )

    ax.xaxis.set_major_locator(ticker.MaxNLocator(integer=True, nbins=10))
    ax.tick_params(axis="both", labelsize=9)

    plt.tight_layout(rect=[0, 0, 0.87, 1])   # leave room for legend
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)


# ── Markdown generation ───────────────────────────────────────────────────────

def github_anchor(text: str) -> str:
    """Convert a heading string to its GitHub Markdown anchor fragment."""
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s-]+", "-", text.strip())
    return text


def generate_markdown(
    proteins: list[dict],
    workdir: Path,
    out_path: Path,
) -> None:
    lines = []

    lines.append("## Disorder region structure prediction analysis\n")
    lines.append(
        "Comparison of per-residue pLDDT confidence across "
        "AlphaFold2, AlphaFold3, and ESMFold2 for 15 intrinsically disordered proteins "
        "from [DisProt](https://disprot.org). "
        "AlphaFold2 predictions are full-length database downloads; "
        "AlphaFold3 and ESMFold2 predictions use the first 700 residues for sequences "
        "longer than 700 amino acids. "
        "pLDDT values are aligned to the full UniProt reference sequence.\n"
    )

    # ── Summary table ─────────────────────────────────────────────────────────
    lines.append("## Summary table\n")
    lines.append(
        "| Protein | UniProt | DisProt | Length | Disorder content | AF very low conf "
        "| AlphaFold2 | AlphaFold3 | ESMFold2 | pLDDT plot |"
    )
    lines.append(
        "|---------|---------|---------|--------|-----------------|------------------|"
        "------------|------------|----------|------------|"
    )

    for p in proteins:
        acc        = p["acc"]
        disprot_id = p["disprot_id"]
        name       = p["protein_name"]
        length     = p["seq_length"]
        disorder   = p["disorder_content"]
        af_conf    = p["af_conf_content"]

        disorder_str = f"{disorder:.3f}" if not np.isnan(disorder) else "N/A"
        af_conf_str  = f"{af_conf:.3f}"  if not np.isnan(af_conf)  else "N/A"

        uniprot_url  = f"https://www.uniprot.org/uniprotkb/{acc}/entry#structure"
        disprot_url  = f"https://disprot.org/{disprot_id}"

        # Thumbnail images — clicking opens the full screenshot
        def img_cell(method: str) -> str:
            rel = screenshot_rel_path(method, acc)
            return f'<a href="{rel}"><img src="{rel}" width="160"/></a>'

        af2_cell = img_cell("alphafold2")
        af3_cell = img_cell("alphafold3")
        esm_cell = img_cell("esmfold2")

        # Anchor link to the protein section below
        section_heading = f"{acc} {name}"
        anchor = github_anchor(section_heading)
        plot_cell = f"[plot](#{anchor})"

        lines.append(
            f"| {name} "
            f"| [{acc}]({uniprot_url}) "
            f"| [{disprot_id}]({disprot_url}) "
            f"| {length} "
            f"| {disorder_str} "
            f"| {af_conf_str} "
            f"| {af2_cell} "
            f"| {af3_cell} "
            f"| {esm_cell} "
            f"| {plot_cell} |"
        )

    lines.append("")

    # ── Per-protein sections ───────────────────────────────────────────────────
    lines.append("---\n")
    lines.append("## Per-protein pLDDT plots\n")

    for p in proteins:
        acc        = p["acc"]
        disprot_id = p["disprot_id"]
        name       = p["protein_name"]
        length     = p["seq_length"]
        disorder   = p["disorder_content"]
        af_conf    = p["af_conf_content"]

        disorder_str = f"{disorder:.3f}" if not np.isnan(disorder) else "N/A"
        af_conf_str  = f"{af_conf:.3f}"  if not np.isnan(af_conf)  else "N/A"

        uniprot_url = f"https://www.uniprot.org/uniprotkb/{acc}/entry#structure"
        disprot_url = f"https://disprot.org/{disprot_id}"

        section_heading = f"{acc} {name}"
        lines.append(f"### {section_heading}\n")
        lines.append(
            f"**UniProt:** [{acc}]({uniprot_url}) &nbsp;|&nbsp; "
            f"**DisProt:** [{disprot_id}]({disprot_url}) &nbsp;|&nbsp; "
            f"**Length:** {length} aa &nbsp;|&nbsp; "
            f"**Disorder content:** {disorder_str} &nbsp;|&nbsp; "
            f"**AF very low conf:** {af_conf_str}\n"
        )

        plot_rel = f"analysis/{acc}_plddt.png"
        lines.append(f"![pLDDT plot for {acc}]({plot_rel})\n")

        lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Markdown written → {out_path}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Generate pLDDT analysis plots and Markdown.")
    parser.add_argument("--tsv",     default=None, help="Path to DisProt seqs TSV")
    parser.add_argument("--workdir", default=".",  help="Working directory (default: .)")
    args = parser.parse_args()

    workdir = Path(args.workdir).resolve()
    tsv_path = Path(args.tsv) if args.tsv else workdir / "DisProt_release_2025_12_seqs.tsv"
    analysis_dir = workdir / "analysis"
    analysis_dir.mkdir(exist_ok=True)

    # ── Load metadata ──────────────────────────────────────────────────────────
    print(f"Loading metadata from {tsv_path} …")
    meta = pd.read_csv(tsv_path, sep="\t")

    # Keep only the 15 proteins that have structure files
    accs_with_structures = set()
    for acc in meta["UniProt ACC"]:
        if (find_af2_pdb(workdir, acc) or
                find_af3_zip(workdir, acc) or
                find_esm_cif(workdir, acc)):
            accs_with_structures.add(acc)

    meta = meta[meta["UniProt ACC"].isin(accs_with_structures)].copy()
    print(f"  Found structure files for {len(meta)} proteins.")

    proteins_out = []

    for _, row in meta.iterrows():
        acc          = row["UniProt ACC"]
        disprot_id   = row["DisProt ID"]
        protein_name = row["Protein name"]
        seq_length   = int(row["Sequence length"])
        ref_seq      = str(row["Sequence"])
        disorder     = float(row["Protein Disorder Content"]) \
                       if pd.notna(row["Protein Disorder Content"]) else np.nan
        af_conf      = float(row["Alphafold Very Low confidence content"]) \
                       if pd.notna(row["Alphafold Very Low confidence content"]) else np.nan

        print(f"\n── {acc} ({protein_name}, {seq_length} aa) ──")

        plddt_arrays: dict[str, np.ndarray] = {}

        # AlphaFold2 ──────────────────────────────────────────────────────────
        af2_path = find_af2_pdb(workdir, acc)
        if af2_path:
            try:
                residues = parse_pdb(af2_path)
                plddt_arrays["alphafold2"] = align_plddt_to_reference(ref_seq, residues)
                print(f"  AF2:     {len(residues)} residues parsed from PDB")
            except Exception as e:
                print(f"  AF2:     ERROR — {e}")
        else:
            print(f"  AF2:     no file found")

        # AlphaFold3 ──────────────────────────────────────────────────────────
        af3_path = find_af3_zip(workdir, acc)
        if af3_path:
            try:
                residues = parse_af3_zip(af3_path, acc)
                plddt_arrays["alphafold3"] = align_plddt_to_reference(ref_seq, residues)
                print(f"  AF3:     {len(residues)} residues parsed from zip/CIF")
            except Exception as e:
                print(f"  AF3:     ERROR — {e}")
        else:
            print(f"  AF3:     no file found")

        # ESMFold2 ────────────────────────────────────────────────────────────
        esm_path = find_esm_cif(workdir, acc)
        if esm_path:
            try:
                residues = parse_cif(esm_path, name=acc)
                plddt_arrays["esmfold2"] = align_plddt_to_reference(ref_seq, residues)
                print(f"  ESMFold: {len(residues)} residues parsed from CIF")
            except Exception as e:
                print(f"  ESMFold: ERROR — {e}")
        else:
            print(f"  ESMFold: no file found")

        # Generate plot ────────────────────────────────────────────────────────
        plot_path = analysis_dir / f"{acc}_plddt.png"
        generate_plot(
            acc=acc,
            ref_seq=ref_seq,
            plddt_arrays=plddt_arrays,
            disorder_content=disorder,
            af_conf_content=af_conf,
            out_path=plot_path,
        )
        print(f"  Plot saved → {plot_path.relative_to(workdir)}")

        proteins_out.append({
            "acc":            acc,
            "disprot_id":     disprot_id,
            "protein_name":   protein_name,
            "seq_length":     seq_length,
            "disorder_content": disorder,
            "af_conf_content":  af_conf,
        })

    # Generate Markdown ────────────────────────────────────────────────────────
    md_path = workdir / "analysis_results.md"
    generate_markdown(proteins_out, workdir, md_path)
    print(f"\nDone. Copy {md_path.name} into your README as needed.")


if __name__ == "__main__":
    main()

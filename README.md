# Disordered regions in predicted protein structures
Exploration of whether ESMFold2 has high pLDDT disordered regions of proteins for which AlphaFold2 has low pLDDT.
Prompted by my [anecdotal observation](https://x.com/anthonygitter/status/2059738561037963602).

## Methods
`DisProt_release_2025_12.tsv` is from https://disprot.org/download.

Run a script on that file to add sequences and sequence prefixes:
```
$ python process_disprot.py DisProt_release_2025_12.tsv DisProt_release_2025_12_seqs.tsv
Loading DisProt_release_2025_12.tsv …
  10,399 rows, 2,341 unique UniProt ACCs
  Removed 0 obsolete rows → 10,399 remaining
  After deduplication: 2,341 unique proteins
Fetching sequences for 2,341 proteins from UniProt …
  [1/2341] Q5NJL5
  ...
  [2341/2341] O95071
Done! Wrote 2,341 rows to DisProt_release_2025_12_seqs.tsv
  [NOTE] 4 protein(s) had no sequence returned from UniProt.
```
The four proteins without sequences are no longer available in UniProtKB.

Take "Sequence Truncated" sequence and predict structure with ESMFold2 at https://biohub.ai/tools/fold with the model esmfold2-2026-05.
Take "Sequence Truncated" sequence and predict structure with AlphaFold3 at https://alphafoldserver.com/.
Download AlphaFold2 structure from UniProt.
Save screenshots of each.

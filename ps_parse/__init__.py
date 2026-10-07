"""Minimal PuzzleScript parsing stack used by the dedupe fingerprint.

A slim, JAX-free copy of the parts of script-doctor
(https://github.com/smearle/script-doctor, MIT) that ``dedup_master`` needs:
the Lark grammar, text preprocessing, the parse-tree -> game-tree transformer
and the mechanics tokenizer. Logic is copied verbatim (only import paths
changed) so fingerprints stay identical to those in the existing dedupe cache.
"""

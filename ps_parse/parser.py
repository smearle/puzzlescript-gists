"""PuzzleScript Lark parser (vendored from script-doctor's ``puzzlescript_jax/parser.py``)."""
from pathlib import Path

from lark import Lark

LARK_SYNTAX_PATH = Path(__file__).resolve().parent / "syntax.lark"


def init_ps_lark_parser():
    with open(LARK_SYNTAX_PATH, "r", encoding='utf-8') as file:
        puzzlescript_grammar = file.read()
    # Initialize the Lark parser with the PuzzleScript grammar
    parser = Lark(puzzlescript_grammar, start="ps_game", maybe_placeholders=False)
    return parser

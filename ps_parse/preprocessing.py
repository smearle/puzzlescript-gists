"""PuzzleScript text preprocessing and parse-tree stripping.

Vendored verbatim from script-doctor's ``puzzlescript_jax/preprocessing.py``
(https://github.com/smearle/script-doctor, MIT). Only the pieces the dedupe
fingerprint needs are kept (``preprocess_ps`` and ``StripPuzzleScript`` plus
their helpers); every kept function and class body is byte-identical to the
original, so fingerprints match the ones already in the dedupe cache.
"""
import logging
import re

from lark import Transformer, Tree, Token
import numpy as np

logger = logging.getLogger(__name__)


class StripPuzzleScript(Transformer):
    """
    Reduces the parse tree to a minimal functional version of the grammar.
    """
    def message(self, items):
        return None

    def strip_newlines_data(self, items, data_name):
        """Remove any instances of section data that are newlines/comments"""
        items = [item for item in items if not (isinstance(item, Tree) and item.data == "newlines_or_comments")]
        items = [item for item in items if not (isinstance(item, Token) and (item.type == "NEWLINES" or item.type == "NEWLINE"))]
        if len(items) > 0:
            return Tree(data_name, items)

    def strip_section_items(self, items, data_name):
        """Remove any empty section items (e.g. resulting from returning None above, when encountering a datum that is all newlines/comments)"""
        return [item for item in items if isinstance(item, Tree) and item.data == data_name]        

    def ps_game(self, items):
        items = [item for item in items if type(item) == Tree]
        return Tree('ps_game', items)

    def objects_section(self, items):
        return Tree('objects_section', self.strip_section_items(items, 'object_data'))

    def legend_section(self, items):
        return Tree('legend_section', self.strip_section_items(items, 'legend_data'))

    def levels_section(self, items):
        items = self.strip_section_items(items, 'level_data')
        items = [i for i in items if i]
        return Tree('levels_section', items)

    def winconditions_section(self, items):
        return Tree('winconditions_section', self.strip_section_items(items, 'condition_data'))
    
    def collisionlayers_section(self, items):
        return Tree('collisionlayers_section', self.strip_section_items(items, 'layer_data'))

    def rules_section(self, items):
        # Allow either wrapped rule_block nodes or direct rule_block_once/loop nodes
        kept = []
        for item in items:
            if isinstance(item, Tree) and item.data in {'rule_block', 'rule_block_once', 'rule_block_loop'}:
                kept.append(item)
        return Tree('rules_section', kept)

    def sounds_section(self, items):
        return

    def prelude_data(self, items):
        return self.strip_newlines_data(items, 'prelude_data')

    def object_data(self, items):
        return self.strip_newlines_data(items[0].children, 'object_data')

    def level_data(self, items):
        return self.strip_newlines_data(items, 'level_data')
        # Remove any Tokens

    def legend_data(self, items):
        return self.strip_newlines_data(items, 'legend_data')
    
    def rule_data(self, items):
        assert len(items) == 1
        if items[0].data.value == 'rule_data_broken':
            return None
        return self.strip_newlines_data(items[0].children, 'rule_data')

    def rule_block_once(self, items):
        items = [i for i in items if i]
        return self.strip_newlines_data(items, 'rule_block_once')

    def rule_block_loop(self, items):
        items = [i for i in items if i]
        return self.strip_newlines_data(items, 'rule_block_loop')
    
    def line_detector(self, items):
        return "..."

    # def rule_block(self, items):
    #     return items[0]
    
    def condition_data(self, items):
        assert len(items) == 1
        if items[0].data.value == 'condition_data_broken':
            return None
        return self.strip_newlines_data(items[0].children, 'condition_data')

    def layer_data(self, items):
        return self.strip_newlines_data(items, 'layer_data')

    def sprite(self, items):
        assert len(items) == 1
        items = items[0].children
        # Remote any item that is a message
        items = [i for i in items if not (isinstance(i, Token) and i.type == 'COMMENT')]

        # Create a 2D array of the items
        grid = []
        row = []
        for s in items:
            # If we encounter a newline, start a new row
            if s == "\n":
                if len(row) > 0:
                    grid.append(row)
                    row = []
            else:
                row.append(s.value)
        max_row_len = max(len(r) for r in grid)
        for i, r in enumerate(grid):
            if len(r) < max_row_len:
                r += [r[-1]] * (max_row_len - len(r))
        try:
            grid = np.array(grid)
        except Exception as e:
            breakpoint()

        return Tree('sprite', grid)

    def levelline(self, items):
        line = [str(i) for i in items]
        assert line[-1] == "\n"
        return line[:-1]

    def levellines(self, items):
        grid = []
        level_lines = items
        grid = [line for line in level_lines[:-1]]
        # TODO: Which of these does OG PS do? Does it do different things in different cases? :/
        # pad all rows with empty tiles
        lvl_width = len(grid[0])
        for i, row in enumerate(grid):
            row_i_len = len(row)
            if row_i_len != lvl_width:
                logger.warn("Maps must be rectangular, yo.")
            if row_i_len < lvl_width:
                row += row[-1] * (lvl_width - len(row))
            elif row_i_len > lvl_width:
                row = row[:lvl_width]
            grid[i] = row
        # Truncate all the rows to the same length
        # row_lens = [len(r) for r in grid]
        # if len(set(row_lens)) > 1:
        #     logger.warning(f"Rows in grid have different lengths: {row_lens}. Truncating to the shortest row.")
        #     min_len = min(row_lens)
        #     for i, row in enumerate(grid):
        #         if len(row) > min_len:
        #             grid[i] = row[:min_len]
        grid = np.array(grid)
        return grid


def remove_sounds_section(txt):
    # Replace contents of the `SOUNDS` section with an empty section
    txt = re.sub(r'^SOUNDS\n.*?\nCOLLISIONLAYERS', 'SOUNDS\n\nCOLLISIONLAYERS', txt, flags=re.DOTALL | re.MULTILINE)
    return txt


def preprocess_rules(txt):
    # Replace any occurrence of `]...[` with `|...|`
    txt = re.sub(r'\]\s*\.\.\.\s*\[', ' | ... | ', txt)
    # Replace any occurrence of `] | [` with `] [`
    txt = re.sub(r'\]\s*\|\s*\[', '] [', txt)
    # Remove newlines
    txt = re.sub(r'\n\n', '\n', txt)
    return txt


def preprocess_collisionlayers(txt):
    # Replace any pairs of commas, separated by whitespace, with a single comma
    txt = re.sub(r',\s*,', ',', txt)
    return txt


def preprocess_levels(txt):
    # Remove any lines beginning with `message` (regardless of whether they're followed by whitespace or not)
    txt = re.sub(r'^\s*message.*', '', txt, flags=re.MULTILINE | re.IGNORECASE)
    # Replace any more-than-double newlines with a double newline
    txt = re.sub(r'\n{3,}', '\n\n', txt)
    return txt


def preprocess_ps(txt):
    # If the game starts with a comment of the style `/*...*/`, (Keys_and_Doors_0.1.0), remove it.
    txt = re.sub(r'^/\*.*?\*/', '', txt, flags=re.DOTALL)

    # Remove whitespace at end of any line
    txt = re.sub(r'[ \t]+$', '', txt, flags=re.MULTILINE)

    # Remove whitespace at start of any line
    txt = re.sub(r'^[ \t]+', '', txt, flags=re.MULTILINE)

    # txt = add_empty_sounds_section(txt)
    txt = remove_sounds_section(txt)

    # If the regular section header is not found, try the header followed by some trailing characters
    if not re.search(r'^LEGEND\n', txt, flags=re.MULTILINE | re.IGNORECASE):
        txt = re.sub(r'^LEGEND\s*.*\n', 'LEGEND\n', txt, flags=re.MULTILINE | re.IGNORECASE)
    if not re.search(r'^OBJECTS\n', txt, flags=re.MULTILINE | re.IGNORECASE):
        txt = re.sub(r'^OBJECTS\s*.*\n', 'OBJECTS\n', txt, flags=re.MULTILINE | re.IGNORECASE)
    if not re.search(r'^COLLISIONLAYERS\n', txt, flags=re.MULTILINE | re.IGNORECASE):
        txt = re.sub(r'^COLLISIONLAYERS\s*.*\n', 'COLLISIONLAYERS\n', txt, flags=re.MULTILINE | re.IGNORECASE)
    if not re.search(r'^SOUNDS\n', txt, flags=re.MULTILINE | re.IGNORECASE):
        txt = re.sub(r'^SOUNDS\s*.*\n', 'SOUNDS\n', txt, flags=re.MULTILINE | re.IGNORECASE)
    if not re.search(r'^RULES\n', txt, flags=re.MULTILINE | re.IGNORECASE):
        txt = re.sub(r'^RULES\s*.*\n', 'RULES\n', txt, flags=re.MULTILINE | re.IGNORECASE)
    if not re.search(r'^WINCONDITIONS\n', txt, flags=re.MULTILINE | re.IGNORECASE):
        txt = re.sub(r'^WINCONDITIONS\s*.*\n', 'WINCONDITIONS\n', txt, flags=re.MULTILINE | re.IGNORECASE)
    if not re.search(r'^LEVELS\n', txt, flags=re.MULTILINE | re.IGNORECASE):
        txt = re.sub(r'^LEVELS\s*.*\n', 'LEVELS\n', txt, flags=re.MULTILINE | re.IGNORECASE)

    txt = txt.replace('\u00A0', ' ')
    # If the file does not end with 2 newlines, fix this
    for i in range(2):
        if not txt.endswith("\n\n"):
            txt += "\n"

    # Remove any lines beginning with "message" (case insensitive)
    txt = re.sub(r'^message .*\n', '\n', txt, flags=re.MULTILINE | re.IGNORECASE)

    # Truncate lines ending with "message"
    # txt = re.sub(r'message.*\n', '\n', txt, flags=re.MULTILINE | re.IGNORECASE)

    # If a rule line contains a trailing "message ...", strip only the message text.
    # This needs to handle forms like "] Message ..." and also "] Sfx1 Message ...".
    # Keep the guard against lines that are actually comments, e.g. "(endgame message)".
    txt = re.sub(r'^((?!\().*\].*?)\s+message .*\n', r'\1\n', txt, flags=re.MULTILINE | re.IGNORECASE)

    ## Strip any comments
    txt = strip_comments(txt)

    # Remove any lines that are just r`=+` (or actually, at least 3 `=` followed by one accidental character;
    # a hack to get around some typos in the dataset)
    txt = re.sub(r'^===*.\n', '', txt, flags=re.MULTILINE)

    # Remove any lines that are just whitespace
    txt = re.sub(r'^\s*\n', '\n', txt, flags=re.MULTILINE)

    # any more-than-double newlines should be replaced by a double newline
    txt = re.sub(r'\n{3,}', '\n\n', txt)

    # Remove any lines that are just a single character. (Very niche patch, this one is. But we know such lines can 
    # never be anything useful, so this should be safe...)
    txt = re.sub(r'^[.]\n', '', txt, flags=re.MULTILINE)

    # Remove everything until "objects" (case insensitive)
    # txt = re.sub(r'^.*OBJECTS', 'OBJECTS', txt, flags=re.MULTILINE | re.DOTALL | re.IGNORECASE)

    sections_pattern = r"""
        ^OBJECTS\n|
        ^LEGEND\n|
        ^SOUNDS\n|
        ^COLLISIONLAYERS\n|
        ^RULES\n|
        ^WINCONDITIONS\n|
        ^LEVELS\n
    """

    sections = re.split(sections_pattern, txt, flags=re.MULTILINE | re.VERBOSE | re.IGNORECASE)
    prelude_section, objects_section, legend_section, sounds_section, collisionlayers_section, rules_section, \
        winconditions_section, levels_section = sections

    rules_section = preprocess_rules(rules_section)
    collisionlayers_section = preprocess_collisionlayers(collisionlayers_section)
    levels_section = preprocess_levels(levels_section)

    # Now put the sections back together
    txt = (f"{prelude_section}\n"
           f"OBJECTS\n{objects_section}"
           f"LEGEND\n{legend_section}"
           f"SOUNDS\n\n"
           f"COLLISIONLAYERS\n{collisionlayers_section}"
           f"RULES\n{rules_section}"
           f"WINCONDITIONS\n{winconditions_section}"
           f"LEVELS\n{levels_section}")

    return txt.lstrip()


def strip_comments(text):
    new_text = ""
    n_open_brackets = 0
    # Move through the text, keeping track of how deep we are in brackets
    for i, c in enumerate(text):
        if c == "(":
            n_open_brackets += 1
        elif c == ")":
            # we ignore unmatched closing brackets if we are outside
            new_n_open_brackets = max(0, n_open_brackets - 1)
            if new_n_open_brackets == 0 and n_open_brackets == 1:
                # If the removed comment has left us with a double-newline (because there was a newline on either side 
                # of it), convert it to a single newline
                if new_text.endswith("\n") and text[i+1] == "\n":
                    new_text = new_text[:-1]
            n_open_brackets = new_n_open_brackets
        elif n_open_brackets == 0:
            new_text += c
    return new_text

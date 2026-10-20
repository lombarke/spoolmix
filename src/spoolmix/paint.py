"""Decode, re-map and re-encode per-triangle color painting in 3MF projects (`paint_color`).

Each painted triangle has a `paint_color="..."` attribute: a hex string whose
characters are 4-bit nibbles in *reverse* order (the last character is read first).
The bitstream is a depth-first tree (TriangleSelector::serialize in OrcaSlicer):

  node nibble  = xxyy: yy = number of split sides (0 = leaf)
  split node   : xx = special side; followed by (split_sides + 1) child nodes,
                 serialized in reverse child order
  leaf, xx<3   : state = xx (0 = not painted / object filament, 1, 2)
  leaf, xx==3  : state = 3 + 15 * (number of 0b1111 nibbles) + next nibble

States are 1-based filament IDs. Strings repeat heavily, so callers cache by string.
"""

from __future__ import annotations

from dataclasses import dataclass

# Child area fractions by number of split sides (1 side -> 2 children, 2 -> 3, 3 -> 4).
# Exact for 1 and 3 sides; for 2 sides the halving geometry gives 1/2, 1/4, 1/4.
_CHILD_AREA = {1: (0.5, 0.5), 2: (0.5, 0.25, 0.25), 3: (0.25, 0.25, 0.25, 0.25)}


@dataclass
class _Node:
    split: int  # number of split sides, 0 for leaf
    special: int = 0
    state: int = 0
    children: list | None = None


def _nibbles(s: str) -> list[int]:
    return [int(c, 16) for c in reversed(s)]


def decode(s: str) -> _Node:
    nib = _nibbles(s)
    pos = 0

    def node() -> _Node:
        nonlocal pos
        code = nib[pos]
        pos += 1
        split = code & 0b11
        if split:
            n = _Node(split, special=code >> 2, children=[])
            n.children = [node() for _ in range(split + 1)]
            return n
        if (code & 0b1100) == 0b1100:
            extra = 0
            nxt = nib[pos]
            pos += 1
            while nxt == 0b1111:
                extra += 1
                nxt = nib[pos]
                pos += 1
            return _Node(0, state=nxt + 15 * extra + 3)
        return _Node(0, state=code >> 2)

    root = node()
    return root


def encode(root: _Node) -> str:
    nib: list[int] = []

    def node(n: _Node) -> None:
        if n.split:
            nib.append((n.special << 2) | n.split)
            for c in n.children:
                node(c)
            return
        st = n.state
        if st >= 3:
            nib.append(0b1100)
            st -= 3
            while st >= 15:
                nib.append(0b1111)
                st -= 15
            nib.append(st)
        else:
            nib.append(st << 2)

    node(root)
    return "".join("0123456789ABCDEF"[x] for x in reversed(nib))


def state_areas(s: str) -> dict[int, float]:
    """Fraction of the triangle's area per state (0 = unpainted)."""
    out: dict[int, float] = {}

    def walk(n: _Node, frac: float) -> None:
        if n.split:
            for c, f in zip(n.children, _CHILD_AREA[n.split]):
                walk(c, frac * f)
        else:
            out[n.state] = out.get(n.state, 0.0) + frac

    walk(decode(s), 1.0)
    return out


def remap(s: str, mapping: dict[int, int]) -> str:
    """Re-number painted states. States not in `mapping` (incl. 0) are kept."""
    root = decode(s)

    def walk(n: _Node) -> None:
        if n.split:
            for c in n.children:
                walk(c)
        elif n.state in mapping:
            n.state = mapping[n.state]

    walk(root)
    return encode(root)

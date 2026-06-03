"""Shared constants for scrambler."""

import re

# A Kùzu type token: an identifier, optionally followed by a `(...)` parameter list
# (e.g. STRING, INT64, DECIMAL(18, 3)). Group 1 captures the bare type name.
KUZU_TYPE = re.compile(r"\s*([A-Za-z_]\w*)\s*(?:\(.*\))?\s*")

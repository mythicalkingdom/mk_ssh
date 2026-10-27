import os
import re
import glob


class SSHConfigNode:
    def __init__(self, type_, **kwargs):
        self.type = type_
        self.__dict__.update(kwargs)


# Keys that accumulate one entry per directive line across matching
# blocks (OpenSSH collects all values). 'sendenv' is handled separately
# (token-level with '-NAME' removal); 'setenv' is NOT here — verified
# against OpenSSH, the first SetEnv line wins like any scalar option.
MULTI_VALUE_KEYS = {
    "identityfile",
    "certificatefile",
    "localforward",
    "remoteforward",
    "dynamicforward",
    "sendenv",
}

# Command-valued directives keep the RAW remainder of the line: verified
# against OpenSSH, quotes are preserved verbatim ('echo' "a b" stays
# 'echo' "a b") and a trailing '#...' is NOT a comment inside a command
# (it is kept for the shell to interpret). The optional '=' separator
# after the keyword IS consumed ("ProxyCommand = echo hi" -> "echo hi").
# KnownHostsCommand verified to behave the same way.
COMMAND_VALUE_KEYS = {
    "proxycommand",
    "remotecommand",
    "localcommand",
    "knownhostscommand",
}


def _glob_match(pattern, text):
    """ssh-style wildcard match: only * and ? are special."""
    regex = "".join(
        ".*" if c == "*" else "." if c == "?" else re.escape(c)
        for c in pattern
    )
    return re.fullmatch(regex, text) is not None


def _match_host(patterns, hostname):
    """Host pattern list semantics: any positive match, no negated match."""
    name = hostname.lower()
    matched = False
    for p in patterns:
        negated = p.startswith("!")
        pat = (p[1:] if negated else p).lower()
        if _glob_match(pat, name):
            if negated:
                return False
            matched = True
    return matched

def _evaluate_match(patterns, hostname, raw=None):
    """
    Expanded Match support.

    Supports:
        Match host PATTERN [PATTERN ...]
        Match user PATTERN [PATTERN ...]
        Match exec COMMAND
    """

    if not patterns:
        return False

    kind = patterns[0].lower()

    if kind == "host":
        return _match_host(patterns[1:], hostname)
    elif kind == "user":
        # In the context of a simple host-selection GUI, we cannot
        # determine the current local user's match against the config
        # without knowing the runtime context. However, we can evaluate
        # this as 'True' if the pattern matches the current system user,
        # or 'False' otherwise. For now, we implement the pattern match
        # logic against the current system user.
        import getpass
        current_user = getpass.getuser()
        return _match_host(patterns[1:], current_user)
    elif kind == "exec":
        # Match exec COMMAND - run the command and match if exit code is 0
        # The command is executed in the user's shell.
        # Use tokenized patterns (quotes already stripped by _tokenize) so
        # pipelines like "uname -s|grep Linux" work correctly.
        import subprocess
        command = " ".join(patterns[1:])
        try:
            # Use shell=True to allow shell features like pipes, etc.
            result = subprocess.run(command, shell=True, capture_output=True, timeout=5)
            return result.returncode == 0
        except (subprocess.TimeoutExpired, OSError, ValueError):
            # On any error (timeout, command not found, etc.), treat as no match
            return False

    raise NotImplementedError(
        f"Unsupported Match criterion '{kind}' "
        f"in Match block: {' '.join(patterns)}"
    )

# ------------------------------------------------------------
# VALUE TOKENIZER (OpenSSH argument semantics)
# ------------------------------------------------------------
# Rules verified against OpenSSH 10.2 (ssh -G differential tests):
#   - unquoted whitespace separates tokens
#   - '...' and "..." are quoted runs: the quotes are stripped and the
#     content is one token (spaces, '#' and '=' are literal inside)
#   - backslash OUTSIDE quotes escapes the next char when it is
#     whitespace, backslash, a double or a single quote; before any other
#     character the backslash is kept literally
#   - backslash INSIDE quotes escapes either quote character (the active
#     one or the other) and backslash; anything else stays literal
#     (verified: "x\'y" -> x'y, 'x\"y' -> x"y, "x\ y" stays literal,
#     "x\ny" stays literal)
#   - unquoted '#' starts a trailing comment only at a token boundary
#     (mid-token, e.g. 'Host a#b' or 'Port 2222#x', it is literal)
#   - an unterminated quote raises ValueError, like OpenSSH's parse error
_ESCAPE_OUTSIDE = " \t\\\"'"


def _tokenize(text):
    """Split an option value into tokens, OpenSSH-style."""
    tokens, cur = [], []
    in_token = False
    quote = None
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if quote is not None:
            # backslash escapes either quote char or backslash inside quotes
            if (c == "\\" and i + 1 < n
                    and text[i + 1] in ("\\", "\"", "'")):
                cur.append(text[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            else:
                cur.append(c)
        elif c in "\"'":
            quote = c
            in_token = True
        elif c == "\\" and i + 1 < n and text[i + 1] in _ESCAPE_OUTSIDE:
            cur.append(text[i + 1])
            in_token = True
            i += 2
            continue
        elif c in " \t":
            if in_token:
                tokens.append("".join(cur))
                cur = []
                in_token = False
        elif c == "#":
            if in_token:
                cur.append(c)        # mid-token '#' is literal
            else:
                break                # trailing comment
        else:
            cur.append(c)
            in_token = True
        i += 1
    if quote is not None:
        raise ValueError("unterminated quote in value: %r" % text)
    if in_token:
        tokens.append("".join(cur))
    return tokens


def _directive_values(key, raw_value):
    """Compute the `values` list for a directive.

    Command-valued directives keep the raw remainder; everything else is
    tokenized (quotes stripped, trailing comments removed)."""
    if key.lower() in COMMAND_VALUE_KEYS:
        return [raw_value]
    return _tokenize(raw_value)


class SSHConfig:
    def __init__(self, nodes=None):
        self.nodes = nodes or []

    # ------------------------------------------------------------
    # PARSER
    # ------------------------------------------------------------
    @staticmethod
    def parse(text, source_file=None):
        lines = text.splitlines()
        nodes = []
        i = 0

        def read_logical_line():
            """Return the next logical line, or None at end of input."""
            nonlocal i
            if i >= len(lines):
                return None
            line_number = i + 1
            raw = lines[i]
            i += 1
            leading = raw[:len(raw) - len(raw.lstrip())]

            if raw.strip().startswith("#"):
                return {"type": "comment", "text": raw, "line": line_number}

            buf = raw
            # Continuation lines
            while buf.rstrip().endswith("\\") and i < len(lines):
                buf = buf.rstrip()[:-1] + " " + lines[i].strip()
                i += 1
            if buf.rstrip().endswith("\\"):
                buf = buf.rstrip()[:-1]

            return {"type": "line", "text": buf, "leading": leading, "line": line_number}

        def parse_kv(text):
            # Accepts "Key value", "Key = value" and "Key=value"
            m = re.match(r"([^\s=]+)\s*=?\s*(.*)$", text.strip())
            if not m:
                return None
            return {"key": m.group(1), "value": m.group(2).strip()}

        def parse_block_body(parent_type="host"):
            """
            Parse a block body (Host or Match).

            parent_type: "host" or "match" - determines terminator behavior:
              - Inside Host: "host" terminates, "match" nests
              - Inside Match: "host" terminates, "match" terminates (sibling)
            """
            nonlocal i
            body = []
            while True:
                start = i
                logical = read_logical_line()
                if logical is None:
                    break

                if logical["type"] == "comment":
                    body.append(SSHConfigNode("comment", text=logical["text"], source_file=source_file,source_line=logical["line"],))
                    continue

                if not logical["text"].strip():
                    body.append(SSHConfigNode("blank", text="", source_file=source_file, source_line=logical["line"],))
                    continue

                kv = parse_kv(logical["text"])
                if kv is None:
                    continue

                key_lower = kv["key"].lower()

                # Host always terminates any block
                if key_lower == "host":
                    i = start
                    break

                # Match terminates Match bodies (sibling Match), but nests in Host bodies
                if key_lower == "match":
                    if parent_type == "match":
                        i = start
                        break
                    # parent_type == "host": nest the Match
                    body.append(SSHConfigNode(
                        "host",
                        keyword=kv["key"],
                        patterns=_tokenize(kv["value"]),
                        raw=kv["value"],
                        body=parse_block_body(parent_type="match"),  # recurse with match as parent
                        leading=logical["leading"],
                        group=None,
                        source_file=source_file,
                        source_line=logical["line"],
                    ))
                    continue

                body.append(SSHConfigNode(
                    "directive",
                    key=kv["key"],
                    values=_directive_values(kv["key"], kv["value"]),
                    raw=kv["value"],
                    leading=logical["leading"],
                    source_file=source_file,
                    source_line=logical["line"],
                ))
            return body

        # MAIN PARSE LOOP
        while True:
            logical = read_logical_line()
            if logical is None:
                break

            if logical["type"] == "comment":
                nodes.append(SSHConfigNode("comment", text=logical["text"]))
                continue

            if not logical["text"].strip():
                nodes.append(SSHConfigNode("blank", text=""))
                continue

            kv = parse_kv(logical["text"])
            if kv is None:
                continue

            if kv["key"].lower() in ("host", "match"):
                parent_type = "host" if kv["key"].lower() == "host" else "match"
                nodes.append(SSHConfigNode(
                    "host",
                    keyword=kv["key"],
                    patterns=_tokenize(kv["value"]),   # quotes stripped
                    raw=kv["value"],
                    body=parse_block_body(parent_type=parent_type),
                    leading=logical["leading"],
                    group=None,
                    source_file=source_file,
                    source_line=logical["line"],
                ))
            else:
                # Global directive (including Include — top-level ones are
                # resolved by resolve_includes like before)
                nodes.append(SSHConfigNode(
                    "directive",
                    key=kv["key"],
                    values=_directive_values(kv["key"], kv["value"]),
                    raw=kv["value"],
                    leading=logical["leading"],
                ))

        return SSHConfig(nodes)

    # ------------------------------------------------------------
    # INCLUDE RESOLUTION (block-aware)
    # ------------------------------------------------------------
    # Instead of splicing included content at top level, every Include
    # directive — wherever it appeared — becomes an "include" node that
    # keeps its position and holds the *parsed* content of the matched
    # files. No flattening: evaluation decides what applies.
    #
    # `files` is a list of per-file node lists (one list per matched file,
    # in lexical/glob order), NOT one concatenated list: OpenSSH enters
    # each included file with the *caller's* section state, so a file's
    # trailing section must never swallow the next file's preamble.
    #
    # The original directive fields (key/values/leading) are kept on the
    # node so a serializer can still re-emit the original Include line.
    def resolve_includes(self, include_root=None, chain=None):
        # OpenSSH resolves *relative* include paths against ~/.ssh for user
        # configuration files and /etc/ssh for the system configuration —
        # never against the including file's directory. `include_root` makes
        # that root explicit; system-config callers pass "/etc/ssh".
        if include_root is None:
            # Default logic: if we are in /etc/ssh, use that, otherwise use ~/.ssh
            # This is a heuristic based on the source file of the first node
            if self.nodes and hasattr(self.nodes[0], 'source_file'):
                if self.nodes[0].source_file.startswith('/etc/ssh'):
                    include_root = '/etc/ssh'
                else:
                    include_root = os.path.expanduser("~/.ssh")
            else:
                include_root = os.path.expanduser("~/.ssh")

        chain = chain or frozenset()
        self.nodes = self._resolve_nodes(self.nodes, include_root, chain)
        return self

    def _resolve_nodes(self, nodes, include_root, chain):
        out = []
        for node in nodes:
            if node.type == "directive" and node.key.lower() == "include":
                out.append(self._expand_include(node, include_root, chain))
            elif node.type == "host":
                # Includes inside Host/Match bodies are resolved in place.
                node.body = self._resolve_nodes(node.body, include_root, chain)
                out.append(node)
            else:
                out.append(node)
        return out

    def _expand_include(self, node, include_root, chain):
        inc = SSHConfigNode(
            "include",
            key=node.key, values=node.values, leading=node.leading,
            raw=getattr(node, "raw", None),
            files=[],
        )
        # values are already tokenized (quotes stripped, comments removed),
        # so each token is one include path
        for pattern in list(node.values):
            full = os.path.expanduser(pattern)
            if not os.path.isabs(full):
                full = os.path.join(include_root, full)

            # Support for directory includes: if the pattern is a directory,
            # OpenSSH (and this app) should include all files within it.
            # Note: OpenSSH's Include directive typically takes files/globs.
            # To support a folder like 'config.d/', we handle it via glob.
            if os.path.isdir(full):
                # Include all files in the directory
                matches = sorted(glob.glob(os.path.join(full, "*")))
            else:
                matches = sorted(glob.glob(full))

            for match in matches:
                real = os.path.realpath(match)
                if real in chain or not os.path.isfile(real):
                    continue

                with open(real, "r", encoding="utf-8",
                          errors="replace") as f:
                    text = f.read()

                sub = SSHConfig.parse(text,source_file=real)
                file_nodes = self._resolve_nodes(
                    sub.nodes, include_root, chain | {real})
                inc.files.append(file_nodes)
        return inc

    # ------------------------------------------------------------
    # GROUP ASSIGNMENT (#GroupStart / #GroupEnd)
    # ------------------------------------------------------------
    def assign_groups(self):
        """Walk the flattened node tree in order — including the content
        of resolved Includes — so hosts from included files get groups
        too, in source order."""
        current = None

        def scan(text):
            nonlocal current
            tokens = text.strip().split()
            # Compare the first token exactly: "#GroupStartExtra" must not
            # open a group.
            if tokens[:1] == ["#GroupStart"]:
                current = " ".join(tokens[1:]) or "ungrouped"
            elif tokens[:1] == ["#GroupEnd"]:
                current = None

        def walk(nodes):
            nonlocal current
            for node in nodes:
                if node.type == "comment":
                    scan(node.text)
                elif node.type == "include":
                    for file_nodes in node.files:
                        walk(file_nodes)
                elif node.type == "host":
                    node.group = current
                    walk(node.body)
        walk(self.nodes)
        return self


    def find_host_node(self, hostname):
        def walk(nodes):
            for node in nodes:

                if node.type == "host":
                    if (
                        node.keyword.lower() == "host"
                        and _match_host(node.patterns, hostname)
                    ):
                        return node

                    result = walk(node.body)
                    if result:
                        return result

                elif node.type == "include":
                    for file_nodes in node.files:
                        result = walk(file_nodes)
                        if result:
                            return result

            return None

        return walk(self.nodes)
    # ------------------------------------------------------------
    # MERGED CONFIG FOR HOST (preview of what ssh will use)
    # ------------------------------------------------------------
    def get_config_for_host(self, hostname):
        """OpenSSH semantics, now with block-aware Include evaluation:

        - walk the config in order, the first value obtained for a key
          wins; IdentityFile and similar keys accumulate;
        - a Host/Match section RESETS the condition for its own body;
        - an Include INHERITS the enclosing condition: directives in the
          included file's preamble are scoped to the enclosing block, and
          if the enclosing block does not match, the Include is skipped
          entirely (OpenSSH's NEVERMATCH behaviour — sections inside an
          included file cannot "reactivate" matching from an inactive
          Include);
        - each included file is entered with the caller's active state,
          so a file ending in a non-matching section does not swallow
          the next file's (or the caller's) directives.
        """
        result = {}

        def apply_directive(node):
            key = node.key.lower()
            if key == "include":
                return  # handled structurally, never as a value

            if key == "sendenv":
                # Token-level accumulation with '-NAME' removal (verified:
                # OpenSSH treats each whitespace-separated name as its own
                # entry; '-NAME' removes by glob pattern, CASE-SENSITIVELY —
                # 'SendEnv LANG lang' + 'SendEnv -lang' keeps LANG).
                result.setdefault(key, [])
                for tok in node.values:
                    if tok.startswith("-") and len(tok) > 1:
                        pat = tok[1:]
                        result[key] = [v for v in result[key]
                                       if not _glob_match(pat, v)]
                    else:
                        result[key].append(tok)
            elif key in MULTI_VALUE_KEYS:
                # One entry per directive line: the tokens of the line are
                # the single value (e.g. "LocalForward 8080 localhost:80").
                result.setdefault(key, [])
                result[key].append(" ".join(node.values))
            elif key not in result:
                # Scalar: first value wins; tokens joined (usually 1).
                result[key] = " ".join(node.values)

        def walk(nodes, active):
            for node in nodes:
                if node.type == "directive":
                    if active:
                        apply_directive(node)

                elif node.type == "host":
                    # If we're already inactive (parent Host didn't match),
                    # skip this block entirely — don't evaluate nested conditions.
                    if not active:
                        continue

                    keyword = node.keyword.lower()

                    if keyword == "host":

                        cond = _match_host(
                            node.patterns,
                            hostname
                        )

                    elif keyword == "match":

                        cond = _evaluate_match(
                            node.patterns,
                            hostname,
                            getattr(node, "raw", None)
                        )

                    else:

                        raise NotImplementedError(
                            f"Unknown section type: {node.keyword}"
                        )

                    # A section resets the condition for its own body.
                    walk(node.body, cond)

                elif node.type == "include":
                    # Conditional inclusion: skip entirely when inactive.
                    if active:
                        # Each file is entered with the caller's state.
                        for file_nodes in node.files:
                            walk(file_nodes, True)

        walk(self.nodes, True)
        return result


# ------------------------------------------------------------
# ENTRY POINT
# ------------------------------------------------------------
def parse_ssh_config_file(path):
    real = os.path.realpath(os.path.expanduser(path))
    with open(real, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()

    cfg = SSHConfig.parse(text, source_file=real)
    # Relative includes resolve against ~/.ssh (user config; pass
    # "/etc/ssh" when parsing a system configuration).
    cfg.resolve_includes(chain=frozenset({real}))
    cfg.assign_groups()
    return cfg

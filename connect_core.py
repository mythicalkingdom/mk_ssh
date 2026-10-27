import os
import platform
import shlex
import shutil
import subprocess
import stat
from pathlib import Path
from ssh_config import parse_ssh_config_file

# ------------------------------------------------------------
# PATH EXPANSION
# ------------------------------------------------------------
def expand_home(p):
    if not p:
        return p
    home = str(Path.home())
    return (
        p.replace("~", home)
         .replace("$HOME", home)
         .replace("%USERPROFILE%", home)
    )


# ------------------------------------------------------------
# LOAD CONFIG
# ------------------------------------------------------------
def load_config(config_path=None):
    if config_path is None:
        # 1. Try user's primary config
        user_config = os.path.join(str(Path.home()), ".ssh", "config")
        if os.path.exists(user_config):
            config_path = user_config
        else:
            # 2. Fallback to system-wide config
            system_config = "/etc/ssh/ssh_config"
            if os.path.exists(system_config):
                config_path = system_config
            else:
                # Fallback to home path anyway to let parser handle the "not found" error
                config_path = user_config

    resolved = expand_home(config_path)
    # Resolve symlinks to ensure that we have the real path for -F and permission checks
    resolved = os.path.realpath(resolved)
    try:
        cfg = parse_ssh_config_file(resolved)
    except ValueError as e:
        # Malformed config (e.g. unterminated quote) — the parser now
        # rejects these loudly instead of silently mis-parsing.
        raise RuntimeError(f"Could not parse {resolved}: {e}") from e
    return cfg, resolved


# ------------------------------------------------------------
# LIST HOSTS
# ------------------------------------------------------------
def _iter_host_nodes(nodes):
    """Yield Host nodes in source order, recursing into Includes
    (including Includes inside Host blocks) — the parser keeps included
    content nested under include nodes, so a flat walk of cfg.nodes
    would miss every host from an included file."""
    for node in nodes:
        if node.type == "host":
            yield node
            yield from _iter_host_nodes(node.body)
        elif node.type == "include":
            for file_nodes in node.files:
                yield from _iter_host_nodes(file_nodes)


def list_hosts(cfg):
    hosts = []
    for node in _iter_host_nodes(cfg.nodes):
        if node.keyword.lower() != "host":
            continue
        for p in node.patterns:
            # Skip patterns that are not selectable aliases: negations,
            # wildcards and the catch-all can't be connected to directly.
            if p == "*" or p.startswith("!") or "*" in p or "?" in p:
                continue
            hosts.append({
                "name": p,
                "group": getattr(node, "group", None)
            })
    hosts.sort(key=lambda h: h["name"].lower())
    return hosts


# ------------------------------------------------------------
# BUILD SSH ARGUMENTS
# ------------------------------------------------------------
def build_ssh_args(cfg, host, config_path=None):
    try:
        effective = cfg.get_config_for_host(host)
    except NotImplementedError as e:
        # The config uses Match criteria this preview can't evaluate
        # (anything except "Match all"). Rather than build possibly-wrong
        # arguments, hand the connection to ssh itself with -F so it uses
        # the SAME config file (not its default one) and evaluates
        # everything under the original alias.
        print(f"warning: {e}")
        print(f"warning: delegating to ssh with -F {config_path} {host}")
        if config_path:
            return ["-F", config_path, host], {}
        return [host], {}

    # Use -F /dev/null to prevent SSH from reading ~/.ssh/config or any
    # other config file. The Python parser has already fully evaluated the
    # config (including Match exec) and we pass all options explicitly.
    args = ["-F", "/dev/null"]

    # Flatten nested lists
    def flatten(val):
        if isinstance(val, list):
            out = []
            for v in val:
                if isinstance(v, list):
                    out.extend(v)
                else:
                    out.append(v)
            return out
        return val

    # Apply flattening to all values
    clean = {}
    for k, v in effective.items():
        clean[k] = flatten(v)
    effective = clean

    # Hostname (args already has -F /dev/null)
    host_name = effective.get("hostname", host)
    if isinstance(host_name, list):
        host_name = host
    args.append(str(host_name))

    # User
    user = effective.get("user")
    if isinstance(user, list):
        user = user[0] if user else None
    if user:
        args.extend(["-l", str(user)])

    # Port
    port = effective.get("port")
    if isinstance(port, list):
        port = port[0] if port else None
    if port:
        args.extend(["-p", str(port)])

    # IdentityFile
    ids = effective.get("identityfile")
    if ids:
        if not isinstance(ids, list):
            ids = [ids]
        for id_ in ids:
            expanded_id = expand_home(str(id_))
            args.extend(["-i", expanded_id])

    # ProxyJump
    proxyjump = effective.get("proxyjump")
    if isinstance(proxyjump, list):
        proxyjump = proxyjump[0] if proxyjump else None
    if proxyjump:
        args.extend(["-J", str(proxyjump)])

    # Boolean flags
    if effective.get("forwardagent") == "yes":
        args.append("-A")
    if effective.get("compression") == "yes":
        args.append("-C")

    # StrictHostKeyChecking
    shk = effective.get("stricthostkeychecking")
    if isinstance(shk, list):
        shk = shk[0] if shk else None
    if shk:
        args.extend(["-o", f"StrictHostKeyChecking={shk}"])

    # UserKnownHostsFile
    ukh = effective.get("userknownhostsfile")
    if ukh:
        if not isinstance(ukh, list):
            ukh = [ukh]
        for f in ukh:
            args.extend(["-o", f"UserKnownHostsFile={expand_home(str(f))}"])

    # Remaining -o options
    skip = {
        "hostname", "user", "port", "identityfile", "proxyjump",
        "forwardagent", "compression", "stricthostkeychecking",
        "userknownhostsfile"
    }

    for key, val in effective.items():
        if key in skip or not val:
            continue
        vals = val if isinstance(val, list) else [val]
        for v in vals:
            # Pass value as-is; subprocess list args don't need shell quoting
            args.extend(["-o", f"{key}={v}"])

    return args, effective


# ------------------------------------------------------------
# LAUNCH SSH (cross-platform)
# ------------------------------------------------------------
def _applescript_escape(s):
    """Escape a string for an AppleScript double-quoted literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _find_best_terminal():
    """
    Dynamically detect the best available terminal on Linux.
    Returns a tuple of (terminal_name, launch_command_prefix).
    """
    # Priority ordered list of terminals and their specific launch flags.
    # Each entry: (name, command_prefix, keep_open_args)
    terminals = [
        ("gnome-terminal", ["gnome-terminal", "--"], ["--", "bash", "-c"]),  # special handling
        ("konsole", ["konsole", "--noclose", "-e"], []),
        ("xfce4-terminal", ["xfce4-terminal", "--hold", "--command"], []),
        ("mate-terminal", ["mate-terminal", "--hold", "--command"], []),
        ("alacritty", ["alacritty", "-e"], []),
        ("kitty", ["kitty"], []),
        ("tilix", ["tilix", "-e"], []),
        ("lxterminal", ["lxterminal", "-e"], []),
        ("xterm", ["xterm", "-hold", "-e"], []),
    ]

    for name, cmd, _ in terminals:
        if shutil.which(cmd[0]):
            # Special handling for gnome-terminal: need to wrap in bash -c to keep open
            if name == "gnome-terminal":
                return name, cmd  # will be handled specially in launch_ssh
            return name, cmd

    return None, None


def launch_ssh(host, cfg, config_path=None):
    args, effective = build_ssh_args(cfg, host, config_path)
    ssh_bin = "ssh"
    system = platform.system().lower()

    print("Launching:", system, ssh_bin, args)

    # Normalize Linux detection
    if "linux" in system:
        term_name, term_cmd = _find_best_terminal()
        if term_name:
            # Build the SSH command string
            ssh_cmd = [ssh_bin, "-tt", *args]

            if term_name == "gnome-terminal":
                # gnome-terminal doesn't have a keep-open flag; wrap in bash -c
                # Use -- to pass the command, then bash -c 'cmd; exec bash'
                cmd_str = " ".join(shlex.quote(a) for a in ssh_cmd)
                subprocess.Popen(term_cmd + ["bash", "-c", f"{cmd_str}; exec bash"])
            else:
                # Other terminals: use their keep-open flags (already in term_cmd)
                subprocess.Popen(term_cmd + ssh_cmd)
            return

        raise RuntimeError("No supported terminal found on this Linux system. Please install gnome-terminal, xterm, or alacritty.")

    # macOS: the command goes through a shell AND an AppleScript string
    # literal, so it needs two levels of quoting.
    elif system == "darwin":
        quoted = [shlex.quote(a) for a in args]
        cmd = f"{ssh_bin} -tt {' '.join(quoted)}"
        subprocess.Popen([
            "osascript",
            "-e",
            'tell application "Terminal" to do script "'
            + _applescript_escape(cmd) + '"'
        ])
        return

    # Windows
    elif system == "windows":
        subprocess.Popen(["wt.exe", "ssh", *args])
        return

    else:
        raise RuntimeError("Unsupported platform")

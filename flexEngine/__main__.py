#**************************************************************************
# * Copyright (C) 2025-present Bert Van Acker (B.MKR) <bva.bmkr@gmail.com>
# *
# * This file is part of the flexIA project.
# *
# * flexIA can not be copied and/or distributed without the express
# * permission of Bert Van Acker
# *************************************************************************
import argparse
import importlib.resources
import socket
import sys
from pathlib import Path

import yaml

from flexEngine import __version__
from flexEngine.security.security import generate_shared_secret

WORKSPACE_SUBDIRS = ("apps", "configuration", "logs")


def _read_template(name):
    return importlib.resources.files("flexEngine").joinpath("templates", name).read_text()


def _prompt(text, default):
    answer = input(f"{text} [{default}]: ").strip()
    return answer or default


def _collect_config_values(non_interactive):
    defaults = {
        "device_name": "flexEngine-agent",
        "primary_hardware_id": socket.gethostname(),
        "project_id": "changeme-project",
        "device_key": "changeme-key",
        "redis_host": "localhost",
        "redis_port": "6379",
        "redis_db": "0",
        "heartbeat_interval": "10",
        "heartbeat_ttl": "12",
        "log_level": "INFO",
        "log_remote": "True",
    }

    if non_interactive:
        values = dict(defaults)
    else:
        print("Configure this agent (press Enter to accept the default).")
        values = {
            "device_name": _prompt("Device name", defaults["device_name"]),
            "primary_hardware_id": _prompt("Primary hardware ID", defaults["primary_hardware_id"]),
            "project_id": _prompt("flexCloud project ID", defaults["project_id"]),
            "device_key": _prompt("flexCloud device key", defaults["device_key"]),
            "redis_host": _prompt("Redis host", defaults["redis_host"]),
            "redis_port": _prompt("Redis port", defaults["redis_port"]),
            "redis_db": _prompt("Redis DB index", defaults["redis_db"]),
            "heartbeat_interval": _prompt("Heartbeat interval (s)", defaults["heartbeat_interval"]),
            "heartbeat_ttl": _prompt("Heartbeat TTL (s)", defaults["heartbeat_ttl"]),
            "log_level": _prompt("Log level", defaults["log_level"]),
            "log_remote": "True" if _prompt("Enable remote logging via flexCloud? (y/n)", "y").lower().startswith("y") else "False",
        }

    values["shared_secret"] = repr(generate_shared_secret(32))
    return values


def _render_config(values):
    content = _read_template("config.yaml.tmpl")
    for key, value in values.items():
        content = content.replace("{{" + key + "}}", str(value))
    return content


def cmd_init(args):
    target = Path(args.path).resolve()
    target.mkdir(parents=True, exist_ok=True)

    existing = [p for p in target.iterdir()] if target.exists() else []
    if existing and not args.force:
        print(
            f"error: '{target}' is not empty. Use --force to scaffold into it anyway.",
            file=sys.stderr,
        )
        return 1

    for subdir in WORKSPACE_SUBDIRS:
        (target / subdir).mkdir(parents=True, exist_ok=True)

    (target / "agent.py").write_text(_read_template("agent.py.tmpl"))

    config_path = target / "configuration" / "config.yaml"
    if args.config:
        source = Path(args.config).expanduser().resolve()
        try:
            with open(source, "r") as f:
                yaml.safe_load(f)
        except (OSError, yaml.YAMLError) as exc:
            print(f"error: could not read '{source}' as YAML: {exc}", file=sys.stderr)
            return 1
        config_path.write_text(source.read_text())
    else:
        values = _collect_config_values(args.non_interactive)
        config_path.write_text(_render_config(values))

    print(f"\nWorkspace ready at: {target}")
    print(f"  apps/            -> deployable applications")
    print(f"  configuration/   -> {config_path.name}")
    print(f"  logs/            -> runtime logs")
    print(f"  agent.py         -> entry point")
    print("\nNext steps:")
    print(f"  cd {target}")
    print("  python agent.py")
    print("\nNote: flexEngine connects to Redis on startup - make sure it is reachable")
    print("(see flexCloud.redis_host/redis_port in configuration/config.yaml).")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="flexEngine", description="flexEngine agent CLI")
    parser.add_argument("--version", action="version", version=f"flexEngine {__version__}")

    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser("init", help="scaffold a new agent workspace")
    init_parser.add_argument("path", nargs="?", default=".", help="workspace directory (default: current directory)")
    init_parser.add_argument("--config", metavar="FILE", help="use an existing config.yaml instead of the interactive prompts")
    init_parser.add_argument("--non-interactive", action="store_true", help="skip prompts and use defaults")
    init_parser.add_argument("--force", action="store_true", help="scaffold even if the target directory is not empty")
    init_parser.set_defaults(func=cmd_init)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not getattr(args, "command", None):
        parser.print_help()
        return 1

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

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
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import psutil
import redis
import yaml

from flexEngine import __version__
from flexEngine.security.security import generate_shared_secret
from flexEngine.utils.auxiliary import load_config

WORKSPACE_SUBDIRS = ("apps", "configuration", "logs")
DEFAULT_LOG_FILE = "logs/syslog_local.log"
PID_FILE_NAME = "agent.pid"
STOP_FILE_NAME = "agent.stop"
STDOUT_LOG_NAME = "logs/agent.out.log"
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
CONFIG_SECTIONS = {
    "logging": ("level", "file", "remote"),
    "flexCloud": ("redis_host", "redis_port", "redis_db"),
    "deviceCredentials": ("name", "primaryHardwareID", "projectID", "key", "shared_secret"),
    "heartbeat": ("heartbeat_interval", "ttl"),
}
PLACEHOLDER_VALUES = {"changeme-project", "changeme-key", "UNKNOWN"}


def _read_template(name):
    return importlib.resources.files("flexEngine").joinpath("templates", name).read_text()


def _prompt(text, default):
    answer = input(f"{text} [{default}]: ").strip()
    return answer or default


DEFAULT_CONFIG_VALUES = {
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


def _collect_config_values(non_interactive, defaults=None):
    defaults = {**DEFAULT_CONFIG_VALUES, **(defaults or {})}

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
    print("  python agent.py       # run in the foreground, or:")
    print("  flexEngine start      # run in the background (see `flexEngine stop`/`status`)")
    print("\nNote: flexEngine connects to Redis on startup - make sure it is reachable")
    print("(see flexCloud.redis_host/redis_port in configuration/config.yaml).")
    return 0


def _pid_file(workspace):
    return workspace / PID_FILE_NAME


def _stop_file(workspace):
    return workspace / STOP_FILE_NAME


def _read_pid(workspace):
    pid_file = _pid_file(workspace)
    if not pid_file.exists():
        return None
    try:
        return int(pid_file.read_text().strip())
    except ValueError:
        return None


def _is_agent_running(pid):
    """True if `pid` is alive and looks like one of our agent.py processes.

    Checking cmdline (not just pid_exists) guards against a stale pid file
    whose pid has since been recycled by an unrelated process.
    """
    if pid is None or not psutil.pid_exists(pid):
        return False
    try:
        return "agent.py" in " ".join(psutil.Process(pid).cmdline())
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def cmd_start(args):
    workspace = Path(args.path).resolve()
    agent_script = workspace / "agent.py"
    if not agent_script.exists():
        print(f"error: no agent.py found at '{workspace}'. Run `flexEngine init` first.", file=sys.stderr)
        return 1

    pid = _read_pid(workspace)
    if _is_agent_running(pid):
        print(f"agent already running (pid {pid}).")
        return 0

    _stop_file(workspace).unlink(missing_ok=True)

    log_path = workspace / STDOUT_LOG_NAME
    log_path.parent.mkdir(parents=True, exist_ok=True)

    popen_kwargs = dict(cwd=str(workspace), stdin=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        popen_kwargs["start_new_session"] = True  # detach from the controlling terminal (like setsid)

    with open(log_path, "a") as log_handle:
        process = subprocess.Popen([sys.executable, str(agent_script)], stdout=log_handle, **popen_kwargs)
    _pid_file(workspace).write_text(str(process.pid))

    time.sleep(0.5)
    if not _is_agent_running(process.pid):
        _pid_file(workspace).unlink(missing_ok=True)
        print(f"error: agent exited immediately - check {log_path}", file=sys.stderr)
        return 1

    print(f"agent started in the background (pid {process.pid}).")
    print(f"  flexEngine status {args.path}   -> check it's running")
    print(f"  flexEngine logs -f {args.path}  -> follow its logs")
    print(f"  flexEngine stop {args.path}     -> stop it")
    return 0


def cmd_stop(args):
    workspace = Path(args.path).resolve()
    pid = _read_pid(workspace)

    if not _is_agent_running(pid):
        print("agent is not running.")
        _pid_file(workspace).unlink(missing_ok=True)
        _stop_file(workspace).unlink(missing_ok=True)
        return 0

    print(f"stopping agent (pid {pid})...")
    _stop_file(workspace).write_text("")

    deadline = time.time() + args.timeout
    while time.time() < deadline and _is_agent_running(pid):
        time.sleep(0.2)

    if _is_agent_running(pid):
        print(f"agent did not stop within {args.timeout}s - killing it.", file=sys.stderr)
        try:
            psutil.Process(pid).kill()
        except psutil.NoSuchProcess:
            pass

    _pid_file(workspace).unlink(missing_ok=True)
    _stop_file(workspace).unlink(missing_ok=True)
    print("agent stopped.")
    return 0


def cmd_status(args):
    workspace = Path(args.path).resolve()
    pid = _read_pid(workspace)

    if not _is_agent_running(pid):
        print("agent is not running.")
        return 1

    uptime = time.time() - psutil.Process(pid).create_time()
    print(f"agent running (pid {pid}), uptime {_format_uptime(uptime)}.")
    return 0


def _gb(n):
    return n / (1024 ** 3)


def _format_uptime(seconds):
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, _ = divmod(seconds, 60)
    parts = [f"{days}d"] if days else []
    parts += [f"{hours}h", f"{minutes}m"]
    return " ".join(parts)


def _collect_stats():
    return {
        "cpu_percent": psutil.cpu_percent(interval=0.5),
        "per_cpu": psutil.cpu_percent(interval=None, percpu=True),
        "mem": psutil.virtual_memory(),
        "disk": psutil.disk_usage("/"),
        "net": psutil.net_io_counters(),
        "uptime": time.time() - psutil.boot_time(),
    }


def _render_stats(stats):
    mem, disk, net = stats["mem"], stats["disk"], stats["net"]
    per_core = " ".join(f"{c:.0f}%" for c in stats["per_cpu"])
    lines = [
        "flexEngine device stats",
        "=" * 40,
        f"CPU load:    {stats['cpu_percent']:.1f}%  (per core: {per_core})",
        f"Memory:      {mem.percent:.1f}%  ({_gb(mem.used):.2f} / {_gb(mem.total):.2f} GB used)",
        f"Storage (/): {disk.percent:.1f}%  ({_gb(disk.used):.2f} / {_gb(disk.total):.2f} GB used)",
        f"Network:     sent {_gb(net.bytes_sent):.2f} GB / recv {_gb(net.bytes_recv):.2f} GB (since boot)",
        f"Uptime:      {_format_uptime(stats['uptime'])}",
        "",
        f"updated {time.strftime('%Y-%m-%d %H:%M:%S')}",
    ]
    return "\n".join(lines)


def cmd_stats(args):
    if args.once:
        print(_render_stats(_collect_stats()))
        return 0

    print("Watching device stats. Press Ctrl+C to exit.")
    try:
        while True:
            report = _render_stats(_collect_stats())
            sys.stdout.write("\033[2J\033[H" + report + "\n")
            sys.stdout.flush()
            time.sleep(max(args.interval - 0.5, 0.1))  # cpu_percent() above already blocks ~0.5s
    except KeyboardInterrupt:
        print()
    return 0


def _resolve_log_file(workspace):
    config_path = workspace / "configuration" / "config.yaml"
    log_file = DEFAULT_LOG_FILE
    if config_path.exists():
        try:
            log_file = load_config(config_path).get("logging", {}).get("file", DEFAULT_LOG_FILE)
        except (OSError, yaml.YAMLError):
            pass

    log_path = Path(log_file)
    return log_path if log_path.is_absolute() else workspace / log_path


def cmd_logs(args):
    workspace = Path(args.path).resolve()
    log_path = _resolve_log_file(workspace)

    if not log_path.exists():
        print(f"error: log file not found at '{log_path}'.", file=sys.stderr)
        print("       is the agent running, and does the path point at its workspace?", file=sys.stderr)
        return 1

    level_filter = args.level.upper() if args.level else None

    def _matches(line):
        return level_filter is None or f" {level_filter} " in line

    with open(log_path, "r") as f:
        tail = [line for line in f.readlines()[-args.lines:] if _matches(line)]
        sys.stdout.writelines(tail)
        sys.stdout.flush()

        if not args.follow:
            return 0

        print(f"-- following {log_path} (Ctrl+C to stop) --")
        try:
            while True:
                line = f.readline()
                if not line:
                    time.sleep(0.5)
                    continue
                if _matches(line):
                    sys.stdout.write(line)
                    sys.stdout.flush()
        except KeyboardInterrupt:
            print()
    return 0


def _config_path(workspace):
    return workspace / "configuration" / "config.yaml"


def _load_workspace_config(config_path):
    with open(config_path, "r") as f:
        return yaml.safe_load(f) or {}


def cmd_config_check(args):
    workspace = Path(args.path).resolve()
    config_path = _config_path(workspace)

    if not config_path.exists():
        print(f"error: no config.yaml found at '{config_path}'. Run `flexEngine init` first.", file=sys.stderr)
        return 1

    try:
        config = _load_workspace_config(config_path)
    except yaml.YAMLError as exc:
        print(f"error: '{config_path}' is not valid YAML: {exc}", file=sys.stderr)
        return 1

    problems = []
    for section, keys in CONFIG_SECTIONS.items():
        if section not in config:
            problems.append(f"missing section '{section}'")
            continue
        for key in keys:
            if key not in config[section]:
                problems.append(f"missing '{section}.{key}'")

    device = config.get("deviceCredentials", {})
    for key in ("projectID", "key"):
        value = str(device.get(key, ""))
        if value in PLACEHOLDER_VALUES:
            problems.append(f"deviceCredentials.{key} is still a placeholder ('{value}') - run `flexEngine config edit`")

    flexcloud = config.get("flexCloud", {})
    heartbeat = config.get("heartbeat", {})
    logging_cfg = config.get("logging", {})

    print(f"Configuration: {config_path}")
    print("-" * 40)
    print(f"device name:         {device.get('name', '?')}")
    print(f"primary hardware ID: {device.get('primaryHardwareID', '?')}")
    print(f"project ID:          {device.get('projectID', '?')}")
    print(f"redis:               {flexcloud.get('redis_host', '?')}:{flexcloud.get('redis_port', '?')} (db {flexcloud.get('redis_db', '?')})")
    print(f"heartbeat:           every {heartbeat.get('heartbeat_interval', '?')}s, ttl {heartbeat.get('ttl', '?')}s")
    print(f"log level:           {logging_cfg.get('level', '?')} -> {logging_cfg.get('file', '?')} (remote={logging_cfg.get('remote', '?')})")
    print()

    redis_host = flexcloud.get("redis_host", "localhost")
    redis_port = flexcloud.get("redis_port", 6379)
    redis_db = flexcloud.get("redis_db", 0)
    try:
        redis.StrictRedis(
            host=redis_host, port=redis_port, db=redis_db, socket_connect_timeout=2
        ).ping()
        print(f"redis connectivity:  OK ({redis_host}:{redis_port})")
    except redis.RedisError as exc:
        problems.append(f"cannot reach redis at {redis_host}:{redis_port} ({exc})")

    if problems:
        print("\nProblems found:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    print("\nConfiguration looks good.")
    return 0


def _config_defaults_from(config):
    device = config.get("deviceCredentials", {})
    flexcloud = config.get("flexCloud", {})
    heartbeat = config.get("heartbeat", {})
    logging_cfg = config.get("logging", {})
    return {
        "device_name": device.get("name", DEFAULT_CONFIG_VALUES["device_name"]),
        "primary_hardware_id": device.get("primaryHardwareID", DEFAULT_CONFIG_VALUES["primary_hardware_id"]),
        "project_id": device.get("projectID", DEFAULT_CONFIG_VALUES["project_id"]),
        "device_key": device.get("key", DEFAULT_CONFIG_VALUES["device_key"]),
        "redis_host": flexcloud.get("redis_host", DEFAULT_CONFIG_VALUES["redis_host"]),
        "redis_port": str(flexcloud.get("redis_port", DEFAULT_CONFIG_VALUES["redis_port"])),
        "redis_db": str(flexcloud.get("redis_db", DEFAULT_CONFIG_VALUES["redis_db"])),
        "heartbeat_interval": str(heartbeat.get("heartbeat_interval", DEFAULT_CONFIG_VALUES["heartbeat_interval"])),
        "heartbeat_ttl": str(heartbeat.get("ttl", DEFAULT_CONFIG_VALUES["heartbeat_ttl"])),
        "log_level": logging_cfg.get("level", DEFAULT_CONFIG_VALUES["log_level"]),
        "log_remote": str(logging_cfg.get("remote", DEFAULT_CONFIG_VALUES["log_remote"])),
        "shared_secret": device.get("shared_secret"),
    }


def cmd_config_edit(args):
    workspace = Path(args.path).resolve()
    config_path = _config_path(workspace)

    if not config_path.exists():
        print(f"error: no config.yaml found at '{config_path}'. Run `flexEngine init` first.", file=sys.stderr)
        return 1

    try:
        config = _load_workspace_config(config_path)
    except yaml.YAMLError as exc:
        print(f"error: '{config_path}' is not valid YAML: {exc}", file=sys.stderr)
        return 1

    current = _config_defaults_from(config)

    print(f"Editing {config_path} (press Enter to keep the current value).")
    values = _collect_config_values(non_interactive=False, defaults=current)

    if args.regenerate_secret:
        values["shared_secret"] = repr(generate_shared_secret(32))
        print("note: generated a new shared secret - the device will need to re-register with flexCloud.")
    elif current["shared_secret"] is not None:
        # current["shared_secret"] was read verbatim from the existing config.yaml,
        # where it's already stored in its final repr()-serialized form - reuse it
        # as-is rather than repr()-ing it again (which would double-escape it).
        values["shared_secret"] = current["shared_secret"]

    config_path.write_text(_render_config(values))
    print(f"\nUpdated {config_path}")
    return 0


def cmd_config_missing(args):
    print("error: specify a config subcommand: `check` or `edit`.", file=sys.stderr)
    return 1


def cmd_docker(args):
    docker_path = shutil.which("docker")
    if docker_path is None:
        print("Docker:        not installed.")
        print("  install: https://docs.docker.com/get-docker/")
        print("  (only needed if you plan to deploy applications as containers)")
        return 1

    try:
        version_result = subprocess.run([docker_path, "--version"], capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        print("Docker:        installed, but `docker --version` timed out.")
        return 1

    if version_result.returncode != 0:
        print(f"Docker:        installed at {docker_path}, but `docker --version` failed.")
        print(f"  {version_result.stderr.strip()}")
        return 1

    print(f"Docker:        installed ({version_result.stdout.strip()})")

    try:
        info_result = subprocess.run([docker_path, "info"], capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        print("Docker daemon: not reachable (`docker info` timed out).")
        return 1

    if info_result.returncode != 0:
        print("Docker daemon: not reachable.")
        print("  start Docker Desktop (or the docker service) and try again.")
        return 1

    print("Docker daemon: running.")
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

    start_parser = subparsers.add_parser("start", help="start the agent as a background process")
    start_parser.add_argument("path", nargs="?", default=".", help="workspace directory (default: current directory)")
    start_parser.set_defaults(func=cmd_start)

    stop_parser = subparsers.add_parser("stop", help="stop a background agent started with `flexEngine start`")
    stop_parser.add_argument("path", nargs="?", default=".", help="workspace directory (default: current directory)")
    stop_parser.add_argument("--timeout", type=float, default=10.0, help="seconds to wait for graceful shutdown before killing it (default: 10)")
    stop_parser.set_defaults(func=cmd_stop)

    status_parser = subparsers.add_parser("status", help="check whether the background agent is running")
    status_parser.add_argument("path", nargs="?", default=".", help="workspace directory (default: current directory)")
    status_parser.set_defaults(func=cmd_status)

    stats_parser = subparsers.add_parser("stats", help="show a live CPU/memory/storage/network dashboard for this device")
    stats_parser.add_argument("--interval", type=float, default=2.0, help="refresh interval in seconds (default: 2)")
    stats_parser.add_argument("--once", action="store_true", help="print a single snapshot and exit")
    stats_parser.set_defaults(func=cmd_stats)

    logs_parser = subparsers.add_parser("logs", help="view an agent's log file")
    logs_parser.add_argument("path", nargs="?", default=".", help="workspace directory (default: current directory)")
    logs_parser.add_argument("-n", "--lines", type=int, default=50, help="number of lines to show (default: 50)")
    logs_parser.add_argument("-f", "--follow", action="store_true", help="follow the log file as it grows (like tail -f)")
    logs_parser.add_argument("--level", choices=LOG_LEVELS, help="only show log lines at this level")
    logs_parser.set_defaults(func=cmd_logs)

    config_parser = subparsers.add_parser("config", help="inspect or interactively edit an agent's configuration")
    config_parser.set_defaults(func=cmd_config_missing)
    config_subparsers = config_parser.add_subparsers(dest="config_command")

    config_check_parser = config_subparsers.add_parser("check", help="validate config.yaml and test redis connectivity")
    config_check_parser.add_argument("path", nargs="?", default=".", help="workspace directory (default: current directory)")
    config_check_parser.set_defaults(func=cmd_config_check)

    config_edit_parser = config_subparsers.add_parser("edit", help="interactively edit config.yaml")
    config_edit_parser.add_argument("path", nargs="?", default=".", help="workspace directory (default: current directory)")
    config_edit_parser.add_argument(
        "--regenerate-secret", action="store_true",
        help="generate a new shared secret (the device will need to re-register with flexCloud)"
    )
    config_edit_parser.set_defaults(func=cmd_config_edit)

    docker_parser = subparsers.add_parser("docker", help="check whether Docker is installed and its daemon is running")
    docker_parser.set_defaults(func=cmd_docker)

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

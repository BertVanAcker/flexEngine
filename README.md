
-----------------------------------------------------------------------------------
  flexEngine: A Pythonic framework to develop trustworthy self-adaptive robotics
-----------------------------------------------------------------------------------

|pypi-version|

# flexEngine

flexEngine is an engine purpose-built for deploying and monitoring flexible industrial automation applications. It streamlines the deployment process of automation tasks, integrates powerful monitoring capabilities, and facilitates maintenance and troubleshooting in complex industrial environments.

---

## Table of Contents

- [Overview](#overview)
- [Features](#features)
- [Installation](#installation)
- [Requirements](#requirements)
- [License](#license)
- [Support](#support)

---

## Overview

flexEngine is designed to provide a robust, scalable, and adaptable platform for industrial automation. It supports:
- **Deploying automation applications:** Seamlessly roll out new functionalities.
- **Real-time monitoring:** Keep track of system performance and key metrics.
- **Modular integration:** Easily integrate third-party tools and extend functionality.

Whether you’re managing a small automation task or overseeing a large industrial operation, flexEngine provides the flexibility and control needed to ensure smooth and efficient operations.

---

## Features

- **Easy Deployment:** Simplified workflows for rapid application deployment.
- **Real-Time Monitoring:** Integrated dashboards and metrics to monitor system health.
- **Scalability:** Designed to grow from small installations to enterprise-level operations.
- **Modular Architecture:** Plug-and-play design allows for custom extensions and integrations.
- **Robust Security:** Adheres to industry-standard security practices to safeguard operations.
- **Logging and Alerts:** Comprehensive logging with customizable alerting mechanisms for proactive maintenance.

---

## Installation

flexEngine is a pure-Python package, so the same lightweight install path
works on Windows, macOS, Linux, and Linux ARMv6+ (e.g. Raspberry Pi) — no
compiled installers needed. Each option below installs flexEngine into an
isolated environment (no admin/root rights required) and then scaffolds a
ready-to-run **agent workspace** (`apps/`, `configuration/`, `logs/`,
`agent.py`).

### Quick install

**macOS / Linux / Linux ARMv6+ (Raspberry Pi):**

```bash
curl -fsSL https://raw.githubusercontent.com/BertVanAcker/flexEngine/master/scripts/install.sh | bash
```

To answer the configuration prompts interactively (device name, flexCloud
credentials, Redis connection, ...) rather than getting the non-interactive
defaults, run it as:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/BertVanAcker/flexEngine/master/scripts/install.sh)"
```

**Windows (PowerShell):**

```powershell
iwr -useb https://raw.githubusercontent.com/BertVanAcker/flexEngine/master/scripts/install.ps1 | iex
```

**Manual install (any platform):**

flexEngine requires Python 3.10 or newer. Check which interpreter you have
before installing:

```bash
python3 --version
```

If that reports an older Python (or none at all), install a 3.10+ interpreter
first — e.g. `sudo apt install python3.10` on Debian/Ubuntu, `brew install
python@3.10` on macOS, or download it from
[python.org](https://www.python.org/downloads/) on Windows — and use that
interpreter explicitly below (e.g. `python3.10`).

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
pip install flexEngine
flexEngine init my-agent-workspace
```

### Setting up the workspace

`flexEngine init` creates the workspace layout and either builds
`configuration/config.yaml` from your answers to a short set of prompts, or
copies in a config file you already have:

```bash
# interactive setup
flexEngine init my-agent-workspace

# bring your own config.yaml
flexEngine init my-agent-workspace --config /path/to/config.yaml

# non-interactive, defaults only (useful for scripted installs)
flexEngine init my-agent-workspace --non-interactive
```

### Running the agent

```bash
cd my-agent-workspace
python agent.py
```

flexEngine connects to Redis on startup (see `flexCloud.redis_host` /
`redis_port` in `configuration/config.yaml`) — make sure a Redis instance is
reachable before starting the agent.

---

## Requirements

- **Operating System:** Windows, macOS, Linux, or Linux ARMv6+ (e.g. Raspberry Pi OS).
- **Python:** 3.10 or newer.
- **Redis:** reachable locally or remotely — required by flexEngine's flexCloud connector.
- **Docker** *(optional)*: only needed if you deploy applications as containers.
- **Note for Linux ARMv6/ARMv7 devices:** one dependency (`psutil`) may not
  have a prebuilt wheel for your architecture and could require compiling
  from source (`sudo apt install python3-dev build-essential`) during
  install.

---


## License
flexEngine is distributed under the Apache 2.0 License. Please review the LICENSE file for further details.

---

## Support

For assistance or inquiries, please reach out via:

Email: bert.vanacker@uantwerpen.be
Issue Tracker: Open an issue on our GitHub Issues page

Maintainers cutting a new release should see [RELEASING.md](RELEASING.md).
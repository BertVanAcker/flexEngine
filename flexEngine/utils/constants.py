#**************************************************************************
# * Copyright (C) 2025-present Bert Van Acker (B.MKR) <bva.bmkr@gmail.com>
# *
# * This file is part of the flexIA project.
# *
# * flexIA can not be copied and/or distributed without the express
# * permission of Bert Van Acker
# *************************************************************************

class GENERIC_STATUS:
    UNKNOWN = "UNKNOWN"
    INCORRECT_CMD = "INCORRECT_CMD"

class SECURITY_STORE:
    SHARED_SECRET = "SHARED_SECRET"

class COMMAND_TYPE:
    UNKNOWN = "UNKNOWN"
    OTA_UPDATE = "OTA_UPDATE"
    PING = "PING"
    PUSH = "PUSH"
    BUILD = "BUILD"
    RUN = "RUN"

class REGISTRY_TYPE:
    UNKNOWN = "UNKNOWN"
    GITHUB = "github"
    DOCKERHUB = "dockerhub"

class REGISTRY_WORKSPACE:
    BASE = "apps/"

class APPLICATION_STATUS:
    UNKNOWN = "UNKNOWN"
    REGISTERED = "REGISTERED"
    BUILD = "BUILD"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    ERROR = "ERROR"

class PACKAGE_TYPE:
    UNKNOWN = "UNKNOWN"
    ROBOSAPIENS = "robosapiens"
    FLEXIA = "flexia"

class WORKFLOW_TYPE:
    UNKNOWN = "UNKNOWN"
    DOCKER = "docker"
    DOCKER_COMPOSE = "docker-compose"
    CUSTOM = "custom"
    NONE = "none"
    APPLICATION_DESCRIPTOR = "application-descriptor"
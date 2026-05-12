#**************************************************************************
# * Copyright (C) 2025-present Bert Van Acker (B.MKR) <bva.bmkr@gmail.com>
# *
# * This file is part of the flexIA project.
# *
# * flexIA can not be copied and/or distributed without the express
# * permission of Bert Van Acker
# *************************************************************************


class COMMUNICATIONMATRIX:
    HEARTBEAT = ":heartbeat"
    COMMAND = ":cmd"
    LOG = ":logs"
    #device status
    CPU_LOAD = ":device_info:cpu_load"
    CPU_TEMPERATURE = ":device_info:cpu_temperature"
    MEMORY_USAGE = ":device_info:memory_usage"
    STORAGE_USAGE = ":device_info:storage_usage"
    IP_ADDRESS = ":device_info:ip_address"
    MAC_ADDRESS = ":device_info:mac_address"
    LOCATION = ":device_info:location"
    #application
    APPLICATIONS = ":applications:"
    APPLICATION_NAME = ":name"
    APPLICATION_TAG = ":tag"
    APPLICATION_TYPE = ":type"
    APPLICATION_REMOTE = ":remote"
    APPLICATION_STATUS = ":status"
    APPLICATION_WORKSPACE = ":workspace"
    APPLICATION_COMMIT_SHA = ":commit_sha"
    APPLICATION_VERSION = ":version"
    APPLICATION_VERSION_HISTORY = ":version_history"
    #devices
    DEVICES = ":devices:"



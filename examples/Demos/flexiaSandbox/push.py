# * Copyright (C) 2025-present Bert Van Acker (B.MKR) <bva.bmkr@gmail.com>
# *
# * This file is part of the flexIA project.
# *
# * flexIA can not be copied and/or distributed without the express
# * permission of Bert Van Acker
# *************************************************************************
from flexEngine.communication.composer import *
from flexEngine.communication.matrix import *
from flexEngine.utils.auxiliary import load_config
from flexEngine.api.flexCloud_mock_api import *

# ----------------------- LOAD THE DEVICE CONFIGURATIONS -----------------------
_config_lattepandaD3_1 = load_config("agents/lattepandaD3_1/configuration/config.yaml")
_device_config_lattepandaD3_1 = _config_lattepandaD3_1.get("deviceCredentials", {})

#fetch configuration of lattepandaD3_2
_config_lattepandaD3_2 = load_config("agents/lattepandaD3_2/configuration/config.yaml")
_device_config_lattepandaD3_2 = _config_lattepandaD3_2.get("deviceCredentials", {})

#fetch configuration of lattepandaD3_2
_config_lattepandaD3_3 = load_config("agents/lattepandaD3_3/configuration/config.yaml")
_device_config_lattepandaD3_3 = _config_lattepandaD3_3.get("deviceCredentials", {})


# ----------------------- LOAD THE FLEXCLOUD CONFIGURATION ----------------------
_config = load_config("configuration/config.yaml")
_flexCloud_config = _config.get("flexCloud", {})

# Generate and store secret
secret = _device_config_lattepandaD3_1.get("shared_secret","UNKNOWN")
register_os_variable(secret, env_var_name=SECURITY_STORE.SHARED_SECRET)

# ----------------------- PUSH APPLICATION TO DEVICES -----------------------
_version = "v0.0.2"
push_appliction(type=REGISTRY_TYPE.GITHUB,url="https://github.com/BertVanAcker/flexia-robosapiens-example.git",version=_version,branche="main",devices=[_device_config_lattepandaD3_1,_device_config_lattepandaD3_2,_device_config_lattepandaD3_3],flexCloud_config=_flexCloud_config)
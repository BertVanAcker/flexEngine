#**************************************************************************
# * Copyright (C) 2025-present Bert Van Acker (B.MKR) <bva.bmkr@gmail.com>
# *
# * This file is part of the flexIA project.
# *
# * flexIA can not be copied and/or distributed without the express
# * permission of Bert Van Acker
# *************************************************************************
from flexEngine.utils.constants import *
from flexEngine.application.registry import *
import threading
class applicationManager(object):
    def __init__(self,name="applicationManager",logger=None,verbose=False):
        """Initialize an application manager component.

                Parameters
                ----------
                name : string
                    name of the property components

                verbose : bool
                    component verbose execution

                See Also
                --------
                ..

                Examples
                --------
                >> appManager = applicationManager(name="applicationManager",verbose=False)

                """

        # --- logger configuration ---
        self._name = name
        self._verbose = verbose
        self._logger = logger


    def buildApplication(self, command):
        """Build an application"""
        _commandParameters = command["params"]
        _packageType = _commandParameters["packageType"]
        _app = _commandParameters["app"]
        _appTag = _commandParameters["tag"]

        #----------------------------FLEXIA SPECIFIC PACKAGES-----------------------
        if _packageType==PACKAGE_TYPE.FLEXIA:
            if self._logger is not None: self._logger.info("Flexia application {name:"+_app+",tag:"+_appTag+"}, build request received.")
            #TODO:FETCH flexIA.ini file and interpret
            x=1


        # ----------------------------ROBOSAPIENS SPECIFIC PACKAGES-----------------------
        elif _packageType==PACKAGE_TYPE.ROBOSAPIENS:
            if self._logger is not None: self._logger.info("RobosapiensIO application {name:" + _app + ",tag:" + _appTag + "}, build request received.")
            # TODO:FETCH robosapiens.ini file and interpret
            x = 1
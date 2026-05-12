#**************************************************************************
# * Copyright (C) 2025-present Bert Van Acker (B.MKR) <bva.bmkr@gmail.com>
# *
# * This file is part of the flexIA project.
# *
# * flexIA can not be copied and/or distributed without the express
# * permission of Bert Van Acker
# *************************************************************************
from flexEngine.utils.auxiliary import load_flexia_config
from flexEngine.utils.constants import *
from flexEngine.application.registry import *
import threading
class applicationManager(object):
    def __init__(self,name="applicationManager",logger=None,flexCloudConnector=None,verbose=False):
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

        # --- flexCloudConnetion ---
        self._flexCloudConnection = False
        self._flexCloudConnector = None
        if flexCloudConnector is not None:
            self._flexCloudConnector = flexCloudConnector
            self._flexCloudConnection = True


    def buildApplication(self, command):
        """Build an application"""
        _buildSucceeded = False
        _commandParameters = command["params"]
        _packageType = _commandParameters["packageType"]
        _app = _commandParameters["app"]

        #---------------------------FETCH REGISTERED APPLICATION---------------------
        appDetails = self.get_app_details(appName=_app)
        if not appDetails:
            if self._logger is not None: self._logger.error("Application {name:" + _app +"}, not found in the registered applications.")

        else:
            if self._logger is not None: self._logger.info("Application {name:" + _app +"}, found in the registered applications, proceed with build action.")
            #----------------------------FLEXIA SPECIFIC PACKAGES-----------------------
            if _packageType==PACKAGE_TYPE.FLEXIA:
                if self._logger is not None: self._logger.info("Start building registered flexIA application {name:"+_app +"}.")

                # Fetch flexIA.ini file and interpret
                try:
                    flexiaConfig = load_flexia_config(config_file=appDetails["workspace"]+"/flexIA.ini")
                    if self._logger is not None: self._logger.info("flexIA.ini file loaded for flexIA application {name:" + _app  +"}.")
                except Exception as e:
                    if self._logger is not None: self._logger.error("Failed to load flexIA.ini file for flexIA application {name:" + _app  +"}.")
                    flexiaConfig = {}

                # Determine build config for current device
                _PrimaryHarwareID = self._flexCloudConnector.primaryHardwareID
                try:
                    _buildConfig = flexiaConfig['build'][_PrimaryHarwareID]
                    if self._logger is not None: self._logger.info("Build configuration for device {"+_PrimaryHarwareID+"} loaded for flexIA application {name:" + _app +"}.")
                except Exception as e:
                    if self._logger is not None: self._logger.error("Failed to load build configuration for device {"+_PrimaryHarwareID+"} for flexIA application {name:" + _app  +"}.")
                    _buildConfig = {}

                # Determine build strategy
                if not _buildConfig:
                    if self._logger is not None: self._logger.error("No build workflow for device {" + _PrimaryHarwareID + "} for flexIA application {name:" + _app  +"}, canceling build.")
                else:
                    if _buildConfig["workflow"] == WORKFLOW_TYPE.UNKNOWN:
                        if self._logger is not None: self._logger.error("Build workflow UNKNOWN for device {" + _PrimaryHarwareID + "} for flexIA application {name:" + _app +"}, canceling build.")

                    elif _buildConfig["workflow"] == WORKFLOW_TYPE.DOCKER:
                        if self._logger is not None: self._logger.info("Docker build workflow for flexIA application {name:" + _app +"} started.")
                        #TODO: trigger docker build in given workspace
                        _buildSucceeded = self.docker_build(appName=_app,tag=appDetails["tag"],workspace=appDetails["workspace"])


                    elif _buildConfig["workflow"] == WORKFLOW_TYPE.DOCKER_COMPOSE:
                        if self._logger is not None: self._logger.info("Docker-compose build workflow for flexIA application {name:" + _app +"} started.")
                        # TODO: trigger docker build in given workspace

                    elif _buildConfig["workflow"] == WORKFLOW_TYPE.APPLICATION_DESCRIPTOR:
                        if self._logger is not None: self._logger.info("Application descriptor build workflow for flexIA application {name:" + _app + "} started.")
                        # TODO: trigger application descriptor build workflow in given workspace

                        # TODO: 1. fetch the application descriptor

                    elif _buildConfig["workflow"] == WORKFLOW_TYPE.CUSTOM:
                        if self._logger is not None: self._logger.info("Custom build workflow for flexIA application {name:" + _app +"} started.")

                    elif _buildConfig["workflow"] == WORKFLOW_TYPE.NONE:
                        if self._logger is not None: self._logger.info("No build workflow required for flexIA application {name:" + _app +"} started.")

                #UPDATE THE STATUS IF BUILD IS SUCCESSFUL
                if _buildSucceeded:
                    self.set_app_status(appName=_app,status=APPLICATION_STATUS.BUILD)
                    #self._flexCloudConnector.write(self._flexCloudConnector.connectorID + COMMUNICATIONMATRIX.APPLICATIONS + _app + COMMUNICATIONMATRIX.APPLICATION_STATUS,APPLICATION_STATUS.BUILD)
                    # TODO: do we also need to update supervisor registry locally?

            # ----------------------------ROBOSAPIENS SPECIFIC PACKAGES-----------------------
            elif _packageType==PACKAGE_TYPE.ROBOSAPIENS:
                if self._logger is not None: self._logger.info("Start building registered RobosapiensIO application {name:" + _app +"}.")
                # TODO:FETCH robosapiens.ini file and interpret
                x = 1


    def runApplication(self, command):
        """Run an application"""
        _launchSucceeded = False
        _commandParameters = command["params"]
        _packageType = _commandParameters["packageType"]
        _app = _commandParameters["app"]

        #---------------------------FETCH REGISTERED APPLICATION---------------------
        appDetails = self.get_app_details(appName=_app)
        if not appDetails:
            if self._logger is not None: self._logger.error("Application {name:" + _app +"}, not found in the registered applications.")

        else:
            if self._logger is not None: self._logger.info("Application {name:" + _app +"}, found in the registered applications, proceed with run action.")
            #----------------------------FLEXIA SPECIFIC PACKAGES-----------------------
            if _packageType==PACKAGE_TYPE.FLEXIA:
                if self._logger is not None: self._logger.info("Start run workflow for registered flexIA application {name:"+_app +"}.")

                # Fetch flexIA.ini file and interpret
                try:
                    flexiaConfig = load_flexia_config(config_file=appDetails["workspace"]+"/flexIA.ini")
                    if self._logger is not None: self._logger.info("flexIA.ini file loaded for flexIA application {name:" + _app  +"}.")
                except Exception as e:
                    if self._logger is not None: self._logger.error("Failed to load flexIA.ini file for flexIA application {name:" + _app  +"}.")
                    flexiaConfig = {}

                # Determine build config for current device
                _PrimaryHarwareID = self._flexCloudConnector.primaryHardwareID
                try:
                    _runConfig = flexiaConfig['run'][_PrimaryHarwareID]
                    if self._logger is not None: self._logger.info("Run configuration for device {"+_PrimaryHarwareID+"} loaded for flexIA application {name:" + _app +"}.")
                except Exception as e:
                    if self._logger is not None: self._logger.error("Failed to load run configuration for device {"+_PrimaryHarwareID+"} for flexIA application {name:" + _app  +"}.")
                    _runConfig = {}

                # Determine run strategy
                if not _runConfig:
                    if self._logger is not None: self._logger.error("No run workflow for device {" + _PrimaryHarwareID + "} for flexIA application {name:" + _app  +"}, canceling build.")
                else:
                    _workflow_link = _runConfig["workflow_link"]
                    if _runConfig["workflow"] == WORKFLOW_TYPE.UNKNOWN:
                        if self._logger is not None: self._logger.error("Run workflow UNKNOWN for device {" + _PrimaryHarwareID + "} for flexIA application {name:" + _app +"}, canceling build.")

                    elif _runConfig["workflow"] == WORKFLOW_TYPE.DOCKER:
                        if self._logger is not None: self._logger.info("Docker run workflow for flexIA application {name:" + _app +"} started.")
                        #TODO: trigger docker RUN in given workspace
                        _launchSucceeded = self.docker_run(appName=_app,tag=appDetails["tag"],workspace=appDetails["workspace"],additional_run_args=_runConfig["arguments"])


                    elif _runConfig["workflow"] == WORKFLOW_TYPE.DOCKER_COMPOSE:
                        if self._logger is not None: self._logger.info("Docker-compose run workflow for flexIA application {name:" + _app +"} started.")
                        _launchSucceeded = self.docker_compose_run(appName=_app,tag=appDetails["tag"],workspace=appDetails["workspace"],additional_run_args=_runConfig["arguments"],workflowlink=_workflow_link)

                    elif _runConfig["workflow"] == WORKFLOW_TYPE.APPLICATION_DESCRIPTOR:
                        if self._logger is not None: self._logger.info("Application descriptor run workflow for flexIA application {name:" + _app +"} started.")
                        # TODO: trigger application descriptor run workflow in given workspace

                        # TODO: 1. fetch the application descriptor

                    elif _runConfig["workflow"] == WORKFLOW_TYPE.DOCKER_CUSTOM_COMPOSE:
                        if self._logger is not None: self._logger.info("Docker-custom-compose run workflow for flexIA application {name:" + _app +"} started.")

                        #TODO SAHAR: 1. generate docker compose file + put docker compose in root

                        _launchSucceeded = self.docker_compose_run(appName=_app, tag=appDetails["tag"],
                                                                   workspace=appDetails["workspace"],
                                                                   additional_run_args=_runConfig["arguments"],
                                                                   workflowlink=_workflow_link)

                    elif _runConfig["workflow"] == WORKFLOW_TYPE.CUSTOM:
                        if self._logger is not None: self._logger.info("Custom run workflow for flexIA application {name:" + _app +"} started.")

                    elif _runConfig["workflow"] == WORKFLOW_TYPE.NONE:
                        if self._logger is not None: self._logger.info("No run workflow required for flexIA application {name:" + _app +"} started.")

                # UPDATE THE STATUS IF BUILD IS SUCCESSFUL
                if _launchSucceeded:
                    self.set_app_status(appName=_app, status=APPLICATION_STATUS.RUNNING)
                    #self._flexCloudConnector.write(self._flexCloudConnector.connectorID + COMMUNICATIONMATRIX.APPLICATIONS + _app + COMMUNICATIONMATRIX.APPLICATION_STATUS,APPLICATION_STATUS.RUNNING)
                    # TODO: do we also need to update supervisor registry locally?


            # ----------------------------ROBOSAPIENS SPECIFIC PACKAGES-----------------------
            elif _packageType==PACKAGE_TYPE.ROBOSAPIENS:
                if self._logger is not None: self._logger.info("Start building registered RobosapiensIO application {name:" + _app +"}.")
                # TODO:FETCH robosapiens.ini file and interpret
                x = 1

    def stopApplication(self, command):
        """Stop an application or application component"""
        _stopSucceeded = False
        _commandParameters = command["params"]
        _packageType = _commandParameters["packageType"]
        _app = _commandParameters["app"]
        _components = _commandParameters["components"]

        #---------------------------FETCH REGISTERED APPLICATION---------------------
        appDetails = self.get_app_details(appName=_app)
        if not appDetails:
            if self._logger is not None: self._logger.error("Application {name:" + _app +"}, not found in the registered applications.")

        else:
            if self._logger is not None: self._logger.info("Application {name:" + _app +"}, found in the registered applications, proceed with stop action.")
            #----------------------------FLEXIA SPECIFIC PACKAGES-----------------------
            if _packageType==PACKAGE_TYPE.FLEXIA:
                if self._logger is not None: self._logger.info("Start stop workflow for running flexIA application {name:"+_app +"}.")

                # Fetch flexIA.ini file and interpret
                try:
                    flexiaConfig = load_flexia_config(config_file=appDetails["workspace"]+"/flexIA.ini")
                    if self._logger is not None: self._logger.info("flexIA.ini file loaded for flexIA application {name:" + _app  +"}.")
                except Exception as e:
                    if self._logger is not None: self._logger.error("Failed to load flexIA.ini file for flexIA application {name:" + _app  +"}.")
                    flexiaConfig = {}

                # Determine build config for current device
                _PrimaryHarwareID = self._flexCloudConnector.primaryHardwareID
                try:
                    _stopConfig = flexiaConfig['run'][_PrimaryHarwareID]
                    if self._logger is not None: self._logger.info("Run configuration for device {"+_PrimaryHarwareID+"} loaded for flexIA application {name:" + _app +"}.")
                except Exception as e:
                    if self._logger is not None: self._logger.error("Failed to load run configuration for device {"+_PrimaryHarwareID+"} for flexIA application {name:" + _app  +"}.")
                    _stopConfig = {}

                # Determine stop strategy
                if not _stopConfig:
                    if self._logger is not None: self._logger.error("No stop workflow for device {" + _PrimaryHarwareID + "} for flexIA application {name:" + _app  +"}, canceling build.")
                else:
                    _workflow_link = _stopConfig["workflow_link"]
                    if _stopConfig["workflow"] == WORKFLOW_TYPE.UNKNOWN:
                        if self._logger is not None: self._logger.error("Stop workflow UNKNOWN for device {" + _PrimaryHarwareID + "} for flexIA application {name:" + _app +"}, canceling build.")

                    elif _stopConfig["workflow"] == WORKFLOW_TYPE.DOCKER:
                        if self._logger is not None: self._logger.info("Docker stop workflow for flexIA application {name:" + _app +"} started.")
                        #TODO: trigger docker STOP in given workspace
                        if _components != "-":
                            try:
                                _componentList = _components.split(';')
                                for comp in _componentList:
                                    print("docker-stop for: "+comp)
                                    # TODO: shutdown all given components
                                    #_stopSucceeded = self.docker_run(appName=_app,tag=appDetails["tag"],workspace=appDetails["workspace"],additional_run_args=_stopConfig["arguments"])
                            except:
                                if self._logger is not None: self._logger.error("Failed to stop components {list:" + _components +"} flexIA application {name:" + _app +"} started.")


                    elif _stopConfig["workflow"] == WORKFLOW_TYPE.DOCKER_COMPOSE:
                        if self._logger is not None: self._logger.info("Docker-compose stop workflow for flexIA application {name:" + _app +"} started.")
                        if _components != "-":
                            try:
                                _componentList = _components.split(';')
                                for comp in _componentList:
                                    print("docker-compose-stop for: "+comp)
                                    _stopSucceeded = self.docker_compose_stop_component(appName=_app,
                                                                              workspace=appDetails["workspace"],
                                                                              workflowlink=_workflow_link,
                                                                              componentName = comp,
                                                                              additional_stop_args=_stopConfig[
                                                                                  "arguments"])
                            except:
                                if self._logger is not None: self._logger.error("Failed to stop components {list:" + _components + "} flexIA application {name:" + _app + "} started.")
                        else:
                            try:
                                _stopSucceeded = self.docker_compose_stop(appName=_app,
                                                                 workspace=appDetails["workspace"],
                                                                 workflowlink=_workflow_link,
                                                                 additional_stop_args=_stopConfig["arguments"])
                            except:
                                if self._logger is not None: self._logger.error("Failed to stop flexIA application {name:" + _app + "} started.")


                    elif _stopConfig["workflow"] == WORKFLOW_TYPE.CUSTOM:
                        if self._logger is not None: self._logger.info("Custom stop workflow for flexIA application {name:" + _app +"} not implemented.")

                    elif _stopConfig["workflow"] == WORKFLOW_TYPE.NONE:
                        if self._logger is not None: self._logger.info("No stop workflow required for flexIA application {name:" + _app +"}.")

                # UPDATE THE STATUS IF STOP IS SUCCESSFUL
                if _stopSucceeded:
                    self.set_app_status(appName=_app, status=APPLICATION_STATUS.STOPPED)
                    #self._flexCloudConnector.write(self._flexCloudConnector.connectorID + COMMUNICATIONMATRIX.APPLICATIONS + _app + COMMUNICATIONMATRIX.APPLICATION_STATUS,APPLICATION_STATUS.STOPPED)
                    # TODO: do we also need to update supervisor registry locally?


    def get_app_details(self,appName =""):
        """Get application details"""
        applicationKeys = self._flexCloudConnector.getKeys(self._flexCloudConnector.connectorID + COMMUNICATIONMATRIX.APPLICATIONS + appName + ":" + "*")
        property_key_map = {}
        for key in applicationKeys:
            # Split the key by ":", assuming the last segment is the application property.
            parts = key.split(":")
            if len(parts) >= 4:
                property_name = parts[-1]
                property_key_map[property_name] = key

        # Define the properties we expect.
        properties = ['name', 'tag', 'type', 'remote', 'workspace', 'status']

        # Prepare a dictionary to hold the details.
        details = {}
        for prop in properties:
            key = property_key_map.get(prop)
            if key:
                value = self._flexCloudConnector.read(key=key)
                # If the value exists and is a byte string, decode it.
                if value and isinstance(value, bytes):
                    value = value.decode('utf-8')
                details[prop] = value
            else:
                # If a property key is missing, you can either leave it out or set it to None.
                details[prop] = None

        return details

    def set_app_status(self,appName ="",status=APPLICATION_STATUS.UNKNOWN):
        """Set application status"""
        _applicationStatusKey = self._flexCloudConnector.connectorID + COMMUNICATIONMATRIX.APPLICATIONS + appName + COMMUNICATIONMATRIX.APPLICATION_STATUS
        try:
            self._flexCloudConnector.write(key=_applicationStatusKey, value=status)
        except:
            if self._logger is not None: self._logger.info("Failed to update status of flexIA application {name:" + appName +"}.")

    #-------------------------------------------------------------------------------------------------------------------
    #                                    BUILD-IN BUILD WORKFLOWS
    #-------------------------------------------------------------------------------------------------------------------
    def docker_build(self,appName="",workspace="apps",tag=""):
        _success = False
        #1. Fetch docker file
        dockerfile_path = os.path.join(workspace, "Dockerfile")
        if not os.path.exists(dockerfile_path):
            if self._logger is not None: self._logger.error("Dockerfile for application {name:"+appName+"} not found in repository "+dockerfile_path+".")
        else:

        #2. Build docker container with tag
            result = subprocess.run(
                ["docker", "build", "-t", tag, "."],
                cwd=workspace,
                capture_output=True,
                text=True,
            )
        #3. Check if build was successfull
            if result.returncode != 0:
                if self._logger is not None: self._logger.error("Docker build failed for application {name:"+appName+"}.")
                if self._logger is not None: self._logger.error(result.stderr)
            else:
                if self._logger is not None: self._logger.info("Successfully build container for application {name:"+appName+"}, with tag: "+tag+".")
                _success=True

        return _success

    def docker_run(self,appName="",workspace="apps",tag="",additional_run_args=""):
        """"Running a build docker application """
        _success = False

        # 1. Fetch docker file
        dockerfile_path = os.path.join(workspace, "Dockerfile")
        if not os.path.exists(dockerfile_path):
            if self._logger is not None: self._logger.error("Dockerfile for application {name:" + appName + "} not found in repository " + dockerfile_path + ".")
        else:
            arguments = additional_run_args.strip('"')
            # Split the string by whitespace
            additional_run_args_processes = arguments.split()
            run_command = ["docker", "run", "--name", appName, "-d"] + additional_run_args_processes + [tag]

            result = subprocess.run(
                run_command,
                cwd=workspace,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                if self._logger is not None: self._logger.error("Failed to run docker application {name:" + appName + "}.")
                if self._logger is not None: self._logger.error(result.stderr)
            else:
                if self._logger is not None: self._logger.info("Successfully run docker application {name:" + appName + "}.")
                _success = True

        return _success

    def docker_stop_standalone(self, containerName="", additional_stop_args=""):
        """Stop a standalone Docker container by name"""
        _success = False

        # 1. Validate container name
        if not containerName:
            if self._logger:
                self._logger.error("No containerName provided for standalone Docker stop.")
            return False

        # 2. Build the stop command
        args = additional_stop_args.strip('"')
        extra = args.split() if args else []
        stop_command = ["docker", "stop"] + extra + [containerName]

        # 3. Execute
        result = subprocess.run(
            stop_command,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            if self._logger:
                self._logger.error(f"Failed to stop standalone container {{name:{containerName}}}.")
                self._logger.error(result.stderr)
        else:
            if self._logger:
                self._logger.info(f"Successfully stopped standalone container {{name:{containerName}}}.")
            _success = True

        return _success

    def docker_compose_run(self,appName="",workspace="apps",workflowlink=None,tag="",additional_run_args=""):
        """Running a docker compose application"""
        _success = False

        # 1. Fetch docker file
        if workflowlink == "":
            composefile_path = os.path.join(workspace, "compose.yaml")
        else:
            composefile_path = os.path.join(workspace, workflowlink)             #TODO: TEST IF WORKS
            workspace = composefile_path.rsplit('/', 1)[0]

        if not os.path.exists(composefile_path):
            if self._logger is not None: self._logger.error("Docker compose file for application {name:" + appName + "} not found in repository " + composefile_path + ".")
        else:
            arguments = additional_run_args.strip('"')
            # Split the string by whitespace
            additional_run_args_processes = arguments.split()
            run_command = ["docker", "compose", "up", "-d","--build"] + additional_run_args_processes

            #result = subprocess.run(
            #    run_command,
            #    cwd=workspace,
            #    capture_output=True,
            #    text=True,
            #    stdin=subprocess.DEVNULL,
            #)
            try:
                result = subprocess.run(
                    run_command,
                    cwd=workspace,
                    capture_output=True,
                    text=True,
                    timeout=300,  # seconds
                )
            except subprocess.TimeoutExpired as e:
                print(f"Command timed out after {e.timeout} seconds")

            if result.returncode != 0:
                if self._logger is not None: self._logger.error("Failed to run docker compose application {name:" + appName + "}.")
                if self._logger is not None: self._logger.error(result.stderr)
            else:
                if self._logger is not None: self._logger.info("Successfully run docker compose application {name:" + appName + "}.")
                _success = True

        return _success

    def docker_compose_stop(self, appName="", workspace="apps", workflowlink=None, additional_stop_args=""):
        """Stopping a docker compose application"""
        _success = False

        # 1. Determine compose file path
        if not workflowlink:
            composefile_path = os.path.join(workspace, "compose.yaml")
        else:
            composefile_path = os.path.join(workspace, workflowlink)
            workspace = os.path.dirname(composefile_path)

        # 2. Check existence
        if not os.path.exists(composefile_path):
            if self._logger:
                self._logger.error(f"Docker compose file for application {{name:{appName}}} not found at {composefile_path}.")
            return False

        # 3. Build the down command
        args = additional_stop_args.strip('"')
        additional_args = args.split() if args else []
        stop_command = ["docker", "compose", "down"] + additional_args

        # 4. Execute
        result = subprocess.run(
            stop_command,
            cwd=workspace,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            if self._logger:
                self._logger.error(f"Failed to stop docker compose application {{name:{appName}}}.")
                self._logger.error(result.stderr)
        else:
            if self._logger:
                self._logger.info(f"Successfully stopped docker compose application {{name:{appName}}}.")
            _success = True

        return _success

    import os
    import subprocess

    def docker_compose_stop_component(self, appName="", workspace="apps", workflowlink=None, componentName="",additional_stop_args=""):
        """Stopping one component of a docker compose application by service name"""
        _success = False

        # 1. Determine compose file path
        if not workflowlink:
            composefile_path = os.path.join(workspace, "compose.yaml")
        else:
            composefile_path = os.path.join(workspace, workflowlink)
            workspace = os.path.dirname(composefile_path)

        # 2. Validate inputs
        if not componentName:
            if self._logger:
                self._logger.error(f"No componentName provided for application {{name:{appName}}}.")
            return False

        if not os.path.exists(composefile_path):
            if self._logger:
                self._logger.error(f"Docker compose file for application {{name:{appName}}} not found at {composefile_path}.")
            return False

        # 3. Build the stop command
        args = additional_stop_args.strip('"')
        extra = args.split() if args else []
        stop_command = ["docker", "compose", "stop", componentName] + extra

        # 4. Execute
        result = subprocess.run(
            stop_command,
            cwd=workspace,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            if self._logger:
                self._logger.error(f"Failed to stop component '{componentName}' of application {{name:{appName}}}.")
                self._logger.error(result.stderr)
        else:
            if self._logger:
                self._logger.info(
                    f"Successfully stopped component '{componentName}' of application {{name:{appName}}}.")
            _success = True

        return _success




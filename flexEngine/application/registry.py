#**************************************************************************
# * Copyright (C) 2023-present Bert Van Acker (B.MKR) <bva.bmkr@gmail.com>
# *
# * This file is part of the hybridIO project.
# *
# * HybridIO can not be copied and/or distributed without the express
# * permission of Bert Van Acker
# *************************************************************************
import os

from flexEngine.communication.matrix import COMMUNICATIONMATRIX
from flexEngine.security.security import generate_app_tag
from flexEngine.utils.constants import *
import subprocess
class registryManager(object):
    def __init__(self,name="appRegistryManager",flexCloudConnector=None,logger=None,verbose=False):
        """Initialize a registry component.

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
                >> registry = registry(name="appRegistry",verbose=False)

                """

        # --- logger configuration ---
        self._name = name
        self._verbose = verbose
        self._logger = logger

        #--- registered applications or application collections ---
        self._registry = []

        # --- flexCloudConnetion ---
        self._flexCloudConnection = False
        self._flexCloudConnector = None
        if flexCloudConnector is not None:
            self._flexCloudConnector = flexCloudConnector
            self._flexCloudConnection = True


        self._logger.info("Application registry initialized")


    def registerApplication(self, command):
        """Register an application"""
        _commandParameters = command["params"]
        _type = _commandParameters["type"]
        _url = _commandParameters["url"]

        if _type==REGISTRY_TYPE.GITHUB:
            #--------------------------- CLONE GITHUB REPOSITORY AND REGISTER ----------------------------
            _branch = _commandParameters["branch"]
            _appName = _url.split("/")[-1].split(".git")[0]
            _workspace = REGISTRY_WORKSPACE.BASE
            try:
                _localPath, _exists = self._clone_repo(repo_url=_url,workspace=_workspace,branch=_branch)
                _app = applicationEntry(name=_appName,type=_type,workspace=_workspace+_appName,repo_url=_url)
                if not _exists:
                    self._registry.append(_app)
                    self.register_backend(applicationEntry=_app)
                    if self._logger is not None:self._logger.info("Application successfully registered")
                else:
                    if self._logger is not None: self._logger.info("Application already registered")

                #TODO: report that app registration is successfull to flexCloud
            except Exception as e:
                _localPath = ""
                if self._logger is not None:self._logger.error("Application not successfully registered")


        elif _type==REGISTRY_TYPE.DOCKERHUB:
            # --------------------------- FETCH FROM DOCKER HUB AND REGISTER ----------------------------
            x=1










    def _clone_repo(self,repo_url: str, workspace: str, branch: str = "master") -> str:
        """
        Clone a Git repository into a workspace. If the repository already exists,
        it will attempt to pull the latest changes.

        :param repo_url: URL of the Git repository.
        :param workspace: Path to the workspace directory.
        :param branch: Branch to clone (default is 'master').
        :return: The local path to the cloned repository.
        :raises Exception: If the clone or pull command fails.
        """
        _workspace_exists = False
        # Ensure the workspace exists.
        if not os.path.exists(workspace):
            os.makedirs(workspace)

        # Derive the repository name from the URL.
        repo_name = os.path.splitext(os.path.basename(repo_url))[0]
        repo_path = os.path.join(workspace, repo_name)

        if os.path.exists(repo_path):
            _workspace_exists = True
            if self._logger is not None: self._logger.info(f"Repository '{repo_name}' already exists. Pulling latest changes...")
            result = subprocess.run(
                ["git", "-C", repo_path, "pull"],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                if self._logger is not None: self._logger.error(f"Error pulling repository: {result.stderr}")
                raise Exception("Git pull failed")
        else:
            if self._logger is not None: self._logger.info(f"Cloning repository '{repo_url}' into '{repo_path}'...")
            result = subprocess.run(
                ["git", "clone", "-b", branch, repo_url, repo_path],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                if self._logger is not None: self._logger.error(f"Error cloning repository: {result.stderr}")
                raise Exception("Git clone failed")

        return repo_path, _workspace_exists

    def register_backend(self,applicationEntry = None):
        if self._flexCloudConnection and applicationEntry is not None:
            baseKey = self._flexCloudConnector.connectorID + COMMUNICATIONMATRIX.APPLICATIONS + applicationEntry.name
            self._flexCloudConnector.write(baseKey+COMMUNICATIONMATRIX.APPLICATION_NAME,applicationEntry.name)
            self._flexCloudConnector.write(baseKey + COMMUNICATIONMATRIX.APPLICATION_TAG, applicationEntry.tag)
            self._flexCloudConnector.write(baseKey + COMMUNICATIONMATRIX.APPLICATION_TYPE, applicationEntry.type)
            self._flexCloudConnector.write(baseKey + COMMUNICATIONMATRIX.APPLICATION_WORKSPACE, applicationEntry.workspace)
            self._flexCloudConnector.write(baseKey + COMMUNICATIONMATRIX.APPLICATION_REMOTE, applicationEntry.repo_url)
            self._flexCloudConnector.write(baseKey + COMMUNICATIONMATRIX.APPLICATION_STATUS, applicationEntry.status)




class applicationEntry(object):
    def __init__(self, name="applicationName", type="github", workspace="apps/", repo_url="", tag=""):
        self._name = name
        if tag == "":
            self._tag = generate_app_tag(prefix="app",unique_length=8)
        else:
            self._tag = tag
        self._type = type
        self._workspace = workspace
        self._repo_url = repo_url
        # --- app status ---
        self._status = APPLICATION_STATUS.REGISTERED

    @property
    def name(self):
        """Get the application name."""
        return self._name

    @name.setter
    def name(self, value):
        """Set the application name."""
        self._name = value

    @property
    def tag(self):
        """Get the tag."""
        return self._tag

    @tag.setter
    def tag(self, value):
        """Set the tag."""
        self._tag = value

    @property
    def type(self):
        """Get the application type."""
        return self._type

    @type.setter
    def type(self, value):
        """Set the application type."""
        self._type = value

    @property
    def workspace(self):
        """Get the workspace."""
        return self._workspace

    @workspace.setter
    def workspace(self, value):
        """Set the workspace."""
        self._workspace = value

    @property
    def repo_url(self):
        """Get the repository URL."""
        return self._repo_url

    @repo_url.setter
    def repo_url(self, value):
        """Set the repository URL."""
        self._repo_url = value

    @property
    def status(self):
        """Get the application status."""
        return self._status

    @status.setter
    def status(self, value):
        """Set the application status."""
        self._status = value



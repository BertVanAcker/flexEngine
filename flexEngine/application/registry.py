#**************************************************************************
# * Copyright (C) 2023-present Bert Van Acker (B.MKR) <bva.bmkr@gmail.com>
# *
# * This file is part of the hybridIO project.
# *
# * HybridIO can not be copied and/or distributed without the express
# * permission of Bert Van Acker
# *************************************************************************
import json
import os

from flexEngine.communication.composer import compose_command
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
        _version = _commandParameters["version"]

        if _type==REGISTRY_TYPE.GITHUB:
            #--------------------------- CLONE GITHUB REPOSITORY AND REGISTER ----------------------------
            _branch = _commandParameters["branch"]
            _appName = _url.split("/")[-1].split(".git")[0]
            _workspace = REGISTRY_WORKSPACE.BASE
            try:
                #0. Check if local exists - not pulling latest releases
                _localPath, _exists = self._clone_repo(repo_url=_url,workspace=_workspace,branch=_branch,autoUpdate=False)

                #1A. local repository does not exist
                if not _exists:
                    #2. determine the commit_SHA for the pulled repository
                    _commit_sha = self._get_commit_sha(_localPath)
                    #3. setup app entry for registration
                    _app = applicationEntry(name=_appName, type=_type, workspace=_workspace + _appName, repo_url=_url,version=_version, commit_sha=_commit_sha)

                    # 4. Check if pushed version exists, registered in flexCloud
                    _versionExists, _commit_sha = self._check_version_exists(applicationEntry=_app, version=_version)
                    if not _versionExists:
                        #5A. Version does not exist in flexCloud -> fresh application registry
                        self._registry.append(_app)
                        self.register_backend(applicationEntry=_app)
                        #6. register version as tag on remote + push
                        self._create_git_version_tag(repo_path=_localPath,version=_version,commit_sha=_commit_sha,push=True)
                        if self._logger is not None: self._logger.info("Application successfully registered")
                    else:
                        # 5B. Version does exist in flexCloud -> fetch application snapshot + register
                        _localPath, _exists = self._fetch_repo_snapshot(repo_url=_url,workspace=_workspace,branch=_branch,commit_sha=_commit_sha)
                        _app = applicationEntry(name=_appName, type=_type, workspace=_workspace + _appName,repo_url=_url, version=_version, commit_sha=_commit_sha)
                        self._registry.append(_app)
                        self.register_backend(applicationEntry=_app,addHistory=False)
                        if self._logger is not None: self._logger.info("Application successfully registered - pulled remote registered version:"+_version)


                # 1B. local repository does exist
                else:
                    #2. setup app entry for registration - no commit_SHA, to be determined later if needed
                    _app = applicationEntry(name=_appName, type=_type, workspace=_workspace + _appName, repo_url=_url,version=_version, commit_sha="")
                    #3. Check if pushed version exists, registered in flexCloud
                    _versionExists, _commit_sha = self._check_version_exists(applicationEntry=_app, version=_version)
                    if not _versionExists:
                        #4A. version does not exist, pull latest snapshot of git
                        _localPath, _exists = self._clone_repo(repo_url=_url, workspace=_workspace, branch=_branch,autoUpdate=True)
                        # 5. determine the commit_SHA for the pulled repository
                        _commit_sha = self._get_commit_sha(_localPath)
                        _app.commit_sha = _commit_sha
                        # 6. register updated application and activate in local repo
                        self._registry.append(_app)
                        self.register_backend(applicationEntry=_app)
                        # 6. register version as tag on remote + push
                        self._create_git_version_tag(repo_path=_localPath, version=_version, commit_sha=_commit_sha,push=True)
                        if self._logger is not None: self._logger.info("New version of the application registered and activated")

                    else:
                        #4B. version does exist, load checkpoint for this version
                        _localPath, _exists = self._fetch_repo_snapshot(repo_url=_url, workspace=_workspace,branch=_branch, commit_sha=_commit_sha)
                        _app = applicationEntry(name=_appName, type=_type, workspace=_workspace + _appName,repo_url=_url, version=_version, commit_sha=_commit_sha)
                        self._registry.append(_app)
                        self.register_backend(applicationEntry=_app, addHistory=False)
                        if self._logger is not None: self._logger.info("Application snapshot loaded for version:" +_version)

            except Exception as e:
                _localPath = ""
                if self._logger is not None: self._logger.error("Application not successfully registered")

                #_app = applicationEntry(name=_appName,type=_type,workspace=_workspace+_appName,repo_url=_url,version=_version,commit_sha=_commit_sha)
                #if not _exists:
                #    self._registry.append(_app)
                #    self.register_backend(applicationEntry=_app)
                #    if self._logger is not None:self._logger.info("Application successfully registered")
                #else:
                #    if self._logger is not None: self._logger.info("Application already registered")
                #    #TODO: if version is not yet registered, then it needs to update the git commit_sha
                #    _exists,_commit_sha = self._check_version_exists(applicationEntry=_app,version=_version)


                #TODO: report that app registration is successfull to flexCloud



        elif _type==REGISTRY_TYPE.DOCKERHUB:
            # --------------------------- FETCH FROM DOCKER HUB AND REGISTER ----------------------------
            x=1

    # def _clone_repo(self,repo_url: str, workspace: str, branch: str = "master",autoUpdate=False) -> str:
    #     """
    #     Clone a Git repository into a workspace. If the repository already exists,
    #     it will attempt to pull the latest changes.
    #
    #     :param repo_url: URL of the Git repository.
    #     :param workspace: Path to the workspace directory.
    #     :param branch: Branch to clone (default is 'master').
    #     :return: The local path to the cloned repository.
    #     :raises Exception: If the clone or pull command fails.
    #     """
    #     _workspace_exists = False
    #     # Ensure the workspace exists.
    #     if not os.path.exists(workspace):
    #         os.makedirs(workspace)
    #
    #     # Derive the repository name from the URL.
    #     repo_name = os.path.splitext(os.path.basename(repo_url))[0]
    #     repo_path = os.path.join(workspace, repo_name)
    #
    #     if os.path.exists(repo_path):
    #         _workspace_exists = True
    #         if autoUpdate:
    #             if self._logger is not None: self._logger.info(f"Repository '{repo_name}' already exists. Pulling latest changes...")
    #             result = subprocess.run(
    #                 ["git", "-C", repo_path, "pull"],
    #                 capture_output=True,
    #                 text=True,
    #             )
    #             if result.returncode != 0:
    #                 if self._logger is not None: self._logger.error(f"Error pulling repository: {result.stderr}")
    #                 raise Exception("Git pull failed")
    #         else:
    #             if self._logger is not None: self._logger.info(f"Repository '{repo_name}' already exists. Automatically pulling latest changes DISABLED, no changes to the local repository...")
    #     else:
    #         if self._logger is not None: self._logger.info(f"Cloning repository '{repo_url}' into '{repo_path}'...")
    #         result = subprocess.run(
    #             ["git", "clone", "-b", branch, repo_url, repo_path],
    #             capture_output=True,
    #             text=True,
    #         )
    #         if result.returncode != 0:
    #             if self._logger is not None: self._logger.error(f"Error cloning repository: {result.stderr}")
    #             raise Exception("Git clone failed")
    #
    #     return repo_path, _workspace_exists
    import os
    import subprocess

    def _clone_repo(
            self,
            repo_url: str,
            workspace: str,
            branch: str = "master",
            autoUpdate: bool = False,
    ) -> tuple[str, bool]:
        """
        Clone a Git repository into a workspace. If the repository already exists,
        it can optionally update the local repository by explicitly checking out
        and pulling the requested branch.

        This avoids Git errors caused by pulling while:
        - no upstream branch is configured
        - the repository is in detached HEAD state
        - Git does not know which branch to pull from

        :param repo_url: URL of the Git repository.
        :param workspace: Path to the workspace directory.
        :param branch: Branch to clone or update.
        :param autoUpdate: If True, fetch and pull latest changes for the branch.
        :return: Tuple containing:
                 - local path to the repository
                 - whether the repository already existed
        :raises Exception: If clone, fetch, checkout, or pull fails.
        """
        _workspace_exists = False

        if not os.path.exists(workspace):
            os.makedirs(workspace)

        repo_name = os.path.splitext(os.path.basename(repo_url))[0]
        repo_path = os.path.join(workspace, repo_name)

        if os.path.exists(repo_path):
            _workspace_exists = True

            if autoUpdate:
                if self._logger is not None:
                    self._logger.info(
                        f"Repository '{repo_name}' already exists. Updating branch '{branch}'..."
                    )

                fetch_result = subprocess.run(
                    ["git", "-C", repo_path, "fetch", "origin", branch],
                    capture_output=True,
                    text=True,
                )

                if fetch_result.returncode != 0:
                    if self._logger is not None:
                        self._logger.error(
                            f"Error fetching branch '{branch}': {fetch_result.stderr}"
                        )
                    raise Exception("Git fetch failed")

                checkout_result = subprocess.run(
                    ["git", "-C", repo_path, "checkout", branch],
                    capture_output=True,
                    text=True,
                )

                if checkout_result.returncode != 0:
                    if self._logger is not None:
                        self._logger.warning(
                            f"Local branch '{branch}' checkout failed. "
                            f"Trying to create it from 'origin/{branch}': {checkout_result.stderr}"
                        )

                    checkout_result = subprocess.run(
                        [
                            "git",
                            "-C",
                            repo_path,
                            "checkout",
                            "-B",
                            branch,
                            f"origin/{branch}",
                        ],
                        capture_output=True,
                        text=True,
                    )

                    if checkout_result.returncode != 0:
                        if self._logger is not None:
                            self._logger.error(
                                f"Error checking out branch '{branch}': {checkout_result.stderr}"
                            )
                        raise Exception("Git checkout failed")

                pull_result = subprocess.run(
                    ["git", "-C", repo_path, "pull", "origin", branch],
                    capture_output=True,
                    text=True,
                )

                if pull_result.returncode != 0:
                    if self._logger is not None:
                        self._logger.error(
                            f"Error pulling branch '{branch}': {pull_result.stderr}"
                        )
                    raise Exception("Git pull failed")

            else:
                if self._logger is not None:
                    self._logger.info(
                        f"Repository '{repo_name}' already exists. "
                        "Automatically pulling latest changes DISABLED, "
                        "no changes to the local repository..."
                    )

        else:
            if self._logger is not None:
                self._logger.info(
                    f"Cloning branch '{branch}' from repository '{repo_url}' into '{repo_path}'..."
                )

            clone_result = subprocess.run(
                ["git", "clone", "-b", branch, repo_url, repo_path],
                capture_output=True,
                text=True,
            )

            if clone_result.returncode != 0:
                if self._logger is not None:
                    self._logger.error(f"Error cloning repository: {clone_result.stderr}")
                raise Exception("Git clone failed")

        return repo_path, _workspace_exists

    def _fetch_repo_snapshot(
            self,
            repo_url: str,
            workspace: str,
            commit_sha: str,
            branch: str = "master",
    ) -> tuple[str, bool]:
        """
        Clone a Git repository into a workspace if needed, then check out a specific
        commit SHA snapshot.

        If the repository already exists locally, it fetches remote updates first so
        that the requested commit can be resolved if it is not present locally yet.

        :param repo_url: URL of the Git repository.
        :param workspace: Path to the workspace directory.
        :param commit_sha: Commit SHA to check out.
        :param branch: Branch to clone initially if the repository does not exist.
        :return: Tuple containing:
                 - local path to the repository
                 - whether the repository already existed
        :raises Exception: If clone, fetch, or checkout fails.
        """
        _workspace_exists = False

        if not os.path.exists(workspace):
            os.makedirs(workspace)

        repo_name = os.path.splitext(os.path.basename(repo_url))[0]
        repo_path = os.path.join(workspace, repo_name)

        if os.path.exists(repo_path):
            _workspace_exists = True

            if self._logger is not None:
                self._logger.info(
                    f"Repository '{repo_name}' already exists. Fetching updates before checkout..."
                )

            result = subprocess.run(
                ["git", "-C", repo_path, "fetch", "--all", "--tags"],
                capture_output=True,
                text=True,
            )

            if result.returncode != 0:
                if self._logger is not None:
                    self._logger.error(f"Error fetching repository: {result.stderr}")
                raise Exception("Git fetch failed")

        else:
            if self._logger is not None:
                self._logger.info(
                    f"Cloning repository '{repo_url}' into '{repo_path}'..."
                )

            result = subprocess.run(
                ["git", "clone", "-b", branch, repo_url, repo_path],
                capture_output=True,
                text=True,
            )

            if result.returncode != 0:
                if self._logger is not None:
                    self._logger.error(f"Error cloning repository: {result.stderr}")
                raise Exception("Git clone failed")

        if self._logger is not None:
            self._logger.info(
                f"Checking out repository '{repo_name}' at commit '{commit_sha}'..."
            )

        result = subprocess.run(
            ["git", "-C", repo_path, "checkout", commit_sha],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            if self._logger is not None:
                self._logger.error(
                    f"Error checking out commit '{commit_sha}': {result.stderr}"
                )
            raise Exception("Git checkout failed")

        return repo_path, _workspace_exists


    def _create_git_version_tag(
            self,
            repo_path: str,
            version: str,
            commit_sha: str,
            push: bool = True,
    ) -> bool:
        """
        Create an annotated Git tag for a backend version and attach it to a commit SHA.

        If the tag already exists locally or remotely, it will not be created or pushed again.

        :param repo_path: Path to the local Git repository.
        :param version: Version tag, for example 'v0.0.1'.
        :param commit_sha: Commit SHA to tag.
        :param push: If True, push the tag to origin.
        :return: True if a new tag was created, False if the tag already existed.
        :raises Exception: If Git commands fail.
        """

        # Make sure we know about remote tags.
        fetch_tags_result = subprocess.run(
            ["git", "-C", repo_path, "fetch", "origin", "--tags"],
            capture_output=True,
            text=True,
        )

        if fetch_tags_result.returncode != 0:
            if self._logger is not None:
                self._logger.error(
                    f"Error fetching Git tags: {fetch_tags_result.stderr}"
                )
            raise Exception("Git fetch tags failed")

        # Check if tag exists locally.
        local_tag_result = subprocess.run(
            ["git", "-C", repo_path, "tag", "--list", version],
            capture_output=True,
            text=True,
        )

        if local_tag_result.returncode != 0:
            if self._logger is not None:
                self._logger.error(
                    f"Error checking local Git tag '{version}': {local_tag_result.stderr}"
                )
            raise Exception("Git local tag check failed")

        if local_tag_result.stdout.strip() == version:
            if self._logger is not None:
                self._logger.info(
                    f"Git tag '{version}' already exists locally. Skipping tag creation and push."
                )
            return False

        # Check if tag exists remotely.
        remote_tag_result = subprocess.run(
            ["git", "-C", repo_path, "ls-remote", "--tags", "origin", version],
            capture_output=True,
            text=True,
        )

        if remote_tag_result.returncode != 0:
            if self._logger is not None:
                self._logger.error(
                    f"Error checking remote Git tag '{version}': {remote_tag_result.stderr}"
                )
            raise Exception("Git remote tag check failed")

        if remote_tag_result.stdout.strip():
            if self._logger is not None:
                self._logger.info(
                    f"Git tag '{version}' already exists on remote. Skipping tag creation and push."
                )
            return False

        tag_message = f"Backend version {version}\nCommit SHA: {commit_sha}"

        tag_result = subprocess.run(
            [
                "git",
                "-C",
                repo_path,
                "tag",
                "-a",
                version,
                commit_sha,
                "-m",
                tag_message,
            ],
            capture_output=True,
            text=True,
        )

        if tag_result.returncode != 0:
            if self._logger is not None:
                self._logger.error(
                    f"Error creating Git tag '{version}': {tag_result.stderr}"
                )
            raise Exception("Git tag creation failed")

        if self._logger is not None:
            self._logger.info(
                f"Created Git tag '{version}' for commit '{commit_sha}'."
            )

        if push:
            push_result = subprocess.run(
                ["git", "-C", repo_path, "push", "origin", version],
                capture_output=True,
                text=True,
            )

            if push_result.returncode != 0:
                if self._logger is not None:
                    self._logger.error(
                        f"Error pushing Git tag '{version}': {push_result.stderr}"
                    )
                raise Exception("Git tag push failed")

            if self._logger is not None:
                self._logger.info(
                    f"Pushed Git tag '{version}' to origin."
                )

        return True

    def _get_commit_sha(self, repo_path: str) -> str:

        """

        Get the current commit SHA of a local Git repository.

        :param repo_path: Path to the local Git repository.

        :return: The current commit SHA.

        :raises Exception: If the path is not a Git repository or the command fails.

        """

        if not os.path.exists(repo_path):

            if self._logger is not None:
                self._logger.error(f"Repository path does not exist: {repo_path}")

            raise Exception("Repository path does not exist")

        result = subprocess.run(

            ["git", "-C", repo_path, "rev-parse", "HEAD"],

            capture_output=True,

            text=True,

        )

        if result.returncode != 0:

            if self._logger is not None:
                self._logger.error(f"Error getting commit SHA: {result.stderr}")

            raise Exception("Git rev-parse failed")

        return result.stdout.strip()

    def register_backend(self,applicationEntry = None,addHistory=True):
        if self._flexCloudConnection and applicationEntry is not None:
            baseKey = self._flexCloudConnector.connectorID + COMMUNICATIONMATRIX.APPLICATIONS + applicationEntry.name
            self._flexCloudConnector.write(baseKey+COMMUNICATIONMATRIX.APPLICATION_NAME,applicationEntry.name)
            self._flexCloudConnector.write(baseKey + COMMUNICATIONMATRIX.APPLICATION_TAG, applicationEntry.tag)
            self._flexCloudConnector.write(baseKey + COMMUNICATIONMATRIX.APPLICATION_TYPE, applicationEntry.type)
            self._flexCloudConnector.write(baseKey + COMMUNICATIONMATRIX.APPLICATION_WORKSPACE, applicationEntry.workspace)
            self._flexCloudConnector.write(baseKey + COMMUNICATIONMATRIX.APPLICATION_REMOTE, applicationEntry.repo_url)
            self._flexCloudConnector.write(baseKey + COMMUNICATIONMATRIX.APPLICATION_STATUS, applicationEntry.status)
            self._flexCloudConnector.write(baseKey + COMMUNICATIONMATRIX.APPLICATION_COMMIT_SHA, applicationEntry.commit_sha)
            self._flexCloudConnector.write(baseKey + COMMUNICATIONMATRIX.APPLICATION_VERSION,applicationEntry.version)
            if addHistory:
                #version history
                self._flexCloudConnector.write_history(baseKey + COMMUNICATIONMATRIX.APPLICATION_VERSION_HISTORY, self._version_history(applicationEntry.version, applicationEntry.commit_sha))

    def _version_history(self, version,commit_sha):
        _history = json.dumps({
            "version": version,
            "commit_sha": commit_sha,
        })
        return _history

    def _check_version_exists(self,applicationEntry,version):
        #Check if the give version is already registered
        _version_exists = False
        _commit_sha = ""

        #Determine key
        baseKey = self._flexCloudConnector.connectorID + COMMUNICATIONMATRIX.APPLICATIONS + applicationEntry.name
        key = baseKey + COMMUNICATIONMATRIX.APPLICATION_VERSION_HISTORY
        _entries=self._flexCloudConnector.read_list(key,all=True)
        #decode versions
        for entry in _entries:
            try:
                if isinstance(entry, bytes):
                    entry = entry.decode("utf-8")
                data = json.loads(entry)
                if data.get("version") == version:
                    _version_exists=True
                    _commit_sha=data.get("commit_sha")
            except json.JSONDecodeError:
                if self._logger is not None:
                    self._logger.warning(f"Skipping invalid JSON entry in Redis key '{key}': {entry}")
                continue

        return _version_exists,_commit_sha

class applicationEntry(object):
    def __init__(self, name="applicationName", type="github", workspace="apps/", repo_url="", tag="",version="v0.0.1",commit_sha=""):
        self._name = name
        if tag == "":
            self._tag = generate_app_tag(prefix=None,unique_length=10)
        else:
            self._tag = tag
        self._type = type
        self._workspace = workspace
        self._repo_url = repo_url
        self._commit_sha = commit_sha
        self._version = version
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

    @property
    def commit_sha(self):
        """Get the commit SHA."""
        return self._commit_sha

    @commit_sha.setter
    def commit_sha(self, value):
        """Set the commit SHA."""
        self._commit_sha = value

    @property
    def version(self):
        """Get the application version."""
        return self._version

    @version.setter
    def version(self, value):
        """Set the application version."""
        self._version = value


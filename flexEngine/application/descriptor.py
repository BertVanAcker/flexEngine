#**************************************************************************
# * Copyright (C) 2025-present Bert Van Acker (B.MKR) <bva.bmkr@gmail.com>
# *
# * This file is part of the flexIA project.
# *
# * flexIA can not be copied and/or distributed without the express
# * permission of Bert Van Acker
# *************************************************************************
from flexEngine.utils.auxiliary import load_config


class applicationDescriptor(object):
    def __init__(self,descriptor='config.yml',verbose=False):
        """Initialize the application descriptor component."""

        self._descriptor = load_config(descriptor)

        # ---- APPLICATION DESCRIPTION ----
        self._appName = self._descriptor.get("name", "Name TBD")
        self._appDescription = self._descriptor.get("description", "Description TBD")

        # ---- MANIFESTS FOR APPLICATION RUNNABLES -----
        self._manifests = []
        _man = self._descriptor.get('manifests', [])
        for m in _man:
            _name = m.get('name', 'N/A')
            _manifestBody = m.get('manifest', None)
            if _manifestBody is not None:
                _type = _manifestBody.get('type', 'N/A')
                _version = _manifestBody.get('version', 'N/A')
                _path = _manifestBody.get('path', 'N/A')
                _manifest = manifest(name=_name,type=_type, version=_version, path=_path)
            else:
                _manifest = manifest(name=_name)
            self._manifests.append(_manifest)

        # ---- DEPENDENCIES FOR APPLICATION RUNNABLES -----
        self._dependecies = []
        _dep = self._descriptor.get('dependencies', [])
        for d in _dep:
            _name = d.get('name', 'N/A')
            _dependecyBody = d.get('dependencies', None)
            if _dependecyBody is not None:
                _component = _dependecyBody.get('depends_on', None)
                _dependency = dependency(name=_name,type="depends_on",component=_component)     #TODO: only "depends_on" dependencies for now, extend!
                self._dependecies.append(_dependency)

        # ---- RUNNABLES OF THE APPLICATION ----
        self._runnables = []
        self._components = self._descriptor.get("components", [])
        for component in self._components:
            _name = component.get('name', 'N/A')
            _type = component.get('type', 'N/A')
            # fetch manifest content
            _runnableManifests = []
            _linkedManifests = component.get('manifests', [])
            for _m in _linkedManifests:
                man_name = _m.get('name', 'N/A')
                for ma in self._manifests:
                    if ma.name == man_name:
                        _runnableManifests.append(ma)
            # fetch dependencies
            _runnableDependencies = []
            for _d in self._dependecies:
                if _d.name == _name:
                    _runnableDependencies.append(_d)

            #fetch requirements content
            _req = component.get("requirements", None)
            if _req is not None:
                _architecture = _req.get("architecture", "generic")
                _os = _req.get("os", "generic")
                _cpu = _req.get("cpu", 1)
                _memory = _req.get("memory", 100)
                _devices = _req.get("devices", None)
                _requirements = requirements(architecture=_architecture, os=_os, cpu=_cpu, memory=_memory, devices=_devices)
            else:
                _requirements = None
            _runnable = runnable(name=_name, type=_type,requirements=_requirements,dependencies=_runnableDependencies,manifest=_runnableManifests)
            self._runnables.append(_runnable)

    @property
    def app_name(self):
        return self._app_name

    @app_name.setter
    def app_name(self, value):
        self._app_name = value

    # Getter and Setter for app_description
    @property
    def app_description(self):
        return self._app_description

    @app_description.setter
    def app_description(self, value):
        self._app_description = value

    # Getter and Setter for runnables
    @property
    def runnables(self):
        return self._runnables

    @runnables.setter
    def runnables(self, value):
        if isinstance(value, list) and all(isinstance(r, runnable) for r in value):
            self._runnables = value
        else:
            raise ValueError("Runnables must be a list of Runnable instances")


class runnable(object):
    def __init__(self, name=None, type=None, manifest=None,dependencies=None, requirements=None):
        """Initialize the application descriptor component."""
        self._name = name
        self._type = type
        self._manifest = manifest
        self._dependencies = dependencies
        self._requirements = requirements

    # Getter and Setter for name
    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, value):
        self._name = value

    # Getter and Setter for type
    @property
    def type(self):
        return self._type

    @type.setter
    def type(self, value):
        self._type = value

    # Getter and Setter for manifest
    @property
    def manifest(self):
        return self._manifest

    @manifest.setter
    def manifest(self, value):
        self._manifest = value

    @property
    def dependencies(self):
        return self._dependencies

    @dependencies.setter
    def dependencies(self, value):
        self._dependencies = value

    def addDependency(self, dependency):
        self._dependencies.append(dependency)

    # Getter and Setter for requirements
    @property
    def requirements(self):
        return self._requirements

    @requirements.setter
    def requirements(self, value):
        self._requirements = value


class requirements(object):
    def __init__(self, architecture=None, os=None, cpu=None, memory=None, devices=None):
        """Initialize the requirements component."""
        self._architecture = architecture
        self._os = os
        self._cpu = cpu
        self._memory = memory
        self._devices = devices

    # Getter and Setter for architecture
    @property
    def architecture(self):
        return self._architecture

    @architecture.setter
    def architecture(self, value):
        self._architecture = value

    # Getter and Setter for os
    @property
    def os(self):
        return self._os

    @os.setter
    def os(self, value):
        self._os = value

    # Getter and Setter for cpu
    @property
    def cpu(self):
        return self._cpu

    @cpu.setter
    def cpu(self, value):
        self._cpu = value

    # Getter and Setter for memory
    @property
    def memory(self):
        return self._memory

    @memory.setter
    def memory(self, value):
        self._memory = value

    # Getter and Setter for devices
    @property
    def devices(self):
        return self._devices

    @devices.setter
    def devices(self, value):
        self._devices = value



class manifest(object):
    def __init__(self,name=None, type=None, version=None, path=None):
        """Initialize the manifest component."""
        self._name = name
        self._type = type
        self._version = version
        self._path = path

        # Getter and Setter for type

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, value):
        self._name = value
    @property
    def type(self):
        return self._type

    @type.setter
    def type(self, value):
        self._type = value

    # Getter and Setter for dockerversion
    @property
    def version(self):
        return self._version

    @version.setter
    def version(self, value):
        self._version = value

    # Getter and Setter for path
    @property
    def path(self):
        return self._path

    @path.setter
    def path(self, value):
        self._path = value


class dependency(object):
    def __init__(self, name=None, type="depends_on", component=None):
        """Initialize the dependency component"""
        self._name = name
        self._type = type
        self._component = component

    @property
    def name(self):
        """Getter for name"""
        return self._name

    @name.setter
    def name(self, value):
        """Setter for name"""
        self._name = value

    @property
    def type(self):
        """Getter for type"""
        return self._type

    @type.setter
    def type(self, value):
        """Setter for type"""
        self._type = value

    @property
    def component(self):
        """Getter for component"""
        return self._component

    @component.setter
    def component(self, value):
        """Setter for component"""
        self._component = value






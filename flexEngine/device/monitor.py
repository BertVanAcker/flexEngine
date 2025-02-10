#**************************************************************************
# * Copyright (C) 2025-present Bert Van Acker (B.MKR) <bva.bmkr@gmail.com>
# *
# * This file is part of the flexIA project.
# *
# * flexIA can not be copied and/or distributed without the express
# * permission of Bert Van Acker
# *************************************************************************
import psutil
import socket
import requests
from flexEngine.utils.constants import *
from flexEngine.utils.auxiliary import *

class monitor(object):
    def __init__(self,name="deviceMonitor",period=60,autostart=False,verbose=False):
        """Initialize a device monitor component.

                Parameters
                ----------
                name : str, optional
                    The name of the component (default is "deviceMonitor").

                period : int, optional
                    Monitoring interval in seconds (default is 60).

                autostart : bool, optional
                    If True, the monitor starts automatically upon initialization (default is False).

                verbose : bool, optional
                    Enables verbose execution logging if set to True (default is False).

                Examples
                --------
                >> monitor = monitor(name="deviceMonitor",verbose=False)

                """
        self._name = name
        self._verbose = verbose
        self._cpuLoad = 0.0
        self._cpuTemperature = 0.0
        self._memoryUsage = 0.0
        self._storageUsage = 0.0
        self._ip_address = GENERIC_STATUS.UNKNOWN
        self._mac_address = GENERIC_STATUS.UNKNOWN
        self._location = GENERIC_STATUS.UNKNOWN

        #Periodic monitoring
        self._period=period
        self.t_monitor = perpetualTimer(self._period,self.update)
        if autostart:
            self.t_monitor.start()

    @property
    def name(self):
        return self._name

    @property
    def verbose(self):
        return self._verbose

    @property
    def cpuLoad(self):
        """"Determine the CPU load of the device"""
        self._cpuLoad = psutil.cpu_percent(interval=1)
        return self._cpuLoad

    @property
    def cpuTemperature(self):
        """"Get the CPU temperature of the device"""
        try:
            sensors = psutil.sensors_temperatures(fahrenheit=False)
            psutil.sensors_temperatures()
            self._cpuTemperature = sensors['coretemp'][0].current if 'coretemp' in sensors else None
        except AttributeError:
            self._cpuTemperature = GENERIC_STATUS.UNKNOWN
        return self._cpuTemperature

    @property
    def memoryUsage(self):
        """"Determine the memory usage of the device"""
        self._memoryUsage = psutil.virtual_memory()
        return self._memoryUsage

    @property
    def storageUsage(self):
        """"Determine the storage usage of the device"""
        self._storageUsage = psutil.disk_usage('/')
        return self._storageUsage

    @property
    def ipAddress(self):
        """"Get the IP address of the device"""
        hostname = socket.gethostname()
        self._ip_address = socket.gethostbyname(hostname)
        return self._ip_address

    @property
    def macAddress(self):
        """"Get the MAC address of the device"""
        # Fetch MAC address
        self._mac_address = None
        try:
            for interface in psutil.net_if_addrs():
                # Check if the interface has a valid MAC address
                if psutil.net_if_addrs()[interface][1].address == self.ipAddress:
                    mac_address = psutil.net_if_addrs()[interface][0].address
                    break
        except AttributeError:
            self._mac_address = GENERIC_STATUS.UNKNOWN
        return self._mac_address

    @property
    def location(self):
        try:
            response = requests.get("https://ipinfo.io", timeout=5)
            location_data = response.json()
            self._location = {
                "city": location_data.get("city"),
                "region": location_data.get("region"),
                "country": location_data.get("country"),
                "coordinates": location_data.get("loc"),
            }
        except requests.RequestException:
            self._location = GENERIC_STATUS.UNKNOWN
        return self._location

    def update(self):
        """Update the device self-monitor """
        self._cpuLoad = self.cpuLoad
        self._cpuTemperature = self._cpuTemperature
        self._memoryUsage = self.memoryUsage
        self._storageUsage = self.storageUsage
        self._ip_address = self.ipAddress
        self._mac_address = self.macAddress
        self._location = self.location
        if self._verbose:
            self.prettyPrint()

    #----------------------------------------PRETTY PRINT---------------------------------------------------------
    def prettyPrint(self):
        """Pretty print the device monitors """
        print(f"CPU Load: {self._cpuLoad}%")
        print("Memory Usage:")
        print(f"  Total: {self._memoryUsage.total / (1024 ** 3):.2f} GB")
        print(f"  Used: {self._memoryUsage.used / (1024 ** 3):.2f} GB")
        print(f"  Free: {self._memoryUsage.available / (1024 ** 3):.2f} GB")
        print(f"  Percentage: {self._memoryUsage.percent}%")
        print(f"IP Address: {self._ip_address}")
        print(f"MAC Address: {self._mac_address}")
        print(f"Temperature: {self._cpuTemperature} °C" if self._cpuTemperature is not GENERIC_STATUS.UNKNOWN else "Temperature: Not available")
        if self._location is not GENERIC_STATUS.UNKNOWN:
            print("Location:")
            print(f"  City: {self._location["city"]}")
            print(f"  Region: {self._location['region']}")
            print(f"  Country: {self._location['country']}")
            print(f"  Coordinates: {self._location['coordinates']}")
        else:
            print("Location: Not available")
        print("Storage Details:")
        print(f"  Total: {self._storageUsage.total / (1024 ** 3):.2f} GB")
        print(f"  Used: {self._storageUsage.used / (1024 ** 3):.2f} GB")
        print(f"  Free: {self._storageUsage.free / (1024 ** 3):.2f} GB")
        print(f"  Percentage: {self._storageUsage.percent}%")




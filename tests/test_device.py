import unittest
import io
from unittest.mock import patch, MagicMock
from flexEngine.device.monitor import *

class TestDeviceMonitor(unittest.TestCase):
    def test_setUp(self):
        self.deviceMonitor = monitor(name="testMonitor",verbose=True)
        self.assertEqual(self.deviceMonitor.name, "testMonitor")
        self.assertEqual(self.deviceMonitor.verbose, True)

    @patch('psutil.cpu_percent', return_value=25.0)
    def test_cpu_load(self, mock_cpu_percent):
        self.deviceMonitor = monitor(name="testMonitor",verbose=True)
        self.assertEqual(self.deviceMonitor.cpuLoad, 25.0)
    @patch('psutil.virtual_memory', return_value=MagicMock(total=8e9, used=4e9, available=4e9, percent=50))
    def test_memory_usage(self, mock_virtual_memory):
        self.deviceMonitor = monitor(name="testMonitor",verbose=True)
        memory = self.deviceMonitor.memoryUsage
        self.assertEqual(memory.total, 8e9)
        self.assertEqual(memory.used, 4e9)
        self.assertEqual(memory.available, 4e9)
        self.assertEqual(memory.percent, 50)

    @patch('psutil.disk_usage', return_value=MagicMock(total=500e9, used=250e9, free=250e9, percent=50))
    def test_storage_usage(self, mock_disk_usage):
        self.deviceMonitor = monitor(name="testMonitor",verbose=True)
        storage = self.deviceMonitor.storageUsage
        self.assertEqual(storage.total, 500e9)
        self.assertEqual(storage.used, 250e9)
        self.assertEqual(storage.free, 250e9)
        self.assertEqual(storage.percent, 50)

    @patch('socket.gethostbyname', return_value='192.168.1.1')
    def test_ip_address(self, mock_gethostbyname):
        self.deviceMonitor = monitor(name="testMonitor",verbose=True)
        self.assertEqual(self.deviceMonitor.ipAddress, '192.168.1.1')

    def test_prettyprint(self):
        self.deviceMonitor = monitor(name="testMonitor")
        self.deviceMonitor.update()


        # Capture the output of print_status() by patching sys.stdout.
        with patch('sys.stdout', new=io.StringIO()) as out:
            self.deviceMonitor.prettyPrint()
            output = out.getvalue()

        # Split the captured output into lines.
        lines = output.strip().splitlines()

        # List of regex patterns corresponding to each expected output line.
        # Note: We allow any numeric values (using [\d\.]+) while verifying the labels.
        expected_patterns = [
            r"^CPU Load:\s+\d+(\.\d+)?%$",  # e.g., "CPU Load: 3.2%"
            r"^Memory Usage:$",  # "Memory Usage:"
            r"^ {2}Total:\s+[\d\.]+\s+GB$",  # "  Total: <number> GB"
            r"^ {2}Used:\s+[\d\.]+\s+GB$",  # "  Used: <number> GB"
            r"^ {2}Free:\s+[\d\.]+\s+GB$",  # "  Free: <number> GB"
            r"^ {2}Percentage:\s+[\d\.]+%$",  # "  Percentage: <number>%"
            r"^IP Address:\s+.+$",  # "IP Address: <value>"
            r"^MAC Address:\s+.+$",  # "MAC Address: <value>"
            r"^Temperature:\s+[\d\.]+\s+°C$",  # "Temperature: <number> °C"
            r"^Location:$",  # "Location:"
            r"^ {2}City:\s+Ieper$",  # "  City: Ieper"
            r"^ {2}Region:\s+Flanders$",  # "  Region: Flanders"
            r"^ {2}Country:\s+BE$",  # "  Country: BE"
            r"^ {2}Coordinates:\s+[\d\.]+,\s*[\d\.]+$",  # "  Coordinates: <number>,<number>"
            r"^Storage Details:$",  # "Storage Details:"
            r"^ {2}Total:\s+[\d\.]+\s+GB$",  # "  Total: <number> GB"
            r"^ {2}Used:\s+[\d\.]+\s+GB$",  # "  Used: <number> GB"
            r"^ {2}Free:\s+[\d\.]+\s+GB$",  # "  Free: <number> GB"
            r"^ {2}Percentage:\s+[\d\.]+%$"  # "  Percentage: <number>%"
        ]

        # Verify that the number of output lines matches our expectation.
        self.assertEqual(len(lines), len(expected_patterns),
                         "The number of output lines does not match the expected format.")

        # Check each line against its expected regular expression.
        for line, pattern in zip(lines, expected_patterns):
            with self.subTest(line=line, pattern=pattern):
                self.assertRegex(line, pattern)


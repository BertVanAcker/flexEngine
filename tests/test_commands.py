import unittest
from unittest.mock import patch
import time
import io
import redis
# Import your compose_command function from its module.
# Adjust the module name as needed. For this example, we assume it is in compose_command.py.
from flexEngine.communication.composer import compose_command
from flexEngine.cloud.connector import *
from flexEngine.security.security import *
from flexEngine.utils.constants import *

class TestComposeCommand(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # Use a dedicated test Redis database (db=15) and flush it before testing.
        cls.redis_host = 'localhost'
        cls.redis_port = 6379
        cls.redis_db = 15  # Use a non‐default db for testing
        cls.redis_client = redis.StrictRedis(
            host=cls.redis_host, port=cls.redis_port, db=cls.redis_db, decode_responses=False
        )
        cls.redis_client.flushdb()

    @classmethod
    def tearDownClass(cls):
        # Clean up the test Redis database.
        cls.redis_client.flushdb()

    def test_compose_command(self):
        # Generate and store secret
        secret = generate_shared_secret(32)
        register_os_variable(secret, env_var_name=SECURITY_STORE.SHARED_SECRET)

        # Define input parameters.
        cmd_type = COMMAND_TYPE.OTA_UPDATE
        params = {"param1": "value1"}
        priority = 3
        expiry_seconds = 120

        # Call the function under test.
        command = compose_command(cmd_type, params, priority, expiry_seconds)

        # Verify that the command fields are computed as expected.
        self.assertEqual(command["cmd"], cmd_type,
                         "The command type should be set correctly.")
        self.assertEqual(command["priority"], priority,
                         "The priority should be set correctly.")
        self.assertEqual(command["params"], params,
                         "The parameters should be stored correctly.")
        # Verify command
        _isValid = verify_signature(command)
        self.assertEqual(_isValid, True,
                         "The command signature is incorrect.")


    def test_receive_OTA_UPDATE(self):
        # Patch sys.stdout to capture printed output.
        with patch('sys.stdout', new=io.StringIO()) as fake_out:
            # Create an instance of flexCloudConnector that uses the test Redis database.
            connector = flexCloudConnector(
                name="flexCloudConnector",
                primaryHardwareID="Oberon-5",
                projectID="sandbox",
                key="3fg445",
                redis_host=self.redis_host,
                redis_port=self.redis_port,
                redis_db=self.redis_db,
                verbose=True
            )
            # Allow time for the listener thread to start and subscribe.
            time.sleep(0.2)

            # Generate and store secret
            secret = generate_shared_secret(32)
            register_os_variable(secret, env_var_name=SECURITY_STORE.SHARED_SECRET)

            # Prepare a command message as a JSON-encoded byte string.
            cmd_type = COMMAND_TYPE.OTA_UPDATE
            params = {"param1": "value1"}
            priority = 3
            expiry_seconds = 120

            # Call the function under test.
            command = compose_command(cmd_type, params, priority, expiry_seconds)
            command_data = json.dumps(command).encode('utf-8')

            # Determine the channel to publish to.
            _commandKey = connector.connectorID+COMMUNICATIONMATRIX.COMMAND
            channel = _commandKey
            # Publish the command message to Redis.
            self.redis_client.publish(channel, command_data)

            # Wait for the message to be processed.
            time.sleep(0.2)

            # Retrieve the captured printed output.
            output = fake_out.getvalue()

        # Verify that the connector printed that a command was received.
        self.assertIn("Command received.", output,
                      "The connector did not print that a command was received.")

        self.assertIn("OTA UPDATE command received.", output,
                      "The command processor did not receive the OTA UPDATE correclty.")

    def test_receive_PING(self):
        # Patch sys.stdout to capture printed output.
        with patch('sys.stdout', new=io.StringIO()) as fake_out:
            # Create an instance of flexCloudConnector that uses the test Redis database.
            connector = flexCloudConnector(
                name="flexCloudConnector",
                primaryHardwareID="Oberon-5",
                projectID="sandbox",
                key="3fg445",
                redis_host=self.redis_host,
                redis_port=self.redis_port,
                redis_db=self.redis_db,
                verbose=True
            )
            # Allow time for the listener thread to start and subscribe.
            time.sleep(0.2)

            # Generate and store secret
            secret = generate_shared_secret(32)
            register_os_variable(secret, env_var_name=SECURITY_STORE.SHARED_SECRET)

            # Prepare a command message as a JSON-encoded byte string.
            cmd_type = COMMAND_TYPE.PING
            params = {"param1": "value1"}
            priority = 3
            expiry_seconds = 120

            # Call the function under test.
            command = compose_command(cmd_type, params, priority, expiry_seconds)
            command_data = json.dumps(command).encode('utf-8')

            # Determine the channel to publish to.
            _commandKey = connector.connectorID+COMMUNICATIONMATRIX.COMMAND
            channel = _commandKey
            # Publish the command message to Redis.
            self.redis_client.publish(channel, command_data)

            # Wait for the message to be processed.
            time.sleep(0.2)

            # Retrieve the captured printed output.
            output = fake_out.getvalue()

        # Verify that the connector printed that a command was received.
        self.assertIn("Command received.", output,
                      "The connector did not print that a command was received.")

        self.assertIn("PING command received.", output,
                      "The command processor did not receive the OTA UPDATE correclty.")



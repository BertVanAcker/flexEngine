import unittest
import time
import redis
from unittest.mock import patch, MagicMock
import io
import json

from flexEngine.cloud.connector import flexCloudConnector
from flexEngine.communication.matrix import *
from flexEngine.utils.constants import *
from flexEngine.security.security import *
from flexEngine.communication.composer import *

class TestFlexCloudConnectorRealRedis(unittest.TestCase):
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


    def test_receives_and_reacts_to_redis_command(self):
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

        self.assertIn("OTA_UPDATE command received.", output,
                      "The command processor did not receive the OTA UPDATE correclty.")

#------------------------------------------------------------------
# Unit Tests Using a Real Redis Instance
#------------------------------------------------------------------
class TestFlexCloudConnectorReadWrite(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # Use a dedicated test Redis database (db=15) and ensure it's clean.
        cls.redis_host = 'localhost'
        cls.redis_port = 6379
        cls.redis_db = 15  # Dedicated test database
        # Create a Redis client with decode_responses=True for easier testing.
        cls.redis_client = redis.StrictRedis(host=cls.redis_host,
                                               port=cls.redis_port,
                                               db=cls.redis_db,
                                               decode_responses=True)
        cls.redis_client.flushdb()

    @classmethod
    def tearDownClass(cls):
        # Clean up the test Redis database.
        cls.redis_client.flushdb()

    def setUp(self):
        # Create a connector instance that uses our test Redis database.
        self.connector = flexCloudConnector(
            name="flexCloudConnector",
            primaryHardwareID="Oberon-5",
            projectID="sandbox",
            key="3fg445",
            redis_host=self.redis_host,
            redis_port=self.redis_port,
            redis_db=self.redis_db,
            verbose=True
        )
        # Flush the database before each test to start fresh.
        self.connector.redis_client.flushdb()

    def get_update_message(self, channel, timeout=1.0):
        """
        Helper method: subscribe to an update channel and wait (up to timeout seconds)
        for a message. Returns the message dict or None.
        """
        pubsub = self.connector.redis_client.pubsub()
        pubsub.subscribe(channel)
        start_time = time.time()
        update_message = None
        while time.time() - start_time < timeout:
            message = pubsub.get_message(timeout=0.1)
            if message and message['type'] == 'subscribe':
                update_message = message
                break
            time.sleep(0.05)
        pubsub.close()
        return update_message

    def test_write_with_string(self):
        update_channel = f"{self.connector.connectorID}_update"
        # Write a simple string value.
        key = "test_key"
        self.connector.write(key, "test_value")
        # Verify that an update message is published.
        message = self.get_update_message(update_channel, timeout=2)
        self.assertIsNotNone(message, "Update message not received for write with string.")
        # Verify that the value is stored correctly.
        stored_value = self.connector.redis_client.get(key)
        self.assertEqual(stored_value, "test_value", "Stored value does not match expected string.")
        # Test the read method.
        read_value = self.connector.read(key)
        self.assertEqual(read_value, "test_value", "Read value does not match expected string.")

    def test_write_with_dict(self):
        update_channel = f"{self.connector.connectorID}_update"
        test_dict = {"a": 1, "b": 2}
        key = "dict_key"
        # Write a dictionary value.
        self.connector.write(key, test_dict)
        # Verify that an update message is published.
        message = self.get_update_message(update_channel)
        self.assertIsNotNone(message, "Update message not received for write with dict.")
        # Verify that the value is stored as a JSON string.
        stored_value = self.connector.redis_client.get(key)
        self.assertEqual(stored_value, json.dumps(test_dict), "Stored value does not match expected JSON string.")
        # Test the read method (should deserialize the JSON into a dict).
        read_value = self.connector.read(key)
        self.assertEqual(read_value, test_dict, "Read value does not match expected dictionary.")

    def test_write_storage_and_read_list(self):
        update_channel = f"{self.connector.connectorID}_update"
        key = "list_key"
        # Write multiple values into a list using write_storage.
        self.connector.write_storage(key, "value1")
        message1 = self.get_update_message(update_channel)
        self.assertIsNotNone(message1, "Update message not received for first write_storage call.")

        self.connector.write_storage(key, "value2")
        message2 = self.get_update_message(update_channel)
        self.assertIsNotNone(message2, "Update message not received for second write_storage call.")

        # Since write_storage uses LPUSH, the list order will be reversed:
        # The most recently pushed element is at index 0.
        stored_list = self.connector.redis_client.lrange(key, 0, -1)
        self.assertEqual(stored_list, ["value2", "value1"], "Stored list does not match expected order.")

        # Test read_list with all=True to return the entire list.
        read_list_all = self.connector.read_list(key, all=True)
        self.assertEqual(read_list_all, ["value2", "value1"], "Read list (all) does not match expected order.")

        # Test read_list with queueSize=1 to return only the first element.
        read_list_single = self.connector.read_list(key, queueSize=0)
        self.assertEqual(read_list_single, ["value2"], "Read list (queueSize=1) does not match expected value.")





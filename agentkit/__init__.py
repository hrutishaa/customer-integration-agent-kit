"""Customer Integration Agent Kit."""
from agentkit.connectors.base import Connector, ConnectorError, Record
from agentkit.connectors.csv_connector import CSVConnector
from agentkit.connectors.rest import RestConnector, RetryPolicy

__all__ = ["CSVConnector", "Connector", "ConnectorError", "Record", "RestConnector", "RetryPolicy"]
__version__ = "0.1.0"

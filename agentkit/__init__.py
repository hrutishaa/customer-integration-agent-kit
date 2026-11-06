"""Customer Integration Agent Kit."""
from agentkit.connectors.base import Connector, ConnectorError, Record
from agentkit.connectors.crm import CRMConnector, SyncResult
from agentkit.connectors.csv_connector import CSVConnector
from agentkit.connectors.rest import RestConnector, RetryPolicy
from agentkit.connectors.sql import SQLConnector
from agentkit.connectors.webhook import WebhookError, WebhookReceiver, WebhookResult, sign
from agentkit.mapping import (
    Change,
    DryRun,
    Mapper,
    MappingError,
    MapResult,
    RecordError,
    load_mapping,
)

__all__ = [
    "CRMConnector",
    "CSVConnector",
    "Change",
    "Connector",
    "ConnectorError",
    "DryRun",
    "MapResult",
    "Mapper",
    "MappingError",
    "Record",
    "RecordError",
    "RestConnector",
    "RetryPolicy",
    "SQLConnector",
    "SyncResult",
    "WebhookError",
    "WebhookReceiver",
    "WebhookResult",
    "load_mapping",
    "sign",
]
__version__ = "0.2.0"

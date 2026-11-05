"""Customer Integration Agent Kit."""
from agentkit.connectors.base import Connector, ConnectorError, Record
from agentkit.connectors.crm import CRMConnector, SyncResult
from agentkit.connectors.csv_connector import CSVConnector
from agentkit.connectors.rest import RestConnector, RetryPolicy
from agentkit.connectors.sql import SQLConnector
from agentkit.connectors.webhook import WebhookError, WebhookReceiver, WebhookResult, sign
from agentkit.mapping import (
    DiffReport,
    FieldError,
    FieldSpec,
    MappingError,
    MappingReport,
    MappingSpec,
    RecordError,
    SchemaMapper,
)

__all__ = [
    "CRMConnector",
    "CSVConnector",
    "Connector",
    "ConnectorError",
    "DiffReport",
    "FieldError",
    "FieldSpec",
    "MappingError",
    "MappingReport",
    "MappingSpec",
    "Record",
    "RecordError",
    "RestConnector",
    "RetryPolicy",
    "SQLConnector",
    "SchemaMapper",
    "SyncResult",
    "WebhookError",
    "WebhookReceiver",
    "WebhookResult",
    "sign",
]
__version__ = "0.2.0"

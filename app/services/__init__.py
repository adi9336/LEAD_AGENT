"""services package: scoring, llm_gateway, clients, lead_service, scheduler glue."""
from app.services.lead_service import process_lead

__all__ = ["process_lead"]

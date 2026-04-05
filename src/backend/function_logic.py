"""
✏️ DEVELOPER CODE - LogResultFn Business Logic

Logs a processing summary for audit and tracking.
Records file metadata, parsing counts, validation result, and webhook status.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Dict, Any
from chask_foundation.backend.models import OrchestrationEvent
from api.orchestrator_requests import orchestrator_api_manager

logger = logging.getLogger()
logger.setLevel(logging.INFO)

PIPELINE_ID = 25957


class FunctionBackend:
    def __init__(self, orchestration_event: OrchestrationEvent):
        self.orchestration_event = orchestration_event
        logger.info(f"Initialized FunctionBackend for org: {orchestration_event.organization.organization_id}")

    def process_request(self) -> str:
        tool_args = self._extract_tool_args()

        logger.info(f"Tool args received: {json.dumps(tool_args)}")
        raw_summary = tool_args.get("processing_summary")
        verbose = tool_args.get("verbose", False)

        if raw_summary:
            # Standard path: processing_summary provided as JSON string or dict
            if isinstance(raw_summary, str):
                try:
                    summary = json.loads(raw_summary)
                except json.JSONDecodeError as e:
                    raise ValueError(f"Invalid JSON in processing_summary: {e}")
            else:
                summary = raw_summary
        elif any(k in tool_args for k in ("file_name", "source", "counts", "validation", "webhook")):
            # Fallback: LLM passed fields directly as individual args
            summary = {k: v for k, v in tool_args.items() if k != "verbose"}
        else:
            raise ValueError("Missing required parameter: processing_summary")

        if verbose:
            logger.info(f"Received processing summary: {json.dumps(summary)}")

        log_entry = self._build_log_entry(summary)

        # Log to CloudWatch
        logger.info(f"Pipeline execution log: {json.dumps(log_entry)}")

        return json.dumps(log_entry)

    def _build_log_entry(self, summary: Dict[str, Any]) -> Dict[str, Any]:
        source_info = summary.get("source", {})
        source_name = source_info.get("name", "Unknown")
        last_four = source_info.get("last_four_digits", "????")
        source_label = f"{source_name} (****{last_four})"

        period = summary.get("statement_period", {})
        period_start = period.get("start", "unknown")
        period_end = period.get("end", "unknown")

        counts = summary.get("counts", {})
        transactions = counts.get("transactions", 0)
        fees = counts.get("fees", 0)
        payments = counts.get("payments", 0)

        validation = summary.get("validation", {})
        validation_status = validation.get("status", "unknown")

        webhook = summary.get("webhook", {})
        webhook_sent = webhook.get("sent", False)
        webhook_http = webhook.get("http_status")
        webhook_error = webhook.get("error")

        overall_status = self._determine_overall_status(validation_status, webhook_sent, webhook_error)

        log_entry = {
            "log_type": "pipeline_execution",
            "pipeline_id": PIPELINE_ID,
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": source_label,
            "period": f"{period_start} to {period_end}",
            "holder": summary.get("holder_masked", "unknown"),
            "summary": {
                "transactions_parsed": transactions,
                "fees_parsed": fees,
                "payments_parsed": payments,
                "total_entries": transactions + fees + payments,
            },
            "validation_result": validation_status,
            "validation_warnings": validation.get("warnings", []),
            "validation_errors": validation.get("errors", []),
            "webhook_result": {
                "delivered": webhook_sent,
                "http_status": webhook_http,
                "records_created": webhook.get("created", 0),
                "duplicates_skipped": webhook.get("skipped_duplicates", 0),
            },
            "overall_status": overall_status,
        }

        return log_entry

    def _determine_overall_status(self, validation_status: str, webhook_sent: bool, webhook_error) -> str:
        if validation_status == "rejected":
            return "rejected"
        if not webhook_sent or webhook_error:
            return "failed"
        if validation_status == "partial_with_warnings":
            return "partial"
        return "success"

    def _extract_tool_args(self) -> Dict[str, Any]:
        extra_params = self.orchestration_event.extra_params or {}
        tool_calls = extra_params.get("tool_calls", [])
        if not tool_calls:
            logger.warning("No tool calls found in orchestration event")
            return {}
        tool_call = tool_calls[0]
        return tool_call.get("args", {})

    def _send_response(self, message: str, is_error: bool = False) -> bool:
        try:
            original_extra_params = self.orchestration_event.extra_params or {}
            tool_call_id = None
            tool_name = None
            if ("tool_calls" in original_extra_params and
                original_extra_params["tool_calls"]):
                tool_call = original_extra_params["tool_calls"][0]
                tool_call_id = tool_call.get("id")
                tool_name = tool_call.get("name")

            extra_params = {
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "is_error": is_error
            }

            if original_extra_params.get("is_test"):
                extra_params["is_test"] = True
                if original_extra_params.get("test_execution_uuid"):
                    extra_params["test_execution_uuid"] = original_extra_params["test_execution_uuid"]

            evolve_response = orchestrator_api_manager.call(
                "evolve_event",
                parent_event_uuid=str(self.orchestration_event.event_id),
                event_type="function_call_response",
                source="agent",
                target="orchestrator",
                prompt=message,
                extra_params=extra_params,
                access_token=self.orchestration_event.access_token,
                organization_id=self.orchestration_event.organization.organization_id,
            )

            if evolve_response.get("status_code") not in (200, 201):
                error_msg = evolve_response.get("error", "Unknown error")
                raise Exception(f"Failed to evolve event: {error_msg}")

            evolved_uuid = evolve_response.get("uuid")
            if not evolved_uuid:
                raise Exception("API response missing uuid for evolved event")

            response_event = self.orchestration_event.model_copy(deep=True)
            response_event.event_id = evolved_uuid
            response_event.event_type = "function_call_response"
            response_event.source = "agent"
            response_event.target = "orchestrator"
            response_event.prompt = message
            response_event.extra_params = evolve_response.get("extra_params", extra_params)

            orchestrator_api_manager.call(
                "forward_oe_to_kafka",
                orchestration_event=response_event.model_dump(),
                topic="orchestrator",
                access_token=response_event.access_token,
                organization_id=response_event.organization.organization_id,
            )

            logger.info(
                f"Response sent to orchestrator via Kafka "
                f"[evolved from {self.orchestration_event.event_id} -> {evolved_uuid}]"
            )
            self.response_event_sent = True
            return True

        except Exception as e:
            logger.error(f"Failed to send response to orchestrator: {e}")
            return False

    def _extract_widget_params(self, param_names: list) -> Dict[str, Any]:
        widget_data = self.orchestration_event.extra_params.get("widget_data", {})
        widgets = widget_data.get("widgets", [])
        widget_values = {w.get("name"): w.get("value") for w in widgets}
        result = {}
        for param_name in param_names:
            result[param_name] = widget_values.get(param_name) or widget_data.get(param_name)
        return result

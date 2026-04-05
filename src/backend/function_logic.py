"""
✏️ DEVELOPER CODE - LogResultFn Business Logic

Logs a processing summary for audit and tracking.
Downloads result files from session, builds structured log entry.
"""

import json
import logging
import urllib.request
from datetime import datetime, timezone
from typing import Dict, Any, Optional
from chask_foundation.backend.models import OrchestrationEvent
from api.orchestrator_requests import orchestrator_api_manager
from api.files_requests import files_api_manager

logger = logging.getLogger()
logger.setLevel(logging.INFO)

PIPELINE_ID = 25957


class FunctionBackend:
    def __init__(self, orchestration_event: OrchestrationEvent):
        self.orchestration_event = orchestration_event
        logger.info(f"Initialized FunctionBackend for org: {orchestration_event.organization.organization_id}")

    def process_request(self) -> str:
        tool_args = self._extract_tool_args()
        verbose = tool_args.get("verbose", False)

        # Download all result files from the session
        statement = self._download_json_from_session_safe("parsed_statement.json")
        validation = self._download_json_from_session_safe("validation_result.json")
        webhook = self._download_json_from_session_safe("webhook_result.json")

        if verbose:
            logger.info(f"Downloaded files - statement: {statement is not None}, validation: {validation is not None}, webhook: {webhook is not None}")

        # Build summary from downloaded files
        source = statement.get("source", {}) if statement else {}
        period = statement.get("statement_period", {}) if statement else {}
        account = statement.get("account_info", {}) if statement else {}

        n_txn = len(statement.get("transactions", [])) if statement else 0
        n_fees = len(statement.get("fees", [])) if statement else 0
        n_payments = len(statement.get("payments", [])) if statement else 0

        validation_status = validation.get("status", "unknown") if validation else "unknown"
        validation_errors = validation.get("validation", {}).get("errors", []) if validation else []
        validation_warnings = validation.get("validation", {}).get("warnings", []) if validation else []

        webhook_sent = webhook.get("webhook_sent", False) if webhook else False
        webhook_http = webhook.get("http_status") if webhook else None
        webhook_response = webhook.get("response", {}) if webhook else {}
        webhook_error = webhook.get("error") if webhook else None

        overall = self._determine_overall_status(validation_status, webhook_sent, webhook_error)

        log_entry = {
            "log_type": "pipeline_execution",
            "pipeline_id": PIPELINE_ID,
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": f"{source.get('name', 'Unknown')} (****{source.get('last_four_digits', '????')})",
            "period": f"{period.get('start', '?')} to {period.get('end', '?')}",
            "holder": account.get("holder_name", "unknown"),
            "summary": {
                "transactions_parsed": n_txn,
                "fees_parsed": n_fees,
                "payments_parsed": n_payments,
                "total_entries": n_txn + n_fees + n_payments,
            },
            "validation_result": validation_status,
            "validation_warnings": validation_warnings,
            "validation_errors": validation_errors,
            "webhook_result": {
                "delivered": webhook_sent,
                "http_status": webhook_http,
                "records_created": webhook_response.get("created", 0) if isinstance(webhook_response, dict) else 0,
                "duplicates_skipped": webhook_response.get("skipped_duplicates", 0) if isinstance(webhook_response, dict) else 0,
            },
            "overall_status": overall,
        }

        logger.info(f"Pipeline execution log: {json.dumps(log_entry)}")
        return json.dumps(log_entry)

    def _download_json_from_session_safe(self, filename: str) -> Optional[dict]:
        """Download JSON file from session. Returns None if not found."""
        try:
            return self._download_json_from_session(filename)
        except (ValueError, Exception) as e:
            logger.warning(f"Could not download {filename}: {e}")
            return None

    def _download_json_from_session(self, filename: str) -> dict:
        """Download a JSON file from the session by filename."""
        session_uuid = self.orchestration_event.orchestration_session_uuid
        files_response = files_api_manager.call(
            "get_all_files_for_session",
            orchestration_session_uuid=session_uuid,
            access_token=self.orchestration_event.access_token,
            organization_id=self.orchestration_event.organization.organization_id,
        )
        files = files_response.get("files", [])
        target = next((f for f in files if f.get("file_name") == filename), None)
        if not target:
            raise ValueError(f"File '{filename}' not found in session")
        with urllib.request.urlopen(target["file_url"]) as resp:
            return json.loads(resp.read().decode("utf-8"))

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

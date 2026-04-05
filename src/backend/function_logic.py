"""
✏️ DEVELOPER CODE - IMPLEMENT YOUR BUSINESS LOGIC HERE

This file contains the business logic for your Lambda function.
You should implement the process_request() method with your custom logic.

The handler.py file is infrastructure code and should NOT be modified.
"""

import json
import logging
from typing import Dict, Any, Optional
from chask_foundation.backend.models import OrchestrationEvent
from api.orchestrator_requests import orchestrator_api_manager

logger = logging.getLogger()
logger.setLevel(logging.INFO)


class FunctionBackend:
    """
    Backend class containing business logic for LogResultFn.

    This class contains ONLY business logic - no infrastructure concerns.

    Developers implement the process_request() method to:
    - Extract parameters using helper methods
    - Perform business logic
    - Return result strings

    The handler (infrastructure code) automatically:
    - Sends responses to the orchestrator
    - Preserves test flags
    - Handles errors and exceptions
    - Frees the agent
    """

    def __init__(self, orchestration_event: OrchestrationEvent):
        """
        Initialize backend with orchestration event.

        Args:
            orchestration_event: The orchestration event containing all request data
        """
        self.orchestration_event = orchestration_event
        logger.info(f"Initialized FunctionBackend for org: {orchestration_event.organization.organization_id}")

    def process_request(self) -> str:
        """
        ✏️ IMPLEMENT YOUR BUSINESS LOGIC HERE

        This method is called by the handler to process the function request.
        Extract parameters, perform your business logic, and return a result string.

        The handler automatically:
        - Catches any exceptions you raise
        - Sends responses to the orchestrator (both success and error)
        - Preserves test flags (is_test, is_node_test)
        - Frees the agent even if your code crashes

        Returns:
            String result to display to the user (handler will send it to orchestrator)

        Raises:
            Exception: Any exception will be caught by handler and sent as error response
        """
        # Extract parameters from tool call
        tool_args = self._extract_tool_args()

        # Get required parameters
        action = tool_args.get("action")
        if not action:
            error_msg = "Missing required parameter: action"
            logger.error(error_msg)
            raise ValueError(error_msg)

        # Get optional parameters
        verbose = tool_args.get("verbose", False)

        if verbose:
            logger.info(f"Organization ID: {self.orchestration_event.organization.organization_id}")
            logger.info(f"Action: {action}")
            logger.info(f"Tool arguments: {json.dumps(tool_args)}")

        # TODO: Implement your function logic here
        # Example:
        # - Validate input parameters
        # - Access Chask APIs using self.orchestration_event.access_token
        # - Perform business logic
        # - Query databases (using foundationMinimal layer)
        # - Call external services
        # - Process data

        # Example result
        result_message = f"Successfully executed action: {action}"

        logger.info(f"Function completed successfully: {result_message}")

        # Return result - handler will send it to orchestrator
        return result_message

    def _extract_tool_args(self) -> Dict[str, Any]:
        """
        Extract tool call arguments from orchestration event.

        Returns:
            Dictionary of tool call arguments
        """
        extra_params = self.orchestration_event.extra_params or {}
        tool_calls = extra_params.get("tool_calls", [])

        if not tool_calls:
            logger.warning("No tool calls found in orchestration event")
            return {}

        # Get first tool call and return its arguments
        tool_call = tool_calls[0]
        return tool_call.get("args", {})

    def _send_response(self, message: str, is_error: bool = False) -> bool:
        """
        Send the function result back to the orchestrator via Kafka.

        Uses evolve_event API to create proper parent-child event linkage
        for event traceability in the Event Tracking System.

        Args:
            message: Result message to send back
            is_error: Whether this is an error response

        Returns:
            True if event was sent successfully, False otherwise
        """
        try:
            # Extract tool call information from original event
            original_extra_params = self.orchestration_event.extra_params or {}
            tool_call_id = None
            tool_name = None
            if ("tool_calls" in original_extra_params and
                original_extra_params["tool_calls"]):
                tool_call = original_extra_params["tool_calls"][0]
                tool_call_id = tool_call.get("id")
                tool_name = tool_call.get("name")

            # Build extra_params for the response event
            extra_params = {
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "is_error": is_error
            }

            # ====================================================================
            # CRITICAL: Preserve test execution flags for 'chask function test'
            # ====================================================================
            # These flags MUST be preserved to ensure test executions are properly
            # tracked and results are returned to the CLI instead of being routed
            # to the operator. DO NOT REMOVE OR MODIFY THIS CODE.
            if original_extra_params.get("is_test"):
                extra_params["is_test"] = True
                if original_extra_params.get("test_execution_uuid"):
                    extra_params["test_execution_uuid"] = original_extra_params["test_execution_uuid"]
            # ====================================================================

            # Evolve the event to create a linked function_call_response child event
            # This maintains parent-child relationships via evolved_from_uuid
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

            # Reconstruct the evolved event for Kafka forwarding
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

            # Send the evolved orchestration event to orchestrator
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
        """
        Extract widget parameters supporting both production and test formats.

        This helper handles two widget data formats:
        - Production: {"widgets": [{"name": "param", "value": "val"}]}
        - Test: {"param": "val"}

        Args:
            param_names: List of parameter names to extract

        Returns:
            Dictionary with extracted parameter values

        Example:
            params = self._extract_widget_params(["api_token", "api_url"])
            api_token = params["api_token"]
        """
        widget_data = self.orchestration_event.extra_params.get("widget_data", {})

        # Try nested widgets array first (production format)
        widgets = widget_data.get("widgets", [])
        widget_values = {w.get("name"): w.get("value") for w in widgets}

        # Extract params with fallback to direct access (test format)
        result = {}
        for param_name in param_names:
            result[param_name] = widget_values.get(param_name) or widget_data.get(param_name)

        return result

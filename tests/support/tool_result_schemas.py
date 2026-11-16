"""Validate real complete producer values with their documentation contracts."""

from jsonschema import Draft202012Validator, FormatChecker

from pulsara_agent.conversation_kernel.tool_outputs import assemble_tool_business_value


def validate_business_result(schema, business_value, result_state="SUCCESS"):
    value = assemble_tool_business_value(public_fields=business_value.public_fields,
        content_parts=business_value.content, result_state=result_state)
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(value)
    return value

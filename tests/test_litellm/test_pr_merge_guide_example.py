"""
Test file demonstrating how to add tests for LiteLLM PR requirements.

This serves as an example for the PR merge guide.
"""

import pytest
from litellm.types.llms.databricks import DatabricksFunction, DatabricksTool


def test_databricks_function_type_compatibility():
    """Test that DatabricksFunction accepts correct type annotations."""
    # Arrange
    function_data = {
        "name": "test_function",
        "description": "A test function",
        "parameters": {"type": "object", "properties": {}},
    }

    # Act
    databricks_function = DatabricksFunction(**function_data)

    # Assert
    assert databricks_function["name"] == "test_function"
    assert databricks_function["description"] == "A test function"
    assert databricks_function["parameters"] == {"type": "object", "properties": {}}


def test_databricks_tool_creation():
    """Test that DatabricksTool can be created with proper function."""
    # Arrange
    function_data = {
        "name": "test_tool",
        "description": "A test tool",
        "parameters": {"type": "object"},
    }

    # Act
    databricks_tool = DatabricksTool(
        type="function", function=DatabricksFunction(**function_data)
    )

    # Assert
    assert databricks_tool["type"] == "function"
    assert databricks_tool["function"]["name"] == "test_tool"


def test_databricks_function_with_dict_description():
    """Test that DatabricksFunction accepts dict description type."""
    # Arrange
    function_data = {
        "name": "test_function",
        "description": {"text": "A test function", "format": "markdown"},
        "parameters": {},
    }

    # Act
    databricks_function = DatabricksFunction(**function_data)

    # Assert
    assert databricks_function["name"] == "test_function"
    assert isinstance(databricks_function["description"], dict)
    assert databricks_function["description"]["text"] == "A test function"


if __name__ == "__main__":
    # Run the tests
    test_databricks_function_type_compatibility()
    test_databricks_tool_creation()
    test_databricks_function_with_dict_description()
    print("✅ All tests passed! This demonstrates proper test structure.")
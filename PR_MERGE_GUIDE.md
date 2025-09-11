# LiteLLM PR Merge Readiness Guide

This guide will walk you through the step-by-step process to get your Pull Request ready for merging into the LiteLLM repository when checks are failing.

## 🎯 Overview

Based on the LiteLLM contributing guidelines, every PR must meet these requirements:

1. ✅ **Sign the Contributor License Agreement (CLA)**
2. ✅ **Add at least 1 test**
3. ✅ **Pass all linting and formatting checks**
4. ✅ **Pass unit tests**
5. ✅ **Keep scope isolated** (one problem at a time)

## 🚀 Step-by-Step Fix Process

### Step 1: Set Up Your Development Environment

```bash
# Ensure poetry is installed
pip install poetry

# Install development dependencies
make install-dev

# Verify setup
make help
```

### Step 2: Check Current Status

First, identify what's failing by running the checks:

```bash
# Run all linting checks
make lint

# Run unit tests
make test-unit
```

### Step 3: Fix Common Issues

#### **Issue: Code Formatting Failures**

If you see Black formatting errors:

```bash
# Fix automatically
make format

# Verify formatting is fixed
make format-check
```

#### **Issue: Linting Errors (Ruff)**

```bash
# Run only Ruff to see specific issues
make lint-ruff

# Most Ruff issues need manual fixes:
# - Remove unused imports
# - Fix variable naming
# - Add missing type hints
# - Fix code style issues
```

#### **Issue: MyPy Type Checking Errors**

```bash
# Run only MyPy to see type issues
make lint-mypy

# Common fixes:
# - Add type hints: def function(param: str) -> int:
# - Add proper imports: from typing import Dict, List, Optional
# - Use type: ignore comments for complex cases
# - Cast types: cast(str, some_object)
```

Example MyPy fixes:
```python
# Before (causes error)
def process_data(data):
    return data.get("key")

# After (fixed)
from typing import Dict, Any, Optional

def process_data(data: Dict[str, Any]) -> Optional[Any]:
    return data.get("key")
```

#### **Issue: Circular Import Errors**

```bash
# Check for circular imports
make check-circular-imports

# Fix by:
# - Moving imports inside functions
# - Using TYPE_CHECKING imports
# - Refactoring code structure
```

#### **Issue: Import Safety Errors**

```bash
# Check import safety
make check-import-safety

# Fix by ensuring all imports are properly protected
```

### Step 4: Add Required Testing

**Every PR must include at least 1 test!**

```bash
# Test files go in tests/test_litellm/
# Follow the same structure as litellm/ directory

# Example: if you modify litellm/utils.py
# Add test to: tests/test_litellm/test_utils.py
```

Example test structure:
```python
import pytest
from litellm import your_function

def test_your_feature():
    """Test your feature with a descriptive docstring."""
    # Arrange
    input_data = {"key": "value"}
    
    # Act
    result = your_function(input_data)
    
    # Assert
    assert result == expected_result
```

### Step 5: Run Full Validation

After making fixes, run the complete check suite:

```bash
# Run everything that CI will check
make lint          # All linting checks
make test-unit     # Unit tests

# For CI compatibility (optional)
make install-dev-ci
```

### Step 6: Prepare for PR Submission

1. **Commit your changes:**
```bash
git add .
git commit -m "Descriptive commit message"
git push origin your-feature-branch
```

2. **Sign the CLA:**
   - Visit: https://cla-assistant.io/BerriAI/litellm
   - Sign before submitting your PR

3. **Create/Update your PR:**
   - Go to GitHub and create a pull request
   - Fill out the PR template completely
   - Provide clear description of changes

## 🔧 Troubleshooting Common Problems

### Problem: Poetry/Dependency Issues
```bash
# Clear cache and reinstall
poetry cache clear --all pypi
poetry install --with dev,proxy-dev --extras proxy
```

### Problem: Test Dependencies Won't Install
```bash
# Try installing test deps separately
poetry install --with dev
cd enterprise && python -m pip install -e . && cd ..
```

### Problem: Specific Test Failures
```bash
# Run specific test file
poetry run pytest tests/test_litellm/test_specific_file.py -v

# Run specific test function
poetry run pytest tests/test_litellm/test_file.py::test_function -v
```

### Problem: Type Checking Issues with External Libraries
```python
# Use type: ignore for unavoidable issues
from external_lib import something  # type: ignore

# Or use try/except for imports
try:
    from openai.types import SomeType
except ImportError:
    SomeType = Any  # fallback
```

## ✅ Final Checklist

Before submitting your PR, ensure:

- [ ] `make lint` passes completely
- [ ] `make test-unit` passes (or at least your new tests)
- [ ] At least 1 test is added for your changes
- [ ] CLA is signed
- [ ] PR description clearly explains the changes
- [ ] Code changes address only 1 specific problem
- [ ] No unrelated files are modified

## 🆘 Getting Help

If you're stuck:

- 💬 [Join Discord](https://discord.gg/wuPM9dRgDw)
- 💬 [Join Slack](https://join.slack.com/share/enQtOTE0ODczMzk2Nzk4NC01YjUxNjY2YjBlYTFmNDRiZTM3NDFiYTM3MzVkODFiMDVjOGRjMmNmZTZkZTMzOWQzZGQyZWIwYjQ0MWExYmE3)
- 📧 Email: ishaan@berri.ai / krrish@berri.ai
- 🐛 [Create an issue](https://github.com/BerriAI/litellm/issues/new)

## 🎉 Success!

Once all checks pass and your PR is approved, it will be merged. Thank you for contributing to LiteLLM! 🚀